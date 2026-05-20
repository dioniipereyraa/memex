# Memex

[![CI](https://github.com/dioniipereyraa/memex/actions/workflows/ci.yml/badge.svg)](https://github.com/dioniipereyraa/memex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Local-first MCP server that indexes your Claude.ai chat history and exposes it to Claude Code (and, soon, to Claude.ai via remote MCP). The goal: give Claude Code the same context Claude.ai already has.

**Status:** pre-alpha. Phases 0 and 1 closed. **Phase 2 in progress:** hybrid FTS5 + RRF search closed (fixes the "Amarok" case); live capture via Chrome extension + local HTTP server working, pending a week of real usage and a phase-close audit.

> Internal docs (`ROADMAP.md`, `DEVLOG.md`) are kept in Spanish on purpose. They are the project journal, not user-facing material.

![Session memory check](docs/screenshots/session-memory-check.jpeg)

*End-to-end demo: a chat on claude.ai captured seconds earlier by the Chrome extension, recalled from Claude Code via `list_recent_chats` and `get_chat`. No extra prompting, no manual context handoff.*

## The problem

Brainstorming and planning happen in Claude.ai. Execution happens in Claude Code. The two worlds do not talk to each other: Claude Code cannot read a chat of yours from Claude.ai, not even the one that originated the task it is currently working on. The memory Anthropic shipped on Claude.ai (March 2026) is curated, not full history, and lives isolated inside Claude.ai.

Memex fills that gap: runs locally, indexes the entire corpus of your chats, and exposes them as MCP tools so Claude can search and pull past context whenever it needs to.

## How it works

```
[Claude.ai]
    ↓  (official JSON export / Chrome ext)
[Ingestor]  →  [SQLite + sqlite-vec]  →  [local embeddings (fastembed / Ollama)]
                                    ↓
                          [core: storage + retrieval]
                                    ↓
                  [MCP stdio]  ───→  Claude Code, Claude Desktop
                  [MCP SSE/HTTP] ──→ Claude.ai (coming soon)
```

Design: pure core (storage, ingest, embeddings, retrieval) decoupled from transport. The same engine serves both stdio and remote MCP without a rewrite.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (package manager)

Embeddings: **zero-config by default** (uses [fastembed](https://github.com/qdrant/fastembed) with a quantized 130 MB model that downloads itself the first time).

If you would rather route through your local Ollama (because you already run it for other models), set:
```bash
export MEMEX_EMBED_BACKEND=ollama
# and optionally:
ollama pull nomic-embed-text
```

## Quickstart

1. Clone the repo and install deps:
   ```bash
   git clone https://github.com/dioniipereyraa/memex
   cd memex
   uv sync
   ```
2. Request your official Claude.ai export (Settings → Privacy → Export data), unzip it, and drop the zip into `data/exports/`.
3. Ingest:
   ```bash
   uv run memex ingest data/exports/<your-export>.zip
   ```
   The first run takes a couple of minutes generating embeddings (downloads the fastembed model on first use).
4. Search:
   ```bash
   uv run memex search "your query" -n 5
   uv run memex stats
   ```

## MCP server tools (v1)

- `search_chats(query, limit=5, source?, mode="hybrid")` searches the corpus. Modes: `hybrid` (default, combines vector search + FTS5 BM25 via Reciprocal Rank Fusion), `semantic` (vectors only), `lexical` (FTS5 only, ideal for proper nouns or exact terms). `source` filters by origin (`conversations`, `design_chat`, `memory`). Deduplicated per conversation.
- `get_chat(uuid, messages_limit=20, messages_offset=0)` fetches a conversation with its messages, paginated. `raw_content` is omitted; each message is truncated to 3000 chars to stay inside the client's token budget.
- `list_recent_chats(limit=10, source?)` lists the latest chats ordered by last update.

Search is also reachable from the CLI with `memex search "query" --mode {hybrid|semantic|lexical}`. For databases created before the hybrid FTS5 work, run `memex reindex-fts` once to populate the lexical index.

## Wiring it into Claude Code

Once your local database is populated (`memex ingest`), start the MCP server with `uv run memex-mcp`. For Claude Code to discover it automatically, add a `.mcp.json` file at the root of your project (or a user-level server in `~/.claude.json`):

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex-mcp"],
      "cwd": "/absolute/path/to/the/memex/repo"
    }
  }
}
```

Set `cwd` to the absolute path where you cloned Memex (where `pyproject.toml` lives). Restart Claude Code and the tools `search_chats`, `get_chat`, `list_recent_chats` will show up in the session.

The same searches are also available from the CLI via `uv run memex search "..."` if you prefer them outside Claude Code.

## Live capture (Phase 2)

So that new Claude.ai chats land in Memex without asking for a manual export:

1. **Start the local HTTP server** in a terminal:
   ```powershell
   uv run memex serve
   ```
   Listens on `127.0.0.1:5777` by default. Keep it running while you browse claude.ai.

2. **Load the Chrome extension** from the `chrome-extension/` folder:
   - Open `chrome://extensions/`
   - Enable **Developer mode**
   - **Load unpacked** → pick `chrome-extension/`
   - Click the Memex icon and confirm the "Server" chip says **responde** (green).

3. **Use claude.ai normally.** Every chat you open or create is ingested automatically. Verify with `memex stats` or by calling `search_chats` from Claude Code.

Details in [chrome-extension/README.md](chrome-extension/README.md).

#### Autostart on Windows (optional)

So you don't have to run `memex serve` by hand every time you log in:

```powershell
.\scripts\install-autostart.ps1 -Install
```

This registers a Scheduled Task (`MemexServe`) that runs `uv run memex serve` in the background at every log on, and triggers it immediately so the server is up right now. No admin required, no console window, no dependence on the shell that started it (you can close the terminal or VS Code and the server keeps serving). Auto-restarts up to 3 times if the wrapper dies.

Manage it with:

```powershell
.\scripts\install-autostart.ps1 -Status
.\scripts\install-autostart.ps1 -Uninstall
```

Logs go to `%LOCALAPPDATA%\Memex\serve.log`. Tail them with `Get-Content "$env:LOCALAPPDATA\Memex\serve.log" -Wait -Tail 20`.

**For non-technical use without terminals** (Phase 5): the cross-platform equivalent (`memex install-service`, with Linux systemd and macOS launchd backends) is on the ROADMAP. The Windows script above is the preview.

### Making Claude use Memex proactively

By default, LLMs are conservative with tools: they prefer to ask before invoking anything. If you say *"remember we talked about X?"*, Claude tends to answer *"I don't recall"* instead of searching.

The docstrings of the 3 tools already include "USE PROACTIVELY" instructions, but you can reinforce it by adding this snippet to your `CLAUDE.md` (global at `~/.claude/CLAUDE.md` for every session, or local at `<project>/CLAUDE.md` for a specific one):

```markdown
## Memex — persistent memory of Claude.ai chats

There is an MCP server `memex` with 3 tools: `search_chats`, `get_chat`, `list_recent_chats`.
They index ALL of the user's Claude.ai history, reachable via hybrid search
(semantic + lexical FTS5).

**Operational rule:** before answering "I have no record", "I don't remember", "this is
the first time I hear about this", or anything equivalent, call `mcp__memex__search_chats`
with the relevant query. Claude Code's native memory starts clean every session; Memex
is the only path into the user's real history.

Typical triggers: "remember when...", "did I tell you about...", "we already talked about...",
"the other day we discussed...", or any reference to a project / person / decision that might
live in history.
```

## Roadmap

See [ROADMAP.md](ROADMAP.md) for phases, close criteria, and current status (in Spanish, it is the internal journal).

## Devlog

See [DEVLOG.md](DEVLOG.md) for the log of decisions, blockers, and progress (in Spanish).

## Inspiration and references

- Official feature request: [anthropics/claude-code#12858](https://github.com/anthropics/claude-code/issues/12858)
- [Claude Historian](https://mcpmarket.com/server/claude-historian), [claude-conversation-extractor](https://github.com/ZeroSumQuant/claude-conversation-extractor): MCP servers for Claude Code / Desktop history. Reference for tool structure.
- [claude-conversation-export](https://github.com/Emnolope/claude-conversation-export): Claude.ai exporter using the same capture strategy. Useful as backfill.
- Spin-off of the [SyncChat](https://github.com/dionipereyrab/SyncChat) project.

## License

MIT.
