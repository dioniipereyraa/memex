// Service worker de Memex Live Capture.
// Recibe capturas del content script, las filtra (solo chats completos), y
// las postea al endpoint local de Memex (`memex serve`). Mantiene stats
// minimal en chrome.storage para que el popup las muestre.

const DEFAULT_SERVER_URL = "http://127.0.0.1:5777";
const INGEST_PATH = "/ingest/conversation";
const HEALTH_PATH = "/health";
const RECENT_ERRORS_MAX = 5;

// Retry para network errors. El primer POST después de un `memex serve` fresh
// puede tardar si fastembed está bajando el modelo (~30-60s la primera vez).
const RETRY_ATTEMPTS = 3;
const RETRY_DELAYS_MS = [2000, 8000]; // entre intento 1-2 y 2-3

// ---------- helpers ----------

const getConfig = async () => {
  const { serverUrl } = await chrome.storage.local.get({
    serverUrl: DEFAULT_SERVER_URL,
  });
  return { serverUrl: typeof serverUrl === "string" && serverUrl ? serverUrl : DEFAULT_SERVER_URL };
};

const getStats = async () => {
  const { stats } = await chrome.storage.local.get({
    stats: {
      serverReachable: null,
      ingested: 0,
      failed: 0,
      lastIngest: null,
      recentErrors: [],
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
    title: title || "(sin título)",
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

  const { serverUrl } = await getConfig();

  // Retry: si el server está bajando el modelo de fastembed la primera vez,
  // o si hubo un network glitch, reintentamos con backoff antes de marcar fail.
  let response;
  let lastNetworkErr = null;
  for (let attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++) {
    try {
      response = await fetch(`${serverUrl}${INGEST_PATH}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
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

// ---------- runtime messaging ----------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "capture") {
    handleCapture(msg.payload)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
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
      sendResponse({ ok: false, error: "URL vacía" });
      return false;
    }
    chrome.storage.local.set({ serverUrl: newUrl }, () => sendResponse({ ok: true }));
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
      },
    }, () => sendResponse({ ok: true }));
    return true;
  }

  return false;
});

console.log("[Memex:bg] service worker arrancó");
