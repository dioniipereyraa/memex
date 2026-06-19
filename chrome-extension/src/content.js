// Content script de Memex (ISOLATED world).
// Puente entre el inject.js (page world) y el background service worker.
// Recibe postMessage del page y reenvía vía chrome.runtime.sendMessage.
//
// Límite de confianza (importante): inject.js corre en el MAIN world de
// claude.ai, igual que correría un script atacante en caso de un XSS en
// claude.ai. No hay canal no observable entre el MAIN y el ISOLATED world, así
// que NO podemos distinguir criptográficamente un mensaje de inject.js de uno
// forjado por otro script del page world (un nonce viajaría por postMessage y
// sería observable). Por eso validamos estructura y origen acá, y aguas abajo
// el server trata todo el contenido capturado como dato no confiable (envelope
// `untrusted_content` + stripeo de control-chars en las MCP tools). Un
// envenenamiento del índice requiere comprometer el page world de claude.ai y
// es un problema de integridad, no de exfiltración.
const KNOWN_KINDS = new Set(["json-response", "json-error"]);

window.addEventListener("message", (event) => {
  // Same-window only (bloquea iframes) y mismo origen (claude.ai).
  if (event.source !== window) return;
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.source !== "memex-inject") return;
  const payload = data.payload;
  if (!payload || typeof payload !== "object") return;

  // Backfill orchestration messages carry a `control` field; the capture path
  // carries a `kind`. The two are disjoint, so the capture validation below is
  // untouched. A control message is still untrusted page-world input, and
  // background treats it as such (it relays a manifest to the server, which
  // returns only the to-fetch subset, plus cosmetic progress; no new
  // exfiltration the page could not already do itself).
  if (typeof payload.control === "string") {
    handleControl(payload);
    return;
  }

  // Forma esperada: un objeto con un `kind` conocido. Descarta basura/forjados
  // mal formados sin pretender que esto sea una frontera de seguridad fuerte.
  if (!KNOWN_KINDS.has(payload.kind)) return;
  chrome.runtime.sendMessage({ type: "capture", payload }).catch(() => {
    // El service worker puede estar dormido; ignoramos el error y se reintenta
    // en el próximo evento (chrome reactiva al recibir el message).
  });
});

// Relay backfill control messages between the page world (inject.js) and the
// background service worker, the only context that can reach the local Memex
// server. `backfill-plan` is request/response (the reply is posted back to the
// page, correlated by reqId); `backfill-progress` is fire-and-forget.
function handleControl(payload) {
  if (payload.control === "backfill-plan" && typeof payload.reqId === "string") {
    chrome.runtime.sendMessage({ type: "backfill-plan", body: payload.body }, (resp) => {
      void chrome.runtime.lastError; // swallow "no receiver" noise if SW asleep
      window.postMessage(
        {
          source: "memex-content",
          kind: "reply",
          reqId: payload.reqId,
          result: resp || null,
        },
        window.location.origin,
      );
    });
  } else if (payload.control === "backfill-progress") {
    chrome.runtime
      .sendMessage({ type: "backfill-progress", progress: payload.body })
      .catch(() => {});
  }
}
