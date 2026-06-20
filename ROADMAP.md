# Roadmap

> Last updated: 2026-06-16

**Current state:** Phases 0 to 4 and 6 closed. **Phase 4 (remote transport): CLOSED** (2026-06-11), validated end-to-end from claude.ai and audited. **Phase 6 (Claude Code / terminal ingestion): CLOSED** (2026-06-11), bulk-ingested, unified search validated, audited (shipping-blocker bug fixed + secret redaction added). **Phase 5 essentially done:** `memex-chats 0.2.1` on PyPI (2026-06-12; 0.2.0 shipped both phases plus the security/resource hardening, 0.2.1 a redaction-bypass patch from a fourth data-theft red-team round). **0.2.2 published on PyPI** (2026-06-12; the capture server now embeds in a subprocess so the always-on process stays at ~0.06 GB instead of ~0.5 GB). The Chrome extension is live on the Web Store (chromewebstore.google.com/detail/memex-live-capture/bncngnabecfilefblppkolhdnaelibnb, Unlisted). Remaining Phase 5 items are non-blocking (Discord post, Windows auto-sync hook). **0.2.3 published on PyPI** (2026-06-16; README now leads with the pain and shows a live demo GIF, plus a `server.json` + `mcp-name` marker so Memex is listed in the official MCP Registry as `io.github.dioniipereyraa/memex`, and submitted to mcp.so). **520 tests green** (+1 skipped), CI green, `ruff` + core `mypy` clean. **Phase 7 (frictionless onboarding): CLOSED** (2026-06-19/20). Both halves shipped: (a) the one-command `memex setup` + a cross-platform `memex install-service`, and (b) the claude.ai auto-backfill (M1-M5) verified live (full pull 94/94 + incremental `POST /ingest/plan` re-running as "0 to fetch" + a popup button with progress; extension repackaged to 0.2.4). **Phase B (PyPI-first install): CLOSED** — stable per-user data dir + self-contained autostart for wheel installs on all three OSes (launchd / systemd / Scheduled Task), **`0.3.1` published on PyPI** and a one-command installer (`scripts/install-pypi.sh`/`.ps1`). **Windows wheel autostart verified live** (the logon Scheduled Task runs `pythonw -m memex.cli.main serve`; a 0.3.1 fix reopens std streams since pythonw has none). Remaining: a live Linux systemd run and the Web Store push of the 0.2.4 extension. Next gate: the Hacker News launch.

## Guiding principle

The context Claude.ai has should be available to Claude Code, and the context Claude Code has should be available from Claude.ai: one memory, reachable from wherever you are talking to Claude. Every phase has to move toward that goal. If a task does not contribute, it does not belong here.

---

## Phase 0: validate retrieval

**Goal:** kill the biggest risk before investing time. Prove that with local embeddings on the real chat corpus, semantic search returns reasonable results.

**Tasks:**
- [x] Inspect the official Claude.ai JSON export (schema, chat count, edge cases). See DEVLOG entry from 2026-05-18.
- [x] Implement `core/models.py` with pydantic: `Project`, `Conversation` (with `source` field: 'conversations' / 'design_chat' / 'memory'), `Message` (with `parent_uuid`, `raw_content`, `has_tool_use` / `has_attachments` flags), `Chunk`, `SearchHit`.
- [x] Implement `core/storage/` (schema with 4 tables + virtual `vec_chunks`, connection, initial migration, repo CRUD). 28 unit tests green.
- [x] Implement `core/ingest/claude_export.py` with four parsers: `parse_project`, `parse_conversations_list`, `parse_design_chat`, `parse_memories`. The curated memory is modeled as a synthetic conversation with a stable uuid.
- [x] Implement `core/ingest/content_renderer.py` to convert Claude.ai `content[]` into plain text. Tool blocks render with markers: `[tool_use: <name>] <input>`, `[result] <text>`.
- [x] Implement `core/ingest/chunker.py` (~500 tokens with overlap 50, char-based with configurable `chars_per_token` factor).
- [x] Implement `core/embeddings/` (`Embedder` interface + Ollama client with `nomic-embed-text`, plus deterministic `FakeEmbedder` for tests). 7 integration tests green against real Ollama.
- [x] Implement `core/retrieval/search.py` (semantic search with sqlite-vec, joins with `messages` and `conversations` to hydrate results). Lives in `core/storage/repo.py::vector_search`.
- [x] Minimal CLI: `memex ingest <path>`, `memex search "<query>"`, `memex stats`.
- [x] Unit tests for chunker, content_renderer, parser, and pipeline. One integration test for the full flow against the real export.
- [x] Run real searches over the full corpus (74 chats, 1024 messages, 614 chunks). 6 of 7 with relevant top-3. Known limitation: queries with rare proper nouns ("Amarok" case) fail; solved with hybrid search in Phase 2.
- [x] Phase-close audit. One blocker found and fixed (`memex-mcp` entrypoint pointing to a non-existent module). Minor follow-ups noted in DEVLOG for Phase 1+.

