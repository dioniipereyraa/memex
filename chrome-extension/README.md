# Memex Live Capture (Chrome extension)

Captura tus chats de claude.ai en vivo y los manda al servidor Memex local. Mientras navegás claude.ai normalmente, esta extensión:

1. Intercepta la respuesta del endpoint `chat_conversations/{id}?tree=True` (mismo que usa el UI de Claude.ai para mostrar el chat).
2. Hace POST de ese JSON al endpoint local de Memex (`http://127.0.0.1:5777/ingest/conversation` por default).
3. Memex chunkea, embedea y persiste en su SQLite local. Listo para que Claude Code lo encuentre vía MCP en segundos.

No requiere modificar nada en claude.ai. No manda nada a ningún servidor externo (todo en `127.0.0.1`).

## Requisitos previos

- Tener Memex instalado y la base inicializada (al menos un `memex ingest` previo, o base vacía).
- Tener el servidor de captura corriendo: en una terminal del repo de Memex, `uv run memex serve`.

## Cargar la extensión

No está publicada en la Chrome Web Store todavía (es local). Carga manual:

1. Abrí `chrome://extensions/` en Chrome o Edge.
2. Activá **Modo desarrollador** (toggle arriba a la derecha).
3. Click en **Cargar descomprimida**.
4. Seleccioná la carpeta `chrome-extension/` de este repo (la que contiene `manifest.json`).
5. La extensión aparece con su ícono. Para verlo siempre, fijala en la barra.

## Probar que funciona

1. Click en el ícono de Memex para abrir el popup.
2. El chip "Servidor" debería decir **responde** (verde). Si dice **no responde**, verificá que `memex serve` esté corriendo.
3. Abrí `claude.ai`, abrí cualquier chat existente o creá uno nuevo.
4. Después de unos segundos (cuando Claude.ai termine de mostrar el chat), volvé al popup. El contador "Chats ingestados" debería haberse incrementado.
5. En una terminal: `uv run memex search "<algo del chat>"` debería encontrarlo.

## Cómo se ve por dentro

```
[Claude.ai]
  ↓ window.fetch interceptado por inject.js (MAIN world)
[inject.js]
  ↓ window.postMessage({source: "memex-inject", payload})
[content.js (ISOLATED world)]
  ↓ chrome.runtime.sendMessage
[background.js (service worker)]
  ↓ POST http://127.0.0.1:5777/ingest/conversation
[Memex server local]
```

## Privacidad y seguridad

- **Solo habla con localhost.** El POST va a `127.0.0.1:5777`. Ni un byte sale de tu máquina.
- **Host permissions limitados.** Solo `https://claude.ai/*` y `http://127.0.0.1:5777/*` y `localhost:5777`.
- **Sin telemetría.** No mide nada. No reporta a ningún servidor.
- **Sin storage de chats en el browser.** A diferencia de SyncChat, no cacheamos los chats en `chrome.storage`; solo guardamos config (URL del server) y stats agregadas (cantidad de chats ingestados, errores recientes).
- **Defense in depth.** El `inject.js` redacta campos sospechosos (`token`, `secret`, etc.) en cualquier body capturado, por si claude.ai cambia su API y empieza a mandar credentials en el body.

## Configuración avanzada

Por default el server URL es `http://127.0.0.1:5777`. Para cambiarlo (ej. otro puerto):

1. Cambiá `--port` al arrancar el server: `uv run memex serve --port 8888`.
2. En el popup de la extensión, escribí `http://127.0.0.1:8888` y dale **Guardar**.

## Limitaciones conocidas

- Captura solo los chats que **abrís** en claude.ai. Los chats que tenés sin abrir no se ingestan hasta que los visites al menos una vez.
- Si claude.ai cambia su endpoint de `chat_conversations/{id}?tree=True`, hay que actualizar la clasificación en `inject.js`.
- Tool blocks (Python, web search, etc.) se renderean como markers `[tool_use: ...]` por el ingest de Memex, igual que con el export oficial.

## Desarrollo

No hay build step. Editás los archivos y le das **Recargar** en `chrome://extensions/` (ícono circular debajo del nombre de la extensión).

Para ver logs:
- Inject + content: `F12` en la pestaña de claude.ai, pestaña "Console".
- Background: `chrome://extensions/`, click en **Inspeccionar vistas: service worker**.
