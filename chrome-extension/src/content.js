// Content script de Memex (ISOLATED world).
// Puente entre el inject.js (page world) y el background service worker.
// Recibe postMessage del page y reenvía vía chrome.runtime.sendMessage.
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "memex-inject") return;
  chrome.runtime.sendMessage({ type: "capture", payload: data.payload }).catch(() => {
    // El service worker puede estar dormido; ignoramos el error y se reintenta
    // en el próximo evento (chrome reactiva al recibir el message).
  });
});
