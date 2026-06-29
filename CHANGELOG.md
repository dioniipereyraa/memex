# Changelog

All notable changes to Memex are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). `0.1.0` is the first alpha release; before it the project lived in `0.0.x`.

## [0.4.3] - 2026-06-28

### Added
- **Opt-in, persisted sync-reachable mode (`memex setup --sync`).** Multi-device sync needed the always-on capture server to be reachable on your Tailscale address, which until now meant hand-running `memex sync serve` (or wiring `--host` + `MEMEX_INGEST_ALLOWED_HOSTS` yourself) every time. `memex setup --sync` turns sync on and makes the autostart service come up sync-reachable, and the choice is **persisted**: set it once and it survives reboots, so you never run a serve command by hand. It prints the single `memex sync connect ...` line to run on your other devices. Turn it back off with `memex setup --no-sync`. Plain `memex setup` keeps your saved choice (a re-run never silently undoes `--sync`) and is unchanged (loopback-only, sync off) for everyone who does not opt in.
- **`memex serve --sync` / `--no-sync`.** `serve` can be told to come up sync-reachable directly, or it reads the persisted `serve_sync` choice when no flag is given (the autostart service uses exactly this, so no per-OS service definition has to change). In sync mode it binds `0.0.0.0` so one server answers both the local extension and the Tailscale address, with the Host allow-list pinned to loopback + the Tailscale IP (resolved at startup, never baked into the service) and the per-install token gating access. If Tailscale is not up, it degrades to loopback-only with a warning instead of failing, so the capture server always starts.

### Changed
- **README: dedicated install/upgrade/multi-device docs.** The Installation section gains an **Upgrading** subsection (upgrade with the same tool that owns the `memex` executable, or a different manager fails with "Executables already exist" and PATH keeps the old version), a **Multi-device** subsection (install Tailscale first, then `memex setup --sync`), and the sync section now leads with the persisted one-time setup, with `memex sync serve` kept as the one-shot alternative.
- The `tailscale` CLI is now resolved with `shutil.which` before the subprocess call (defense in depth, no behavior change).

## [0.4.2] - 2026-06-28

### Added
- **One-command cross-device sync setup.** Pairing two devices used to take several careful steps with two easy-to-miss footguns (run `serve` with both `--host` AND `MEMEX_INGEST_ALLOWED_HOSTS`, `enable` the gate separately, hand-copy a token). Now it is one command per device:
  - **`memex sync serve`** (source): resolves a reachable address (auto-detected from Tailscale, or `--host`), enables sync, binds to that address AND auto-adds it to the Host allow-list, and prints the exact `memex sync connect` line to run on the other device. Binds to that address only, so it coexists with the always-on loopback capture server, and works identically on macOS, Linux, and Windows (no per-OS network setup, no PowerShell env-var dance).
  - **`memex sync connect --url ... --token ...`** (destination): the single line `sync serve` prints. It enables sync, pairs, and runs a two-way reconcile in one step, so a fresh device is set up and caught up at once.
- **Claude Code ingest backstop on Windows.** A wheel install on macOS autostarts both the capture server and a 15-minute `ingest-claude-code` backstop, but Windows only autostarted the server, so a Windows user's local Claude Code sessions were indexed once at setup and never refreshed. `memex install-service` / `memex setup` now also register a `MemexIngest` Scheduled Task (logon + every 15 minutes). It runs under `pythonw`, so the ingest command reopens its streams to `ingest.log` when headless (the same fix the server got for Windows).

## [0.4.1] - 2026-06-27

