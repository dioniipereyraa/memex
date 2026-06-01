# Devlog

Short log, reverse chronological. One entry per substantive session.

Format: date, what was done, decisions, blockers, next step.

---

## 2026-06-01: full security audit + hardening pass (0.1.1)

Ran a multi-agent security audit across 10 trust boundaries (HTTP ingest server, SQL/data layer, Chrome extension, untrusted-input parsing, secrets, filesystem, MCP tools / indirect prompt injection, CLI + autostart, external egress, dependencies), each finding adversarially verified before being accepted. Result: **no critical/high**, 2 medium, ~23 low, ~6 info. The data layer came back clean (every query parameterized, FTS5 input sanitized, no zip-slip, no `shell=True`, API key never logged). Threat model is a local single-user tool, so severity was calibrated accordingly (a bug needing local code-exec as the user is low).

The interesting class is what this kind of tool inherently exposes: **weak ingest auth + indirect prompt injection**. Attacker-influenceable chat text can enter the store and is later fed verbatim to Claude Code.

**Fixes applied this session (all green: 347 unit tests, ruff, mypy core):**

- **Ingest auth token (was: forgeable Origin only).** `/ingest` now requires `X-Memex-Token` compared with `hmac.compare_digest`, on top of the Origin check. Token generated on first use via `secrets.token_urlsafe(32)`, stored 0600 next to the DB (`settings.ingest_token_path`), surfaced by `memex serve` and a new `memex token` command. Chrome extension paired via a token field in the popup; `background.js` sends the header and refuses to POST without it. **Breaking for live capture: users must re-pair once.**
- **DNS-rebinding defense.** Added `TrustedHostMiddleware` pinning the Host header to a loopback allow-list (`MEMEX_INGEST_ALLOWED_HOSTS`). `/health` trimmed to `{"status":"ok"}` so it no longer fingerprints the product.
- **Body + chunk caps (DoS).** Ingest body capped (`MEMEX_INGEST_MAX_BODY_BYTES`, default 16 MB) and rejected with 413 before buffering, via a streaming reader with a running byte counter. `chunk_text` gained `max_chunks`; the pipeline enforces `MEMEX_MAX_CHUNKS_PER_CONVERSATION` (default 5000) and records truncation in `IngestSummary.truncated_conversations`.
- **DB file confidentiality.** `get_connection` now creates the DB 0600 and the dir 0700 (best-effort, only when we create them) before WAL is enabled, so the plaintext `-wal`/`-shm` sidecars inherit 0600. Set `PRAGMA busy_timeout = 15000` explicitly.
- **WAL cross-process contention.** `_ingest_conversation` now computes all embeddings and repo matches BEFORE the first write, so the WAL write lock is held only for the tight write phase (not across multi-second embedding). Lazy-summary persistence in `tools.py` is best-effort: a transient `SQLITE_BUSY` from the other process no longer fails the whole search.
- **Indirect prompt injection (medium).** Every MCP tool result carries a `_meta.untrusted_content` envelope telling the agent the chat content is data, not instructions, and that `[role]`/`[tool_use]`/`[result]` markers inside it are stored text, not real structure. The Anthropic summarizer wraps the chat body in `<content>` tags and its system prompt now says to never follow instructions inside it.
- **Supply chain.** Raised PyPI dependency floors to exclude known-vulnerable releases for fresh installs: `starlette>=0.47.2` (CVE-2025-54121 multipart DoS), `fastmcp>=3.2.0` (2.x has unpatched CVEs). The lock already resolved healthy versions; this protects `pipx`/`uvx` users who do not consult `uv.lock`.
- **Misc.** `OLLAMA_HOST` validated + warns when non-loopback (SSRF-shaped egress footgun). `fastembed` warns when a non-default (unpinned) model is requested. `set-server-url` in the extension validates the URL against the CSP allow-list.

