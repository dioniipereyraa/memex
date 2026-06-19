// Memex Live Capture popup. Shows stats and lets the user configure the server.

const $ = (id) => document.getElementById(id);

const fmtAgo = (ts) => {
  if (!ts) return "-";
  const seconds = Math.floor((Date.now() - ts) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
};

const renderChip = (state) => {
  const chip = $("server-chip");
  chip.className = "chip";
  if (state === true) {
    chip.classList.add("ok");
    chip.textContent = "responding";
  } else if (state === false) {
    chip.classList.add("bad");
    chip.textContent = "no response";
  } else {
    chip.classList.add("unknown");
    chip.textContent = "unknown";
  }
};

const render = ({ stats, config }) => {
  if (config && config.serverUrl) {
    $("server-url").value = config.serverUrl;
  }
  if (config && typeof config.token === "string") {
    $("token").value = config.token;
  }
  renderChip(stats?.serverReachable ?? null);

  $("ingested-count").innerHTML = `<code>${stats?.ingested ?? 0}</code>`;
  $("failed-count").innerHTML = `<code>${stats?.failed ?? 0}</code>`;

  const last = stats?.lastIngest;
  if (last && last.uuid) {
    $("last-ingest").hidden = false;
    $("last-title").textContent = last.title || "(no title)";
    $("last-when").textContent = fmtAgo(last.at);
  } else {
    $("last-ingest").hidden = true;
  }

  const errs = stats?.recentErrors || [];
  if (errs.length === 0) {
    $("errors-block").hidden = true;
  } else {
    $("errors-block").hidden = false;
    const errorsEl = $("errors");
    errorsEl.replaceChildren();
    for (const e of errs) {
      const div = document.createElement("div");
      div.textContent = `[${e.kind}] ${e.detail || ""} `;
      const span = document.createElement("span");
      span.style.opacity = ".6";
      span.textContent = `· ${fmtAgo(e.at)}`;
      div.appendChild(span);
      errorsEl.appendChild(div);
    }
  }

  const bf = stats?.backfill;
  const bfEl = $("backfill-status");
  if (!bf || !bf.phase) {
    bfEl.hidden = true;
  } else {
    bfEl.hidden = false;
    bfEl.textContent = fmtBackfill(bf);
  }
};

const fmtBackfill = (bf) => {
  if (bf.phase === "error") return `Backfill error: ${bf.error || "unknown"}`;
  const skipped = bf.skipped ? `, ${bf.skipped} up to date` : "";
  if (bf.phase === "done") {
    const failed = bf.failed ? `, ${bf.failed} failed` : "";
    return `Backfill done: ${bf.done}/${bf.toFetch} fetched${skipped}${failed}`;
  }
  if (bf.phase === "start") return `Backfill starting: ${bf.toFetch} to fetch${skipped}`;
  return `Backfilling ${bf.done}/${bf.toFetch}...${skipped}`;
};

const refresh = async () => {
  const response = await chrome.runtime.sendMessage({ type: "get-status" });
  if (response) render(response);
};

$("save-url").addEventListener("click", async () => {
  const url = $("server-url").value.trim();
  if (!url) return;
  await chrome.runtime.sendMessage({ type: "set-server-url", serverUrl: url });
  await chrome.runtime.sendMessage({ type: "ping-server" });
  refresh();
});

$("save-token").addEventListener("click", async () => {
  const token = $("token").value.trim();
  await chrome.runtime.sendMessage({ type: "set-token", token });
  await chrome.runtime.sendMessage({ type: "ping-server" });
  refresh();
});

$("ping").addEventListener("click", async () => {
  renderChip(null);
  $("server-chip").textContent = "testing...";
  await chrome.runtime.sendMessage({ type: "ping-server" });
  refresh();
});

$("reset").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "reset-stats" });
  refresh();
});

// Trigger the history backfill. The work runs in the claude.ai page (it needs
// the user's session + the patched fetch), so we inject a one-shot call to
// `window.__memexBackfill` (defined by inject.js) into the active tab's MAIN
// world. It is fire-and-forget: progress flows back through background into the
// stats this popup polls, so closing the popup does not stop the run.
async function startBackfill() {
  const status = $("backfill-status");
  status.hidden = false;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.startsWith("https://claude.ai/")) {
    status.textContent = "Open a claude.ai tab, then click Backfill.";
    return;
  }
  status.textContent = "Starting backfill...";
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        if (typeof window.__memexBackfill === "function") {
          window.__memexBackfill();
        } else {
          throw new Error("Memex is not loaded on this page; reload claude.ai.");
        }
      },
    });
    status.textContent = "Backfill running... (you can close this popup)";
  } catch (e) {
    status.textContent = `Could not start: ${e && e.message ? e.message : e}`;
  }
}

$("backfill").addEventListener("click", startBackfill);

// Initial refresh + ping when the popup opens, then poll so backfill progress
// updates live while the popup stays open.
chrome.runtime.sendMessage({ type: "ping-server" }).finally(refresh);
setInterval(refresh, 1500);