### Fixed
- **Multi-device sync failed on any non-trivial history.** `memex sync push` / `reconcile` batched conversations by a fixed COUNT (up to 500 per request), but each conversation carries its chunk vectors (~11 KB per chunk at dim 768), so a batch of a real history far exceeded the receiver's 16 MB request-body cap. The push was rejected (HTTP 413), which surfaced to the client as a confusing `Broken pipe` / "could not reach peer". Sync now batches by serialized BYTES: it fills each request up to a budget kept under the peer's cap, then flushes, so a history of any size transfers across several requests. Pull batches the same way, estimating each record's size from the manifest.
  - `/sync/manifest` now advertises the device's `max_body_bytes` and a per-conversation `chunk_count`, so a peer sizes its batches to the real cap. A peer too old to advertise them (e.g. one still on `0.4.0`) is handled with a safe fallback (the local 8 MB budget), so an updated device still syncs with it.
  - A single conversation too large to fit in any request (one record over the peer's body cap) is skipped with a clear message and counted, instead of aborting the whole sync. Raise `MEMEX_INGEST_MAX_BODY_BYTES` on the peer (or lower `MEMEX_MAX_CHUNKS_PER_CONVERSATION`) to transfer it.

### Added
- **`MEMEX_SYNC_MAX_BATCH_BYTES`** (default 8 MB): tunes the per-request byte budget the sync client fills before flushing.

## [0.4.0] - 2026-06-24

### Added
- **Multi-device sync: one Claude across your devices.** `memex sync` keeps your conversations consistent across the machines you run Memex on, peer-to-peer, with no central server and nothing leaving your hardware. Each device runs its own local store and they reconcile directly over `memex serve`'s token-gated `/sync/*` endpoints. Conversations carry their embeddings, so the receiver never re-embeds. `memex sync pair` registers a peer (token stored `0600`); `reconcile` syncs both ways (last writer wins by update time, a same-timestamp divergence is reported as a fork and left untouched); `pull` / `push` force one direction; `status` shows what is paired and the last sync.
- **Master on/off gate, off by default.** The whole feature exposes nothing until `memex sync enable`; while off the `/sync/*` endpoints return 404 (not 401, so they do not reveal they exist) and the sync commands refuse, so a single-device install has no extra surface. `disable` turns it back off (paired peers are kept).
- **Optional auto-sync.** With `MEMEX_SYNC_AUTO=true` (and sync enabled), `memex serve` reconciles with each paired peer on startup and every `MEMEX_SYNC_INTERVAL_SECONDS` (default 900), taking the ingest lock non-blocking so it never delays a live capture and skipping any peer that is offline.

### Security
- Sync endpoints are token-only (a peer is not a browser, so no Origin is required) with the Host still pinned by `TrustedHostMiddleware`, and refuse on an embedding model/dim mismatch. Pairing shares an access token, so it is a full-trust relationship (only pair devices you control); the threat model is documented internally.

## [0.3.2] - 2026-06-22

### Security
- **Phase 7 + Phase B close audit (findings fixed).** A five-dimension audit at the phase boundary; the auth gate held (Origin + token, constant-time compare, `0600` token, TTY-gated print) and all findings were medium/low.
  - The generated systemd unit now rejects a DB or log path containing a newline or quote (a newline could inject an extra directive such as an `ExecStartPre=` that runs at login) and sets `UMask=0077`; the headless `serve.log` is created `0600`.
  - `POST /ingest/plan` caps the conversation list (the request body byte cap does not bound per-item work), dedupes uuids, and short-circuits an empty list.
  - The short-lived ingest worker subprocess no longer inherits the parent's `ANTHROPIC_API_KEY` / GitHub OAuth secret, and its stderr (which can carry the absolute DB path) is logged server-side instead of returned to the client.
  - Dropped `populate_by_name` from settings: it also read every setting from its bare field name in the environment (`db_path`, `anthropic_api_key`, ...), not only the documented `MEMEX_*` alias.
  - Chrome extension: `chrome.scripting.executeScript` self-checks the page origin, the background worker gates runtime messages by sender (config/admin messages only from the popup), and the token field is masked.

### Fixed
- **`get_chat` over MCP returned 20 messages by default instead of 10**, doubling the worst-case response size that the 10 default exists to keep under the client token cap.
- **macOS `memex install-service` reported success even when a launchd agent failed to load**; it now returns a non-zero exit code, matching the Windows behavior.
- **The one-command installer failed under `sh` on Debian/Ubuntu.** `scripts/install-pypi.sh` used a bash-only option while the docs pipe it to `sh` (dash on those systems); it is now POSIX.

### Removed
- Dead `repo.list_projects()` and a stale `__version__` constant (the version now resolves from the installed package metadata).

## [0.3.1] - 2026-06-20

### Fixed
- **Windows autostart: the live-capture server crashed instantly under the logon Scheduled Task.** The task runs `pythonw` (no console window), where `sys.stdout` / `sys.stderr` are `None`, so the server's startup console output and its `isatty()` check raised before it could bind the port (the task fired but nothing listened on 5777). `memex serve` now reopens the streams to a `serve.log` in the data dir when there is no console, so the Windows task starts the server, and Windows finally gets capture-server logs. The Scheduled Task definition is unchanged; only `serve` was hardened.

## [0.3.0] - 2026-06-20

### Added
- **`memex setup`: one-command onboarding.** Wires Memex into Claude Code end to end in a single idempotent command: registers the MCP server (`claude mcp add`), installs the always-on live-capture service, indexes local Claude Code sessions, and prints the extension pairing token. Each step degrades to a warning instead of aborting the rest. Flags: `--no-mcp` / `--no-autostart` / `--no-ingest` / `--remote` / `-y`.
- **Cross-platform autostart on macOS.** `memex install-service` now has a real launchd backend (it was a print-only stub), so install / uninstall / status work on macOS (launchd), Linux (systemd user unit), and Windows (Scheduled Task) alike. Default agents are the live-capture server plus the 15-minute Claude Code ingest backstop; the claude.ai connector is opt-in via `--remote` (it crash-loops without `MEMEX_REMOTE_*` config, so it is no longer installed by default).
- **One-click claude.ai history backfill (Chrome extension `0.2.4`).** A "Backfill claude.ai history" button in the popup imports your entire chat history into Memex with no manual export. It enumerates your conversations, asks the server which are new or changed (incremental), and fetches only those through the existing capture pipe, with live progress in the popup. Re-running is cheap (already-indexed, unchanged chats are skipped) and an interrupted run resumes on the next click. Adds the `scripting` permission to trigger the pull in the page.
- **PyPI-first install.** The DB and exports default to a stable absolute path: `<repo>/data` from a cloned/editable install (unchanged for existing users), or the OS per-user data directory from a `pip`/`pipx` install (macOS `~/Library/Application Support/memex`, Windows `%LOCALAPPDATA%\memex`, XDG `~/.local/share/memex` elsewhere). `memex install-service` (and `memex setup`) now register autostart from a wheel install too, on macOS (launchd), Linux (systemd), and Windows (a logon Scheduled Task), generating self-contained service definitions that run the installed CLI directly (`pythonw -m memex.cli.main serve` on Windows) without the repo. `MEMEX_DB_PATH` / `MEMEX_EXPORTS_DIR` override the location.

### Server
- **`POST /ingest/plan`**: takes a conversation manifest (`{uuid, updated_at}`) and returns only the new or changed uuids, comparing by instant (the ingest normalizes fractional seconds, so a string compare would be wrong). Same Origin + token auth as `/ingest/conversation`; the indexed set is compared server-side and never leaves the machine. A POST (not GET) so the cross-origin request reliably carries the extension Origin.

## [0.2.2] - 2026-06-12

### Performance
- **The live-capture server (`memex serve`) embeds each chat in a short-lived subprocess that exits**, instead of in the always-on process. It was holding the embedding model resident after the first capture (~0.5 GB: model + the onnxruntime arena, which `del` + `gc` does not return to the OS on macOS). Now a child process per capture loads the model, embeds, stores, and exits, so the OS reclaims everything and the server itself stays at its ~0.06 GB baseline (measured: parent stayed at 0.06 GB while the transient child peaked at ~0.63 GB, then exited). The payload is passed to the child over stdin (nothing sensitive touches disk). Cost: a ~3-5s model load per captured chat, which is fine for background, occasional live capture. Toggle with `MEMEX_INGEST_EMBED_IN_SUBPROCESS` (default true); set false to embed in-process for lower per-capture latency at a higher steady footprint.

## [0.2.1] - 2026-06-12

Security patch from a fourth adversarial red-team round (four parallel attackers, focused on data theft: secrets leaking to the cloud and reading chats from outside). The remote connector and the local surface held with no data-exposure finding; the fixes below close redaction bypasses found by executing real attacks, plus minor hardening.

### Security
- **64-hex key no longer exempted by a non-adjacent digest word.** A 256-bit hex key (Ethereum private key / AES / HMAC) survived redaction if any free-text word like `object`, `commit`, or `hash` appeared anywhere on the line (`the commit message references <key>`). The digest carve-out now requires the marker to ABUT the token (`sha256:HEX`, `commit HEX`, `etag: "HEX"`), mirroring the adjacency-only rule the entropy pass already used. Real digest lines stay preserved; an attacker can no longer prepend a stray word to dodge masking.
- **Dot/colon-segmented secrets are caught.** A Discord bot token (`id.timestamp.hmac`), a Telegram token (`id:secret`), or any secret chunked across dot/colon-separated pieces evaded the high-entropy fallback, which judged each piece alone and found each under the length floor. Added dedicated Discord/Telegram rules and a general dotted-run rule (fires only when the concatenated body is long, high-entropy, and mixes all three character classes, so dotted domains, semvers, package coordinates, and code paths are untouched).
- **Invisible-character split closed at ingest.** A zero-width space / soft hyphen / bidi control inserted mid-secret split the token so each half fell under the floor and survived; the existing Unicode-format strip ran only at MCP read time, too late (the split token was already chunked and embedded). Redaction now strips `Cf`-category and TAG-block code points first.
- **PEM ReDoS fixed.** A packed run of unterminated `-----BEGIN ... PRIVATE KEY-----` markers (no newline, no END) forced a 16 KB lazy forward scan per marker (~10 s on 2.9 MB). The header now requires a trailing newline, so a junk `BEGIN` fails immediately; redaction stays linear. Real PEM/armored blocks (which always newline after the header) are unaffected.
- **Indirect-injection envelope now names the project block.** `get_chat` returns a Claude.ai Project `description` / `prompt_template` verbatim (a project's prompt_template reads like a system prompt). The untrusted-content note enumerated only title/summary/snippet/message text; it now explicitly covers the project fields so the consuming model treats them as untrusted reference data too. (They were already control-char-stripped; this closes the framing gap.)
- **Chrome extension hardening.** `content.js` now also checks the message origin and validates the payload shape before forwarding a capture. Honest limitation documented in code: there is no un-observable channel between the page (MAIN) and isolated worlds, so a nonce cannot make the inject→content hop forge-proof against a claude.ai page-world compromise; that vector is integrity (index poisoning), not exfiltration, and downstream the server treats all captured content as untrusted. The misleading "redaction" comment on the request-body scrubber was corrected (it is best-effort, not a security boundary).
- A consolidated round-4 MUST_REDACT / MUST_PRESERVE corpus and a packed-PEM perf regression test guard all of the above.

### Performance
- **Default embedding batch size lowered from 4 to 2 (memory).** With `embed_threads` capped at 2, a larger batch buys no throughput (measured ~45 s for 200 chunks at batch 1/2/4 alike); it only inflates the onnxruntime per-batch arena, the real memory driver. Measured embedder peak RSS at the 500-token chunk size: ~0.67 GB (batch=1), ~0.97 GB (batch=2), ~1.56 GB (batch=4). So a small batch is a near-free RAM win for both the background ingest and the live-capture server (which also embeds in-process). `MEMEX_EMBED_BATCH_SIZE=1` drops it to ~0.67 GB. (Tested and rejected: disabling the onnxruntime CPU memory arena via fastembed's `enable_cpu_mem_arena=False` made peak RSS *worse* at batch=4, 2.29 GB vs 1.56 GB, so the arena stays on. Also tested and rejected: releasing the model after an idle period via `del + gc.collect()`; measured that it does NOT return the RSS to the OS on macOS, the freed arena stays in the process heap, so the model would have to be embedded in a short-lived subprocess to actually reclaim it. Left for a future change given the per-capture reload cost.)

### Known limitation
- **One availability-only limitation (no data exposure) in the upstream OAuth proxy's dynamic client registration**, mitigated at the tunnel layer. It exposes no chat data and the fix belongs upstream; the technical detail is tracked privately rather than published.

## [0.2.0] - 2026-06-12

Phase 4 (remote MCP transport for claude.ai) and Phase 6 (Claude Code / terminal ingestion): Memex is now reachable from claude.ai web/Desktop/mobile, and indexes both claude.ai chats and local Claude Code sessions in one store. Hardened for public release with a full security audit plus three adversarial red-team rounds, and a resource audit that cut ingest memory from a worst case of ~20 GB (concurrent runs) to ~0.5 GB steady state.

### Added
- **`memex serve-remote`:** serves the same 4 MCP tools over Streamable HTTP at `/mcp`, protected by OAuth. Designed for a loopback bind behind a tunnel (e.g. Tailscale Funnel) that publishes `MEMEX_REMOTE_BASE_URL`.
- **GitHub OAuth with an identity allow-list.** claude.ai registers via dynamic client registration and the user authorizes through a GitHub OAuth App; `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS` is enforced on every request (fail closed: the server refuses to start with an empty allow-list, and any non-listed GitHub account gets 401 after the OAuth dance). Each entry matches the username or the immutable numeric account id (the id resists username rename/reuse). OAuth state is persisted encrypted on disk, so restarts do not break the connection.
- New remote settings: `MEMEX_REMOTE_BASE_URL`, `MEMEX_REMOTE_PORT`, `MEMEX_GITHUB_CLIENT_ID`, `MEMEX_GITHUB_CLIENT_SECRET`, `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS`. README gained a "Connecting from claude.ai" guide.
- **`memex ingest-claude-code`:** indexes local Claude Code / terminal sessions (`~/.claude/projects/**/*.jsonl`) into the same store under a new `claude_code` source, so one search spans claude.ai chats and Claude Code work. Incremental (unchanged sessions skipped via `content_hash`), and each session is auto-associated to the registered repo of its working directory (including repos keyed by a git remote). Sub-agent side threads, harness plumbing, and assistant `thinking` are excluded; tool calls are kept as markers.
- **Secret redaction on the Claude Code path.** Terminal output and file contents are scanned for common credential shapes (provider API keys, JWTs, PEM private keys, `KEY=`/`SECRET=` assignments, `Bearer` tokens, URL-embedded passwords) and masked as `[REDACTED:...]` before storage/embedding; raw block content is not persisted for this source. Best-effort, since these logs can be reached through the remote claude.ai connector. See the Security section below for the hardening that followed.
- **Automatic Claude Code sync.** A `SessionEnd` hook (`scripts/session-end-hook.sh`) ingests each session as it closes (detached, low priority, never blocks the session), and a 15-minute scheduled scan (`scripts/scheduled-ingest.sh` + launchd plist template) picks up anything the hook missed. A new `LazyEmbedder` defers loading the embedding model until there is actually something to embed, so an all-skipped scan costs nothing.
- **Always-on launchd agents (macOS).** `memex serve` and `memex serve-remote` can be installed as launchd agents (`scripts/serve-daemon.sh`, `scripts/serve-remote-daemon.sh`, plist templates with `RunAtLoad` + `KeepAlive`), so capture and the claude.ai connector survive reboots without a terminal. README gained a "Running always-on (macOS)" section.
- **One-command installers.** `scripts/install.sh` (macOS/Linux) and `scripts/install.ps1` (Windows): install uv if missing, `uv sync` (fetches the pinned Python, no system Python needed), verify with `memex doctor`. README install section rewritten per OS.
- **Process naming.** Long-lived processes now show as `Memex capture` / `Memex connector` / `Memex ingest` / `Memex mcp` in Activity Monitor / ps (via `setproctitle`) instead of bare `python3.13`.
- New embedding settings: `MEMEX_EMBED_THREADS` (default 2) and `MEMEX_EMBED_BATCH_SIZE` (default 4) to bound ingest CPU/RAM.

### Security

Full pre-release audit (4 parallel auditors) followed by three adversarial red-team rounds (4 attackers each, executing real attacks against redaction and the connector). All round-1/2 fixes held under re-attack; the connector showed no auth bypass across all three rounds.

- **Redaction substantially reinforced** (`redact.py`): dozens of vendor-specific key shapes (Stripe, Twilio, SendGrid, GitLab, Vault, DigitalOcean, New Relic, npm, PyPI, Slack, Google OAuth, GitHub fine-grained, OpenSSH/PGP keys, age, otpauth, more); a dedicated 64-hex rule (entropy can never catch pure hex); base64-keys-with-slash rule for AWS/Azure-style secrets; JWTs with empty `alg:none` signatures; labeled values in English and Spanish (`contraseña`, `clave`, `secreto`); a length-tiered high-entropy fallback for secrets with no known shape. Redaction now runs before truncation so a secret straddling the cut is masked whole.
- **Over-redaction (false positives) attacked just as hard:** file paths, S3 object keys, git SHAs, SRI/Go integrity hashes, and long CamelCase code identifiers all survive redaction, so search keeps working. A consolidated MUST_REDACT / MUST_PRESERVE adversarial corpus guards both directions in CI.
- **Conversation titles now pass through redaction.** The title (model-generated, can quote a secret) was the one ingested field that bypassed it, and it leaked via search results, `get_chat`, and the summarizer (which forwards the title to the Anthropic API). Single fix at the parser closes all three paths.
- **Capture token no longer echoed into daemon logs.** `memex serve` prints the token only on a TTY; daemon scripts and launchd plists set `umask` 077; existing logs chmod'd 0600.
- **Remote allow-list reloads live.** `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS` is re-read from `.env` when the file changes, so revoking a login takes effect without a restart (fail-safe to the last good set). Parsed with exact-key matching (a `..._OLD=` line can no longer shadow it), reading the file directly so an exported env var cannot silently pin a stale list (warned if set).
- **Host validated before auth.** `TrustedHostMiddleware` moved outermost on the remote app, so a bad `Host` header is rejected before the per-request GitHub API call.
- **Invisible-character injection defense.** All tool result strings are stripped of Unicode format characters (category `Cf`, including bidi controls, zero-width chars, and the TAG block, which can encode hidden ASCII instructions).
- **ReDoS eliminated in redaction.** Every regex quantifier bounded, the quadratic `url-credentials` pattern fixed (180 KB blob: ~6 s to ~3 ms), and the per-token line back-scan window-bounded; redaction stays linear on multi-MB single-line blobs (minified JS, lockfiles, hex dumps).
- **OAuth consent screen pinned by regression test.** With dynamic client registration, the consent screen is the defense against a malicious-redirect token theft (confused deputy); a test now asserts it stays enabled, and token-result caching stays off so GitHub-side revocation remains immediate.

### Fixed
- **Embedder no longer spawns (and re-spawns) a worker subprocess during ingest.** `FastEmbedEmbedder` passed `parallel=1` to fastembed, which (counter to intent) takes the worker-pool branch: it spawns a subprocess that loads its own copy of the ONNX model (~+0.6 GB) and is re-created on every `embed()` call (once per 32-chunk pipeline batch), so a long session re-loaded the model hundreds of times. Switched to `parallel=None` (inline inference, model stays resident): ~40% faster and ~0.6 GB lower peak RSS, and it removes a latent crash for any non-import-safe caller.
- **Lowered the default embedding batch size from 8 to 4.** The onnxruntime per-batch arena (not the model itself) is the real memory driver and scales with `batch_size * sequence_length`. At the default 500-token chunk size, measured peak RSS is ~2.4 GB at batch=8 vs ~1.6 GB at batch=4 vs ~1.0 GB at batch=2. batch=4 keeps a background ingest at a safe footprint with good throughput; `MEMEX_EMBED_BATCH_SIZE` still tunes it.
- **Redaction quadratic back-scan on a newline-free blob.** `_line_before` (used by the hex-key and high-entropy passes) called `rfind("\n", 0, pos)`, which scans from the line start to the token on every match; with thousands of 64-hex tokens on one line (a single-line JSON array of git object hashes, an `npm ls` dump) this was O(n^2) and ran on every captured session. A 2 MB single-line hex blob took ~9 s; now window-bounded and linear (~0.8 s). Regression test added.
- **Only one ingest at a time.** `memex ingest-claude-code` takes a non-blocking `fcntl.flock` on `data/ingest.lock`; a concurrent run (hook + schedule + manual could overlap) skips cleanly instead of multiplying memory. Released automatically on process exit, even on crash; no-op where `fcntl` is absent (Windows).

### Changed
- The FastMCP server and the 4 tool wrappers moved from `transports/stdio.py` to a shared `transports/mcp_server.py` (`build_server()` factory). `memex-mcp` (stdio) behaves exactly as before; no config changes needed.
- `conversations.source` CHECK widened to include `claude_code`. Pre-existing databases are migrated in place by recreating the table (data, indexes, and FK dependents preserved); idempotent and automatic on next open.

## [0.1.1] - 2026-06-01

Security hardening release following a full multi-agent audit (no critical/high findings; the data layer audited clean). Local single-user threat model.

### Security
- **Live-capture access token.** `POST /ingest/conversation` now requires an `X-Memex-Token` header in addition to the extension Origin check; the Origin header alone is forgeable by any non-browser local process. The token is generated on first use, stored user-only (0600) next to the database, printed by `memex serve`, and available via the new `memex token` command. **Breaking: the Chrome extension must be re-paired once** (paste the token into the popup's new token field).
- **DNS-rebinding defense.** The ingest server pins the Host header to a loopback allow-list via `TrustedHostMiddleware` (`MEMEX_INGEST_ALLOWED_HOSTS`). `GET /health` no longer returns the service name, so it is not a presence-fingerprint oracle.
- **Request/resource caps.** Ingest request bodies are capped (`MEMEX_INGEST_MAX_BODY_BYTES`, default 16 MB) and rejected with `413` before buffering. Chunking is bounded per conversation (`MEMEX_MAX_CHUNKS_PER_CONVERSATION`, default 5000).
- **Database confidentiality.** The SQLite DB is created `0600` and its directory `0700` (so the plaintext WAL/SHM sidecars are not world-readable on shared hosts). `PRAGMA busy_timeout` is now set explicitly.
- **Indirect prompt-injection mitigation.** MCP tool results include a `_meta.untrusted_content` envelope marking retrieved chat content as data (not instructions); the Anthropic summarizer fences the chat body and is told never to follow instructions inside it.
- **Supply chain.** Raised dependency floors for PyPI installs: `starlette>=0.47.2` (CVE-2025-54121) and `fastmcp>=3.2.0`.

### Changed
- WAL write lock is held only during the DB write phase: embeddings are computed before the transaction opens, reducing cross-process `SQLITE_BUSY` between `memex serve` and `memex-mcp`. Lazy-summary persistence is best-effort and no longer fails a search on transient lock contention.
- `OLLAMA_HOST` is validated and warns when it points off-box; `fastembed` warns when a non-default (unpinned) embedding model is selected; the extension validates the server URL against the CSP allow-list.

## [0.1.0] - 2026-05-25

First public alpha. Published to PyPI as `memex-chats`; Chrome extension `memex-live-capture` submitted to the Chrome Web Store. Bundles Phase 3 (quality pass) and Phase 5 (release packaging) work.

### Added (Phase 5 packaging, 2026-05-25)
- `memex doctor` diagnostic command. Checks Python version, database existence + schema version, embedder instantiability, live-capture server reachability, summarizer configuration (only if enabled), registered repos count, and indexed corpus count. Reports OK / WARN / FAIL per check, exits non-zero only on FAIL. 4 new unit tests.
- `memex install-service` cross-platform autostart dispatcher. Detects host OS and delegates: Windows runs the existing Scheduled Task installer, Linux writes a new systemd user unit (`~/.config/systemd/user/memex-serve.service`) and starts it via `systemctl --user`. macOS prints manual instructions (launchd integration deferred to 0.2.0). 6 new unit tests covering the dispatch logic with mocked `platform.system` and `subprocess.run`.
- New `scripts/install-autostart.sh` for Linux. Subcommands `install`, `uninstall`, `status`. Resolves `uv` lazily at install time, falls back to PATH lookup if `uv` is not absolute. Auto-creates `~/.local/state/memex/` for logs.
- `chrome-extension/WEB_STORE_CHECKLIST.md`: full Web Store submission playbook (developer account, privacy policy URL, asset sizes, listing copy, permissions justification, post-approval checklist).
- `PRIVACY.md` at repo root. Linked from the Chrome Web Store listing.

### Changed (release)
- Package renamed from `memex` to `memex-chats` for PyPI publication. Both `memex` and `memex-mcp` are already taken on PyPI by unrelated projects (the latter was claimed the same day we attempted to publish). The CLI entry points stay `memex` and `memex-mcp`, so `.mcp.json` configs do not change. `Development Status` classifier bumped from `Pre-Alpha` to `Alpha`. Added `Operating System :: OS Independent` classifier. New `[project.urls]` section with Homepage / Repository / Issues / Changelog links.
- README quickstart restructured: "install from PyPI" is now the recommended path (option A), source install is option B. Diagnostics section added linking `memex doctor`. Autostart section unified across Windows + Linux + macOS placeholder.
- Chrome extension manifest description translated to English.
- Final English audit across `src/`, tests, and remaining docs (CLAUDE.md, chrome-extension README, popup, background). No code logic changes; user-visible error strings and MCP tool docstrings now in English so a non-Spanish-speaking user gets a coherent experience.
- Chrome extension submission ZIP build target moved from top-level `dist/` to `chrome-extension/dist/` so it does not collide with `uv build` artifacts when running `uv publish`.

### Phase 3 (quality pass, 2026-05-22 to 2026-05-24)

### Added
- Optional auto-summary generation per chat, powered by Claude Haiku via the Anthropic API. Opt-in by setting `MEMEX_SUMMARY_ENABLED=true` and `ANTHROPIC_API_KEY`. Summaries are generated lazily when `search_chats` returns a chat that does not have one cached: up to 3 in parallel per call (`ThreadPoolExecutor`), silent fail per chat if the API errors. The summary is stored in `conversations.summary` and persists, so subsequent searches hit cache and do not pay the API again.
- `core/summaries/` module: `Summarizer` ABC, `AnthropicSummarizer` (real backend, lazy import of the SDK), `FakeSummarizer` (deterministic, used in tests), `get_default_summarizer()` factory that returns `None` when the feature flag is off.
- `conversations.content_hash` column (SHA-256 hex of canonical text). The pipeline computes and persists it on every ingest. Lets the lazy summarizer (and future consumers) detect content changes so a stale summary can be invalidated.
- `repo.get_conversation_text(uuid)` reconstructs the canonical message stream of a chat (same format the chunker uses); `repo.update_conversation_summary(uuid, text)` patches only the summary field without touching the rest of the row.
- `anthropic>=0.40` as a new optional dependency: install with `uv sync --extra summaries`.
- Additive schema migration (`_apply_additive_migrations` in `db.py`) so existing local databases gain `content_hash` without a reset.
- `stdio.search_chats` resolves the summarizer once per process via `get_default_summarizer()` and passes it through.
- 24 new unit tests covering `FakeSummarizer`, the factory, `AnthropicSummarizer` error paths, the lazy wire in `tools.search_chats` (no-summarizer path, generation for missing summaries, cache reuse, cap at 3, persistence to DB, per-chat silent fail), pipeline `content_hash` persistence, cached-summary preservation across same-content reingests, and the additive schema migration on a legacy database.

### Changed
- `_ingest_conversation` computes the canonical text and `content_hash` before inserting; if the chat already exists with the same hash and a cached summary, the summary is preserved across the upsert (the parser's `summary` field would otherwise overwrite a lazy-generated one).
- `tools.get_chat` defaults lowered to fit comfortably inside the Claude Code MCP token budget: `messages_limit` 20 → 10, per-message text cap 3000 → 1500 chars. Worst-case response ~17k chars (was ~62k, which occasionally exceeded the client limit and triggered the "result saved to file" fallback). Hard max `messages_limit=100` unchanged; callers needing more detail can opt in explicitly. Docstrings updated so Claude paginates with `messages_offset=10` on long chats.
- `ROADMAP.md` and `DEVLOG.md` translated to English (previously Spanish, kept as internal journal). README note about Spanish internal docs removed.

### Added (chat ↔ repo association, Phase 3 sub-task 2)
- New `repos` and `chat_repos` tables (many-to-many with `source ∈ {'auto', 'manual'}`, `confidence`, cascade FKs on both ends).
- New `core/repos/` module:
  - `keys.py`: `normalize_path`, `normalize_remote` (SCP/HTTPS git URLs), `canonical_repo_key` (prefers remote over path).
  - `discovery.py`: `parse_repo(path)` reads `.git/config` and `pyproject.toml`/`package.json`/`Cargo.toml`; produces a `RepoInfo`. `ChatRepoAssociation` dataclass for joined rows.
  - `matcher.py`: `match_text(text, repos, threshold)` returns `Match(repo_key, confidence)` per repo with four signals (remote URL 1.0, path 0.9, manifest name 0.8, display name 0.5; highest wins per repo).
- Storage helpers in `core/storage/repo.py`: `insert_repo`, `get_repo`, `list_repos`, `delete_repo`, `associate_chat_repo` (refuses to overwrite `manual` with `auto`), `dissociate_chat_repo`, `list_repos_for_conversation` (joined, hydrated), `list_conversations_for_repo`.
- Pipeline auto-scan at ingest: `_ingest_conversation` runs the matcher against all registered repos after persisting the conv and upserts `source='auto'` associations. No-op when no repos are registered.
- CLI: new `memex repos` sub-app (`add`, `list`, `remove`, `scan`) and top-level `memex tag` / `memex untag` for manual overrides.
- `tools.search_chats(query, ..., repo=...)` accepts a path / git remote URL / canonical key. Resolves it via `_resolve_repo_key`, then `_apply_repo_boost` lowers the distance of associated hits by `REPO_BOOST_WEIGHT (0.3) * confidence` and re-sorts. Oversamples candidates (×5) when boosting so chats just outside the top-N can surface. Unregistered repo argument short-circuits with an actionable error pointing at `memex repos add`.
- `stdio.search_chats` MCP wrapper exposes `repo` to Claude Code; docstring instructs it to pass the cwd when working inside a repo.
- 65 new unit tests across keys (21), discovery (11), matcher (15), storage helpers (18), pipeline auto-scan (4), CLI (15), and search boost / resolve (7).

### Added (SessionStart hook + find_related, Phase 3 sub-tasks 3 and 4)
- `memex session-context` CLI command. Auto-detects the active repo from cwd (new `find_repo_root` walks up looking for `.git`, handles both directory and gitlink-file forms used by worktrees). Resolves to a registered repo, prints a short Markdown blob with up to N associated chats (manual first, then auto by confidence). Designed to be wired into Claude Code's `SessionStart` hook in `.claude/settings.json`. Silent no-op when no `.git`, repo not registered, or no associations (diagnostics go to stderr).
- `_resolve_repo_key` extracted from `transports/tools.py` to new `core/repos/resolve.py`. Single source of truth shared by `search_chats(repo=...)`, `find_related(repo=...)`, and the new session-context command.
- `find_related(context, limit, repo)` MCP tool: takes free-form text and returns semantically similar chats via pure vector search. Capped at `FIND_RELATED_MAX_INPUT_CHARS=4000` chars to bound embedder latency. Same repo-boost mechanic as `search_chats`. Wired into `stdio.py` as the 4th MCP tool with docstring guiding Claude when to prefer it over `search_chats`.
- 16 new unit tests: 5 for the session-context CLI (no-git, unregistered, no-associations, prints associated, limit respected), 4 for `find_repo_root`, 7 for `find_related` (empty context, shape, truncation, limit clamp, unknown repo, boost reorders, embedder error).

## [0.0.2] - 2026-05-20

Phase 2 closed. Live capture + hybrid search work end-to-end. First public-facing polish (badges, screenshot, CONTRIBUTING, CHANGELOG, CI). Windows autostart as preview of Phase 5. Closing audit applied, 3 important fixes + 4 minor fixes landed in this release.

### Added
- CI workflow on GitHub Actions (`ruff check`, `ruff format --check`, `mypy`, unit tests). Read-only permissions, 10 min job timeout.
- `CONTRIBUTING.md` with local setup, code style, and PR workflow.
- This `CHANGELOG.md`.
- Badges in the README: CI status, License MIT, Python 3.12+.
- "Session memory check" screenshot in the README, embedded as end-to-end demo of the live capture + recall flow.
- Chrome extension icons (16/32/48/128 PNG + SVG source under `chrome-extension/icons/`). Manifest declares them both top-level (`icons`) and in `action.default_icon` so the toolbar and the extension page render the brand instead of the gray placeholder.
- Tests for `memex serve` CLI (CliRunner mocking `uvicorn.run` and `connect_and_init`) and for ingest rollback when the embedder fails mid-batch (closes two audit follow-ups from 2026-05-19).
- Windows autostart for the HTTP server (Phase 5 preview): `scripts/install-autostart.ps1` registers a Scheduled Task running `uv run memex serve` at log on, with `LogonType S4U` (no window, independent from the shell that triggered it) and auto-restart on failure. Manage with `-Install` / `-Uninstall` / `-Status`. Logs to `%LOCALAPPDATA%\Memex\serve.log`. The cross-platform formal version (`memex install-service` with systemd / launchd backends) stays on the Phase 5 roadmap.

### Changed
- README translated fully to English. `ROADMAP.md` and `DEVLOG.md` remain in Spanish (internal journal).
- `ruff format` applied across `src/` and `tests/` (16 files reformatted, semantics unchanged). The check is now back in CI.
- Comment in `transports/http_ingest.py::_get_conn` rewritten to accurately describe the threading model and the future invariant for background tasks.

### Fixed
- `scripts/_run-server.ps1`: log file no longer has mixed encoding. The previous version used `Out-File -Encoding utf8` for the banner line plus `*>> $LogFile` for the server output, but PowerShell 5.1's `*>>` defaults to UTF-16 LE, garbling the file. Now uses `2>&1 | Out-File -Encoding utf8` for consistent UTF-8.
- `chrome-extension/src/popup.js`: replaced `innerHTML` with DOM API (`createElement` + `textContent`) when rendering recent error entries. Defense-in-depth even though the only data source is the local server.
- `scripts/install-autostart.ps1`: `New-Item -Force -ItemType Directory` instead of `Test-Path` + `New-Item`. Consistent with the wrapper script and removes a theoretical race between check and create.
- Removed em dashes used as connectors (project rule) in `popup.js` (`"—"` placeholder and `— ${fmtAgo()}` separator).

## Phase 2 (in progress)

Goal: live capture from claude.ai + hybrid search good enough that Claude Code actually finds the right chat.

### Added
- Hybrid search (`hybrid` mode) combining vector search and FTS5 BM25 via Reciprocal Rank Fusion. Default for `search_chats`. Fixes the "Amarok" case where lexical-only beats semantic-only on proper nouns.
- `search_chats(mode=...)` parameter accepting `hybrid`, `semantic`, `lexical`.
- `memex reindex-fts` CLI command to populate FTS5 index on databases created before hybrid landed.
- Local HTTP ingest server (`memex serve`) on `127.0.0.1:5777` for live capture.
- Chrome extension (MV3) that captures conversations from claude.ai and posts them to the local server.
- `fastembed` embedder as the new zero-config default (130 MB quantized ONNX model). Ollama moved to opt-in via `MEMEX_EMBED_BACKEND=ollama` and the `[ollama]` extra.

### Changed
- `ollama` dependency moved to `[project.optional-dependencies]` under the `ollama` extra. Install with `uv pip install -e .[ollama]` if needed.
- `starlette>=0.40` and `uvicorn>=0.30` promoted to direct dependencies (no longer relying on transitive resolution through `fastmcp`).
- `cli/main.py` no longer hardcodes "embedder: Ollama"; it reports the active backend (`settings.embed_backend`) and the model name reported by the embedder instance.
- Chrome extension popup readable in dark mode.
- Chrome extension `inject.js` uses `window.location.origin` as `postMessage` target instead of `"*"`, preventing other page-world scripts on claude.ai from intercepting captured chat JSON.
- Chrome extension `manifest.json` adds an explicit CSP (`script-src 'self'; connect-src 'self' http://127.0.0.1:5777 http://localhost:5777`).

### Fixed
- `transports/stdio.py` no longer leaks raw exception messages to MCP clients; it returns `Error interno ({Type})` and logs the detail server-side.
- Ollama embedder catches `httpx.ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError` explicitly before falling back to substring matching.
- `_to_iso` in `storage/repo.py` uses `strftime` instead of `replace("+00:00", "Z")`; robust to non-UTC zones.
- `tools.search_chats(mode="lexical")` raises a clear error when the query sanitizes to empty (previously returned `[]` silently).
- Chrome extension `background.js` retries 3 times with backoff (2s, 8s) on network errors, covering the case where fastembed downloads the model on first ingest and the server takes 30-60s to respond.

### Removed
- Empty `src/memex/core/retrieval/` directory.
- `pytest-cov` from dev dependencies (was not used in CI or docs).

## Phase 1 (closed)

Goal: ingestion pipeline + storage + first MCP tools working end-to-end.

### Added
- `core/ingest/` pipeline parsing the official Claude.ai export (`conversations.json`, `users/*/design_chats/*.json`, `memories.json`) into Project, Conversation, Message, Chunk models.
- `core/storage/` over SQLite + sqlite-vec with FTS5 enabled.
- `core/embeddings/` with Embedder ABC, Ollama implementation, and `FakeEmbedder` for tests.
- MCP server (`memex-mcp`) over stdio with `search_chats`, `get_chat`, `list_recent_chats`.
- CLI (`memex`) with `ingest`, `search`, `stats` commands.

## Phase 0 (closed)

Goal: project scaffold + decisions of record.

### Added
- Initial pyproject.toml, uv setup, package layout (`src/memex/`).
- Pydantic settings (`config.py`).
- Test infrastructure (pytest, asyncio mode, `not integration` marker).
- `CLAUDE.md`, `ROADMAP.md`, `DEVLOG.md` for project context.
