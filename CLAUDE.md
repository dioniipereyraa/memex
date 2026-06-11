# CLAUDE.md

Context and rules for any Claude Code instance working in this repo (including parallel worktrees).

## Project idea in one line

The context Claude.ai has should also be available to Claude Code. Everything else (storage, embeddings, MCP, capture) is plumbing to get there.

Full detail in [README.md](README.md) and [ROADMAP.md](ROADMAP.md).

## Working rules (apply ALWAYS)

1. **Read the code before and after editing.** Before so you do not break anything, after to verify what ended up there.
2. **Keep README, ROADMAP, and DEVLOG in sync with every relevant change.** Update them in the same iteration as the code.
3. **Review the code you just wrote for bugs** before closing the task.
4. **When closing each ROADMAP phase, audit the whole project** for bugs, obsolete code, and vulnerabilities. Deliver a written report.
5. **Plan before coding.** No writing code without a clear plan.
6. **If there are real doubts, ask.** Do not assume.
7. **Code and plans designed to scale.** Clear separation of responsibilities (pure core, swappable transport, embedder and storage behind interfaces).
8. **No em dashes as connectors.** Use commas, periods, parentheses. Applies to docs, commits, code, and replies to the user.
9. **No Claude shoutouts in commits.** No `Co-Authored-By`, no AI footers. Commits signed only by the human author.
10. **Apply these rules in every iteration.**

## Stack

