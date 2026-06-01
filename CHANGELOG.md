# Changelog

All notable changes to Memex are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). `0.1.0` is the first alpha release; before it the project lived in `0.0.x`.

## [Unreleased]

(nothing yet — Phase 4 work starts after this line)

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
