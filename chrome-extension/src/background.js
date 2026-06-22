// Service worker de Memex Live Capture.
// Recibe capturas del content script, las filtra (solo chats completos), y
// las postea al endpoint local de Memex (`memex serve`). Mantiene stats
// minimal en chrome.storage para que el popup las muestre.

const DEFAULT_SERVER_URL = "http://127.0.0.1:5777";
const INGEST_PATH = "/ingest/conversation";
const HEALTH_PATH = "/health";
const PLAN_PATH = "/ingest/plan";
const RECENT_ERRORS_MAX = 5;

// Retry para network errors. El primer POST después de un `memex serve` fresh
// puede tardar si fastembed está bajando el modelo (~30-60s la primera vez).
const RETRY_ATTEMPTS = 3;
const RETRY_DELAYS_MS = [2000, 8000]; // entre intento 1-2 y 2-3

// ---------- helpers ----------

// Origins the server URL is allowed to point at. Kept in sync with the
// manifest `connect-src` CSP, which already blocks any other destination.
const ALLOWED_ORIGINS = ["http://127.0.0.1:5777", "http://localhost:5777"];

const getConfig = async () => {
  const { serverUrl, token } = await chrome.storage.local.get({
    serverUrl: DEFAULT_SERVER_URL,
    token: "",
  });
  return {
    serverUrl: typeof serverUrl === "string" && serverUrl ? serverUrl : DEFAULT_SERVER_URL,
    token: typeof token === "string" ? token : "",
  };
};

const getStats = async () => {
  const { stats } = await chrome.storage.local.get({
    stats: {
      serverReachable: null,
      ingested: 0,
      failed: 0,
      lastIngest: null,
      recentErrors: [],
      backfill: null,
    },
  });
  return stats;
};

const setStats = async (stats) => {
  await chrome.storage.local.set({ stats });
};

const recordIngestSuccess = async (uuid, title, chunks) => {
  const stats = await getStats();
  stats.ingested += 1;
  stats.serverReachable = true;
  stats.lastIngest = {
    uuid: uuid || null,
    title: title || "(no title)",
    chunks: typeof chunks === "number" ? chunks : null,
    at: Date.now(),
  };
  await setStats(stats);
};

const recordIngestFailure = async (kind, detail) => {
  const stats = await getStats();
  stats.failed += 1;
  if (kind === "network") stats.serverReachable = false;
  stats.recentErrors.unshift({
    kind,
    detail: String(detail).slice(0, 200),
    at: Date.now(),
  });
  if (stats.recentErrors.length > RECENT_ERRORS_MAX) {
    stats.recentErrors = stats.recentErrors.slice(0, RECENT_ERRORS_MAX);
  }
  await setStats(stats);
};

// ---------- main capture flow ----------