**Close criterion:** at least 7 of 10 searches return a relevant chat in top-3. If it fails, conscious decision about switching model (bge-base), tuning chunking, or reconsidering the approach.

**Estimated duration:** 1 to 2 days.

---

## Phase 1: MCP MVP

**Goal:** let Claude Code use Memex via stdio in real sessions.

**Tasks:**
- [x] FastMCP server with the 3 tools: `search_chats`, `get_chat`, `list_recent_chats`. Implemented in `src/memex/transports/tools.py` (pure logic) + `src/memex/transports/stdio.py` (MCP layer).
- [x] Stdio transport (`memex-mcp` as entrypoint). Re-registered in `pyproject.toml`.
- [x] Configuration documented for Claude Code (`.mcp.json`). In README, section "Connecting to Claude Code".
- [x] Clear error handling: `EmbedderError` wrapped in `{"error": ...}` JSON. Empty queries, non-existent uuids, invalid sources return actionable errors without crashing.
- [x] Use Memex in real sessions. Validated in 2 Claude Code sessions exercising all 3 tools (`search_chats`, `get_chat` with pagination, `list_recent_chats`). In the first `get_chat` invocation a bug was caught (response exceeded the client max-tokens limit), fixed with pagination + truncation. In the second iteration Claude Code discovered `messages_offset` on its own, which validates the quality of the docstrings.
- [x] Phase-close audit. No blockers. Minor follow-ups closed: dead code in `stdio.search_chats`, docs synced (CLAUDE.md, README, ROADMAP). Follow-ups deferred to Phase 4: generic error message to the remote MCP client (avoid leaking paths/queries), explicit catch of connection exceptions in `OllamaEmbedder` (today with fragile substring check).

**Close criterion:** Memex running in Claude Code, 5 real sessions with at least one tool invoked, no crashes.

**Estimated duration:** 1 to 2 weeks.

---

## Phase 2: live capture + hybrid retrieval

**Dual goal:**
1. Improve retrieval quality with hybrid search (FTS5 + vectors). Solves the "rare proper nouns" case (e.g., "Amarok") where pure semantic search fails.
2. Make new chats appear in Memex without asking for a manual export.

