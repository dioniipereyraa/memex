# Devlog

Short log, reverse chronological. One entry per substantive session.

Format: date, what was done, decisions, blockers, next step.

---

## 2026-05-23 (evening): get_chat defaults lowered + DEVLOG/ROADMAP translated to English

Two unrelated cleanup tasks bundled at the close of the on-demand summaries sub-task, before starting the next Phase 3 item.

**Fix: `get_chat` response size out of Claude Code's token budget.**

During the real smoke test of the lazy summaries flow, calling `get_chat` on a 38-message chat ("Plan de estudio") triggered Claude Code's "result saved to file" fallback (the response was ~66k chars, above the client's ~25-30k token cap). The MCP layer is doing what it should (saving to file when too big), but the UX is brutal: Claude sees a noisy error message and has to read the file in chunks.

Root cause: defaults in `tools.get_chat` were too generous. `messages_limit=20` × `GET_CHAT_MESSAGE_TEXT_MAX_CHARS=3000` + overhead → ~62k chars worst case. Right at the borderline; long chats with verbose messages exceed it.

Fix: lower the defaults. `messages_limit` 20 → 10, per-message cap 3000 → 1500. Worst case now ~17k chars, well below the limit. `GET_CHAT_MESSAGES_LIMIT_MAX=100` unchanged: if Claude needs more, it asks explicitly. Docstring on the MCP wrapper updated so Claude paginates with `messages_offset=10` on long chats (the previous hint was `messages_offset=20`).

Trade-off: each `get_chat` call brings less content. Long chats need more paginations (e.g. the 38-message chat takes 4 calls instead of 2). Mitigated by the lazy summaries: Claude can use the summary to decide whether deep pagination is worth it.

Files touched: `src/memex/transports/tools.py` (constants + docstring), `src/memex/transports/stdio.py` (wrapper docstring), `tests/unit/test_tools.py` (one test renamed `test_default_returns_first_20` → `test_default_returns_first_10` with the new expected values, plus a stale comment updated). README description of `get_chat` synced.

**Docs cleanup: DEVLOG and ROADMAP translated to English.**

Previously they were kept in Spanish on purpose (internal journal). The user decided to align with the rest of the project (README, CHANGELOG, CONTRIBUTING all in English) so external contributors do not hit the language wall when opening the journal. Full translation done, em dashes used as connectors removed throughout (project rule), entry headers reformatted from `## date — title` to `## date: title`. The README note about "internal docs in Spanish" was removed.

CLAUDE.md left in Spanish: not in the scope the user asked to translate. It will get translated whenever it is touched next.

**State at close:**
- 220 unit tests green.
- `ruff check` + `ruff format --check` + `mypy` clean.
- No code logic changed beyond the two constants in `tools.py`; the wire-up of summaries, retrieval, ingest, etc. is identical.

**Next:** Phase 3 sub-task 2, chat ↔ project/repo association.

---

## 2026-05-23 (afternoon): Pivot of the summaries sub-task, ingest-time to on-demand

After the first smoke test on the 74 chats of the official export (which also threw 401 due to an invalid API key and let us validate the silent fail end-to-end), we pivoted the design: summaries are no longer generated at ingest time, but lazily when `search_chats` returns results without a cached summary.

**Why:**
- Latency: 74 chats × ~2s/sequential call = ~2-3 minutes of spinner without visible progress. Unacceptable.
- Cost: 80% of the corpus are chats that probably will never be searched. Generating summaries for all of them spends thousands of dead tokens.
- Live capture: every new Claude.ai chat paid for a summary instantly. Same problem, ghost spending.

**What is new:**
- `tools.search_chats` accepts an optional `Summarizer | None`. If not None, the `_generate_lazy_summaries` helper identifies the top-N conversations without a cached summary, pre-loads the canonical text (`repo.get_conversation_text`) in the main thread, and dispatches up to `SEARCH_SUMMARY_LAZY_CAP=3` parallel calls with `ThreadPoolExecutor`. Each successful summary is persisted with `repo.update_conversation_summary`. Silent fail per chat.
- `stdio.search_chats` resolves the summarizer once per process (cached with a sentinel) via `get_default_summarizer()` and passes it through. That way the `MEMEX_SUMMARY_ENABLED` flag works out-of-the-box in Claude Code sessions.
- Pipeline reverted to NOT generating summaries at ingest. But `conversations.content_hash` is still computed and persisted: the lazy summarizer will use it in future versions to invalidate stale summaries (today the column is used only to preserve cached summaries across re-ingests of the same content).
- New helpers in `repo`: `get_conversation_text(uuid)` reconstructs the canonical message stream (same format the chunker uses); `update_conversation_summary(uuid, text)` patches only the summary without touching the rest of the row.

**Bug found during the test refactor:**
SQLite binds each `Connection` to the thread that created it (`check_same_thread=True` default). The first implementation of the helper called `repo.get_conversation_text(conn, uuid)` inside the `ThreadPoolExecutor` callback and blew up with `ProgrammingError`. Fix: pre-load all texts in the main thread before the pool. The pool only executes the slow part (HTTP call to the LLM). As a bonus, what is being parallelized becomes clearer.

**Minor technical decision:**
Renamed the loop variable `result` to `gen_result` inside the helper because it collided with the `result: list[SearchHit]` built at the end and confused mypy (which ended up seeing `result.append` as an attribute of `str | None`).

**State at pivot close:**
- 220 unit tests green (197 original + 23 new from the summaries module / lazy wire / migration / content_hash).
- `ruff check` + `ruff format --check` + `mypy` clean.
- `MEMEX_SUMMARY_ENABLED=true` with a valid API key activates the lazy flow automatically in any Claude Code session connected to `memex-mcp`.

**What stays in the handoff for the next session:**
- Real smoke with a valid API key from Claude Code: run a `search_chats` from a session and verify that the first 3 chats without a summary get enriched, that subsequent searches return cache hits, that the cost in `console.anthropic.com` matches the expectation (~$0.01/new chat).
- Next Phase 3 sub-task: chat ↔ project/repo association.

---

## 2026-05-23: Phase 3, sub-task 1, auto-generated summaries at ingest (discarded approach)

> **Note:** this approach was discarded the same day. See entry "Pivot of the summaries sub-task: ingest-time to on-demand" above. The infrastructure (module `core/summaries/`, settings, schema migration, `content_hash`) is reused; what changed is WHERE the summarizer is invoked.

Formal kickoff of Phase 3 with the highest-visible-value sub-task: each ingested chat can carry a short summary generated by Claude Haiku. Opt-in (default OFF). Goes into `conversations.summary` and shows up in `list_recent_chats`, `get_chat`, and `memex search`. The idea: Claude Code can dismiss chats without paying tokens to read them.

**Scope decisions (aligned with the user before coding):**
- **Opt-in**. Activate with `MEMEX_SUMMARY_ENABLED=true` + `ANTHROPIC_API_KEY`. Default OFF to avoid burning API on bulk ingest by accident (74+ chats of the official export = real cost).
- **Sync**. Block ingest until the summary is in DB. Simple, no background workers or state machines. Haiku takes 1-3s, acceptable for individual ingest.
- **Silent fail**. If the API fails (no key, rate limit, network), the chat ingest continues: a warning is logged, the chat keeps the summary from the export (or None) and a `summaries_failed` counter increments. Never aborts.
- **Hash-based regen**. Each conversation has a new `content_hash` column (SHA-256 hex of the canonical text). Re-ingesting the same chat does NOT re-call the API if the hash matches and a summary already exists; it does regenerate if the chat changed (new messages via live capture). That saves money on bulk re-ingests of the same export.