- Python 3.12+, package manager [uv](https://docs.astral.sh/uv/).
- [FastMCP](https://github.com/jlowin/fastmcp) for the MCP server (supports stdio and SSE/HTTP).
- SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) for storage and vector search.
- [fastembed](https://github.com/qdrant/fastembed) by default (zero-config, embedded ONNX) or optional [Ollama](https://ollama.com) with `nomic-embed-text`. Backend configurable via `MEMEX_EMBED_BACKEND`.
- `pydantic` + `pydantic-settings` for config and models.
- `typer` + `rich` for CLI.
- `pytest`, `ruff`, `mypy` for test/lint/typecheck.

## Architecture

```
src/memex/
├── config.py            ← settings with pydantic-settings (DONE)
├── core/                ← pure library, no transport
│   ├── models.py        ← Project, Conversation, Message, Chunk, SearchHit
│   ├── storage/         ← SQLite + sqlite-vec + FTS5 (schema, db, repo)
│   └── ingest/          ← parsers + chunker + pipeline (content_renderer, chunker, claude_export, claude_code, pipeline)
├── core/embeddings/     ← factory + interfaces
│   ├── base.py          ← Embedder ABC + EmbedderError + l2_normalize
│   ├── fastembed_embedder.py  ← default (ONNX, zero-config)
│   ├── ollama.py        ← optional (extra `ollama`)
│   ├── fake.py          ← deterministic FakeEmbedder for tests
│   └── __init__.py      ← get_default_embedder() factory based on MEMEX_EMBED_BACKEND
├── core/summaries/      ← LLM summarizer (Phase 3)
│   ├── base.py          ← Summarizer ABC + SummarizerError
│   ├── anthropic_summarizer.py  ← real backend, lazy SDK import
│   ├── fake.py          ← deterministic FakeSummarizer for tests
│   └── __init__.py      ← get_default_summarizer() factory, returns None if disabled
├── transports/          ← MCP bindings + local HTTP
│   ├── tools.py         ← pure logic of the 4 MCP tools
│   ├── mcp_server.py    ← shared FastMCP server + tool wrappers, build_server(auth=None) factory
│   ├── stdio.py         ← thin stdio entrypoint (memex-mcp), no auth
│   ├── http_ingest.py   ← local HTTP server for live capture (Starlette)
│   └── http.py          ← remote MCP over Streamable HTTP + GitHub OAuth allow-list (Phase 4)
└── cli/                 ← CLI with typer (ingest, search, stats, serve, serve-remote, reindex-fts)
```

**Dependency rule:** `core/` does not import from `transports/` or `cli/`. Arrows point inward.

**State as of 2026-06-11 (Phase 4 closed, Phase 6 code complete):**
- Phases 0 to 4 closed with audit; 0.1.0 on PyPI, 0.1.1 security hardening shipped.
- Phase 6: local Claude Code / terminal sessions are ingested into the same store (`memex ingest-claude-code`, source `claude_code`) via `core/ingest/claude_code.py`. One search spans claude.ai chats and Claude Code work. The `conversations.source` CHECK was widened with a table-recreation migration in `storage/db.py`.
- `vector_search`, `text_search`, and `hybrid_search` live in `core/storage/repo.py`. The `core/retrieval/` directory was removed (it was empty); if retrieval logic grows (re-ranking, complex filters), it gets recreated with real content.
- Remote MCP (`memex serve-remote`): loopback bind behind a tunnel (Tailscale Funnel) publishing `MEMEX_REMOTE_BASE_URL`; auth is a GitHub OAuth proxy with a username allow-list enforced per request (claude.ai only supports authless or full OAuth, never pasted tokens). Pending: real end-to-end validation from claude.ai + phase-close audit. Live capture uses `transports/http_ingest.py` (a different local server, not the MCP).

## Common commands

```bash
uv sync                       # install deps + create .venv
uv sync --extra summaries     # also install anthropic SDK (for the optional summaries feature)
uv run pytest                 # tests (-m 'not integration' to skip integration)
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src/memex/core    # type check (strict in core)
uv run memex --help           # CLI (ingest, search, stats, serve, reindex-fts)
uv run memex-mcp              # stdio MCP server (for Claude Code / Desktop)
uv run memex serve            # local HTTP server for live capture from Chrome ext
uv run memex serve-remote     # remote MCP (Streamable HTTP + OAuth) for claude.ai connectors
uv run memex ingest-claude-code   # index local Claude Code / terminal sessions
```

## Multi-Claude with git worktrees

To run several Claudes in parallel on independent tasks:

```bash
git worktree add ../Memex-ingest feature/ingest
git worktree add ../Memex-embed  feature/embeddings
git worktree add ../Memex-store  feature/storage
```

Each worktree is a separate folder with its own branch and its own `.venv` (uv isolates on its own). They converge to the same `.git`. Each Claude works without stepping on files, and at merge time all changes land in the same repo.

**Limits:** worktrees do not see each other until merge. Better to split by independent module, not by cross-cutting feature. The coordinator is the human (or a "lead" Claude on `main`).

## Sensitive data

Everything in `data/` is personal and NEVER goes to the repo (already excluded by `.gitignore`):
- `data/exports/*.zip`: Claude.ai exports with real conversations.
- `data/memex.db`: SQLite database with indexed chats.

The `MEMEX.md` file is also in `.gitignore` because it is an internal context document (SyncChat handoff), not for users.

## Commit conventions

- Clear messages, imperative mood, English or Spanish (whichever is consistent within the message).
- No `Co-Authored-By: Claude...`. No AI footers. No `Generated with Claude Code`.
- One commit per logical unit of change.

## Mistakes, bugs, and security findings (do not repeat)

Running log of things that broke, were found in audits, or were non-obvious design constraints. Read before touching the related area.

### Remote transport / claude.ai connectors (Phase 4, 2026-06-11)
- **claude.ai connectors cannot use a pasted token or custom header.** The only auth schemes its UI accepts are *authless* or *full OAuth 2.0 with dynamic client registration* (PKCE S256). The ROADMAP's original "local token in header" idea was impossible for this client. If you ever re-touch remote auth, do not reach for a bearer/header scheme: it will not connect.
- **The connection originates from Anthropic's cloud, never from the user's device.** So the server MUST be on a public HTTPS URL with a public IPv4 `A` record; `localhost`/private IPs are rejected at DNS validation. A tunnel (Tailscale Funnel) is mandatory, not optional. The user's machine must be on and the tunnel up for the connector to respond.
- **Allow-list must be enforced on every request and fail closed.** `AllowlistGitHubProvider.verify_token` (`transports/http.py`) re-checks the GitHub identity on each call (token-result caching is disabled by default in fastmcp's `GitHubTokenVerifier`, so revocation is immediate). `build_remote_app` refuses to start with an empty allow-list. Never add a code path that builds the remote server with `auth=None`; the only legitimately authless server is local stdio.
- **Match the immutable numeric id (`sub`), not just the username (`login`).** GitHub usernames are reusable after an account is deleted/renamed, so a username-only allow-list means a renamed/reused handle could grant access to the whole corpus. The allow-list now accepts either form (security audit LOW-1). The `login` claim itself is trustworthy (set live from `api.github.com/user`, keyed by the server-held token, not client-controllable) so it is fine to match on; the `sub` option is the stronger pin.
- **Test pitfall: do not patch via `Class.__mro__[1]`.** A fastmcp version bump can reorder the MRO and silently change what gets patched. Patch the method on the class where it is defined (e.g. `GitHubProvider.verify_token`), which `super()` resolves to regardless of intermediate classes.

### Claude Code ingestion + CHECK migration (Phase 6, 2026-06-11)
- **`PRAGMA foreign_keys` is a no-op inside a transaction.** The `conversations.source` CHECK migration (recreate-table to widen the allowed values) toggles `foreign_keys = OFF` so the `DROP TABLE` does not cascade. The first cut toggled it while an implicit transaction was still open, so FKs stayed ON and the swap failed with "no such table" against a forward-referenced parent. Fix: `conn.commit()` before the toggle. Verify with `PRAGMA foreign_keys` returning 0 if ever in doubt.
- **SQLite cannot ALTER a CHECK constraint in place.** Adding a new `source` value means recreating the table (the 12-step procedure). Copy columns by intersection of old and new, not `SELECT *`, so a DB predating a column (`ingested_at`, `content_hash`) still migrates. Always make it idempotent by checking the live DDL (`sqlite_master.sql`) for the new value first.
- **Reuse the renderer's unknown-block behavior on purpose.** `content_renderer` ignores unknown block types, so assistant `thinking` blocks are dropped for free without special-casing. When adding a new transcript format, lean on that instead of writing a second renderer.

### Security audit lessons (final sweep, 2026-06-11)
- **Bound every quantifier in redaction regexes.** A greedy `[...]*://` prefix made `url-credentials` O(n²) on a long whitespace-free blob (~6 s on 180 KB), and redaction runs on every captured session. Terminal/file capture routinely contains minified JS / base64 / long log lines, so unbounded patterns are a real DoS, not theoretical. Use `{0,N}` everywhere and add a large-blob timing regression test.
- **Redaction is best-effort; pair pattern rules with a high-entropy fallback.** Prefix-only rules miss random secrets with no known shape. A Shannon-entropy fallback (skip pure-hex/UUIDs and oversized base64 to avoid mangling) catches the rest. Redact BEFORE truncation so a secret straddling the cut is masked whole.
- **Never echo a secret to a stream that a daemon redirects to a file.** `memex serve` printed the ingest token at INFO; under launchd that went to a world-readable `data/serve.log`. Gate secret prints behind `sys.stdout.isatty()`, set `umask 077` in daemon scripts, and `Umask` (077) in the launchd plists (launchd creates `StandardOutPath` logs 0644 otherwise).
- **A config-time allow-list does not revoke until restart.** `AllowlistGitHubProvider` captured the allow-list once at construction; with a persistent daemon, removing a login had no effect until a manual restart. Re-read it on `.env` mtime change (fail-safe to the last good, non-empty set). GitHub-side token revocation was already immediate (token caching is off).
- **Put `TrustedHostMiddleware` outermost.** FastMCP appends user middleware after its auth backend, so a TrustedHost passed to `http_app(middleware=...)` runs AFTER auth — meaning a bad-Host request still triggers the per-request GitHub API call. Use `app.add_middleware(...)` (prepends) so Host is validated first.
- **`resolve_repo_key` must match the `repos.path` column, not just the key.** A repo with a git remote is keyed by the remote URL (`canonical_repo_key` prefers it), so resolving a filesystem path (a session `cwd`, or a user-passed `--repo` path) by key alone silently returns `None` for every cloned-from-GitHub repo. The Phase 6 cwd→repo association shipped broken because of this and a test that only covered `remote_url=None`. When a feature resolves a path to a registered repo, cover the git-remote-keyed case explicitly.
- **Indexing terminal/file capture widens the remote blast radius; redact secrets.** Claude Code sessions contain command output and file contents with real credentials, and the remote claude.ai connector can surface that indexed text. `core/ingest/redact.py` masks common secret shapes on the `claude_code` path before storage/embedding, and `raw_content` is not persisted for that source. Any future source that ingests machine/terminal capture (not genuine conversation) must run through redaction too. The `_meta.untrusted_content` injection envelope is source-agnostic, so new sources inherit it for free.

## Persistent memory

There is project memory at `C:\Users\dioni\.claude\projects\d--Dionisio-Memex\memory\`. It contains workflow rules, user context, setup decisions. Read it at the start of each session.