**Tasks:**
- [x] **Hybrid search FTS5 + RRF.** Schema `fts_chunks` (unicode61 remove_diacritics). `repo.text_search`, `repo.hybrid_search` (RRF k=60), `repo.rebuild_fts_index`. `tools.search_chats` with `mode: hybrid|semantic|lexical` (default hybrid). CLI `memex search --mode` and `memex reindex-fts`. Validated against the real corpus: Amarok case solved in hybrid mode top-2; no regression on semantic queries. (2026-05-19)
- [x] **Local HTTP endpoint** (`transports/http_ingest.py` with Starlette). `POST /ingest/conversation` with origin check (`chrome-extension://` / `moz-extension://`) + shape validation + error handling. `pipeline.ingest_single_conversation()` reusable. CLI `memex serve --host --port --db`. 14 tests with TestClient and a live smoke test with real uvicorn. (2026-05-19)
- [x] **Memex Chrome extension** (`chrome-extension/`). MV3, host_permissions to `claude.ai/*` + `127.0.0.1:5777/*`. inject.js based on SyncChat (rename), content.js as bridge, background.js POSTs to the endpoint with stats in `chrome.storage`, popup HTML/JS with status indicator. README with unpacked load instructions. (2026-05-19)
- [x] **Idempotency**: already covered by the existing architecture. `repo.add_chunk` + `delete_chunks_for_conversation` keep chunks + vec_chunks + fts_chunks in sync; re-ingesting the same chat replaces without duplicating.
- [x] **Windows autostart (Phase 5 preview)**: `scripts/install-autostart.ps1` with verbs `-Install` / `-Uninstall` / `-Status`. Registers a Scheduled Task that starts `uv run memex serve` hidden on user logon, no admin. Replaceable by cross-platform `memex install-service` when we reach Phase 5. (2026-05-20)
- [x] Real use of the Chrome ext: new chats on claude.ai being ingested via `memex serve` running as Scheduled Task. End-to-end flow validated in real browsing, survives VS Code close, reboots, and OS changes in dual boot. (2026-05-20)
- [x] Phase-close audit: no blockers. 3 important fixes applied at close (mixed encoding in the PowerShell wrapper log, XSS hardening of the Chrome ext popup with `innerHTML` + `textContent` + DOM API, stale CI doc). 4 minor fixes (test count in ROADMAP, inconsistent `New-Item` between scripts, imprecise comment in `http_ingest.py`, residual em dash in `popup.js`). Detail in DEVLOG. (2026-05-20)
- [ ] **Deferred to Phase 3+**: live capture of project chats (`design_chats`). The parser and the server already distinguish `Source.DESIGN_CHAT` and accept `?source=design_chat`, but the Chrome ext only intercepts `/chat_conversations/{id}` (regex in `chrome-extension/src/inject.js`) and posts without the query param. Missing: identify the real URL claude.ai uses for chats inside a project, extend the inject.js regex, add `source` routing in background.js. Does not block Phase 2 because the main corpus works; resume when source completeness is prioritized.

**Close criterion:** opening a new chat on Claude.ai makes it queryable from Claude Code in less than 1 minute.

**Estimated duration:** ~1 week.

---

## Phase 3: quality pass

**Goal:** raise retrieval quality and the relevance of injected context.

**Tasks:**
- [x] **Auto-generated summaries with Claude Haiku, on-demand at the first `search_chats`.** Opt-in via `MEMEX_SUMMARY_ENABLED=true` + `ANTHROPIC_API_KEY`. Optional extra `summaries` brings the Anthropic SDK. Design: when `tools.search_chats` returns hits, if a summarizer is active, up to 3 summaries are generated lazily in parallel (`ThreadPoolExecutor`) only for the top-N conversations without a cached summary. The result is persisted with `repo.update_conversation_summary`, so the next query of the same conv hits cache. Silent fail per chat: if the API fails for one, that result comes back without a summary and the rest proceed normally; the search never aborts. The pipeline persists `conversations.content_hash` (SHA-256 hex of the canonical text) to invalidate stale summaries when content changes (live capture appends messages). Pivot decision: the original approach (generate at ingest-time) was slow and spent on chats the user never consulted; on-demand pays only for what gets used. 24 new tests. (2026-05-23)
- [x] **Chat ↔ project/repo association.** New `repos` and `chat_repos` tables. `core/repos/` module: `keys.py` canonicalizes paths + git remote URLs into stable keys; `discovery.py` reads `.git/config` and `pyproject.toml`/`package.json`/`Cargo.toml`; `matcher.py` scans chat text for 4 signals (remote URL 1.0, path 0.9, manifest name 0.8, display name 0.5) with a 0.5 threshold. Auto-scan runs at ingest time. CLI: `memex repos add/list/remove/scan` + `memex tag/untag` for manual overrides (manual associations are sticky). `search_chats(query, repo=...)` accepts a path / remote URL / canonical key, resolves it, and boosts associated chats by `0.3 * confidence` (lower distance = better; chats outside the repo still appear). 65 new unit tests covering keys (21), discovery (11), matcher (15), storage helpers (18), pipeline auto-scan (4), CLI (15), search boost + resolve (7). (2026-05-24)
- [x] **Optional `SessionStart` hook.** New `memex session-context` command. Detects the active repo from cwd (walks up looking for `.git` via new `find_repo_root` helper), resolves it through the registered-repos table, and prints a short Markdown blob with the top N chats (manual first, then auto by confidence). Silent no-op when no `.git`, repo not registered, or no associations (diagnostics go to stderr so the hook does not pollute the injected context). User wires it into `.claude/settings.json` `SessionStart` hook. `_resolve_repo_key` extracted from `transports/tools.py` to `core/repos/resolve.py` so both the CLI and the search tool share it. 9 new tests (5 CLI command + 4 `find_repo_root`). (2026-05-24)
- [x] **`find_related(current_context)` tool.** New MCP tool for "more like this" retrieval: takes free-form text (paragraph, file contents, current discussion) and returns semantically similar chats. Pure vector search (no FTS, since long input makes BM25 less informative). Input capped at `FIND_RELATED_MAX_INPUT_CHARS=4000` chars. Same repo-boost semantics as `search_chats`. Wired into `stdio.py` as the 4th MCP tool. 7 new tests. (2026-05-24)
- [x] Phase-close audit. No blockers. 2 critical fixes applied during the pass: `assert` in `cli/main.py::session_context` replaced with explicit branch (asserts get stripped under `python -O`), and `…` (U+2026) in `repos scan` status replaced with `...` for cp1252 safety. 6 minor deferrals documented in DEVLOG (N+1 in scan_repos, scan transaction granularity, CLI help language mix, theoretical race in associate_chat_repo, Rich table truncation on long keys, case-sensitivity asymmetry between Windows/Linux). Detail in DEVLOG entry "2026-05-24 (close)". (2026-05-24)