**What is new:**
- New `src/memex/core/summaries/`: `base.py` with `Summarizer` ABC and `SummarizerError`, `fake.py` with deterministic `FakeSummarizer` for tests, `anthropic_summarizer.py` with the real client (lazy SDK import, truncates input to 12k chars to bound cost, Spanish prompt focused on "what problem and what decisions"), `__init__.py` with `get_default_summarizer()` returning `None` if the flag is OFF.
- `anthropic>=0.40` added as optional extra `summaries` in pyproject. Install with `uv sync --extra summaries`.
- `conversations.content_hash TEXT` column in schema. New additive migration in `db.py::_apply_additive_migrations` that checks `pragma_table_info` and runs `ALTER TABLE ADD COLUMN` if absent (SQLite has no `IF NOT EXISTS` on ALTER). Old DBs migrate on the first `init_schema`.
- `_ingest_conversation` refactored: now it computes `full_text` and `content_hash` BEFORE inserting the conv (before it computed them after). That allows querying `repo.get_conversation(uuid)` to inspect the old hash and summary and decide whether to regenerate.
- `IngestSummary` gains 3 counters: `summaries_generated`, `summaries_skipped_cached`, `summaries_failed`. CLI `memex ingest` shows them in the table when a summarizer is active. The HTTP ingest response also includes them.
- New settings: `MEMEX_SUMMARY_ENABLED` (bool, default false), `MEMEX_SUMMARY_MODEL` (default `claude-haiku-4-5-20251001`), `MEMEX_SUMMARY_MAX_TOKENS` (default 200), `ANTHROPIC_API_KEY` (reuses the SDK's standard name).
- 20 new unit tests: `tests/unit/test_summaries.py` (14) for FakeSummarizer + factory + AnthropicSummarizer (mocked), and 6 tests in `TestPipelineSummaries` inside `test_pipeline.py` for the wire (opt-out, opt-in happy path, cached re-ingest, regen on content change, silent fail, live capture).

**Minor technical decisions:**
- File `anthropic_summarizer.py` (not `anthropic.py`) to avoid shadowing the SDK package when doing `from anthropic import Anthropic` inside.
- The factory sentinel is `None` (not `NoOpSummarizer`). The pipeline checks `if summarizer is not None` before calling. Simpler and explicit.
- The truncation test uses a unique character (`Ω`) to count the body in the payload, avoiding collisions with the "a"s in the ES template (chat, mensajes, concatenados contribute 4 a's to the template).

**State at close:** 216 unit tests green (197 + 19 new, exact count 216), `ruff check` + `ruff format --check` + `mypy` clean. Full suite run after the pipeline refactor to ensure no regression.

**What was intentionally NOT done:**
- Smoke test with real API, left for the user to try with their key.
- Close audit (project rule applies at PHASE close, not sub-task).

**Next step:** user smoke test with `MEMEX_SUMMARY_ENABLED=true` on a small ingest. If it works, consider the Discord announcement. Then start sub-task 2 of Phase 3: chat ↔ project/repo association (higher real value per chat with the user).

---

## 2026-05-20: Formal close of Phase 2 (audit + format + release 0.0.2)

Phase 2 closed. Live capture + hybrid search functional end-to-end, Windows autostart operational, 197 unit tests green, CI green. Version bumped to `0.0.2`.

**Close audit (sub-agent + manual review):**
- Scope: everything added since the audit on 2026-05-19 (commit `b0c1cf6`). 5 commits, 18 files.
- No blockers to close the phase. Core functionality without critical issues.

**Important fixes applied at close (3):**
- `scripts/_run-server.ps1`: log with mixed encoding. `Out-File -Encoding utf8` for the banner + `*>> $LogFile` for the exe output. PowerShell 5.1 default for `*>>` is UTF-16 LE, so the file got mixed and `Get-Content -Tail` showed intermittent garbage. Fix: `2>&1 | Out-File -Encoding utf8` to keep UTF-8 consistent.
- `chrome-extension/src/popup.js`: `innerHTML` interpolating `e.kind`, `e.detail`, and `fmtAgo()` when rendering recent errors. Data comes from the local server (trusted), but the project rule is defense-in-depth. Fix: replaced with DOM API (`createElement` + `textContent` + append). Bonus: two em dashes used as connectors removed (project rule).
- `CONTRIBUTING.md` said CI runs `ruff format --check` but that step had been removed (the polish DEVLOG entry documented it). Resolved by reactivating the step in CI after applying the format.

**Minor fixes (4):**
- `ROADMAP.md` said "190 unit tests green" when it was already 197. Updated.
- `New-Item -ItemType Directory` inconsistent between the two `.ps1` files: one with `-Force`, the other with a prior `Test-Path`. Unified to `-Force` (idempotent).
- Imprecise comment in `http_ingest.py::_get_conn` about when handlers run and why `check_same_thread=False`. Rewritten to reflect the real Starlette + uvicorn threading model and leave the invariant clear for future background tasks.
- `popup.js:6`: em dash `"—"` as placeholder when there was no timestamp. Changed to `"-"`. Em dash as separator on the error line also removed (changed to `·`).

**Also applied: `ruff format` + reactivate `--check` in CI.**

16 files reformatted. Zero semantic changes (verified: 197 tests still green after the format). The `ruff format --check` step returned to the CI workflow; the visible debt of "the repo does not follow its own formatter" is closed.

**Incidental CI improvements (manual review):**
- `permissions: contents: read` added to the workflow for principle of least privilege.
- `timeout-minutes: 10` on the test job (default was 6 hours, low risk but friction if something hangs).

**Decisions taken with the user:**
- Version bump `0.0.1 → 0.0.2` (conservative; pre-1.0 supports both, the user preferred patch over minor).
- `ruff format` applied at this close, not delayed further. Closes the visible debt while the phase is paused between features.
- Sub-task `design_chats` (live capture of project chats) remains deferred. Does NOT block Phase 2: the main corpus (loose chats + curated memory from the official export) works. Re-noted in ROADMAP as deferred to Phase 3+.

**Phase 2 close criteria met:**
- "Opening a new chat on Claude.ai makes it queryable from Claude Code in less than 1 minute": validated in real browsing with `memex serve` running as Scheduled Task. Observed latency: instant capture, ingest + embedding ~2-5s, immediately queryable.
- Flow robustness: survives VS Code close, Windows reboots, and dual boot (validated by the user).

**Next:** Phase 3 (quality pass). Auto-generated summaries at ingest with cheap Haiku, chat ↔ project/repo association so Claude Code matches the current repo, optional `SessionStart` hook for proactive context injection, `find_related(current_context)` tool. Detail in `ROADMAP.md`.

---

## 2026-05-20: Server autostart on Windows (Phase 5 preview)

Goal: not having to run `uv run memex serve` by hand every time you start a session. Early Windows-only solution; the formal cross-platform version is left for `memex install-service` in Phase 5 of the ROADMAP.

**What is new:**
- `scripts/install-autostart.ps1`: PowerShell script with verbs `-Install` / `-Uninstall` / `-Status`. Registers a Scheduled Task `MemexServe` with "At log on" trigger for the current user, no window, no admin. Re-installing overwrites (idempotent). `-Install` also triggers the task immediately so the server is up in the current session without waiting for the next logon.
- `scripts/_run-server.ps1`: wrapper invoked by the Scheduled Task. Sets the working directory to the repo (parent of the script), resolves `uv` in PATH at run time (not embedded at install, so it survives uv installation changes), and redirects all streams to the log file with append.

**Decisions taken with the user:**
- Standalone script in `scripts/`, NOT a CLI subcommand (`memex autostart`). Justification: it is Windows-only today; the subcommand deserves cross-platform design (Linux systemd / macOS launchd) and that work belongs to Phase 5. The current .ps1 gets cleanly replaced when we get there and serves as a base.
- `-Install` starts the server immediately in addition to registering the task.
- Re-install overwrites without asking (idempotent). Useful when we update the script and want to reapply.
- Log: `%LOCALAPPDATA%\Memex\serve.log`, append, no rotation for now.

**Bug detected and resolved during the initial test:**
First config used `LogonType Interactive` with `-WindowStyle Hidden` in the PowerShell argument. Result: every time the task started a **visible empty CMD window** appeared, and when the user closed VS Code the task died (exit code `STATUS_CONTROL_C_EXIT`, leaving orphan python/uv processes not listening on the port). Root cause: PowerShell creates the window before processing `-WindowStyle Hidden`, and `LogonType Interactive` ties the task to the interactive shell session that triggered it; closing that shell groups the processes into the same VS Code Job Object and kills them in a chain.

Applied fix: `LogonType S4U` (Service for User). The task runs as the user but without an interactive session, no window, independent from the shell that triggers it. No password or admin required; requires the "Log on as a batch job" privilege (granted by default to users on Win10/11). Verified: the task survives VS Code close and any terminal.

**Final task settings:**
- `LogonType S4U`, `RunLevel Limited`. No elevation.
- `RestartCount 3`, `RestartInterval 1 minute`: if the wrapper dies, Task Scheduler restarts it up to 3 times with 1 min wait.
- `ExecutionTimeLimit TimeSpan.Zero`: no execution time limit (default was 3 days).
- `AllowStartIfOnBatteries`, `DontStopIfGoingOnBatteries`, `StartWhenAvailable`, `MultipleInstances IgnoreNew`.
- Action: `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File <wrapper>`. `-File` points to the wrapper to avoid inline nested quote escape-hell.
- Repo detection: `(Get-Item $PSScriptRoot).Parent.FullName` from `scripts/`. If you move the repo, you must re-install (documented in the docstring).

**Another item added in the same session:**
- Live capture of project chats (`design_chats`) noted as an open sub-task in Phase 2. Deferred at phase close to avoid blocking the audit. The parser and the server already distinguish `Source.DESIGN_CHAT`; what is missing is the Chrome ext (regex in `inject.js` + routing in `background.js`) and identifying the real URL claude.ai uses. Documented in ROADMAP.

**Tests:** I did not add tests for the .ps1 (it would be a PowerShell test, out of scope for the pytest suite). Manual testing with `-Status`, `-Install`, and the definitive test "close VS Code and verify the server keeps responding": all passed.

---

## 2026-05-20: Residual tech debt from the audit (serve test + ingest rollback)

Closed 2 of the 3 follow-ups noted in the 2026-05-19 audit. The third (lazy settings) was evaluated and we decided to defer with justification.

**What is new:**
- `tests/unit/test_cli.py::TestServeCommand`: 4 tests for the `memex serve` command mocking `uvicorn.run` and `connect_and_init` with monkeypatch. Cover: defaults (host/port/log_level), custom `--host` and `--port` flags, `--db` injects the conn into `http_ingest._conn` with `check_same_thread=False`, without `--db` the conn stays intact. They take advantage of the fact that `import uvicorn` and `from memex.core.storage.db import connect_and_init` inside `serve()` re-resolve from the module on each invocation.
- `tests/unit/test_pipeline.py::TestPipelineRollback`: 3 tests for the ingest rollback when the embedder fails mid-batch. Cover: (1) `ingest_single_conversation` rolls back and re-raises, leaving no conv or messages in the DB, (2) `ingest_export` reports the error in `summary.errors` without persisting the conv, (3) if one conv fails, the others in the same export are ingested OK (per-conv isolation via try/except in the loop). Local helper `FailingEmbedder` wraps `FakeEmbedder` and fails on specific calls.

**Bug found while writing the test helper:** the first version of the payload only overwrote `text` and kept `content[]` from the original template. The parser uses `content[]` first (`claude_export.py:163-179`) and only falls back to `text`. Result: the chunker received 4-7 chars instead of 20K, produced 1 chunk, 1 batch, 1 embedder call, and `fail_on_call=2` never triggered. Not a production bug; the test helper was wrong. Documented in the docstring of `_long_text_payload`.

**Consciously deferred item:** `settings = get_settings()` evaluated at import time. There is no broken test today that needs it (existing tests use `@patch("memex.X.settings")` with MagicMock, which will keep working independently). The follow-up stays open, waiting for a concrete case that motivates the refactor before investing in it. Reasoned in `handoff.md`.

**Test state:** 197 unit tests green (190 previous + 4 serve + 3 rollback). ruff check on modified files clean. mypy not affected (we did not touch `src/memex/core`, `transports`, or `config.py`).

---

## 2026-05-20: Public repo polish (CI, badges, docs, screenshot, icons)

Block D of the plan we had been pushing since the handoff. Everything in a single commit because it belongs to the same goal: leaving the repo presentable for people arriving from Discord (first favorite on the thread today in the morning, signal that the polish is worth doing now).

**What is new:**
- `.github/workflows/ci.yml`: triggered on push and PR to `main`, ubuntu-latest + Python 3.12 via `astral-sh/setup-uv@v3` with cache enabled. Runs `ruff check`, `mypy` (over `core` + `config.py` + `transports`, same scope the handoff states is clean) and `pytest tests/unit -q`. Skips integration (needs Ollama).
- 3 badges at the top of `README.md`: CI status, License MIT, Python 3.12+.
- Screenshot embedded in `README.md`: the "Session memory check" from Discord at `docs/screenshots/session-memory-check.jpeg`, with caption explaining what is shown (Claude Code recalls a claude.ai chat captured seconds earlier by the Chrome ext, via `list_recent_chats` + `get_chat`).
- `CONTRIBUTING.md`: project scope, local setup, checks that CI runs, condensed code style (no em dashes, no AI footers, imperative in commits, architecture rule `core/` does not import from `transports/` or `cli/`), PR workflow.
- `CHANGELOG.md`: Keep a Changelog format. `[Unreleased]` section with the polish + README translation, and sections per phase (Phase 2 in progress / Phase 1 closed / Phase 0 closed) with real bullets based on previous commits.
- Chrome extension icons: 4 PNG (16/32/48/128) + source SVG in `chrome-extension/icons/`. Design: stylized "M" with two orange dots on the legs, cream background. Iteration: the first version had no padding and looked tight at 16 px; the user sent a second one with the M centered on viewBox 104x104. That one stayed.
- `chrome-extension/manifest.json`: declares `icons` top-level (for the `chrome://extensions` page and the Web Store when applicable) and `default_icon` inside `action` (for the toolbar). Replaces the gray placeholder.

**Minor but noted decision:** the CI workflow originally included `ruff format --check`. Local verification showed that 15 files in the repo are not aligned with the `ruff format` default (the project historically uses `ruff format` but never ran `--check` in CI). I dropped the step instead of dumping a 15-file format-bomb into this same commit. If at some point we decide to align with `ruff format` and keep the check in CI, that goes in its own session.

**Local verification before push:**
- `ruff check src tests`: clean.
- `mypy src/memex/core src/memex/config.py src/memex/transports`: 0 issues in 20 files.
- `pytest tests/unit -q`: 190 passed (1 preexisting warning from fastembed about the updated nomic model on HF, not ours).
- `chrome-extension/manifest.json`: JSON parses OK.
- Em dashes: the only two remaining are legitimate (one in the README snippet the user would copy to their CLAUDE.md, the other in the CONTRIBUTING rule that quotes itself).

**State at close:** repo ready for push. CI should pass green on the first trigger.

---

## 2026-05-20: README in English (whole body)

Translation of the README to English. Until today it was an English intro blockquote + Spanish body. Decision agreed with the user (handoff from previous session): full English README for an international audience, `ROADMAP.md` and `DEVLOG.md` stay in Spanish as the project's internal log. Explicit note in the README pointing that out so an external reader is not surprised when opening those files.

**Incidental changes leveraging the translation:**
- Quickstart step 3: the comment said "takes a while generating embeddings with Ollama". No longer true since `1c90ad6` (fastembed is default). Now it says "downloads the fastembed model on first use".
- ASCII diagram: `local embeddings (fastembed / Ollama)` instead of `local embeddings with Ollama`. Reflects the real state.

**Pending items from the handoff that stay open:**
- Public repo polish (badges, embedded screenshot, Chrome ext icons, CONTRIBUTING.md, CHANGELOG.md).
- Read Discord feedback.
- Tech debt: `memex serve` test, lazy settings, mid-ingest rollback.
- Phase 2 sub-task: real use for a week + close audit.

---

## 2026-05-19: Post-Discord cleanup (audit + Block A)

Full project audit (first exhaustive audit since Phase 1). Sub-agent reviewed code, docs, deps, and quality before going public. Verdict: nothing blocking, but accumulated debt worth closing now that the repo is public.

**Critical fixes (4):**
- `cli/main.py:63` printed "embedder: Ollama" hardcoded even though the default is fastembed. User-visible bug. Now it shows the real backend (`settings.embed_backend`) + the `model_name` of the already-initialized embedder.
- `pyproject.toml` did not declare `starlette` or `uvicorn` as direct deps (they arrived transitively via `fastmcp`). Added; if fastmcp drops them in a future version, `memex serve` does not break.
- `chrome-extension/src/inject.js` used `postMessage("*")` as target; any other script on the claude.ai page world could intercept the chat JSON. Now `window.location.origin` (claude.ai is always same-origin).
- `chrome-extension/manifest.json` without `content_security_policy`. Explicit CSP added for extension pages (`script-src 'self'; connect-src 'self' http://127.0.0.1:5777 http://localhost:5777`).

**Important fixes (5):**
- `stdio.py` no longer leaks `{e}` to the MCP client; now returns `Error interno ({Type})` and the detail stays only in the log. Test updated to verify the raw message does not leak to the client.
- `ollama.py` now explicitly catches `httpx.ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError` before the substring fallback. Less fragile against wording changes.
- `_to_iso` (repo.py) uses explicit `strftime` instead of `replace("+00:00", "Z")`. Robust against non-UTC zones.
- `tools.search_chats(mode="lexical")` returns a clear error if the query sanitizes to empty (it was silent before, returning `[]`).
- `chrome-extension/src/background.js` now retries 3 times with backoff (2s, 8s) on network errors. Covers the case "fastembed downloading the model for the first time" where the server takes 30-60s before responding.

**Dead code removed:**
- `src/memex/core/retrieval/` (empty directory with empty `__init__.py`). If retrieval logic grows, it gets recreated with real content.
- `parse_conversation_dict` is no longer a one-line wrapper over the private one; it was promoted in place (the private `_parse_conversation_dict` was renamed to public and the wrapper deleted).

**Dependencies reorganized:**
- `ollama` moved to `[project.optional-dependencies]` extra `ollama` (no longer default; whoever wants it installs `uv pip install -e .[ollama]`).
- `starlette>=0.40` and `uvicorn>=0.30` added as direct deps.
- `pytest-cov` removed from dev deps (was not used in CI or docs).

**Test gap closed:**
- `tests/unit/test_storage.py::TestHybridSearch::test_hybrid_when_query_sanitizes_to_empty`: covers the case where one of the two engines (text_search) returns `[]` and the RRF still has to deliver the hits from the other (vector_search).

**Docs synced:**
- `CLAUDE.md`: stack now says "fastembed default / Ollama optional". Repo tree reflects `embeddings/fastembed_embedder.py`, `transports/http_ingest.py`, no `retrieval/`. State as of 2026-05-19. Common commands include `memex serve`.
- `ROADMAP.md`: test count updated to 190.

**Pending (not done today, marked for next session via handoff.md):**
- Translate README to English (whole body).
- Public repo polish: badges, embedded screenshot, Chrome ext icons, CONTRIBUTING.md, CHANGELOG.md.
- `memex serve` test (CliRunner mocking uvicorn).
- Live capture sub-task: try it in real use for a week, Phase 2 close criterion.
- `settings` evaluated at import time (Phase 0 follow-up still open).

**Final state:** 190 unit + 7 integration tests green. Ruff and mypy clean. Audit passed without blockers.

---

## 2026-05-19: First public post on the official Anthropic Discord

Posted on the official Anthropic server, in the forums channel (thread `1506428270353060001`). It is the first time Memex leaves the private work repo to an external audience.

Post structure (final, after iterating several versions):
- Hook: "Making Claude remember. Building a fix."
- Problem setup (claude.ai plans, Claude Code executes, they do not share context).
- "Talking to one person" metaphor for the Memex idea.
- Technical section "Under the hood" with stack (MCP + sqlite-vec + FTS5 + RRF).
- 1 week of dogfooding on the own corpus (74 chats / 1024 messages).
- What works today / What is missing.
- One open question: "Does this match a real pain you have, or am I solving a problem only I have?"
- Repo link at the end with the caveat "pre-alpha, runs from source, no installer yet".

Tags chosen: `MCP Server`, `Browser Extension`, `CLI`, `Open Source`, `Utility`.

Single image: screenshot of Claude Code doing an end-to-end "memory check". The user asks "do you remember what we talked about" without mentioning Memex; Claude Code invokes `list_recent_chats` + `get_chat` on its own, finds the chat captured seconds earlier via the Chrome ext, summarizes the content, and even identifies the meta-context ("you were testing the live capture flow"). One image shows the whole system working.

Repo changes associated with the post:
- GitHub description translated to English via `gh repo edit`.
- README with English intro paragraph at the top (commit `c6420e9`). The body stays in Spanish for now.

Pending: read feedback and reactions on the thread when they appear.

---

## 2026-05-19: Zero-config embedder, fastembed default, Ollama optional

**Motivation:** the "feature or bug" question about BYO-Ollama from the Discord post made us notice that Ollama friction is real for casual users. Replacing the embedder with something embedded turns that trade-off into "clearly a feature": local-first stays, but without an external daemon.

**What was done:**
- `pyproject.toml`: added dep `fastembed>=0.4.0` (~30 MB of additional deps: numpy + onnxruntime + tokenizers).
- `config.py`: new setting `embed_backend: "fastembed" | "ollama"` (default `"fastembed"`). `embed_model: str | None`, each backend uses its default if not set.
- `core/embeddings/fastembed_embedder.py`: new `Embedder` implementation. Model is lazy-loaded the first time (import + ONNX download to `~/.cache/fastembed/`). Default `nomic-ai/nomic-embed-text-v1.5-Q` (quantized, 130 MB). Same dim 768. L2 normalizes by default.
- `core/embeddings/ollama.py`: adjusted so `embed_model=None` falls to `DEFAULT_MODEL = "nomic-embed-text"`. No-op for users who already had the setting.
- `core/embeddings/__init__.py`: factory `get_default_embedder()` that returns the configured embedder. Case-insensitive backend, validates.
- Refactor of the 3 call sites (`cli/main.py`, `transports/http_ingest.py`, `transports/stdio.py`) to use the factory instead of hardcoding `OllamaEmbedder()`.
- `tests/unit/test_embedder_factory.py`: 10 tests covering the factory (valid backends, case-insensitive, whitespace, invalid, default, fastembed empty input).
- `.env.example` and `README.md`: new docs with default backend + alternative.

**Known trade-off:** embeddings from Ollama and fastembed for the same "nomic-embed-text" are not bit-exact (Ollama uses GGUF, fastembed uses ONNX, different quantization/tokenizer). The difference is small but when switching backends it is worth re-ingesting so the whole DB has vectors from the same model. Documented in the module docstring.

**State:**
- 189 unit tests green (+10 new from the factory).
- Ruff and mypy clean (21 source files).
- Zero-config default: `uv sync` + `uv run memex serve` and the model downloads itself the first time.

**How it affects the Discord post:**
- Question #2 can now be sharper: "local-first zero-config: feature, or is the typical 'upload my embeddings to your cloud' more comfortable?"
- The bullet "Chrome ext + local HTTP server captures new chats automatically" keeps making sense, but "BYO Ollama" stops being friction and moves to the optional section.

---

## 2026-05-19: Live capture, HTTP backend + Chrome extension

**Context:** complete Phase 2 with live capture. Until now Memex only indexed the official export zip (everything you chat on claude.ai after the export stays out). Live capture closes that gap: every chat you open or create on claude.ai shows up in Memex within seconds, automatic.

**Architecture:** two pieces in this repo (not depending on SyncChat).

```
[Claude.ai] → inject.js (intercepts fetch) → content.js → background.js → POST http://127.0.0.1:5777/ingest/conversation → Memex SQLite
```

**Backend (mini-batch 1):**
- `transports/http_ingest.py`: Starlette app with two endpoints. `GET /health` (ping for the popup), `POST /ingest/conversation` (receives the raw JSON from Claude.ai's API, same shape as `conversations.json`). Origin check restricts to `chrome-extension://` and `moz-extension://`. Shape validation with clear HTTP codes (400 bad payload, 403 origin, 503 Ollama down, 500 unexpected).
- `core/ingest/pipeline.py::ingest_single_conversation()`: refactor to reuse the "ingest one chat" logic from the endpoint. Commit/rollback at the end.
- `core/ingest/claude_export.py::parse_conversation_dict()`: promoted to public (was private). Common piece for the 3 chat sources.
- `core/storage/db.py`: `get_connection` and `connect_and_init` accept `check_same_thread=False`. Necessary because Starlette/uvicorn run handlers in a thread pool. SQLite is thread-safe at the C level; the Python client check is explicitly relaxed.
- CLI `memex serve --host --port --db`: starts uvicorn with the app. Designed to run persistently in a terminal.
- 14 tests with TestClient covering health, origin check, ingest happy path, idempotency, shape validation.

**Chrome extension (mini-batch 2):**
- `chrome-extension/manifest.json` (MV3) with host_permissions limited to `https://claude.ai/*` and `http://127.0.0.1:5777/*`.
- `inject.js`: copy from SyncChat with rename (`syncchat-inject` → `memex-inject`). Monkey-patches `window.fetch`, classifies only `conv-full` and `conv-create`, posts via `window.postMessage`. Keeps scrubbing of sensitive fields (defense in depth).
- `content.js`: 10 lines, bridge from page world to service worker.
- `background.js`: filters only complete chats, POSTs to `http://127.0.0.1:5777/ingest/conversation`. Stats in `chrome.storage.local` for the popup (ingested chats, recent errors, last ingest). Configurable via popup.
- `popup.html` + `popup.js`: server status (green/red chip), counters, recent error list, URL configuration.
- `chrome-extension/README.md`: unpacked load instructions + test flow + privacy.

**Decision: Memex's own Chrome ext, not a SyncChat fork.** The interceptor is ~100 lines, copying it is trivial. Reusing installed SyncChat would force the user to have both products and create coupling we do not need. The background is radically simpler (no WS, no reconnect, no chat storage; the backend is already idempotent).

**Live smoke test (backend with real uvicorn, not TestClient):**
- `memex serve --port 5778 --db /tmp/memex_smoke.db` in background.
- `GET /health` returns 200 OK.
- POST with `Origin: chrome-extension://abc...` returns 200 with `{"status": "ok", "uuid": "smoke-conv-1", "conversations": 1, "messages": 1, "chunks": 1}`.
- POST without Origin returns 403 (origin check works in production, not only in TestClient).
- `memex stats --db /tmp/memex_smoke.db` returns 1 conv, 1 msg, 1 chunk persisted.

**Pending for public use (Phase 5):**
- `memex install-service`: register autostart in the OS (Windows Task Scheduler / launchd / systemd) so the daemon starts on login without the user opening a terminal. Noted in the plan.
- Chrome Web Store publication (review ~5-10 days).

**State:**
- `uv run pytest tests/unit`: 179 passed (same as after mini-batch 1; the Chrome ext does not add Python tests).
- `uv run ruff check`, `uv run mypy`: clean.
- Backend end-to-end validated with real server.
- Chrome ext ready to load as unpacked and test against claude.ai.

**To close Phase 2:** real use of the Chrome ext for a week, smoke test of new chats showing up in `memex search`, and close audit.

---

## 2026-05-19: Proactive tool descriptions + CLAUDE.md recipes

**Context:** the first real MCP test with an ambiguous user message ("did you see I told you about exportal on claude.ai?") showed that the other Claude **did not proactively use** `search_chats`. It answered "I have no record" after reading MEMORY.md (which has no Exportal info) instead of searching in Memex. It offered to search for the user instead of doing it on its own.

This happens because LLMs are conservative with tools by design (prefer asking before acting) AND the tool docstrings described *what they do* without saying *when it is convenient to use them*.

**What was done:**

1. **Docstrings of the 3 MCP tools** rewritten in `stdio.py` with explicit sections "USE PROACTIVELY when:" and "BEFORE answering X, invoke this tool":
   - `search_chats`: trigger on phrases like "remember when...", "did you see that...", "we already talked about...", questions about specific projects/people/decisions, context that seems "lost" between sessions. Explicit "before saying 'I have no record' invoke this".
   - `get_chat`: use after `search_chats`, not to discover, yes to dig in.
   - `list_recent_chats`: chronological browse when there is no keyword. Explicit "do not use to search by topic".

2. **README updated** with section "Make Claude use Memex proactively". Includes ready-to-paste snippet for `~/.claude/CLAUDE.md` (global) or `<project>/CLAUDE.md` (local) with the rule "before answering 'I don't remember', use `mcp__memex__search_chats`".

3. **Docstrings in `tools.py`** left as they are (they are dev-facing, not LLM-facing; do not affect tool behavior through the MCP).

**Why the docstring alone is not enough:** no wording 100% forces Claude to use a tool. It is a balance: more aggressive docstrings raise proactive use frequency but also the risk of misuse. The user can reinforce with instructions in their CLAUDE.md.

**Non-functional changes for tests:** none. Docstrings are metadata for the LLM, the code stays identical. Tests pass equally.

**To take effect:** restart the Claude Code session with Memex mounted (the MCP server starts as a subprocess once; docstrings are exposed at startup).

**State:** 165 unit + 7 integration green. Ruff and mypy clean.

---

## 2026-05-19: Phase 2 sub-task, hybrid search FTS5 + RRF

**Context:** first task of Phase 2 is to solve the "Amarok" case before live capture. The user chose to prioritize retrieval quality over data volume.

**What was done:**
- `schema.sql`: new virtual table `fts_chunks` with FTS5, tokenizer `unicode61 remove_diacritics 2` (matches "amarok" with "Amarók", "AMAROK", etc.). Comments explaining how it stays in sync with `chunks` and `vec_chunks`.
- `repo.add_chunk` and `repo.delete_chunks_for_conversation`: now keep the THREE tables in sync (chunks + vec_chunks + fts_chunks). Same DELETE + INSERT pattern that vec_chunks already had.
- `repo.text_search(conn, query, limit, dedupe_by_conversation)`: BM25 over fts_chunks. Sanitizes the query with `_sanitize_fts_query` (extracts `\w+` words and quotes them to avoid loose FTS5 operators). If the query is malformed, returns an empty list instead of propagating `OperationalError`.
- `repo.hybrid_search(conn, query, query_embedding, limit, ..., rrf_k=60)`: combines `vector_search` + `text_search` with Reciprocal Rank Fusion. Score = Σ 1/(rrf_k+rank). Default k=60 (Cormack 2009). Result: `SearchHit.distance = -rrf_score` to keep "lower = better".
- `repo.rebuild_fts_index(conn)`: maintenance helper. Deletes and repopulates `fts_chunks` from `chunks`. **Commits at the end** (it is a self-contained operation, not part of a long transaction).
- `tools.search_chats`: new `mode: "hybrid" | "semantic" | "lexical"` parameter, default `"hybrid"`. Lexical mode does not call the embedder (skip Ollama).
- `stdio.search_chats` (MCP wrapper): exposes `mode` with a docstring that clarifies when each one is convenient (Claude will use it to decide).
- CLI: `memex search --mode {hybrid|semantic|lexical}` + new command `memex reindex-fts` to populate the index on pre-existing DBs without re-embedding.
- New tests (12): coverage of `text_search` (including dedup, query sanitization with special characters, case-insensitive, rebuild), `hybrid_search` (rescue when only text matches, dedup), and `tools.search_chats` (invalid mode, default hybrid, lexical skip embedder).

**Real bug caught during live validation:**
- `rebuild_fts_index` did not commit. The CLI executed the INSERT, reported "614 chunks indexed", but `conn.close()` rolled back and the index stayed empty. Fix: explicit commit at the end of the function. I documented the why (self-contained operation, different from the rest of the repo's "caller commits" pattern).

**End-to-end validation on the real corpus (614 chunks):**

| Mode | Top-3 for "Amarok" |
|---|---|
| `semantic` (the only one available before) | Exportal (0.84), Probadno random (0.88), Math (0.88). **FAILS**. |
| `lexical` (pure FTS5) | **"Desbloquear radio Amarok 2012 con VCDS" (-8.6)**. ONLY match. |
| `hybrid` (RRF combined) | Exportal (-0.0164), **Amarok (-0.0164)**, Probadno (-0.0159). **FIXED**. |

No regression on previous semantic searches: "Chrome extension to export chats" returns the same top-3 as before (in hybrid, the #1 has almost twice the score of the next ones for adding FTS signal).

**State:**
- `uv run pytest tests/unit`: 165 passed (was 153, +12).
- `uv run ruff check`, `uv run mypy`: clean.
- `memex reindex-fts` functional.
- `memex search "Amarok" --mode hybrid`: returns the correct chat in top-2.

**Pending to close Phase 2:**
- Live capture: adapt SyncChat's Chrome ext to write to the same SQLite. Local ingest endpoint. Idempotency.

---

## 2026-05-18: Close of Phase 1, audit + docs sync

**Close audit (sub-agent):**

No blockers. Verdict: closes as is. Actionable items found:

1. **Dead code**: `except EmbedderError` in `stdio.search_chats` never runs because `tools.search_chats` already catches the exception and returns `{"error": ...}`. Fixed: removed from the wrapper, kept the general `except Exception`. Also removed useless import of `EmbedderError` in `stdio.py`.
2. **Docs out of sync (several)**: fixed.
   - `CLAUDE.md`: `transports/` said `(PENDING, Phase 1)`, now marks `tools.py` and `stdio.py` as DONE. Obsolete comment about `memex-mcp` updated.
   - `README.md`: description of `search_chats` mentioned a phantom parameter `date_range?` (does not exist; the real one is `source`). Removed "under construction in Phase 1" paragraphs. Added `messages_limit`/`messages_offset` to the `get_chat` description.
   - `ROADMAP.md`: outdated test count.
3. **Test gaps**: 2 new added.
   - `test_embedder_error_becomes_json_error`: validates that `tools.search_chats` catches `EmbedderError` and converts it to `{"error": ...}`. Documents the contract that justifies removing the `except` from the stdio wrapper.
   - `test_offset_beyond_total_returns_empty`: validates that `get_chat` with `messages_offset >= total_messages` returns an empty window without crashing, `truncated=False`.

**Follow-ups deferred to Phase 4 (when I build remote MCP):**
- `stdio.py` returns `f"Error interno: {e}"` to the client. Today it is local single-user, low risk. In remote MCP it is better to return a generic message to the client and keep the detail only in the log to avoid leaking paths/queries.
- `OllamaEmbedder` detects connection errors with a substring check (`"connect"`, `"refused"`, etc.). Fragile if `ollama` or `httpx` change the wording (for example in another language). Better: explicit catch of `httpx.ConnectError` / `httpx.TimeoutException` before the substring fallback.

**Final state of Phase 1:**
- 3 MCP tools working in real Claude Code (validated by use, not just by tests).
- 153 unit + 7 integration tests green.
- `uv run ruff check`, `uv run mypy`: clean.
- Audit done, no blockers.
- Docs synced with the real state of the code.

**Phase 1 CLOSED.** Next: Phase 2 (live capture via SyncChat Chrome ext + hybrid search FTS5 + vectors to solve the "Amarok" case).

---

## 2026-05-18: Fix, get_chat exceeded Claude Code max-tokens

**What happened:**
After the first Phase 1 commit, the first real session in Claude Code calling `get_chat` on a 32-message chat (uuid `00ef7e7b-…`, "Exportal Companion extension") failed with `result (107.581 characters) exceeds maximum allowed tokens`. Claude Code diverted the result to a separate file and had to read it in chunks manually. Broken UX for non-trivial chats.

This was exactly the risk the Phase 0 audit had anticipated and that I had left as "I add pagination if it happens". It happened on the first real chat, not on an extreme one of 264 messages.

**What was done:**
1. `get_chat` now accepts `messages_limit` (default 20, max 100) and `messages_offset` (default 0). Lets Claude paginate long chats.
2. `get_chat` always strips `raw_content` (JSON of tool_use/tool_result blocks) from the response. It is ~10-30% of the weight and rarely used by Claude.
3. Each message `text` is truncated to `GET_CHAT_MESSAGE_TEXT_MAX_CHARS=3000` with marker `…[truncated]`. Claude's code dumps blew past the limit on their own.
4. The response includes `total_messages`, `messages_returned`, `truncated: bool`, `messages_offset` so Claude knows if there is more and how to ask for it.
5. `search_chats` now truncates each result's `summary` to `SEARCH_SUMMARY_MAX_CHARS=500`. Some export summaries weighed 2-3k chars and added up in responses of 5 results.
6. Helper `_truncate(s, max_chars)` adds marker `…[truncated]` if it cut.
7. 8 new tests: 6 of pagination in `get_chat`, 1 of raw_content stripped, 1 of summary truncation in `search_chats`.

**Post-fix validation:**
Calling `get_chat` on the same 32-message uuid that broke before:
- Size: 31.6k chars (was 107.5k, **70% reduction**).
- `total_messages: 32`, `messages_returned: 20`, `truncated: true`. Claude can ask for the other 12 with `messages_offset=20`.
- `raw_content` absent in each message.
- Texts intact (none exceeded 3000 chars individually in this chat).

**Tests:**
- 151 unit tests green (was 143, +8).
- Ruff and mypy clean.

---

## 2026-05-18: Phase 1 MVP, MCP server stdio

**What was done:**
- `src/memex/transports/tools.py`: pure implementations of the 3 tools (`search_chats`, `get_chat`, `list_recent_chats`). Take `conn` and `embedder` as parameters, return serializable dicts. No dependency on FastMCP, fully testable.
- `src/memex/transports/stdio.py`: FastMCP server with the 3 tools registered via `@server.tool`. SQLite connection and `OllamaEmbedder` as lazy singletons. `EmbedderError` is caught and returned as `{"error": ...}` JSON. Logging configured to stderr (stdout reserved for JSON-RPC).
- `pyproject.toml`: re-added the script `memex-mcp = "memex.transports.stdio:main"` (was commented out since the Phase 0 audit).
- `tests/unit/test_tools.py`: 17 tests of the pure functions (empty queries, source filter, ordering, errors).
- `tests/unit/test_stdio_server.py`: 6 tests of the MCP server (3 tools registered, call_tool works end-to-end, errors wrapped in JSON).
- `README.md`: added the configuration snippet for Claude Code (`.mcp.json` with absolute cwd).

**Real bug caught by the smoke test:**
- `sqlite3.ProgrammingError`: SQLite objects are thread-bound. FastMCP by default runs sync tools in a thread pool, so our singleton connection failed when used from another thread. Fix: `@server.tool(run_in_thread=False)` on each tool. Tools stay running in the event loop, which is reasonable because they are short I/O. Documented the reason in the module docstring.

**MCP server smoke test (in-process, not via JSON-RPC):**
- The 3 tools stay registered with their descriptions.
- `server.call_tool("list_recent_chats", {"limit": 3})` returns `ToolResult` with `TextContent` containing valid JSON and the real chats from the DB.
- `server.call_tool("get_chat", {"uuid": "does-not-exist"})` returns `{"error": ...}` without crashing.
- `server.call_tool("search_chats", {"query": "  "})` returns an error without consulting Ollama.

**Decisions:**
- Tools return `str` (pretty-printed JSON) instead of dicts. Gives explicit control of the format and avoids FastMCP's automatic serializations that could change.
- Hard limits: `search` max 50 results, `list_recent_chats` max 100. Avoids huge payloads that overload Claude's context.
- `get_chat` does not paginate; returns all messages. If real use shows hundreds-of-messages chats saturating, we add pagination. Over-engineering for now.
- `source` filter in `search_chats` applies in Python after asking the DB for 3x more candidates. Low cost, avoids complicating the SQL.

**State:**
- `uv run pytest tests/unit`: 143 passed (was 137, +6 new from the server).
- `uv run ruff check`, `uv run mypy`: clean.
- `uv run memex-mcp`: starts clean, registers the 3 tools.

**Next step (real Phase 1 close criterion):**
- Connect it to Claude Code via `.mcp.json` and use it in real sessions.
- 5 real sessions with at least one tool invoked, no crashes.
- Close audit once that is met.

---

## 2026-05-18: Close of Phase 0, dedup + audit

**What was done:**
- `repo.vector_search` now accepts `dedupe_by_conversation: bool = True` (default ON). Returns at most one chunk per conversation, the closest one. To get N unique it requests `k = N * 5` from `vec_chunks` and dedupes in Python. Solves the visible UX problem in the validation: previous searches had 2-3 chunks from the same chat occupying top-5 slots.
- 2 new dedup tests, plus 2 direct ones for `delete_chunks_for_conversation`, plus 6 CLI tests with `typer.testing.CliRunner` (invalid paths, empty DB, help). Total: 112 unit tests green.
- Full project audit pre-close (with sub-agent, in `tools/audit-fase0.md` mentally). Verdict: closes without major blockers, only one immediately actionable thing.

**Critical bug caught by the audit:**
- `pyproject.toml` declared the script `memex-mcp = "memex.transports.stdio:main"` but `transports/` only has an empty `__init__.py`. Any `uv run memex-mcp` would blow up with `ModuleNotFoundError`. Removed (commented out) until Phase 1 implements the stdio transport.

**Doc sync done:**
- `CLAUDE.md` described `transports/{tools,stdio,http}.py` and `core/retrieval/` as existing modules; added `(DONE)` / `(PENDING, Phase 1)` annotation per module + note explaining that `vector_search` lives in `storage/repo.py` for initial simplicity.

**Final retrieval validation (7 real searches over the user's corpus):**
| Query | Relevant top-3 | Top-1 distance |
|---|---|---|
| "Chrome extension to export chats" | 3/3 | 0.62 |
| "decision about project architecture" | 1/3 | 0.67 |
| "exportal" | 3/3 | 0.86 |
| "Amarok" | 0/3 (semantic fails with a rare proper noun) | 0.84 |
| "extension" | 3/3 | 0.81 |
| "level set 0" | 3/3 (math, perfect) | 0.72 |
| "clone the project on linux" | 2/3 | 0.75 |

**Passes 6 of 7 (85%)**. Close criterion was 7/10. Closes with margin.

**Known limitation:** purely semantic search fails on rare proper nouns mentioned once (Amarok case). Will be solved in Phase 2 with hybrid search (FTS5 + vectors + RRF).

**Follow-ups noted (from the audit, not urgent):**
- `settings = get_settings()` at `config.py` import would stay stale if tests change env vars post-import. Minor refactor for Phase 1 if needed.
- `pipeline._lookup_msg` is O(M*C) per conversation. Trivial today, could hurt with a 50x larger corpus.
- Streaming `conversations.json` to avoid loading 50+ MB in memory with large historical corpora. Optimization for Phase 3.
- `OllamaEmbedder` does not test the "model not installed" or "service 404" case. Important for Phase 1 (error handling in MCP).
- `vector_search` with dim != 768 would fail with an obscure error. Worth validating at search start.

**Final state of Phase 0:**
- 112 unit tests green, 7 integration tests green (real Ollama).
- `uv run ruff check`, `uv run mypy`: clean.
- Functional CLI: `ingest`, `search`, `stats`.
- Corpus indexed: 74 conversations, 1024 messages, 614 chunks.
- Retrieval validated on real data with reasonable quality.

**Phase 0 CLOSED.** Next: Phase 1 (stdio MCP server for Claude Code).

---

## 2026-05-18: End-to-end pipeline + functional CLI

**What was done:**
- `core/ingest/pipeline.py`: complete orchestrator. Takes zip + DB + Embedder, does parse, render, chunk, embed, store. Order: projects, design_chats, conversations, memories. Transaction per conversation (one error does not break the rest). Idempotent via upserts + `delete_chunks_for_conversation` before re-chunking.
- `cli/main.py` with `typer` + `rich`: commands `memex ingest <zip>`, `memex search "<query>" [-n N]`, `memex stats`. Tables and output with colors.
- `repo.delete_chunks_for_conversation()`: helper to clear old chunks + their vectors before re-ingest.
- 6 new unit tests (end-to-end pipeline, idempotency, FK orphan handling, etc.). Total: 103 unit tests green.
- `tests/integration/test_full_flow.py`: integration test that parses the real export with OllamaEmbedder.

**Real bug caught by smoke test on the full corpus:**
- The 7 design_chats point to `project_uuid`s that are NOT in `projects/*.json` of the export (the user has projects that were not exported). They failed with FK violation and got ingested with `errores=7`. Fix: if the referenced `project_uuid` does not exist, it is set to `None` before insert (benign orphanhood). Test added to avoid regression.

**Smoke test against the real export (1.71 MB, embedding generation ~1-2 min):**
- 2 projects, 74 conversations (66 loose + 7 design_chats + 1 curated memory), 1024 messages, 614 indexed chunks, 147 empty messages skipped, **0 errors**.
- `memex search "Chrome extension to export chats"` returns top-3 with distances 0.67-0.69, exactly the three user's conversations about Exportal (their other Chrome ext project). Retrieval works.
- `memex search "decision about tech stack python or rust"` returns higher distance (0.88), less precise results (vaguer query).
- `memex stats`: shows distribution by source (conversations=66, design_chat=7, memory=1).

**State:**
- `uv run pytest tests/unit`: 103 passed.
- `uv run ruff check`, `uv run mypy`: clean.
- CLI functional end-to-end with real data.

**Phase 0 ready to close.** Remaining: formal evaluation (10 representative searches) and, if results are satisfactory, phase-close audit and then Phase 1 (MCP server).

---

## 2026-05-18: Embeddings module, interface + Ollama

**What was done:**
- `core/embeddings/base.py`: `Embedder` ABC with `dim`, `model_name`, `embed(texts)` and helper `embed_one(text)`. Public function `l2_normalize` so any implementation can return unit vectors (aligns L2 with cosine in sqlite-vec).
- `core/embeddings/fake.py`: deterministic `FakeEmbedder` for tests. Hashes text with SHA-256, decomposes it into int32 normalized to [-1, 1], applies L2 normalize. Same text returns same vector. Useful for pipeline tests without touching Ollama.
- `core/embeddings/ollama.py`: `OllamaEmbedder` using the official `ollama` 0.6.2 client. Reads `model` and `host` from settings. Detects real `dim` on first embed. L2 normalizes by default.
- `tests/unit/test_embeddings.py`: 15 tests (l2_normalize + FakeEmbedder).
- `tests/integration/test_ollama_embedder.py`: 7 tests that talk to real Ollama. Automatic skip if the service does not respond on `OLLAMA_HOST`. Includes semantic sanity test: "brown labrador dog" must be closer to "chocolate labrador playing" than to "advanced math formulas".

**Results:**
- 97 unit tests green (was 82, +15).
- 7 integration tests green with Ollama running locally.
- Semantic sanity passes: similarity ranking reflects real text affinity.
- `nomic-embed-text` confirms dim=768, normalizable embeddings, deterministic.

**Implementation decisions:**
- `FakeEmbedder` in `core/embeddings/fake.py` (not in `tests/`) so it is available if someone wants to use it without Ollama in their own code.
- Integration tests marked with `pytestmark = [integration, skipif(not _ollama_available())]`. Does `urllib.request.urlopen(f"{host}/api/tags")` at collection; skips cleanly if Ollama does not respond.
- Default normalize=True. If future implementations use a model that already returns unit vectors, they can disable.
- Dim is read at the first real embed, not hardcoded (besides the settings fallback).

**State:**
- `uv run pytest tests/unit`: 97 passed.
- `uv run pytest tests/integration`: 7 passed.
- `uv run ruff check`: clean. `uv run mypy`: clean.

**Next step:**
- End-to-end orchestrator: `core/ingest/pipeline.py` that takes the zip path and does parse, chunk, embed, store. CLI with typer: `memex ingest <zip>`, `memex search "<query>"`, `memex stats`. After: 10 real searches over the corpus, Phase 0 close criterion.

---

## 2026-05-18: Ingest module, renderer, chunker, parsers

**What was done:**
- `core/ingest/content_renderer.py`: converts `content[]` (with `text`, `tool_use`, `tool_result` blocks) to plain text. Tool blocks go as markers (`[tool_use: <name>] <input>`, `[result] <text>`, `[result error] ...`). Truncated to `MAX_TOOL_INPUT_CHARS=500` and `MAX_TOOL_RESULT_CHARS=1000`. Unknown blocks are ignored (leaves the door open to new types).
- `core/ingest/chunker.py`: char-based with configurable `chars_per_token` factor (default 4). Returns `list[ChunkSpan]` with `(text, char_start, char_end)`. `text[char_start:char_end] == text` always. Parameter validation with `ValueError`.
- `core/ingest/claude_export.py`: 4 parsers (`parse_project`, `parse_conversations_list`, `parse_design_chat`, `parse_memories`). Private helpers unify `conversations.json` and `design_chats/*.json`. Curated memory is synthesized as a conversation with `uuid='memory-<account_uuid>'` (idempotent across re-ingests).
- 53 new unit tests (21 renderer, 13 chunker, 19 export), 82 totals green.

**Bug caught by smoke test on the real export:**
- Some messages in `design_chats/*.json` do not bring `updated_at`. It was `KeyError`. Fallback to `created_at`. Test added to avoid regression.

**Smoke test on the real corpus (without printing content):**
- 2 projects parsed (1 empty starter, 1 with 819-char `prompt_template`).
- 66 loose conversations with 900 messages, 58 with tool_use rendered with markers.
- 7 design_chats with 123 messages, all correctly linked to their project (project_uuid present).
- Curated memory parsed (3634 chars) with stable synthetic uuid.
- Total ingestable: 74 conversations, 1024 messages.

**Implementation decisions:**
- Char-based chunking, not token-based. Simpler, no tokenizer dependency, configurable via `chars_per_token`. If retrieval results are poor in Phase 0 we switch.
- Renderer ignores unknown `type` blocks instead of failing. Robust against future export changes.
- Unknown sender falls back to HUMAN (defensive).
- `parse_memories` receives optional `now` for deterministic tests; in prod uses `datetime.now(UTC)`.

**State:**
- `uv run pytest tests/unit`: 82 passed.
- `uv run ruff check src tests scripts`: clean.
- `uv run mypy src/memex/core src/memex/config.py`: clean.
- Smoke test on the real export: everything parsed without errors.

**Next step:**
- Embeddings module: Ollama client + `Embedder` interface. After that, the end-to-end orchestrator (parse, chunk, embed, store) + CLI.

---

## 2026-05-18: Storage layer, models, schema, db, repo

**What was done:**
- `src/memex/config.py` with `pydantic-settings`. Reads `.env` and env vars. Alias per env var (OLLAMA_HOST, MEMEX_EMBED_MODEL, MEMEX_DB_PATH, MEMEX_CHUNK_SIZE, etc.). Range validation on chunk_size and chunk_overlap.
- `src/memex/core/models.py` with pydantic v2: `Project`, `Conversation` (with `source` enum), `Message` (with `raw_content` and flags), `Chunk`, `SearchHit`. Enums `Source` and `Sender` as `StrEnum`. `extra="forbid"` so an unexpected field fails early.
- `src/memex/core/storage/schema.sql`: 4 STRICT tables (`projects`, `conversations`, `messages`, `chunks`) + virtual table `vec_chunks` (sqlite-vec) + `schema_meta` for versioning. FKs with `ON DELETE CASCADE` on messages/chunks per conversation, `ON DELETE SET NULL` on project_uuid and message_uuid. CHECK constraints on `source` and `sender`. Indexes on updated_at, project_uuid, conversation_uuid and created_at.
- `src/memex/core/storage/db.py`: `get_connection()` loads sqlite-vec, sets `foreign_keys=ON`, `journal_mode=WAL` and `synchronous=NORMAL`. `init_schema()` idempotent. `connect_and_init()` as shortcut.
- `src/memex/core/storage/repo.py`: functional CRUD (no classes) with upserts (`ON CONFLICT DO UPDATE`). `add_chunk()` inserts the chunk and its embedding atomically (same rowid in chunks.id and vec_chunks.rowid). `vector_search()` runs KNN join with `MATCH ? AND k = ?`.
- Tests: `tests/conftest.py` with fixtures (in-memory db, project, conversation, messages, chunks). `tests/unit/test_models.py` (11 tests) and `tests/unit/test_storage.py` (17 tests). 28 tests pass.

**Bugs found and fixed along the way:**
- vec0 KNN does not accept parametrized `LIMIT ?` when there are JOINs. You have to use `k = ?` in the WHERE. The `vector_search` query already reflects it.
- The `chunk` fixture had `message_uuid` pointing to a message the test was not inserting. Separated into two fixtures (`chunk` without message, `chunk_with_message` with). Added test that proves the FK rejects orphans.
- Ruff flagged `class X(str, Enum)` (legacy) and `timezone.utc` (legacy in 3.11+). Migrated to `StrEnum` and `datetime.UTC`.

**Implementation decisions:**
- Repo as functions, not classes. Simpler, no state to maintain, no complicated DI.
- Datetime serialized with `Z` suffix (not `+00:00`) to keep compatibility with the official Claude.ai export format.
- `raw_content` saved as JSON in TEXT. The repo deserializes on read. Allows analyzing tool blocks later without re-parsing the export.
- L2 as distance metric. nomic-embed-text returns normalizable embeddings, so L2 ranking matches cosine ranking.

**State:**
- `uv run pytest tests/unit`: 28 passed.
- `uv run ruff check src tests scripts`: clean.
- `uv run mypy src/memex/core src/memex/config.py`: clean.

**Next step:**
- Schema and models closed, `main` bottleneck is lifted. Now we can open the three parallel worktrees:
  - `feature/ingest`: parser of `conversations.json`, `design_chats/*.json`, `memories.json`, `projects/*.json` + chunker + content renderer (tool markers).
  - `feature/embeddings`: Ollama client + `Embedder` interface.
  - `feature/retrieval-cli`: search tool (wraps `repo.vector_search`) + CLI with `typer`.

---

## 2026-05-18: Official export inspection

**What was done:**
- Script `scripts/inspect_export.py` that opens the zip without extracting it, walks the JSON files, and reports schema and statistics. Read-only, does not leak content (text is redacted as `<str:N chars>`).
- Full inspection of the real export (1.71 MB).

**Findings (on the real export, content redacted):**
- 12 files in the zip: `users.json`, `memories.json`, 2 `projects/*.json`, 7 `design_chats/*.json`, `conversations.json` (5.9 MB).
- Total indexable: 73 chats (66 loose + 7 inside projects), 900 messages.
- Message schema: `uuid`, `text` (legacy), `content[]` (new), `sender` (human/assistant), `created_at`, `updated_at`, `attachments`, `files`, `parent_message_uuid`.
- `content[].type`: `text` (1015), `tool_use` (246), `tool_result` (245). Tool blocks come with integration metadata (Slack, GitHub, MCP servers).
- `text` and `content[].text` coexist in 876 of 900 messages. Mean difference 19 chars (probably block separators). We take `content` as canonical.
- No forks/branches: each message has exactly one parent. `parent_message_uuid` stays in the model in case future exports bring trees.
- `summary` is populated in each conversation (mean 1067 chars). Auto-generated by Claude.ai, free. Anticipates Phase 3.
- Median message: 223 chars (~55 tokens). Median chat: 3138 chars (~785 tokens). Max chat: 132k chars.
- `memories.json` brings the Anthropic curated memory (3634 chars in `conversations_memory`). The handoff said it was isolated in Claude.ai; with the official export we have it on disk.

**Decisions (taken with the user):**
- Index `memories.json` as a synthetic conversation with `source='memory'`. Enters the same pipeline.
- Tool blocks render as plain text with markers (`[tool_use: <name>] <input>`, `[result] <text>`). Preserves context without complex parsing.
- Separate `projects` table with FK from `conversations`. Allows retrieving `prompt_template` and future tools like `list_projects()`.
- Chunking: ~500 tokens with overlap, per conversation (the original plan confirmed after seeing the real chat size).

**Base schema (four tables + virtual vec):**
- `projects` (uuid, name, prompt_template, creator, timestamps)
- `conversations` (uuid, title, summary, source, project_uuid FK, account_uuid, timestamps)
- `messages` (uuid, conversation_uuid, parent_uuid, sender, text, raw_content JSON, has_tool_use, has_attachments, timestamps)
- `chunks` (id, conversation_uuid, message_uuid, sender, text, char_start, char_end, created_at)
- `vec_chunks` (chunk_id, embedding FLOAT[768]) sqlite-vec virtual table

**Next step:**
- Implement `core/models.py` with pydantic (Project, Conversation, Message, Chunk, SearchResult) and `core/storage/schema.sql` with the tables and indexes. That is the bottleneck on which ingest, embeddings, and retrieval depend; it stays on `main`. After, open the three parallel worktrees.

---

## 2026-05-18: Repo kickoff

**What was done:**
- Spin-off of SyncChat. New repo at `d:\Dionisio\Memex`.
- Reading the handoff doc (excluded from the public repo via `.gitignore`).
- Full plan approved: structure, stack, phases, close criteria.
- Base setup: `.gitignore`, `pyproject.toml`, `.env.example`, `.python-version`, folder structure (`src/memex/{core,transports,cli}`, `tests/{unit,integration}`, `data/exports`, `scripts`).
- README, ROADMAP, and this DEVLOG written with a practical and concise tone.
- Move of the official Claude.ai export to `data/exports/` (gitignored).
- Git initialization and clean initial history.

**Decisions this session:**
- Stack: Python 3.13 (3.12+ supported), uv as package manager, FastMCP, sqlite-vec, local Ollama with `nomic-embed-text`.
- Public repo from day 1, name `memex`.
- Multi-Claude via git worktrees (not just branches). Phase 0 division: schema on main first, then three parallel worktrees (ingest, embeddings, retrieval+cli).
- Full bug, dead code, and vulnerability audit at the close of each phase.

**Blockers / notes:**
- Ollama installed but not yet verified via CLI (PATH not refreshed, no rush until we start writing the embedder).

**Next step:**
- Phase 0, first task: inspect the Claude.ai JSON export to define schema and data model based on real data.
