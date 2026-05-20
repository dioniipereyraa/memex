// Popup de Memex Live Capture. Muestra stats y permite configurar el servidor.

const $ = (id) => document.getElementById(id);

const fmtAgo = (ts) => {
  if (!ts) return "-";
  const seconds = Math.floor((Date.now() - ts) / 1000);
  if (seconds < 60) return `${seconds}s atrás`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m atrás`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h atrás`;
  const days = Math.floor(hours / 24);
  return `${days}d atrás`;
};

const renderChip = (state) => {
  const chip = $("server-chip");
  chip.className = "chip";
  if (state === true) {
    chip.classList.add("ok");
    chip.textContent = "responde";
  } else if (state === false) {
    chip.classList.add("bad");
    chip.textContent = "no responde";
  } else {
    chip.classList.add("unknown");
    chip.textContent = "desconocido";
  }
};

const render = ({ stats, config }) => {
  if (config && config.serverUrl) {
    $("server-url").value = config.serverUrl;
  }
  renderChip(stats?.serverReachable ?? null);

  $("ingested-count").innerHTML = `<code>${stats?.ingested ?? 0}</code>`;
  $("failed-count").innerHTML = `<code>${stats?.failed ?? 0}</code>`;

  const last = stats?.lastIngest;
  if (last && last.uuid) {
    $("last-ingest").hidden = false;
    $("last-title").textContent = last.title || "(sin título)";
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

$("ping").addEventListener("click", async () => {
  renderChip(null);
  $("server-chip").textContent = "probando…";
  await chrome.runtime.sendMessage({ type: "ping-server" });
  refresh();
});

$("reset").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "reset-stats" });
  refresh();
});

// Refresh inicial + ping al abrir.
chrome.runtime.sendMessage({ type: "ping-server" }).finally(refresh);