**Estimated duration:** 1 to 2 weeks.

---

## Phase 4: remote transport

**Goal:** Claude.ai consumes Memex as a remote MCP.

**Tasks:**
- [x] **Streamable HTTP transport.** (Plan said "SSE/HTTP"; claude.ai deprecated HTTP+SSE, so Streamable HTTP it is.) Shared server factory extracted to `transports/mcp_server.py`; `transports/http.py` mounts it at `/mcp` with `TrustedHostMiddleware` pinned to the public hostname. CLI `memex serve-remote` binds loopback behind a tunnel (Tailscale Funnel) that publishes `MEMEX_REMOTE_BASE_URL`. (2026-06-11)
- [x] **Auth: GitHub OAuth with username allow-list.** The original "local token in header" idea is impossible for claude.ai (its UI only supports authless or full OAuth with dynamic client registration). Implemented as FastMCP's `GitHubProvider` (OAuth proxy over a GitHub OAuth App, DCR + PKCE) subclassed by `AllowlistGitHubProvider`: `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS` enforced on every request, fail closed (server refuses to start with an empty allow-list). 14 new tests. (2026-06-11)
- [x] Document how to connect from Claude.ai. README section "Connecting from claude.ai" (Funnel, OAuth App, `.env`, connector add); `.env.example` updated. (2026-06-11)
- [x] Real end-to-end validation: Tailscale Funnel up (`https://dionisios-macbook-air.tail2a5fa8.ts.net`), GitHub OAuth App created, connector added in claude.ai, `search_chats` invoked from a real chat and returning real history. Mac DB populated from a fresh export (2 projects, 96 conversations, 1607 messages, 1064 chunks). (2026-06-11)
- [x] Phase-close audit. Two parallel auditors (security with attacker mindset over the internet-exposed surface; correctness/dead-code/docs over the full phase diff). **No critical/high; no correctness bugs; docs in sync.** Security verdict: the core claim holds, the allow-list runs per request against an unspoofable live GitHub identity, no fail-open path. Fixes applied at close: allow-list now also matches the immutable numeric id (`sub`) not just username (LOW-1 hardening); 2 stale docstrings updated; fragile `__mro__[1]` test patch replaced. (2026-06-11)

**Estimated duration:** ~1 week.

