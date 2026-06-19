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

  // NO es una frontera de seguridad: es un best-effort por nombre de clave sobre
  // el body del request, por si claude.ai algún día mete auth ahí (hoy usa
  // cookies, así que no aplica). La redacción real de secretos es server-side
  // (core/ingest/redact.py) sobre el contenido almacenado; no confiar en esto.
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

  const patchedFetch = async function (input, init) {
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

  window.fetch = patchedFetch;

  // ---- Active backfill (Phase 7 M2) ----
  // Pull the user's full claude.ai history into Memex on demand. Runs here in
  // the page (the user's cookies + the patched fetch), enumerates the chat
  // org's conversations, and fetches each FULL conversation through
  // `patchedFetch`, so the existing capture pipe (postMessage -> content.js ->
  // background -> POST /ingest/conversation) ingests them with no extra wiring.
  // The org/list calls go through `originalFetch` (they are not conversations,
  // so they must not be captured). claude.ai chats are intentionally NOT
  // redacted; this reuses the live-capture path, so that posture is preserved.
  // The server dedups by uuid/content_hash, so re-running is idempotent.
  let backfillRunning = false;

  const fetchJson = async (url) => {
    const response = await originalFetch(url, { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`${response.status} for ${url}`);
    return response.json();
  };

  const chatOrgId = async () => {
    const orgs = await fetchJson("/api/organizations");
    const list = Array.isArray(orgs) ? orgs : [];
    // The chat org is the one whose capabilities include "chat". A separate
    // "api" org (capabilities like ["api", "api_individual"]) also exists and
    // is the wrong one to enumerate.
    const org = list.find((o) => (o.capabilities || []).includes("chat")) || list[0];
    if (!org || !org.uuid) throw new Error("no claude.ai chat organization found");
    return org.uuid;
  };

  const listConversations = async (orgId) => {
    // Flat array, offset pagination (verified: limit=1000 returns all, no
    // page overlap). Page in chunks so very large accounts stay bounded.
    const PAGE = 100;
    const all = [];
    for (let offset = 0; ; offset += PAGE) {
      const page = await fetchJson(
        `/api/organizations/${orgId}/chat_conversations?limit=${PAGE}&offset=${offset}`,
      );
      const rows = Array.isArray(page) ? page : [];
      all.push(...rows);
      if (rows.length < PAGE) break;
    }
    return all;
  };

  const memexBackfill = async ({ concurrency = 3, delayMs = 200 } = {}) => {
    if (backfillRunning) {
      console.log(TAG, "backfill already running, ignoring");
      return { skipped: true };
    }
    backfillRunning = true;
    try {
      const orgId = await chatOrgId();
      const convs = await listConversations(orgId);
      const total = convs.length;
      console.log(TAG, `backfill: ${total} conversations to pull`);
      let done = 0;
      let failed = 0;
      const queue = convs.slice();
      const worker = async () => {
        while (queue.length) {
          const conv = queue.shift();
          const url =
            `/api/organizations/${orgId}/chat_conversations/${conv.uuid}` +
            "?tree=True&rendering_mode=messages&render_all_tools=true";
          try {
            // The patched fetch captures this conv-full response and posts it
            // through the existing pipe; we do not read the body ourselves.
            await patchedFetch(url, { headers: { accept: "application/json" } });
          } catch (err) {
            failed += 1;
            console.warn(TAG, "backfill fetch failed for", conv.uuid, String(err));
          }
          done += 1;
          if (done % 10 === 0 || done === total) {
            console.log(TAG, `backfill progress ${done}/${total}`);
          }
          // Gentle throttle so claude.ai and the local ingest are not hammered.
          if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
        }
      };
      const workers = Math.max(1, Math.min(concurrency, total || 1));
      await Promise.all(Array.from({ length: workers }, worker));
      console.log(TAG, `backfill complete: ${done}/${total} fetched, ${failed} failed`);
      return { total, done, failed };
    } finally {
      backfillRunning = false;
    }
  };

  // Exposed for a manual trigger from the claude.ai console (M2):
  //   await window.__memexBackfill()
  // The popup button, progress UI, and resumability come in M4.
  window.__memexBackfill = memexBackfill;

  console.log(TAG, "fetch hooked + backfill ready (M2)");
})();
