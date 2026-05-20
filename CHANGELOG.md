# Changelog

All notable changes to Memex are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and lives in `0.0.x` while in pre-alpha. No tags have been cut yet; the entries below summarize work by phase as defined in [ROADMAP.md](ROADMAP.md).

## [Unreleased]

Active development on Phase 2 (live capture + hybrid search) and Phase 5 prep (public-facing polish).

### Added
- CI workflow on GitHub Actions (lint, format check, mypy, unit tests).
- `CONTRIBUTING.md` with local setup, code style, and PR workflow.
- This `CHANGELOG.md`.
- Badges in the README: CI status, License MIT, Python 3.12+.
- "Session memory check" screenshot in the README, embedded as end-to-end demo of the live capture + recall flow.
- Chrome extension icons (16/32/48/128 PNG + SVG source under `chrome-extension/icons/`). Manifest declares them both top-level (`icons`) and in `action.default_icon` so the toolbar and the extension page render the brand instead of the gray placeholder.

### Changed
- README translated fully to English. `ROADMAP.md` and `DEVLOG.md` remain in Spanish (internal journal).

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