**Phase 4 CLOSED (2026-06-11).** Memex is consumable from claude.ai (web, Desktop, mobile) as a custom connector, validated end-to-end. Operational caveat: requires the Mac on + Funnel up + `memex serve-remote` running; making it a persistent service is deferred (macOS launchd is a 0.2.0 item).

---

## Phase 5: release

**Goal:** other people use it.

**Tasks:**
- [x] **Cross-platform service install (`memex install-service`).** Dispatcher CLI command that detects the host OS and runs the right installer: Windows (existing Scheduled Task via PowerShell), Linux (new systemd user unit at `~/.config/systemd/user/memex-serve.service`). macOS prints manual start instructions; launchd support deferred to 0.2.0. (2026-05-25)
- [x] **`memex doctor` diagnostic command.** Reports OK / WARN / FAIL across Python version, database, embedder, live-capture server, summarizer config, registered repos, and indexed corpus. Exits 1 only on FAIL so it is script-safe. The "what is wrong with my setup?" answer for users. (2026-05-25)
- [x] **PyPI prep: rename `memex` → `memex-mcp`, refresh pyproject metadata.** Description in English, `Operating System :: OS Independent` classifier, `Development Status :: 3 - Alpha`, `[project.urls]` for Homepage / Repository / Issues / Changelog. CLI entry point stays `memex`. Build smoke (`uv build`) produces a clean `memex_mcp-0.1.0-py3-none-any.whl`. Publication itself to PyPI requires the maintainer's token; commands documented. (2026-05-25)
- [x] **Chrome Web Store submission checklist.** Manifest description translated to English, version aligned to `0.1.0`. Full submission playbook in `chrome-extension/WEB_STORE_CHECKLIST.md`: developer account fee, privacy policy URL, asset sizes (icons, screenshots, promo tiles), submission ZIP build, listing copy ready to paste, permissions justification table. Actual submission requires the maintainer's developer account. (2026-05-25)
- [x] README quickstart revised for installable alpha (`pip install memex-chats` / `uvx --from memex-chats memex` path first, source path second). Autostart section unified across Windows + Linux + macOS placeholder. (Distribution renamed to `memex-chats` after `memex-mcp` turned out to be claimed on PyPI by an unrelated project; CLI commands `memex` and `memex-mcp` are unchanged.)
- [x] **PyPI publish.** `memex-chats 0.1.0` live at https://pypi.org/project/memex-chats/ (2026-05-25). First publish attempt under `memex-mcp` failed with HTTP 403 because that name had been claimed earlier the same day by an unrelated MCP project; renamed the distribution to `memex-chats` (CLI commands unchanged, no breaking change for existing `.mcp.json` configs). Wheel + sdist uploaded with an explicit file list to `uv publish` after a stray Chrome ext ZIP in `dist/` confused the tool the first time; layout fixed afterwards by moving Web Store artifacts to `chrome-extension/dist/`.
- [x] **Chrome Web Store submission.** `memex-live-capture 0.1.0` submitted on 2026-05-25 with `Unlisted` visibility for alpha. Listing copy, screenshots, permissions justifications, and the `PRIVACY.md` URL provided per the checklist. In review (typical first-pass: 5 to 10 business days).
- [x] **Chrome Web Store: published** (2026-06-12). Live at chromewebstore.google.com/detail/memex-live-capture/bncngnabecfilefblppkolhdnaelibnb (Unlisted). Listing copy version bumped to 0.2.1; a 0.2.1 submission ZIP is built at `chrome-extension/dist/` if a resubmit is wanted.
- [ ] Screencast / demo video (nice to have, not blocking).
- [ ] Discord: an update post is drafted (announcing 0.2.x, the connector, Claude Code ingestion, the security work) with the Web Store link; a second "finished" post is planned. Not yet posted by the maintainer.
- [ ] Windows auto-sync: a PowerShell equivalent of the SessionEnd hook (the bash hook is macOS/Linux only).
- [ ] Add a `SECURITY.md`-driven private disclosure flow: DONE (SECURITY.md shipped); maintainer must enable GitHub "Private vulnerability reporting" in repo settings for the button to appear.
- [x] macOS launchd support. Shipped in 0.2.0: plist templates + daemon scripts for `serve`, `serve-remote`, and the scheduled ingest, documented in the README "Running always-on (macOS)" section. The `install-service` CLI on macOS points there. (2026-06-11)

