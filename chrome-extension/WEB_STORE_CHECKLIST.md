# Chrome Web Store submission checklist

This is the playbook to submit the Memex Live Capture extension to the
Chrome Web Store. Everything in the repo (`manifest.json`, icons, code)
is already submission-ready. The remaining work is on the developer
dashboard side: pay the one-time fee, fill the listing, upload assets,
submit for review.

## Before you submit

- [ ] **Chrome Web Store Developer account.** One-time $5 USD fee at
  https://chrome.google.com/webstore/devconsole. Use the same Google
  account that owns the repo (or a dedicated dev account, your call).
- [ ] **Decide visibility.** Public (anyone can search and install) vs
  Unlisted (need the direct link). Unlisted is fine for an alpha; you
  share the link in the Discord post.
- [ ] **Privacy policy URL.** Even though Memex is local-only, the store
  requires a privacy policy URL for any extension that requests
  `host_permissions`. Easiest path: add a short `PRIVACY.md` to the
  repo with a one-paragraph statement ("does not transmit anything off
  device; all communication is with 127.0.0.1") and link to it via the
  GitHub raw URL or a GitHub Pages page.

## Assets needed (collect once, upload to the dashboard)

| Asset | Format | Size | Notes |
|---|---|---|---|
| Icon | PNG, square | 128x128 | Already in `chrome-extension/icons/icon-128.png` |
| Small promo tile | PNG | 440x280 | Need to create. Memex "M" + tagline, neutral background. |
| Marquee promo tile (optional) | PNG | 1400x560 | Skip for alpha; needed only if you want the homepage spotlight. |
| Screenshots | PNG or JPG | 1280x800 or 640x400 | Need at least 1; up to 5. Suggestions below. |

### Screenshot suggestions (1280x800 each)

1. **The Memex popup open with status "responding" (green chip).** Shows
   the value prop: server up, chats being captured.
2. **A Claude Code session calling `search_chats` and getting a chat
   from your claude.ai history back.** This is the "memory check" image
   you already have (`docs/screenshots/session-memory-check.jpeg`).
3. **The `memex doctor` output with all checks OK.** Reassures
   first-time users that the install is verifiable.
4. (Optional) Memex CLI `memex repos list` showing a couple of repos.
5. (Optional) Architecture diagram (from README) as an explainer slide.

## Build the submission ZIP

The store wants a single ZIP of the extension folder (NOT the whole
repo). The output goes to `chrome-extension/dist/`, NOT to the top-level
`dist/` (which is reserved for the Python `uv build` artifacts that go
to PyPI).

From the repo root, cross-platform:

```bash
python -c "
import os, zipfile
src = 'chrome-extension'
dst = 'chrome-extension/dist/memex-extension-0.2.4.zip'  # name = manifest version
os.makedirs(os.path.dirname(dst), exist_ok=True)
keep_dirs = ('icons', 'src')
keep_files = ('manifest.json',)
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zf:
    for f in keep_files:
        zf.write(os.path.join(src, f), arcname=f)
    for d in keep_dirs:
        for root, _, files in os.walk(os.path.join(src, d)):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src).replace(os.sep, '/')
                zf.write(full, arcname=rel)
print('built', dst)
"
```

ALWAYS rebuild right before uploading: the source files can be edited after a
previous zip was packaged (e.g. the 2026-06-22 audit fixes to
`background.js` / `popup.js` / `popup.html` post-dated the first 0.2.4 zip, so
the stale zip would have shipped without them). Bump the manifest `version`
before packaging if the code changed since the last published build.

Verify the zip:

```bash
unzip -l chrome-extension/dist/memex-extension-0.2.4.zip
# Expected entries (no .md files, no hidden files):
#   manifest.json
#   icons/
#   src/
```

## Store listing copy (paste into the dashboard)

**Item name (max 75 chars):**
```
Memex Live Capture
```

**Summary (max 132 chars):**
```
Capture your claude.ai chats live into a local index, and backfill your whole history. Pairs with the Memex MCP server.
```

**Description (long, no char limit but keep it scannable):**
```
Memex Live Capture is the browser piece of the Memex project, a local
MCP server that indexes your Claude.ai chat history and exposes it to
Claude Code (and other MCP clients).

This extension intercepts the chat data Claude.ai already sends to your
browser, and POSTs it to a local Memex server running on 127.0.0.1.
Nothing leaves your machine.

New chats are captured automatically as you use claude.ai. To import the
chats you had BEFORE installing, click "Backfill claude.ai history" in
the popup: it enumerates your conversations, asks the local server which
are new or changed, and pulls only those through the same local pipe,
with live progress. Re-running is cheap (already-indexed chats are
skipped) and an interrupted run resumes on the next click.

Requires:
  * Memex installed locally (pip install memex-chats).
  * `memex serve` running.

Repo and docs: https://github.com/dioniipereyraa/memex

Privacy:
  * Sends data only to 127.0.0.1. No external telemetry.
  * No analytics. No remote storage.
  * Permissions limited to claude.ai and localhost:5777.

Status: alpha (0.2.4). Open source, MIT.
```

**Category:**
```
Productivity (primary), Developer Tools (secondary)
```

**Language:** English (US).

## Permissions justification (required for review)

The dashboard asks you to justify each permission. Use these:

| Permission | Justification |
|---|---|
| `storage` | Saves the user's local server URL and aggregated stats (count of captured chats). No chat content is stored in extension storage. |
| `scripting` | Used only when the user clicks "Backfill claude.ai history": the popup injects the backfill routine into the user's own active claude.ai tab (`chrome.scripting.executeScript`, self-checks the page origin) so it can enumerate past conversations and import them through the local pipe. Not used for automatic capture. |
| `host: https://claude.ai/*` | Read the chat data the user is already viewing on claude.ai, so it can be indexed locally. |
| `host: http://127.0.0.1:5777/*` | Forward captured chats to the Memex server running on the user's machine. |
| `host: http://localhost:5777/*` | Same as above; some users configure `localhost` instead of `127.0.0.1`. |
| `content scripts on claude.ai` | Inject the fetch interceptor that classifies chat-conversation responses. |

## After submitting

- [ ] Review typically takes **5 to 10 business days** for a first
  submission. Subsequent updates are usually faster.
- [ ] Email notifications go to the dashboard account; check spam.
- [ ] If the reviewer asks for changes, the listing goes to "needs
  attention". Address and resubmit; the timer restarts.

## Once approved

- [ ] Get the public listing URL.
- [ ] Update `README.md` to link "Install from Chrome Web Store" instead
  of "Load unpacked".
- [ ] Update the Discord post or thread with the install link.
- [ ] Tag a new release (`v0.1.1` or similar) if the manifest changed.