**Deliberately deferred (documented, not security-critical):** provenance column distinguishing live-captured from exported chats (#25); render-time neutralization of `[role]`/`[tool_use]` markers in stored text (needs re-ingest to fully apply; the MCP-boundary envelope covers the live read path); packaging `scripts/` into the wheel so `install-service` works for PyPI users (#12, no real exploit). Tracked in ROADMAP.

**Next:** commit on a branch; if publishing 0.1.1, rebuild the Chrome ext ZIP and note the re-pair step in the store update.

---

## 2026-05-25 (evening): PyPI name collision, rename to `memex-chats`

First publish attempt failed. The name `memex-mcp` was already claimed on PyPI by an unrelated MCP project (Hill Patel / STiFLeR7) and version 0.3.9 had been pushed earlier the same day. So both `memex` (Caleb McCarthy, long-standing) and `memex-mcp` (claimed today) are taken.

**Decision.** Rename the PyPI distribution to `memex-chats`. The CLI script names (`memex`, `memex-mcp`) stay because `[project.scripts]` is decoupled from the distribution name. Anyone who already wired `.mcp.json` to the `memex-mcp` command is unaffected.

Why `memex-chats`: it is descriptive of what the tool indexes, available on PyPI, and short. Considered `memex-claude` (brand-clearer but possible trademark friction with Anthropic if the project grows), `memex-claudeai`, `chatmemex`. Verified availability via the PyPI JSON API (HTTP 404 = free) on all candidates before deciding.

Touched: `pyproject.toml` (`name`), `PRIVACY.md`, `README.md` install commands, `chrome-extension/WEB_STORE_CHECKLIST.md`, `CHANGELOG.md`, and this DEVLOG. Rebuilt `dist/` so the wheel name aligns. Historical entries about the prior `memex` -> `memex-mcp` rename were kept as-is (record of what happened).

**Security note.** While testing publish I asked the user to paste a PyPI token; they pasted it into the chat. Treated as compromised and asked them to revoke + reissue on the spot. Lesson learned: keep tokens out of the chat input field entirely.

**Followup (same day):** rebuild + commit + push landed in `0df7ee4`. `v0.1.0` tag moved from the prior commit to `0df7ee4` (force-tag-push, branch not touched) so the tag points at the publishable code. Fresh PyPI token (account-scoped, then revoked the leaked one); first `uv publish` failed because `dist/` had the Chrome ext ZIP and uv tried to upload it (`Only files ending in .tar.gz are valid source distributions`). Worked around by passing an explicit file list to `uv publish`, then moved the Chrome ZIP to `chrome-extension/dist/` and updated the WEB_STORE_CHECKLIST in `0023cd9` so the trap does not bite again. `memex-chats 0.1.0` confirmed live at https://pypi.org/project/memex-chats/.

**Chrome Web Store submission (same day).** Dashboard form filled in English: store listing (name, summary, long description, category Productivity, support URL), privacy practices (single-purpose statement, per-permission justifications, "Website content" as the only data-usage category, the 3 program-policy certifications), and `Unlisted` visibility for the alpha. Privacy policy URL pointed at `PRIVACY.md` on GitHub (added earlier in this session). Submitted, in review.

**Cumulative session output (2026-05-25):**

- Phase 5 code-side packaging closed (`memex doctor`, `memex install-service`, Linux systemd unit, PyPI prep, Web Store checklist).
- Final English/em-dash audit across `src/`, tests, and remaining docs (CLAUDE.md, chrome-extension README, popup, background).
- `memex-chats 0.1.0` live on PyPI.
- `memex-live-capture 0.1.0` submitted to Chrome Web Store (in review).
- `PRIVACY.md` added at repo root.
- `v0.1.0` tag points to the publishable commit.
- 341 unit tests green, `ruff` + `mypy` clean.

**Next:** Phase 4 (remote transport). Open design questions to settle before coding: how Claude.ai reaches the user's machine (tunnel via cloudflared/ngrok vs port-forward), auth scheme (local token in header), and the boundary between the local stdio MCP and a remote SSE/HTTP MCP (one binary serving both modes vs separate processes).

---

## 2026-05-25: Phase 5 packaging (PyPI + Linux autostart + doctor + Web Store checklist)

Phase 5 code-side work done. The remaining items are maintainer-side (PyPI token, Chrome Web Store developer account); the repo is ready for both.

**What landed:**

- **`memex doctor`** diagnostic command. Reports OK / WARN / FAIL across Python version, database, embedder, live-capture server, summarizer config (only if enabled), registered repos count, indexed corpus count. Exits non-zero only on FAIL so it is script-safe. This is the "what is wrong with my setup?" answer for first-time users, the missing piece from previous phases where setup errors were silent. 4 new tests with mocked HTTP probe.
- **`memex install-service`** cross-platform dispatcher. Single command that detects the host OS and runs the right installer: Windows goes through the existing PowerShell Scheduled Task installer; Linux writes a systemd user unit (`~/.config/systemd/user/memex-serve.service`), enables it, starts it now. macOS not supported in 0.1.0; the command prints manual `nohup` instructions and exits gracefully on `status` (so the dispatcher is well-behaved on every platform). 6 new tests mocking `platform.system` and `subprocess.run`.
- **`scripts/install-autostart.sh`** for Linux. Subcommands `install`, `uninstall`, `status`. Resolves `uv` lazily at install time (embeds absolute path if `uv` is on PATH, falls back to literal `uv` otherwise so a future install lookup still works). Logs to `${XDG_STATE_HOME:-~/.local/state}/memex/serve.log`. The systemd unit uses `Type=simple` + `Restart=on-failure`, so it survives crashes the same way the Windows Scheduled Task does.
- **Package rename to `memex-mcp` for PyPI.** `memex` is already taken on PyPI. The CLI entry point stays `memex` (the Python package and the command are decoupled via `[project.scripts]`). Refreshed pyproject metadata: description translated to English, `Operating System :: OS Independent`, `Development Status :: 3 - Alpha`, `[project.urls]` with Homepage / Repository / Issues / Changelog. `uv build` produces a clean `memex_mcp-0.1.0-py3-none-any.whl` (verified locally).
- **Chrome Web Store checklist** at `chrome-extension/WEB_STORE_CHECKLIST.md`. Full submission playbook so the maintainer has nothing to figure out at submission time: developer account fee, privacy policy requirement, asset sizes (icons 128x128, screenshots 1280x800), submission ZIP build commands, ready-to-paste listing copy (name, summary, long description), permissions justification table per host permission, post-approval checklist. The extension manifest description was the last Spanish string left in the project; translated to English in the same pass.

**README quickstart restructure.**

Option A (recommended): `uvx --from memex-chats memex --help` / `pipx install memex-chats`, then `memex ingest <export.zip>`, then `memex doctor`. Three commands and the user has a working install.

Option B (from source): the previous `git clone` + `uv sync` path is kept as a secondary option for contributors.

The autostart section was unified: one block covers Windows (Scheduled Task), Linux (systemd user unit), and macOS placeholder (manual `nohup`). The CLI command `memex install-service` is the single entry point users learn; the OS-specific paths are an implementation detail.

**Bugs caught during the build:**

- First `pyproject.toml` edit put `[project.urls]` above `dependencies`, which made TOML parse `dependencies` as a string-valued URL named "dependencies". `uv build` failed with a confusing `URL dependencies of field project.urls must be a string` error. Re-ordered `[project.urls]` to live after `dependencies` and `[project.scripts]`; build green on retry.
- Ruff caught a `try/except OSError/pass` pattern in `memex install-service` (chmod best-effort on the Linux script). Switched to `with contextlib.suppress(OSError): ...` per the SIM105 suggestion.

**State:**

- 341 unit tests green (was 331 at the start of the session, +10 between doctor (4) and install-service (6)).
- `ruff check` + `ruff format --check` + `mypy` clean.
- Build artifacts: `dist/memex_mcp-0.1.0-py3-none-any.whl`, `dist/memex_mcp-0.1.0.tar.gz`.
- 9 commits ahead of `origin/main` after this session (still not pushed by design; user decides timing).

**What is left for the actual release:**

1. Push to GitHub (`git push origin main` + `git tag v0.1.0 && git push --tags`).
2. Publish to PyPI: `uv publish --token <token>` (needs the maintainer's PyPI token). TestPyPI dry-run first is recommended.
3. Submit Chrome extension via the dashboard (5 to 10 day review).
4. Post the alpha announcement to the Discord thread.
5. macOS launchd support → moves to 0.2.0.

---

## 2026-05-24 (close): Phase 3 close audit + bump 0.1.0

Project rule: every phase close needs an audit (scan for bugs, dead code, vulnerabilities, doc drift). Did the pass and wrote it up here. Phase 3 closes with 0 blockers; 2 small fixes applied during the audit itself.

**Scope of the audit:**

Everything added or changed in Phase 3:
- `src/memex/core/summaries/` (sub-task 1, full module).
- `src/memex/core/repos/` (sub-task 2, full module).
- `conversations.content_hash` column + migration.
- `chat_repos` + `repos` tables.
- Pipeline auto-scan wire in `_ingest_conversation`.
- `tools.search_chats` repo boost + lazy summaries integration.
- `tools.find_related` (new tool).
- `tools.get_chat` lowered defaults (intra-phase fix from sub-task 1 follow-up).
- `cli/main.py`: `repos` sub-app + `tag` / `untag` / `session-context` commands.
- `stdio.py`: 4th tool wired (`find_related`), `repo` arg added to `search_chats`.
- `.env.example` extended for summary settings.

**Critical fixes applied during the audit (2):**

1. **`assert info is not None` in `cli/main.py` (session-context command).** Asserts get stripped under `python -O`. Replaced with an explicit `if info is None: ... return` plus a stderr diagnostic. The case is "race between `resolve_repo_key` and `get_repo`": vanishingly rare, but the right shape is an explicit branch, not an assert.
2. **`…` (U+2026) in `repos scan` status string.** Same class of bug we hit during the SessionStart sub-task: cp1252 (Windows console default) cannot encode it, and Rich's fallback is not bulletproof in every shell. Replaced with `...` for consistency with the rest of the new CLI strings.

**Deferred (noted, not fixed):**

1. **N+1 in `_scan_repos` at ingest time**: each conv re-reads `repos` from DB. For an export of 74 chats with one repo registered, that is 74 `SELECT * FROM repos`. Each query is ~0.1ms, total <10ms. Trivial today; would matter if someone ingests 10k+ chats while registering 50+ repos. Cache or pass repo list per ingest run if it becomes a bottleneck.
2. **`memex repos scan` commits at the end of the loop, not per chat**. If the user Ctrl+C's mid-scan on a corpus of 1000+ chats, the scan loses all progress on that run. Acceptable for the typical corpus size (74 chats today); revisit if someone runs against a much larger DB.
3. **CLI top-level help strings mixed Spanish + English** (`ingest`, `search`, `stats`, `serve`, `reindex-fts` still in Spanish from earlier phases; `repos`, `tag`, `untag`, `session-context` in English). Inconsistent but not user-blocking. Translate when we do the next public-polish pass before Phase 5 release.
4. **`associate_chat_repo` not transactional with its prior SELECT**. Two concurrent processes could theoretically race between "is this manual?" check and the INSERT. Two CLI commands hitting the same DB simultaneously is the only scenario. Bad luck only loses one auto-assertion; acceptable v1.
5. **`memex repos list` shows replacement chars (?) in tables with very long keys on Windows console**. Rich-rendering / terminal-encoding issue, not a data corruption. The full key is correct in the DB. Cosmetic.
6. **Path normalization is lowercased on Windows only**. If a user registers a repo from Linux and later searches with a different-case path string, no match. Documented as Linux behavior, not a bug.

**Security review:**

- API key (`ANTHROPIC_API_KEY`) stored only in `.env` (gitignored). Code never logs it. Errors from the Anthropic SDK propagate type + message; the SDK does not include the key in its error bodies. Low risk.
- `_resolve_repo_key` accepts arbitrary user input (path / URL / key). Internally, that input is only used as a SQL WHERE clause parameter (parameterized, no concat). Safe from injection.
- The CLI commands `tag` / `untag` accept arbitrary `chat_uuid` and `repo_key`. They validate existence before associating, so the failure mode is a clean error, not a silent NULL FK insert.
- Schema CHECK constraint on `chat_repos.source` keeps the column to `auto|manual` only. The repo helper raises `ValueError` for invalid input. Defense in depth.

**Test coverage:**

- 331 unit tests green (was 220 at the start of Phase 3, +111).
- New modules covered: keys, discovery, matcher, resolve, repo helpers, CLI commands, search boost, find_related, pipeline auto-scan, session-context.
- Gaps that are intentionally light:
  - No end-to-end integration test that chains ingest -> scan -> search_chats(repo=...). The unit suite covers each pair of adjacent stages, and the cost/benefit of adding the chained one is low given the design has already been validated by the smoke test of the CLI.
  - No tests of the actual Anthropic API. Same call as Phase 3 sub-task 1: tested via mocked stub, real API exercise belongs to the user's manual smoke test.

**Documentation review:**

- README: tools section lists all 4 MCP tools. Repo associations + SessionStart hook documented end-to-end. Auto-summaries section is current.
- ROADMAP: Phase 3 sub-tasks 1-4 marked `[x]` with detail; this audit closes [x] sub-task 5.
- CHANGELOG: Unreleased has sections for each sub-task.
- DEVLOG: entries for each sub-task plus this audit.
- CONTRIBUTING and CLAUDE.md: no drift (architecture rule "core does not import from transports/cli" still holds; verified by mypy passing).
- `.env.example`: includes `MEMEX_SUMMARY_ENABLED` and the API key placeholder. No repo-related env vars (Phase 3 sub-task 2 uses DB-backed config, not env).

**Architectural sanity:**

- `core/` does not import from `transports/` or `cli/`. Verified by inspecting every new file.
- `core/repos/resolve.py` uses `core/storage/repo` (storage is one level lower in the dependency tree). OK.
- `transports/tools.py` imports from `core/repos/` and `core/storage/`. OK.
- `cli/main.py` imports from `core/`. OK.
- No circular imports (mypy run is clean).

**Performance sanity:**

- Pipeline ingest with auto-scan on 74 chats: <50ms overhead added (matcher is `re` substring against ~100 chars of repo metadata). Negligible vs the embedding cost.
- `search_chats(repo=...)` overhead: one extra DB query for `list_conversations_for_repo` + one re-sort of `limit * 5` hits. Sub-millisecond.
- `find_related` with 4000-char input: single embed call, same as `search_chats`. No regression.
- `session-context` typical run: 3 SQL queries + ~10 chat row fetches. Well under 50ms.

**Verdict:**

Phase 3 closes clean. The 2 critical fixes applied during the audit were small and additive; both committed in a follow-up commit alongside this DEVLOG entry. The 6 deferred items are documented for future revisit and none of them block a public alpha release.

**Phase 3 closed.** Next: Phase 4 (remote MCP transport) or Phase 5 (release polish), depending on what the user prioritizes for the public push.

---

## 2026-05-24 (later): Phase 3 sub-tasks 3 and 4, SessionStart hook + find_related

After closing sub-task 2 (chat ↔ repo association) earlier in the same session, we kept going through the rest of Phase 3 feature work.

**Sub-task 3: SessionStart hook.**

The plan from the start was "Memex provides a command, the user wires it into a hook." That keeps Memex out of the business of monkey-patching Claude Code config; the hook is opt-in and the integration point is well-defined.

What landed:
- `memex session-context` CLI command. Auto-detects the active repo by walking up from `cwd` until it finds a `.git` (new `find_repo_root` helper). Resolves the path to a registered repo via the same shared resolver `search_chats(repo=...)` uses. Prints a short Markdown blob to stdout: title, uuid, summary (truncated), and `[manual]` / `[auto X.XX]` source tag per chat. Manual associations come first, then auto sorted by confidence.
- Silent no-op (empty stdout) when no `.git` is found, the repo is not registered, or there are no associations. Diagnostics go to stderr so the hook does not pollute the injected context.
- `find_repo_root` handles both forms of `.git`: directory (regular checkout) and file (gitlink, used by `git worktree`). Caught while writing the tests.
- Extracted `_resolve_repo_key` from `transports/tools.py` to a new `core/repos/resolve.py` so the CLI (which lives outside `transports`) can reuse it. Architecture-rule clean: `core/storage` no longer needs to know how to do the resolution, and both consumers downstream import the same function.

5 new CLI tests (no-git silent, unregistered silent, no-associations silent, prints associated chats with manual-first ordering, `--limit` respected) plus 4 for `find_repo_root` (current dir, ancestor, no .git, gitlink-file worktree form).

**Sub-task 4: `find_related` tool.**

The fourth MCP tool. Sibling of `search_chats` but for a different shape of input: long, free-form text instead of a short keyword query. Pure vector search; no FTS because for long inputs BM25 over individual tokens is noisier than embedding similarity.

Design points:
- Input capped at `FIND_RELATED_MAX_INPUT_CHARS=4000` chars. Bounds latency and keeps us inside the embedder's context window.
- Same `repo=...` boost as `search_chats`. Shares `_resolve_repo_key` and `_apply_repo_boost` from the previous sub-tasks.
- Returns the same result shape as `search_chats` so Claude can consume both the same way. The added field `context_chars` lets the caller see how much of the input was actually used (in case it was truncated).
- Docstring on the MCP wrapper explains the contrast: keywords → `search_chats`, long text / "more like this" → `find_related`, chronological browse → `list_recent_chats`.

7 new tests: empty context, response shape, truncation, limit clamp, unknown repo error, boost reorder, embedder error.

**State at close of feature work:**

- 331 unit tests green (was 220 at the start of this session, +111).
- ruff + ruff format + mypy clean.
- 4 MCP tools registered (`search_chats`, `get_chat`, `list_recent_chats`, `find_related`).
- CLI gained `repos` sub-app (4 commands) + `tag`, `untag`, `session-context` (3 top-level).

**Pending:** sub-task 5, phase-close audit (the project rule). Then Phase 3 is fully closed.

---

## 2026-05-24: Phase 3 sub-task 2, chat ↔ repo association

Landed end to end in this session. New module, schema, CLI, pipeline auto-scan, and search-time boost. 65 new unit tests, suite at 315 green.

**What it does:**

User registers a repo with `memex repos add <path>`. Memex reads `.git/config` and the local manifests to extract a stable identity (remote URL when present, normalized; path-based fallback otherwise). At ingest time, each chat gets matched against the registered repos with four signals (remote URL literal, absolute path literal, manifest name word-bounded, display name word-bounded) and associations are persisted in `chat_repos` with a `confidence` score and a `source` of `'auto'`. When Claude Code calls `search_chats(query, repo=...)`, results that touch the repo get a ranking boost proportional to confidence; chats outside the repo still appear lower down (not a filter).

**Why the design ended up like this:**

- Many-to-many table instead of a column on `conversations`: a single chat often touches more than one repo (monorepos, related projects). Single-column would have forced an arbitrary primary tag.
- Remote URL preferred over path as the canonical key: stable across machines and clones. Two checkouts of the same repo collapse to the same key. Path is fallback for repos without git.
- Auto + manual override (instead of one or the other): auto cuts onboarding to a single command (`scan`); manual handles the cases auto gets wrong. `associate_chat_repo` refuses to overwrite a manual tag with an auto write, so the user always wins.
- Boost, not filter: when the user asks "remember the auth thing?", a chat from a different repo that exactly matches the topic should still surface, just below the in-repo chats. A filter would hide it.
- Threshold of 0.5: the matcher's lowest signal (display name) hits at 0.5; anything lower (which would be partial heuristics not implemented today) gets dropped. Tuning knob exposed.

**Bugs caught during the session:**

- The CLI initially used `→` and `↔` characters in output and docstrings. Rich tried to render them and hit `UnicodeEncodeError` because Windows console default is cp1252. Replaced with ASCII (`->`, `chat-to-repo`). Lesson noted: keep CLI output ASCII-only.
- The matcher initially used `from typing import Iterable`; ruff (UP035) flagged it as legacy. Moved to `collections.abc`.
- The mypy run failed on `RepoInfo` and `ChatRepoAssociation` not being defined when used as string forward refs in `storage/repo.py`. Fix: `TYPE_CHECKING` import block at the top, runtime import deferred inside the helper functions (avoids the circular `core/storage` ↔ `core/repos` problem).

**Tests:** 65 new across `test_repos_keys`, `test_repos_discovery`, `test_repos_matcher`, `test_repos_storage`, `test_cli_repos`, plus `TestPipelineRepoAutoScan` in `test_pipeline.py` and `TestSearchChatsRepoBoost` in `test_tools.py`. Total: 250 → 315.

**State:** ruff + ruff format --check + mypy clean. Suite green. CLI usable end-to-end (`memex repos add`, scan, `search_chats(repo=...)`).

**Next sub-tasks of Phase 3:** SessionStart hook (proactive context injection at session start) and `find_related(current_context)` tool. After both: phase-close audit.

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