---

## Security hardening (0.1.1, 2026-06-01)

Full multi-agent audit across 10 trust boundaries; no critical/high, data layer clean. Fixes shipped in 0.1.1 (see CHANGELOG / DEVLOG):
- [x] Per-install access token on `/ingest` (`X-Memex-Token`) + extension pairing; `memex token` command.
- [x] `TrustedHostMiddleware` (DNS-rebinding) + non-fingerprinting `/health`.
- [x] Request body cap (413) + per-conversation chunk cap.
- [x] DB created `0600` / dir `0700`; explicit `busy_timeout`; shorter WAL write-lock hold; best-effort lazy-summary persist.
- [x] Indirect-prompt-injection envelope on MCP results + summarizer body fencing.
- [x] Dependency floors raised (`starlette>=0.47.2`, `fastmcp>=3.2.0`); `OLLAMA_HOST` / embed-model / extension-URL validation.

Deferred follow-ups (low severity, tracked here):
- [ ] Provenance column to distinguish live-captured from exported chats, surfaced through the MCP tools as an `unverified` flag.
- [ ] Render-time neutralization of `[role]`/`[tool_use]`/`[result]` markers inside stored text (needs a re-ingest to fully apply; the MCP-boundary envelope already covers the read path).
- [ ] Package `scripts/` into the wheel (or use `importlib.resources`) so `install-service` works for PyPI installs; revisit `-ExecutionPolicy Bypass`.
- [ ] Pin the fastembed model revision / verify the cached artifact hash.

---

## Phase 6: Claude Code / terminal ingestion

**Goal:** close the loop the other way. The guiding principle was "the context Claude.ai has should also be available to Claude Code"; this adds "and the context Claude Code has should be available everywhere too". One store, searchable from claude.ai (remote connector) and from Claude Code (stdio), covering both halves of the user's history. (Previously listed as out of scope deferring to Claude Historian; the user prioritized a single unified brain, so it moved in scope.)

**Tasks:**
- [x] **`Source.CLAUDE_CODE`** added to the model + a `conversations.source` CHECK migration. SQLite cannot ALTER a CHECK in place, so `_migrate_conversations_source_check` recreates the table (data + indexes preserved, FKs handled) for pre-existing DBs; idempotent. (2026-06-11)
- [x] **Parser `core/ingest/claude_code.py`.** One `~/.claude/projects/**/<sessionId>.jsonl` -> one conversation (uuid=sessionId, title from `ai-title` or derived, timestamps from first/last event). Reuses `content_renderer` (tool markers kept, `thinking` dropped for free as an unknown block). Filters per the user's choices: `isSidechain` sub-agents, `isMeta` lines, and harness plumbing (slash-command / bash wrappers) dropped. Malformed lines skipped, not fatal. (2026-06-11)
- [x] **Pipeline `ingest_claude_code_sessions`** + CLI `memex ingest-claude-code`. Incremental: unchanged sessions (same `content_hash`) skipped without re-embedding (`skip_unchanged` flag), so re-scans over hundreds of files are cheap. Each session auto-associated to the registered repo of its `cwd` (resolved via `resolve_repo_key`, confidence 1.0) so `search_chats(repo=...)` boosts work on that project. (2026-06-11)
- [x] **Secret redaction** (`core/ingest/redact.py`) on the `claude_code` path: masks provider API keys, JWTs, PEM blocks, `KEY=`/`SECRET=` assignments, `Bearer` tokens, and URL passwords before storage/embedding; `raw_content` not persisted for this source. Added after the close audit flagged that the remote connector exposes terminal output verbatim (user chose redact + full access). (2026-06-11)
- [x] Tests: parser (filters, tool_result render, title/timestamps, malformed lines, redaction), pipeline (incremental skip, re-ingest, repo association incl. git-remote-keyed repos), redaction unit suite, and the CHECK migration. **396 green.** (2026-06-11)
- [x] Real bulk ingest of the local sessions + unified-search validation (one query returning both claude.ai and Claude Code hits). (2026-06-11)
- [x] **Phase-close audit** (security/privacy + correctness). One shipping-blocker fixed (cwd→repo association failed for git-remote-keyed repos: `resolve_repo_key` now also matches the `repos.path` column); one HIGH by-design privacy risk addressed with secret redaction; plus symlink containment, title/timestamp robustness, and doc-sync fixes. (2026-06-11)
- [ ] Keep-fresh automation (periodic launchd scan or a Claude Code SessionEnd hook). MVP is the manual command; deferred.