const handleCapture = async (payload) => {
  if (!payload || payload.kind !== "json-response") return;
  const classification = payload?.meta?.classification;
  // Solo nos interesan los chats completos. `conv-create` también los trae
  // (cuando se crea un chat nuevo claude.ai devuelve el body completo).
  if (classification !== "conv-full" && classification !== "conv-create") return;

  const body = payload.body;
  if (!body || typeof body !== "object" || !body.uuid) {
    await recordIngestFailure("bad-payload", "respuesta JSON sin uuid");
    return;
  }

  const { serverUrl, token } = await getConfig();

  if (!token) {
    await recordIngestFailure(
      "no-token",
      "No access token set. Run `memex token` and paste it in the popup.",
    );
    return;
  }

  // Retry: si el server está bajando el modelo de fastembed la primera vez,
  // o si hubo un network glitch, reintentamos con backoff antes de marcar fail.
  let response;
  let lastNetworkErr = null;
  for (let attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++) {
    try {
      response = await fetch(`${serverUrl}${INGEST_PATH}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Memex-Token": token },
        body: JSON.stringify(body),
      });
      lastNetworkErr = null;
      break;
    } catch (err) {
      lastNetworkErr = err;
      if (attempt < RETRY_ATTEMPTS) {
        const delay = RETRY_DELAYS_MS[attempt - 1] || 8000;
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }
  }
  if (lastNetworkErr || !response) {
    await recordIngestFailure("network", lastNetworkErr || "no response");
    return;
  }

  if (!response.ok) {
    let errText = "";
    try { errText = await response.text(); } catch (_) {}
    await recordIngestFailure(
      `http-${response.status}`,
      errText.slice(0, 200),
    );
    return;
  }

  let data = {};
  try { data = await response.json(); } catch (_) {}
  await recordIngestSuccess(data.uuid || body.uuid, body.name, data.chunks);
};

// ---------- history backfill (Phase 7 M3) ----------

// Ask the local server which conversations from the page's manifest
// ({uuid, updated_at}[]) are new or changed. The server compares against its
// indexed set and returns only the uuids to fetch; the indexed set never leaves
// the server. A POST is used so the request reliably carries the extension
// Origin (a cross-origin GET may omit it). Any failure (no token, server down,
// bad response) falls back to "fetch everything"; the server dedups on ingest,
// so correctness never depends on this optimization.
const planBackfill = async (body) => {
  const conversations = body && Array.isArray(body.conversations) ? body.conversations : [];
  const all = conversations.map((c) => c && c.uuid).filter(Boolean);

  const { serverUrl, token } = await getConfig();
  if (!token) return { toFetch: all };

  try {
    const response = await fetch(`${serverUrl}${PLAN_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Memex-Token": token },
      body: JSON.stringify({ conversations }),
    });
    if (!response.ok) return { toFetch: all };
    const data = await response.json();
    if (data && Array.isArray(data.toFetch)) return { toFetch: data.toFetch };
    return { toFetch: all };
  } catch (_) {
    return { toFetch: all };
  }
};

const recordBackfillProgress = async (progress) => {
  if (!progress || typeof progress !== "object") return;
  const stats = await getStats();
  stats.backfill = { ...progress, at: Date.now() };
  await setStats(stats);
};

// ---------- runtime messaging ----------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  // Trust split by sender. Page-relayed kinds (capture / backfill) must come
  // from a claude.ai content script (a tab on claude.ai); config/admin kinds
  // must come from the extension's own popup (no tab). A claude.ai page-world
  // script can already forge the page-relayed kinds (accepted residual), but it
  // must never reach set-token / set-server-url to hijack where data is sent.
  const fromClaudeTab =
    !!sender.tab &&
    typeof sender.url === "string" &&
    sender.url.startsWith("https://claude.ai/");
  const fromPopup = sender.id === chrome.runtime.id && !sender.tab;
  const PAGE_MSGS = ["capture", "backfill-plan", "backfill-progress"];
  const ADMIN_MSGS = ["set-token", "set-server-url", "reset-stats", "get-status", "ping-server"];
  if (PAGE_MSGS.includes(msg.type) && !fromClaudeTab) return false;
  if (ADMIN_MSGS.includes(msg.type) && !fromPopup) return false;

  if (msg.type === "capture") {
    handleCapture(msg.payload)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // async response
  }

  if (msg.type === "backfill-plan") {
    planBackfill(msg.body)
      .then((result) => sendResponse(result))
      // On an unexpected failure, reply null so the page falls back to
      // fetching everything (never an empty list, which would skip all).
      .catch(() => sendResponse(null));
    return true; // async response
  }

  if (msg.type === "backfill-progress") {
    recordBackfillProgress(msg.progress)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true; // async response
  }

  if (msg.type === "get-status") {
    Promise.all([getStats(), getConfig()]).then(([stats, config]) => {
      sendResponse({ stats, config });
    });
    return true;
  }

  if (msg.type === "ping-server") {
    (async () => {
      const { serverUrl } = await getConfig();
      try {
        const r = await fetch(`${serverUrl}${HEALTH_PATH}`, { method: "GET" });
        const reachable = r.ok;
        const stats = await getStats();
        stats.serverReachable = reachable;
        await setStats(stats);
        sendResponse({ reachable });
      } catch (_) {
        const stats = await getStats();
        stats.serverReachable = false;
        await setStats(stats);
        sendResponse({ reachable: false });
      }
    })();
    return true;
  }

  if (msg.type === "set-server-url") {
    const newUrl = typeof msg.serverUrl === "string" ? msg.serverUrl.trim() : "";
    if (!newUrl) {
      sendResponse({ ok: false, error: "Empty URL" });
      return false;
    }
    let parsed;
    try {
      parsed = new URL(newUrl);
    } catch (_) {
      sendResponse({ ok: false, error: "Invalid URL" });
      return false;
    }
    if (!ALLOWED_ORIGINS.includes(parsed.origin)) {
      sendResponse({
        ok: false,
        error: "URL must be http://127.0.0.1:5777 or http://localhost:5777",
      });
      return false;
    }
    chrome.storage.local.set({ serverUrl: newUrl }, () => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "set-token") {
    const token = typeof msg.token === "string" ? msg.token.trim() : "";
    chrome.storage.local.set({ token }, () => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "reset-stats") {
    chrome.storage.local.set({
      stats: {
        serverReachable: null,
        ingested: 0,
        failed: 0,
        lastIngest: null,
        recentErrors: [],
        backfill: null,
      },
    }, () => sendResponse({ ok: true }));
    return true;
  }

  return false;
});

console.log("[Memex:bg] service worker started");
