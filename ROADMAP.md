# Roadmap

> Last updated: 2026-05-23

**Current state:** Phases 0, 1, and 2 closed (2026-05-20). **Phase 3 in progress:** first sub-task closed (auto-generated summaries **on-demand at the first `search_chats`** with Claude Haiku, opt-in via `MEMEX_SUMMARY_ENABLED` + `ANTHROPIC_API_KEY`). Remaining sub-tasks: chat ↔ project/repo association, SessionStart hook, `find_related` tool, phase-close audit. **220 unit tests green**, CI green, `ruff` + `mypy` clean.

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
- [ ] Chat ↔ project/repo association (so Claude Code matches against the current repo).
- [ ] Optional `SessionStart` hook to inject proactive context.
- [ ] `find_related(current_context)` tool.
- [ ] Phase-close audit.

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
- [ ] Polish README, screencast, Reddit/Discord post (SyncChat playbook).
- [ ] Package for `uvx memex` or installer.
- [ ] Handle feedback.

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