**Phase 6 CLOSED (2026-06-11).** One unified store searchable from claude.ai (remote connector) and Claude Code (stdio), covering both halves of the history.

---

## Phase 7: frictionless onboarding — auto-backfill of claude.ai history (PLANNED)

**Why:** the demo magic (recall an old claude.ai chat from Claude Code) only lands if the user's full history is already indexed on first run. Today it is not. The Chrome extension is purely passive: it monkey-patches claude.ai's `fetch` and keeps only the responses the site itself makes (`GET /chat_conversations/{id}` when you open a chat, `POST /chat_conversations` when you create one; see `chrome-extension/src/inject.js`). So old chats enter Memex only via a manual export zip or by opening each chat by hand. A new user installs Memex and finds an empty store, which kills the first impression. This phase makes "install -> full history searchable" the default path. It is the gate before the Hacker News launch.

**Architectural constraint (do not forget this):** Claude Code / the terminal has NO access to the user's claude.ai account (no cookies, no API token; Anthropic exposes no history API). The backfill therefore CANNOT be triggered from a Claude Code `SessionStart` hook, however intuitive that sounds. The only contexts that hold the claude.ai session are (a) the browser, where the extension already runs in claude.ai's MAIN world with the user's cookies, or (b) a manually supplied `sessionKey` cookie. The pull must originate in one of those.

**Onboarding (one-command setup) — Phase A shipped 2026-06-19.** The other half of frictionless onboarding is collapsing the install itself. `memex setup` now wires the whole thing in one idempotent command: registers the MCP server (`claude mcp add`), installs the always-on service, indexes local Claude Code sessions, and prints the extension pairing token, degrading each step to a warning instead of aborting. `memex install-service` gained a real macOS launchd implementation (it was a print-only stub), so autostart is cross-platform now (launchd / systemd / Scheduled Task), installing `serve` + the ingest backstop by default and the remote connector only with `--remote` (it crash-loops without `.env` config, so opt-in). Still repo-anchored in Phase A. **Phase B (PyPI-first), mostly done:** (1) the DB/exports default resolves to an absolute path, `<repo>/data` from a cloned/editable install (unchanged for existing users) and an OS-conventional per-user dir from a wheel/PyPI install (macOS `~/Library/Application Support/memex`, Windows `%LOCALAPPDATA%`, XDG elsewhere), so `uvx memex setup` has a stable DB home with no clone (`config.py:_default_data_dir`). (2) Self-contained autostart for wheel installs on all three OSes: `cli.services` generates a launchd plist (macOS) / systemd user unit (Linux) / logon Scheduled Task (Windows, via `schtasks /Create /XML`, running `pythonw -m memex.cli.main serve`), so it does not depend on the repo `scripts/` or a console script on PATH. Verified by generating + parsing each artifact (`plistlib` for the plist, `ElementTree` for the task XML), and **Windows verified live end to end** (`pipx install memex-chats` -> `memex install-service` -> the logon Scheduled Task starts the server, `0.3.1` after the headless-pythonw stream fix). A one-command bootstrap (`scripts/install-pypi.sh` / `.ps1`) installs uv + the tool + runs setup. Remaining: a live Linux systemd run. (Persistent autostart wants `pip`/`pipx` or `uv tool`, not transient `uvx`.)

