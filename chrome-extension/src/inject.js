// Inject script de Memex Live Capture.
// Corre en MAIN world (acceso al window de claude.ai). Monkey-patcha fetch para
// interceptar respuestas del API y postearlas al content script vía
// window.postMessage. Basado en el equivalente de SyncChat.
(() => {
  const TAG = "[Memex:inject]";
  const originalFetch = window.fetch;

  // target = window.location.origin (no "*") para no filtrar el JSON del chat
  // a otros frames/scripts del page world. claude.ai siempre es same-origin.
  const TARGET_ORIGIN = window.location.origin;
  const post = (payload) => {
    window.postMessage({ source: "memex-inject", payload }, TARGET_ORIGIN);
  };

  const safeUrl = (input) => {
    if (typeof input === "string") return input;
    if (input instanceof Request) return input.url;
    if (input instanceof URL) return input.toString();
    return String(input);
  };

  // Defense in depth: si claude.ai alguna vez pone auth en el body del request,
  // redactamos antes de pasarlo al background. Hoy no aplica (usa cookies).
  const SENSITIVE_KEY_RX = /(token|secret|key|password|authorization|api[_-]?key|bearer|cookie|session)/i;
  const scrubSensitive = (bodyStr) => {
    if (typeof bodyStr !== "string" || bodyStr.length === 0) return bodyStr;
    try {
      const obj = JSON.parse(bodyStr);
      const walk = (node) => {
        if (Array.isArray(node)) { node.forEach(walk); return; }
        if (node && typeof node === "object") {
          for (const k of Object.keys(node)) {
            if (SENSITIVE_KEY_RX.test(k) && typeof node[k] === "string") {
              node[k] = "[REDACTED]";
            } else {
              walk(node[k]);
            }
          }
        }
      };
      walk(obj);
      return JSON.stringify(obj);
    } catch (_) {
      return bodyStr; // no era JSON, lo dejamos como estaba
    }
  };

  const isInteresting = (url) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      return url.startsWith("https://claude.ai/api/");
    }
    return url.startsWith("/api/");
  };

  const classify = (url, method) => {
    const path = url.split("?")[0];
    if (method === "GET" && /\/chat_conversations\/[^/]+$/.test(path)) return "conv-full";
    if (method === "POST" && /\/chat_conversations$/.test(path)) return "conv-create";
    return "other";
  };

  const consumeJson = async (response, meta) => {
    try {
      const text = await response.text();
      let body = text;
      try { body = JSON.parse(text); } catch (_) {}
      post({ kind: "json-response", meta, body, ts: Date.now() });
    } catch (err) {
      post({ kind: "json-error", meta, error: String(err), ts: Date.now() });
    }
  };

  window.fetch = async function patchedFetch(input, init) {
    const url = safeUrl(input);
    const method = (init && init.method) || (input instanceof Request ? input.method : "GET");

    let requestBody = null;
    if (init && init.body && typeof init.body === "string") {
      requestBody = scrubSensitive(init.body);
    }

    const startedAt = Date.now();
    const response = await originalFetch.apply(this, arguments);

    if (!isInteresting(url)) return response;

    const classification = classify(url, method);
    if (classification === "other") return response;

    const contentType = response.headers.get("content-type") || "";

    const meta = {
      url,
      method,
      status: response.status,
      contentType,
      requestBody,
      startedAt,
      classification,
    };

    // Solo nos importan las respuestas JSON que traen el chat completo.
    if (contentType.includes("application/json")) {
      consumeJson(response.clone(), meta);
    }

    return response;
  };

  console.log(TAG, "fetch hooked (v0.1.0)");
})();
