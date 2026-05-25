# Roadmap

> Last updated: 2026-05-23

**Current state:** Phases 0 to 3 closed. **Phase 5 in progress:** code-side packaging tasks done (PyPI prep + Linux autostart + `memex doctor` + Chrome Web Store checklist). Remaining: maintainer-side submissions (PyPI publish, Chrome Web Store), screencast, Discord update. **341 unit tests green**, CI green, `ruff` + `mypy` clean.

## Guiding principle

The context Claude.ai has should also be available to Claude Code. Every phase has to move toward that goal. If a task does not contribute, it does not belong here.

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
- [ ] SSE/HTTP transport in FastMCP.
- [ ] Auth (local token, port forwarding or tunnel).
- [ ] Document how to connect from Claude.ai.
- [ ] Phase-close audit.

**Estimated duration:** ~1 week.

---

## Phase 5: release

**Goal:** other people use it.

**Tasks:**
- [x] **Cross-platform service install (`memex install-service`).** Dispatcher CLI command that detects the host OS and runs the right installer: Windows (existing Scheduled Task via PowerShell), Linux (new systemd user unit at `~/.config/systemd/user/memex-serve.service`). macOS prints manual start instructions; launchd support deferred to 0.2.0. (2026-05-25)
- [x] **`memex doctor` diagnostic command.** Reports OK / WARN / FAIL across Python version, database, embedder, live-capture server, summarizer config, registered repos, and indexed corpus. Exits 1 only on FAIL so it is script-safe. The "what is wrong with my setup?" answer for users. (2026-05-25)
- [x] **PyPI prep: rename `memex` → `memex-mcp`, refresh pyproject metadata.** Description in English, `Operating System :: OS Independent` classifier, `Development Status :: 3 - Alpha`, `[project.urls]` for Homepage / Repository / Issues / Changelog. CLI entry point stays `memex`. Build smoke (`uv build`) produces a clean `memex_mcp-0.1.0-py3-none-any.whl`. Publication itself to PyPI requires the maintainer's token; commands documented. (2026-05-25)
- [x] **Chrome Web Store submission checklist.** Manifest description translated to English, version aligned to `0.1.0`. Full submission playbook in `chrome-extension/WEB_STORE_CHECKLIST.md`: developer account fee, privacy policy URL, asset sizes (icons, screenshots, promo tiles), submission ZIP build, listing copy ready to paste, permissions justification table. Actual submission requires the maintainer's developer account. (2026-05-25)
- [x] README quickstart revised for installable alpha (`pip install memex-chats` / `uvx --from memex-chats memex` path first, source path second). Autostart section unified across Windows + Linux + macOS placeholder. (Distribution renamed to `memex-chats` after `memex-mcp` turned out to be claimed on PyPI by an unrelated project; CLI commands `memex` and `memex-mcp` are unchanged.)
- [ ] Submit Chrome extension to the Web Store (manual, blocked on the maintainer's developer account).
- [ ] Publish to PyPI (manual, blocked on the maintainer's PyPI token).
- [ ] Screencast / demo video (nice to have, not blocking).
- [ ] Update Discord post and gather feedback.
- [ ] macOS launchd support (deferred to 0.2.0).

---

## Out of scope

- Multi-user or cross-account sharing.
- Cloud or hosted.
- Indexing Claude Code chats (that is already covered by [Claude Historian](https://mcpmarket.com/server/claude-historian)).
- Fancy browse UI (CLI and MCP are enough).
- Attachments, tool_use, files (text only in v1 and v2).
- Team-level shared memory.

## Open risks

- **Anthropic ships an official solution:** high probability in 6 to 12 months. Mitigation: position as local-first with full corpus access.
- **Poor retrieval with local embeddings:** mitigated by Phase 0.
- **Anthropic ToS around capture:** same risk as SyncChat. Decision: publish openly with a disclaimer, same level as ShareGPT.