**Approach A (default): active backfill in the extension.** `inject.js` already lives in the page with the session and a patched `fetch`; extend it from passive interception to active pull.
- [x] **(M1, verified 2026-06-19)** Conversation-list endpoint is `GET /api/organizations/{org_uuid}/chat_conversations?limit=&offset=`, a flat JSON array; offset pagination works (no page overlap) and `limit=1000` returned the full list. Pick the org whose `capabilities` include `chat` from `GET /api/organizations` (the `api`-only org is the wrong one). Each list item carries `uuid` + `updated_at` (enough for incremental skip). Full conversation: `GET .../chat_conversations/{id}?tree=True&rendering_mode=messages&render_all_tools=true` returns `chat_messages`; since `inject.js` classifies `conv-full` by path (query stripped), the backfill fetch is captured by the existing hook with no pipe changes (M2).
- [x] **(M2, verified 2026-06-19)** Active backfill in `inject.js`: `window.__memexBackfill()` enumerates the chat org, pages the full conversation list, and fetches each conversation's full content through the patched fetch, so the existing pipe (postMessage -> content.js -> background -> `POST /ingest/conversation`) ingests it with ZERO changes elsewhere. Verified live: 94/94 fetched, 0 failed, +5 brand-new chats indexed (the rest deduped by uuid/content_hash). Concurrency 3 + a 200 ms throttle, with a re-entrancy guard. Console-triggered for now; the popup "Backfill history" button is M4.
- [x] **(M3, verified 2026-06-19)** Incremental: before fetching, the extension sends the conversation manifest ({uuid, updated_at}) to `POST /ingest/plan`, which compares against the indexed set (by instant, since fractional seconds are normalized) and returns only the to-fetch uuids. The indexed set never leaves the server. A POST (not GET) so the request reliably carries the extension Origin (a cross-origin GET may drop it). Any failure falls back to fetching all (the server still dedups). Verified live: a fully-indexed account re-runs as "0 to fetch, 94 up to date". Resumability falls out of this: a closed-tab or interrupted run just re-clicks, and the already-stored convs are skipped. (Provenance tag deferred; not needed for the skip.)
- [x] **(M4, verified 2026-06-19)** Popup "Backfill history" button (triggers `window.__memexBackfill` in the page's MAIN world via `chrome.scripting.executeScript`, so no content.js trigger relay) + live progress UI. Progress flows inject -> content -> background -> popup over a new `control` message channel kept disjoint from the capture path (so the validated capture flow is untouched). Fire-and-forget: closing the popup does not stop the run.
- [ ] Periodic re-list (not just live capture) so chats created on other devices also land.

**Approach B (fallback, power user): `memex pull --session-key <cookie>`.** Headless CLI that hits the same internal API directly with a pasted `sessionKey`. One-time paste, then schedulable. Caveats: ToS-grey, the cookie rotates/expires, brittle to API changes. Not the default; documented as the option for users who will not install the extension.

**Provenance:** ties into the deferred 0.1.1 item (a column distinguishing live-captured / exported / backfilled chats, surfaced as `unverified`). Backfilled chats should be tagged so the source stays auditable.

**Close criterion:** a brand-new user, after install + extension pair (or one `memex pull`), has their full claude.ai history searchable from Claude Code with no manual export and no opening chats by hand. The demo GIF reproduces on a fresh machine.

**Risks:** depends on claude.ai's internal, unofficial API, so it can break when Anthropic changes it (but the extension already depends on that API, so it is more surface, not new surface). ToS: same posture as the existing capture (publish openly with a disclaimer). Volume/rate: throttle to avoid hammering claude.ai.

**Estimated duration:** ~1 to 1.5 weeks.

---

## Out of scope

- Multi-user or cross-account sharing.
- Cloud or hosted.
- Fancy browse UI (CLI and MCP are enough).
- Attachments, tool_use, files (text only in v1 and v2).
- Team-level shared memory.

## Open risks

- **Anthropic ships an official solution:** high probability in 6 to 12 months. Mitigation: position as local-first with full corpus access.
- **Poor retrieval with local embeddings:** mitigated by Phase 0.
- **Anthropic ToS around capture:** same risk as SyncChat. Decision: publish openly with a disclaimer, same level as ShareGPT.
