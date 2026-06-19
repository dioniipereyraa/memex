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

  // ---- Active backfill (Phase 7 M2-M4) ----
  // Pull the user's full claude.ai history into Memex on demand. Runs here in
  // the page (the user's cookies + the patched fetch): enumerate the chat org's
  // conversations, ask the extension which are new or changed (incremental,
  // M3), and fetch those FULL conversations through `patchedFetch`, so the
  // existing capture pipe (postMessage -> content.js -> background ->
  // POST /ingest/conversation) ingests them. Progress is reported to the
  // extension for the popup (M4). The org/list calls use `originalFetch` (not
  // conversations, so they must not be captured). claude.ai chats are
  // intentionally NOT redacted; this reuses the live-capture path, so that
  // posture is preserved. The server dedups by uuid/content_hash, so a run is
  // always safe to repeat.
  let backfillRunning = false;

  // Request/response bridge to the extension's privileged world. inject.js
  // cannot reach the local server (claude.ai's CSP blocks it) or chrome.runtime
  // (this is the page world), so it asks content.js, which relays to
  // background.js. Replies are correlated by a request id. The known-set used
  // for the incremental filter is computed in background and never returned to
  // this page world; we only get back the to-fetch subset, which claude.ai
  // already has. (The page world is untrusted for integrity either way; this
  // adds no exfiltration the page could not already do with its own session.)
  let reqSeq = 0;
  const pending = new Map();

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const data = event.data;
    if (!data || data.source !== "memex-content" || data.kind !== "reply") return;
    const resolve = pending.get(data.reqId);
    if (resolve) {
      pending.delete(data.reqId);
      resolve(data.result);
    }
  });

  const askExtension = (control, body, timeoutMs = 15000) =>
    new Promise((resolve) => {
      const reqId = `m${(reqSeq += 1)}`;
      pending.set(reqId, resolve);
      post({ control, reqId, body });
      setTimeout(() => {
        if (pending.has(reqId)) {
          pending.delete(reqId);
          resolve(null); // timeout: the caller falls back to fetching all
        }
      }, timeoutMs);
    });

  const reportProgress = (progress) => post({ control: "backfill-progress", body: progress });

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

      // M3 (incremental): ask the extension which uuids are new or changed.
      // The extension relays the manifest to the local server, which compares
      // updated_at against its indexed set (by instant) and returns the subset
      // to fetch. A null reply (no extension / server down / timeout) falls back
      // to fetching everything; the server still dedups, so correctness holds.
      const manifest = convs.map((c) => ({ uuid: c.uuid, updated_at: c.updated_at }));
      const plan = await askExtension("backfill-plan", { conversations: manifest });
      let targets = convs;
      if (plan && Array.isArray(plan.toFetch)) {
        const want = new Set(plan.toFetch);
        targets = convs.filter((c) => want.has(c.uuid));
      }
      const total = convs.length;
      const toFetch = targets.length;
      const skipped = total - toFetch;
      console.log(
        TAG,
        `backfill: ${total} conversations, ${toFetch} to fetch, ${skipped} already up to date`,
      );

      let done = 0;
      let failed = 0;
      reportProgress({ phase: "start", total, toFetch, skipped, done, failed });

      const queue = targets.slice();
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
          if (done % 5 === 0 || done === toFetch) {
            reportProgress({ phase: "progress", total, toFetch, skipped, done, failed });
            console.log(TAG, `backfill progress ${done}/${toFetch}`);
          }
          // Gentle throttle so claude.ai and the local ingest are not hammered.
          if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
        }
      };
      const workers = Math.max(1, Math.min(concurrency, toFetch || 1));
      await Promise.all(Array.from({ length: workers }, worker));

      reportProgress({ phase: "done", total, toFetch, skipped, done, failed });
      console.log(
        TAG,
        `backfill complete: ${done}/${toFetch} fetched, ${skipped} skipped, ${failed} failed`,
      );
      return { total, toFetch, skipped, done, failed };
    } catch (err) {
      reportProgress({ phase: "error", error: String(err) });
      console.warn(TAG, "backfill error", String(err));
      throw err;
    } finally {
      backfillRunning = false;
    }
  };

  // Exposed on window so the popup can trigger it via chrome.scripting in the
  // MAIN world (M4), and for a manual `await window.__memexBackfill()` in the
  // console. Anyone in the page world can call it, but it only fetches the
  // user's own chats with the user's own session, which the page could already
  // do, so it grants no new capability.
  window.__memexBackfill = memexBackfill;

  console.log(TAG, "fetch hooked + backfill ready (M4)");
})();
