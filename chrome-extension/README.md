# Memex Live Capture (Chrome extension)

Captures your claude.ai chats live and sends them to the local Memex server. While you browse claude.ai normally, this extension:

1. Intercepts the response of the `chat_conversations/{id}?tree=True` endpoint (the same one Claude.ai's UI uses to render the chat).
2. POSTs that JSON to the local Memex endpoint (`http://127.0.0.1:5777/ingest/conversation` by default).
3. Memex chunks, embeds, and persists into its local SQLite. Ready for Claude Code to find it via MCP within seconds.

It does not require modifying anything on claude.ai. It does not send anything to any external server (everything stays on `127.0.0.1`).

## Prerequisites

- Memex installed and the database initialized (at least one prior `memex ingest`, or an empty DB).
- The capture server running: in a terminal at the Memex repo, `uv run memex serve`.

## Loading the extension

It is not published on the Chrome Web Store yet (it is local). Manual load:

1. Open `chrome://extensions/` in Chrome or Edge.
2. Enable **Developer mode** (toggle top right).
3. Click **Load unpacked**.
4. Select the `chrome-extension/` folder from this repo (the one with `manifest.json`).
5. The extension appears with its icon. To see it always, pin it to the toolbar.

## Testing it works

1. Click the Memex icon to open the popup.
2. The "Server" chip should say **responding** (green). If it says **no response**, verify that `memex serve` is running.
3. Open `claude.ai`, open any existing chat or create a new one.
4. After a few seconds (once Claude.ai finishes rendering the chat), go back to the popup. The "Chats ingested" counter should have incremented.
5. In a terminal: `uv run memex search "<something from the chat>"` should find it.

## What it looks like inside

```
[Claude.ai]
  ↓ window.fetch intercepted by inject.js (MAIN world)
[inject.js]
  ↓ window.postMessage({source: "memex-inject", payload})
[content.js (ISOLATED world)]
  ↓ chrome.runtime.sendMessage
[background.js (service worker)]
  ↓ POST http://127.0.0.1:5777/ingest/conversation
[local Memex server]
```

## Privacy and security

- **Only talks to localhost.** The POST goes to `127.0.0.1:5777`. Not a single byte leaves your machine.
- **Limited host permissions.** Only `https://claude.ai/*` and `http://127.0.0.1:5777/*` and `localhost:5777`.
- **No telemetry.** Does not measure anything. Does not report to any server.
- **No chat storage in the browser.** Unlike SyncChat, we do not cache chats in `chrome.storage`; we only store config (server URL) and aggregated stats (number of chats ingested, recent errors).
- **Defense in depth.** `inject.js` redacts suspicious fields (`token`, `secret`, etc.) in any captured body, in case claude.ai changes its API and starts sending credentials in the body.

## Advanced configuration

By default the server URL is `http://127.0.0.1:5777`. To change it (e.g., another port):

1. Change `--port` when starting the server: `uv run memex serve --port 8888`.
2. In the extension popup, write `http://127.0.0.1:8888` and hit **Save**.

## Known limitations

- It captures only the chats you **open** on claude.ai. Chats you have without opening are not ingested until you visit them at least once.
- If claude.ai changes its `chat_conversations/{id}?tree=True` endpoint, the classification in `inject.js` has to be updated.
- Tool blocks (Python, web search, etc.) render as `[tool_use: ...]` markers via the Memex ingest, the same as with the official export.

## Development

There is no build step. You edit the files and hit **Reload** in `chrome://extensions/` (circular icon under the extension name).

To see logs:
- Inject + content: `F12` on the claude.ai tab, "Console" tab.
- Background: `chrome://extensions/`, click **Inspect views: service worker**.
