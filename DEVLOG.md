# Devlog

Short log, reverse chronological. One entry per substantive session.

Format: date, what was done, decisions, blockers, next step.

---

## 2026-06-28: Mac<->Windows converge + persisted sync-reachable install (0.4.3)

Two things this session: converged two real device stores end to end (validating the full sync loop on real data, not throwaway DBs), then built the install redesign so multi-device is one persisted command. On branch `feature/install-sync-reachable`.

- **Mac <-> Windows converge (live).** Ran `memex sync connect` from the Mac against Windows over Tailscale, then resolved the long tail: `connect` pulled 25 / pushed 64 but left 10 forks (same `updated_at`, different content hash, i.e. the same claude.ai chat captured with different completeness on each device) + 3 failed + 2 oversized. The Mac had >= chunks on all 10 forks (9 strictly more, 1 tie), so `memex sync push --peer windows` (Mac-wins hash-diff override) was the correct resolution with no completeness lost; the 2 oversized big `claude_code` sessions (11.76 / 9.46 MB) pushed once the local budget was raised (`MEMEX_SYNC_MAX_BATCH_BYTES=14000000` -> a 12.8 MB budget, under Windows' 16 MB body cap). End state: **both at 185, identical content hashes**, vec/fts consistent, retrieval verified. Found a data-hygiene wart: a `ram-iso-1` test fixture (0 messages / 13 chunks) had leaked into the Mac's real DB and rode along to Windows; deleting it from one side alone will not stick (sync does not propagate deletes, the next reconcile pulls it back), so it needs a coordinated cleanup on both ends (noted, not done).
- **Install redesign: opt-in, persisted sync-reachable mode.** New `serve_sync` flag in the sync gate file (`sync/state.py`: `set_gate`/`is_serve_sync`/`set_serve_sync`, read-modify-write so toggling one flag never clobbers the other, atomic write, gate stays apart from the frequently-written history). `memex serve` grew `--sync/--no-sync`; with no flag it reads the persisted `serve_sync` (only when the master gate is also on). In sync mode it binds `0.0.0.0` (one server answers both the loopback extension and the Tailscale address), allow-lists loopback + the detected Tailscale IP resolved at startup, and degrades to loopback with a warning if Tailscale is down (the capture server must always start). `memex setup --sync/--no-sync` persists the choice and prints the one `memex sync connect` line.
- **Key design choice.** `serve` self-configures from the persisted flag, so **no OS service definition changes** (launchd / systemd / Scheduled Task all already run `memex serve`, repo and wheel alike). One source of truth, zero template churn.
- **Shared helpers + hardening.** Moved `detect_tailscale_ip` / `merge_hosts` to a leaf `cli/_net.py` (used by both `sync serve` and `serve --sync`, no circular import); `detect_tailscale_ip` now resolves the `tailscale` binary with `shutil.which` (defense in depth). Added `memex --version` (the UPGRADE doc needs a way to verify the installed version).
- **Docs.** README: a new Installation **Upgrading** subsection (the pipx/uv "Executables already exist" shadowing, the real Windows pain), a **Multi-device** subsection (Tailscale first, then `memex setup --sync`), and the sync section now leads with the persisted one-time setup, `memex sync serve` kept as the one-shot. CHANGELOG `0.4.3`, version bumped (pyproject + server.json + uv.lock).
- **Validation.** 18 new tests (serve_sync state flag round-trips + preservation; `serve --sync`/`--no-sync`/persisted/disabled-gate/no-Tailscale-fallback; `setup --sync`/`--no-sync`/plain-preserves; `--version`). 613 green, ruff + format + core mypy clean.
- **Code-review pass (2026-06-29, fixes folded in).** An independent review found one real consent bug + a few hardening notes; fixed before any release: (1) MEDIUM — `setup --sync` persisted the gate BEFORE the `Proceed?` confirm, so declining still flipped sync on; now the gate is written only after confirmation (with a decline test that locks it). (2) `serve --sync` now prints an explicit "reachable on every interface; the token is the only gate" warning (the plain beyond-loopback warning was suppressed in sync mode). (3) `serve --host X --sync` now warns that `--sync` binds 0.0.0.0 and ignores `--host`. (4) Tests now assert the token is NEVER echoed to the non-tty stream and that the app is REBUILT (not the prebuilt module app) so TrustedHostMiddleware really sees the Tailscale host. 615 green.
- **Live install validated on Windows.** Installed 0.4.3 from the branch via `uv tool install git+...@feature/install-sync-reachable`. Gotcha (not our bug): the entrypoint copy to `~/.local/bin/memex-mcp.exe` failed with "Acceso denegado (os error 5)" — first because a running `memex-mcp.exe` (Claude Code's MCP child) locked it, then because Windows Defender real-time scan blocked writing the new .exe. Fix: close Claude Code + kill memex procs, and add a Defender exclusion for `%USERPROFILE%\.local\bin` and `%APPDATA%\uv`. Worth a README note.
- **Next.** PR for the branch; the user uploads the 0.2.5 extension; converge Linux when it is back online (offline this session, still on 161); optionally clean `ram-iso-1` on both devices.

## 2026-06-28: Pre-launch polish, part 1 (sync setup UX + Windows ingest backstop)

Pre-launch "leave it fine and well-done" pass, on branch `feature/sync-setup-ux`. The live re-test had shown the sync setup is power-user-grade; this removes most of the friction and closes a Windows gap.

- **One command per device for sync setup.** `memex sync serve` (source) resolves a reachable address (auto-detected via `tailscale ip -4`, or `--host`), enables the master gate, binds to that address AND auto-adds it to the Host allow-list (mutating `settings.ingest_allowed_hosts` then rebuilding the app so TrustedHost picks it up), and prints the single `memex sync connect ...` line for the other device. `memex sync connect --url ... --token ...` (destination) enables + pairs + reconciles in one step (it is the line `serve` prints). Binds to the address ONLY (not 0.0.0.0), so it coexists with the always-on loopback capture server without a port clash. Cross-platform, so it kills the PowerShell env-var dance on Windows too. Smoke-tested live both ways (serve binds + `/health` ok + `/sync` 401 without token; `connect` pulled 161 in one command). 7 tests.
- **Claude Code ingest backstop on Windows.** macOS wheel installs autostart both `serve` and a 15-min `ingest-claude-code` backstop; Windows only autostarted `serve`, so a Windows user's local Claude Code sessions were indexed once at setup and never refreshed (Linux wheel has the same gap, noted for a follow-up). Added a `MemexIngest` Scheduled Task (LogonTrigger + a PT15M TimeTrigger repetition, PT1H limit, IgnoreNew); refactored `windows_install_wheel` to install/uninstall/status both tasks. The task runs under `pythonw` (no console), where the ingest command's `console.print` would crash on a None stdout, so `_redirect_streams_if_headless` is now parameterized with a log name and called from `ingest-claude-code` (-> `ingest.log`). 3 tests.
- **Docs.** README sync section rewritten to lead with `memex sync serve` (one command, same on all three OSes); `[Unreleased]` changelog.
- **595 tests green**, ruff + format + core mypy clean. 3 commits (`61cf402` sync serve, `5ca2d30` Windows backstop, `9ef16e7` docs).
- **Deferred on purpose:** full time-boxed auto-pairing (an unauthenticated pairing-code endpoint that hands the token over the network). `sync serve` already removes the token-retyping footgun by printing the whole pair line, so the remaining win (zero copy) is not worth adding a new unauthenticated network surface without its own security/red-team pass. Recommend it as a separate, reviewed change.
- **Next.** Version bump + release for these, then the user uploads the 0.2.5 extension. Optional follow-ups: the Linux ingest backstop (systemd timer), the time-boxed auto-pairing.

## 2026-06-27: Cross-device re-test on published 0.4.0 + sync size-aware batching (0.4.1)

Ran the pending cross-device live re-test on the published `0.4.0` (Mac <-> Linux over Tailscale, Phase 3 master gate enabled on both). It worked end to end (both devices converged to 161 conversations) but surfaced a real bug and an operational gotcha; fixed the bug on `fix/sync-size-aware-batching` (branch + PR per the workflow rule).

- **Re-test result.** `memex sync reconcile` pulled 3 (Linux's) and pushed 158 (the Mac's), 0 forks, both devices ended at 161. The master gate behaved: with sync off Linux's `/sync/*` returned 404; after `enable` + an exposed `serve` + the token, it synced.
- **Operational gotcha (not a code bug).** The first attempt failed with `Invalid host header`: `memex serve --host <tailscale-ip>` only sets the bind interface; `TrustedHostMiddleware` validates the `Host` header against `MEMEX_INGEST_ALLOWED_HOSTS` (default loopback) separately. The peer must add the dialed address to that env var. Already documented in the README sync section; added to CLAUDE.md mistakes.
- **The bug.** `memex sync push`/`reconcile` batched conversations by COUNT (up to 500 per request), but each conversation ships its chunk vectors (~11 KB/chunk at dim 768), so pushing the Mac's 158 (~6 k chunks) in one batch blew the 16 MB `ingest_max_body_bytes`. The server rejects the oversized body with 413, but it closes mid-upload, so the client saw a misleading `Broken pipe` ("could not reach peer"). I completed the re-test with a one-off `batch_size=1` script, then fixed it properly.
- **The fix (`0.4.1`).** Batch by serialized BYTES, never by count. `_serialize_and_push` accumulates records up to a budget and flushes before exceeding it (measuring real JSON bytes); `_fetch_and_insert` does the same on pull, estimating each record's size from the manifest's `chunk_count` + dim. `/sync/manifest` now advertises the device's `max_body_bytes` and per-conversation `chunk_count`; `_resolve_budget` fills to 80% of the peer's cap, falling back to the local 8 MB default when a peer is too old to advertise it. A single conversation that alone exceeds the budget is skipped with a clear log + an `oversized` count in the summary/CLI (raise the peer's `MEMEX_INGEST_MAX_BODY_BYTES` to move it), not a fatal abort. New setting `MEMEX_SYNC_MAX_BATCH_BYTES` (default 8 MB).
- **Validation.** 5 new tests (manifest advertises cap + chunk_count, budget clamps to peer cap, push splits by size, push skips an oversized record without aborting, pull splits by estimated size); full suite 587 green, ruff + core mypy clean. **Validated LIVE:** the updated Mac pulled all 161 conversations from the still-`0.4.0` Linux peer in 23 byte-bounded requests, 0 failed, chunks=vec=fts consistent (also proving the old-peer fallback path: Linux advertised no `max_body_bytes`, so the client used the 8 MB default).
- **Done same day.** `0.4.1` published to PyPI (latest), merged to `main` via PR #3 (merge `25529af`), tagged `v0.4.1`; Linux upgraded with `uv tool upgrade` (its long-running `serve` keeps the old code until restarted, so it still advertises no `max_body_bytes` for now, which the client's fallback handles). Also bumped the Chrome extension 0.2.4 -> 0.2.5 and rebuilt its zip from current src (the prior 0.2.4 zip was stale, predating the Jun-22 audit fixes). The Web Store push of the 0.2.5 extension is the remaining distribution gate.

## 2026-06-24: Multi-device sync Phase 3 (gate, status, conflicts, hardening) + 0.4.0

Closed Phase 8's Phase 3 on `feature/sync-phase3` (branch + PR per the workflow rule). Sync graduates from experimental and gets a real on/off.

- **Master gate, off by default.** New `sync/state.py` persists an `enabled` flag in `sync_state.json`. The whole feature is off until `memex sync enable`: the `/sync/*` endpoints return **404 while disabled** (not 401, so a non-user device does not even reveal the endpoint exists), the CLI data commands refuse, and the auto-sync loop checks the gate each tick. The flag is read per request, so a toggle takes effect without restarting `serve`, and is fail-closed (a corrupt/half-written gate reads as off).
- **CLI `enable` / `disable` / `status`.** `status` shows whether sync is on, the auto-sync setting, the local embedding model/dim, and a per-peer table (last sync time + counts; `--check` pings each peer for reachability + token + model match). Per-peer breadcrumbs live in a SEPARATE `sync_history.json` so a frequent sync write can never race-clobber the rarely-written gate; both write atomically (temp + `os.replace`).
- **Conflict policy confirmed + made observable.** Last-writer-wins by `updated_at` stays the policy. The same-`updated_at`-different-content fork (previously skipped silently) is now returned by `select_reconcile` as `forks` and reported by `reconcile` ("N conflicts left untouched; resolve with `pull`/`push --peer`"). Message-level merge was deferred on purpose: merging messages would force a re-chunk + re-embed, which breaks the no-re-embed design.
- **Red-team pass + fixes.** (1) `insert_record` now re-asserts the `max_chunks_per_conversation` cap and guards `messages`/`chunks` are lists: the sync insert path bypasses the ingest pipeline, so it inherited none of the pipeline's bounds, letting a compromised peer push an unbounded chunk count. (2) `_MAX_SYNC_UUIDS` lowered 5000 -> 1000: each requested uuid materializes a full conversation (text + chunks + vectors) into one in-memory response, and the client only ever batches 500, so the old cap let a token holder amplify one call to hundreds of MB. (3) Documented the pairing trust boundary (a paired peer shares the token, can spoof the LWW timestamp to overwrite, and pushes content the receiver does not re-redact) in `docs/internal/security-notes.md`. Lessons in CLAUDE.md.
- **0.4.0** is the first release carrying sync (`0.3.1`/`0.3.2` were pre-sync). Version bumped in `pyproject.toml` + `server.json` + `uv.lock`, `dist/` rebuilt. **Merged to `main` via PR #2 (merge commit `73c3a71`), tagged `v0.4.0`, and published to PyPI (now the latest).**
- **Tests.** 55 sync tests (was 33): state gate + atomic writes, endpoints 404 when disabled, CLI enable/disable/status + the data-command gate, reconcile fork reporting, cross-device dedup (one row per uuid), and the `insert_record` hardening guards. Full suite 582 green, ruff + format + core mypy clean.
- **Next.** A cross-device live re-test on the published `0.4.0` with `memex sync enable` on both ends (the master gate is new since the pre-Phase-3 live test); upload the 0.2.4 Chrome extension to the Web Store (still 0.2.1). Deferred (optional): a per-device provenance tag + `search --device` filter; republish the MCP registry to 0.4.0.

## 2026-06-24: Multi-device sync live cross-device test (Mac -> Linux over Tailscale)

Validated the sync between the two real machines, end to end, together with the user.

- **Setup.** A throwaway test serve on the Mac (`:5901`, isolated DB in a scratch dir, `MEMEX_INGEST_ALLOWED_HOSTS` with the Mac's Tailscale IP, bound `--host 100.122.177.107`), separate from the live capture server on 5777. The client ran on the Linux box against a test DB.
- **Result: PASS.** `reconcile` reported `pulled 2, pushed 0`, and `memex search "how should the gearbox feel"` on the Linux box returned the conversations seeded on the Mac, ranked by hybrid retrieval. Independently confirmed from the Mac serve log (the Linux IP `100.70.96.57` hit `GET /sync/manifest` 200 then `POST /sync/conversations` 200). Tailscale reachability + bind + Host allow-list + token auth all hold over the real network.
- **Gotchas worth keeping.** (1) The installed PyPI `memex` (0.3.1) predates the sync feature, so the client had to run from a repo clone (`uv run memex sync ...`); shipping sync for real needs a published bump carrying it. (2) The pairing token's capital `O` got copy-pasted as a `0` (zero), giving a 401; O/0 ambiguity is a real hazard for tokens passed through chat. Fixed by correcting the stored token in `sync_peers.json`.
- **Not yet live-tested:** PUSH cross-device (the Linux test DB was empty so the reconcile only pulled); it is covered by the local two-instance test + unit tests, and the network path is now proven.
- **Cleanup.** Test serve down, the live 5777 untouched, Mac test data removed.

## 2026-06-24: Multi-device sync Phase 2 (bidirectional + auto-trigger)

Continued straight into Phase 2 (same session). Added push + a safe two-way reconcile + opt-in auto-sync, and refactored the wire format into one shared place so the server and client cannot drift.

- **Shared wire format (`sync/records.py`).** Pulled `serialize_conversation` + `insert_record` out of `http_ingest`/`client` into one module used by both sides, plus `local_manifest` and the diff helpers. No behavior change to Phase 1.
- **Push (`POST /sync/push` + `client.push`).** The receiver accepts full records (same shape `/sync/conversations` returns), refuses on a model/dim mismatch (409), caps the list, and inserts through the repo. `push` sends the local conversations the peer is missing or has differently (local authoritative).
- **Reconcile (`client.reconcile`).** Two-way, leaves both devices equal. Decision worth recording: the plan defers conflict policy to Phase 3, but a naive bidirectional reconcile by content-hash would let an OLDER copy overwrite a NEWER one (the same claude.ai chat captured on both devices at different times is the common case). So reconcile (and only reconcile / auto-sync) uses last-writer-wins by `updated_at`: pull where the peer is newer-or-absent, push where local is newer-or-absent, skip equal, leave a same-timestamp fork untouched (full conflict policy still Phase 3). The explicit `pull`/`push` commands stay one-directional overrides (hash-diff, the user picks the direction).
- **Auto-trigger.** `MEMEX_SYNC_AUTO` (off by default) + `MEMEX_SYNC_INTERVAL_SECONDS` (default 900, min 60). A Starlette lifespan task in `memex serve` reconciles with each peer on startup and every interval, off the event loop in a thread, with its own short-lived DB connection. It takes the single-flight ingest lock NON-BLOCKING and skips the tick if an ingest holds it (so a sync write never contends with the embedding pipeline and never blocks a capture); offline peers are skipped. The manual commands do not touch the lock.
- **CLI.** Added `memex sync push` and `memex sync reconcile` (shared target/identity helpers with `pull`).
- **Tests + checks.** +11 tests (push endpoint auth/insert/mismatch/cap; client push; reconcile makes both equal + idempotent + newer-wins-no-overwrite; auto-sync skips-when-locked / iterates-peers / no-peers-noop). 560 tests green, ruff + format + mypy clean on core + the new files.
- **Next:** a live Mac<->Linux reconcile over Tailscale, then Phase 3 (conflict policy beyond LWW, `enable`/`disable`/`status`, red-team the sync surface, drop the experimental label).

## 2026-06-24: Multi-device sync Phase 1 (one-directional manual pull)

Started the multi-device sync feature (one Claude across the user's devices; full plan in `docs/internal/multidevice-sync-plan.md`). First committed the single-flight ingest lock left from the prior session (`97f1417`), then built Phase 1: a manual, one-directional pull between two paired devices, reusing the already-running `memex serve` HTTP server (the user chose "reuse serve, opt-in Tailscale bind" over a separate sync daemon).

- **Server (`transports/http_ingest.py`).** Two token-gated endpoints, NO extension Origin required (a peer is not a browser), Host still pinned by `TrustedHostMiddleware`: `GET /sync/manifest` returns `{embed_model, embed_dim, conversations:[{uuid, content_hash, updated_at, source}]}` with no bodies, and `POST /sync/conversations` (`{uuids:[...]}`, capped at 5000) returns the full records (conversation row + messages + chunks + their vectors, plus the Project row when the conversation is a `design_chat`). Vectors travel so the receiver never re-embeds and never loads the model.
- **Vectors out of the DB.** `repo.get_chunks_with_embeddings_for_conversation` reads the float32 blobs straight from `vec_chunks` (by `rowid = chunks.id`) and deserializes them with `struct`.
- **Client (`memex/sync/`).** `peers.py` is a 0600 JSON peer registry next to the DB (peer address + the peer's token). `client.pull` fetches the manifest, refuses on embedding model/dim mismatch, diffs by uuid + content_hash, fetches only the new/changed records in batches under the cap, and inserts each through the repo (`insert_conversation`/`insert_message` + `delete_chunks_for_conversation` + `add_chunk` with the travelled vector) so chunks/vec/fts stay consistent and chunk ids are reassigned locally (no cross-device id collision). Idempotent: a re-pull with nothing new fetches nothing. HTTP is via stdlib `urllib` (no new dependency) and is injectable so the diff/insert logic is tested without a socket.
- **CLI.** `memex sync pair --name --url [--token]` (verifies connectivity + token at pairing time, saves regardless), `memex sync peers`, `memex sync unpair <name>`, `memex sync pull [--peer <name>]`.
- **Off by default.** Nothing new is exposed unless the user pairs a peer AND deliberately binds `memex serve` beyond loopback (`--host` + `MEMEX_INGEST_ALLOWED_HOSTS`, both pre-existing). The `enable`/`disable`/`status` UX and a config gate land in Phase 3.
- **Bug found and fixed in review (rule 3).** A synced `design_chat` carries a `project_uuid` foreign key; inserting it on a device that lacks the project failed the FK (reproduced: `IntegrityError FOREIGN KEY constraint failed`). Fixed by shipping the Project row inside the record and upserting it before the conversation, with a graceful fallback (drop the project link rather than fail the whole insert if a peer does not ship the project).
- **Tests + checks.** 22 new tests in `test_sync.py` (peer store round-trip + 0600 perms, endpoint auth/shape/caps, pull insert + searchability + idempotency + changed-hash replace + model/dim mismatch refused + project sync + missing-project degradation). 549 tests green, ruff + format clean, mypy clean on core + the new files.
- **Next:** Phase 2 (bidirectional + auto-trigger on `serve` startup and a sparse interval, reusing the ingest lock so a sync insert never stacks a model load). Then a live Mac<->Linux pull over Tailscale before graduating from experimental.

## 2026-06-23: Ingest RAM investigation + single-flight lock across all embed paths

Goal: cut the transient ingest RAM peak (~0.69 GB observed on the `Memex ingest` worker) as much as possible without hurting search. Measured every lever on the real model (`nomic-embed-text-v1.5-Q`, batch=1/threads=1) instead of guessing.

- **Findings (measured peak RSS, minimal process).** nomic 768d = ~0.54 GB; `all-MiniLM-L6-v2` 384d = ~0.26 GB (35x faster) but English-centric; `bge-small-en` 384d = ~0.40 GB; `paraphrase-multilingual-MiniLM-L12-v2` 384d = ~0.88 GB (huge multilingual vocab → MORE RAM). `enable_cpu_mem_arena=False` made no difference (538 vs 545 MB). Chunk size 500 -> 256 tok saves ~85 MB and ~2x speed. So the peak is dominated by the fixed model+runtime load; batch/threads were already at the minimum.
- **Quality gate.** A semantic recall test (Spanish + English, low lexical overlap) showed nomic 6/6 vs all-MiniLM 4/6, the misses being Spanish with near-zero margin. Conclusion: for a Spanish+English corpus nomic is near the practical floor; a smaller model trades real recall for RAM. Documented as an opt-in only if usage becomes English-dominant. No model change shipped.
- **Shipped: serialize every embedder loader.** The CLI ingest already held a non-blocking `ingest.lock`, but the live-capture worker did not, and `http_ingest` awaits each subprocess, so concurrent captures (backfill burst / multiple tabs) or capture-vs-CLI could stack ~0.5 GB loads. Extracted the lock to `memex/ingest_lock.py` (`acquire_nonblocking` for the CLI backstop, `acquire_blocking(timeout)` for live capture, `release`). `_ingest_in_subprocess` now takes an in-process `asyncio.Lock` (serializes the server's own workers) plus the cross-process lock blocking-with-timeout (a capture waits for an in-flight ingest, else 503s and is re-sent), and never loads a second model concurrently. CLI keeps the non-blocking skip.
- **Verification.** 527 unit tests pass (+4 new in `test_ingest_lock.py`), ruff + format clean, mypy clean on core + the new/touched files. No DB or schema change; user data untouched.
- **Next:** optional `MEMEX_CHUNK_SIZE` reduction (needs a re-index to be consistent); the smaller-model swap stays documented-but-unshipped. Then debate a cross-device sync design (separate project reusing memex's core).

## 2026-06-22: Phase 7 + Phase B close audit (findings fixed)

Ran the owed phase-boundary audit across five dimensions in parallel (install/autostart, HTTP ingest + auth, the Chrome extension, config/data/redaction, and a project-wide dead-code/doc sweep), then verified each finding against the code and implemented the fixes. No critical/high-remote findings; the auth gate held (Origin+token, constant-time compare, 0600 token, TTY-gated print). 523 tests green, ruff + core mypy clean.

- **systemd unit hardening.** `render_systemd_unit` now rejects a DB/log path containing a newline or quote (a newline could inject an `ExecStartPre=` that runs at login) and the unit gets `UMask=0077` (parity with the launchd `Umask`; the wheel systemd log was world-readable). The headless `serve.log` reopen (`_redirect_streams_if_headless`) is now 0600.
- **`/ingest/plan` amplification.** Added a 50k item cap (the body byte cap bounds bytes, not list length, so a token-holder could force a full-table scan + millions of timestamp parses on the event loop) plus uuid dedupe and an empty-list short-circuit.
- **Worker env/stderr hygiene.** The short-lived ingest worker no longer inherits `ANTHROPIC_API_KEY` / the GitHub OAuth secret (it never uses them); its stderr (which can carry the absolute DB path) is logged server-side, not returned to the client.
- **Config env-exposure.** Removed `populate_by_name=True`: it also let every setting be set by its bare snake_case env name (`db_path`, `anthropic_api_key`, ...), not just the `MEMEX_*` alias. Tests now construct `Settings` by alias.
- **Extension.** `executeScript` self-checks `location.origin === "https://claude.ai"` inside the injected func (TOCTOU on the active tab); `background.js` gates runtime messages by sender so admin kinds (`set-token`/`set-server-url`) only come from the popup; the token field is `type="password"`.
- **Bugs / dead code.** `get_chat` MCP default 20 -> 10 (matched the pure tool via the shared constant); macOS `install-service` returns non-zero when a launchd load printed FAILED; removed dead `repo.list_projects()` and the stale `__version__ = "0.0.1"` (now resolved from package metadata).
- **Docs.** README macOS wheel log path (`com.memex.serve.log`), "3 tools" -> 4 (with `find_related`), the "coming soon"/SSE diagram label; `chrome-extension/README.md` (published + token pairing + Backfill); `http_ingest` docstring lists `/ingest/plan`. Added regression tests (the `/ingest/plan` item cap + dedupe, the systemd path guard, the `UMask`). The one live residual (the page-world backfill reply channel is forgeable, integrity-only) is recorded in `docs/internal/security-notes.md`; fixed-issue lessons in CLAUDE.md.

## 2026-06-22: Linux live test started, fixed the one-liner (dash incompat)

Began the last open Phase B verification: the live Linux/systemd install. Pre-flight reviewed the whole Linux path (`install-pypi.sh` -> `memex setup -y` -> `_run_install_service` Linux/wheel branch -> `linux_install_wheel` -> `render_systemd_unit`) and the wheel data-dir resolution; rendered the exact systemd unit to eyeball it. Code path is correct (ExecStart runs the uv-tool python `-m memex.cli.main serve`, `MEMEX_DB_PATH` pinned to `~/.local/share/memex`, serve on `127.0.0.1:5777`).

- **Bug found on the very first command (the point of the live test).** The one-liner `curl ... | sh` failed with `sh: 14: set: Illegal option -o pipefail`: `install-pypi.sh` was `#!/usr/bin/env bash` + `set -euo pipefail`, but piping to `sh` runs it under dash on Debian/Ubuntu, which has no `pipefail`. Slipped past macOS because there `/bin/sh` is bash. Fix: rewrote the script as POSIX (`#!/bin/sh`, `set -eu`); the dropped `pipefail` is covered by the existing `command -v uv` check. Verified clean with `sh -n` and `dash -n`. The script ships from GitHub raw `main` (not the wheel), so a push fixes the live one-liner with no PyPI re-publish. Other repo `.sh` scripts are unaffected (run via shebang or explicit `bash`). Logged in CLAUDE.md mistakes.
- **Immediate unblock** while the fix lands: re-run with `| bash` instead of `| sh`.
- **Verified live (2026-06-22).** Re-ran the one-liner with `| bash` on the Linux box; the install completed and the systemd user unit came up (serve active, `/health` ok, DB at `~/.local/share/memex`). **Phase B autostart is now verified live on all three OSes** (macOS launchd, Linux systemd, Windows Scheduled Task). The dash incompatibility above is the only issue found.
- **Next:** push the `install-pypi.sh` POSIX fix to `main` so the canonical `curl ... | sh` works for everyone (no PyPI re-publish needed, it is served from raw `main`).

## 2026-06-20: verification pass, 0.3.0 release prep, Windows wheel autostart

Validation + finishing Phase B. Ran a full verification of yesterday's work and closed the last wheel-autostart gap.

- **Verification pass.** Full suite green; `memex doctor` all OK (DB at the new absolute path, 148 convs); ran `memex setup -y` end to end on the repo install for the first time (idempotent: MCP "already registered", autostart reloaded, ingest "2 new / 37 unchanged", token printed). Built the wheel and installed it in an isolated venv to confirm the PyPI-first path: `source_repo_root()` -> None, `db_path` -> `~/Library/Application Support/memex` (not the repo), the generated plist runs the venv's python, `python -m memex.cli.main` + the `memex` console script work. No stray state left.
- **0.3.0 release prep.** Bumped `memex-chats` 0.2.3 -> 0.3.0 (additive features since 0.2.3), synced `server.json` + `uv.lock`, dated the CHANGELOG. The published PyPI build is still 0.2.3, so a plain `pipx install memex-chats` gets the old one; testing uses the local `0.3.0` wheel (or a real publish, user's token, done in the terminal, never pasted in chat). Not published yet.
- **Windows wheel autostart (closes Phase B step 2).** Considered (and rejected) auto-cloning the repo from the installer (version skew vs the pinned wheel, a `git` dependency, running unaudited code). Instead implemented the native path, matching mac/linux: `cli.services.windows_install_wheel` generates a logon Scheduled Task via `schtasks /Create /XML` (XML avoids `/TR` quoting pitfalls) running `pythonw -m memex.cli.main serve` (no console window, no repo, no PATH dep). `_run_install_service` now dispatches the Windows wheel branch to it. The DB resolves to `%LOCALAPPDATA%\memex` from the installed package, so the task and the CLI share it without pinning. Verified by parsing the generated XML with `ElementTree`; not live-tested here (no Windows). 518 tests. Next: the user tests the `pipx install <wheel> && memex setup` flow live on Windows + Linux.

**0.3.0 published + Windows live test -> 0.3.1 fix.** User published 0.3.0 to PyPI and `pipx install memex-chats` worked on Windows (`memex.exe` / `memex-mcp.exe` on PATH). The Scheduled Task registered fine (`schtasks /Query` -> Ready), but the server never came up: `memex serve` worked by hand, yet `schtasks /Run` did not bind 5777. Root cause: the task runs `pythonw` (no console), where `sys.stdout`/`sys.stderr` are `None`, so `serve`'s `console.print(...)` and `sys.stdout.isatty()` raised before `uvicorn.run`. Fix (0.3.1): `_redirect_streams_if_headless()` reopens the streams to `serve.log` in the data dir when there is no console (also gives Windows logs), and the `isatty()` check is None-guarded; the task XML is unchanged. Reproduced the failure with `sys.stdout = sys.stderr = None` and confirmed the fix. Lesson logged: a Windows background task uses `pythonw`, so any CLI entry point it runs must tolerate `None` std streams.

**Windows autostart VERIFIED working (0.3.1), plus a red-herring lesson.** After publishing 0.3.1 and `pipx upgrade`, the task still failed, and the `schtasks /V` query revealed why: the `MemexServe` task was an OLD one from a long-standing repo clone on the user's Windows box (`D:\Dionisio\Memex`, running `_run-server.ps1`), there since May, masking ours. So we had been debugging the wrong task; our `pythonw` task was never the one registered. Once the user deleted it (`schtasks /Delete /TN MemexServe /F`) and re-ran `memex install-service install` from the pipx `memex` (confirmed `C:\Users\dioni\.local\bin\memex.exe`, memex-chats 0.3.1), the task became `...\pipx\venvs\memex-chats\Scripts\pythonw.exe -m memex.cli.main serve`, `schtasks /Run` started it, and `/health` returned ok. **Phase B autostart is now verified live on all three OSes** (macOS launchd, Linux systemd pending a live run, Windows Scheduled Task). Lesson: a stale install with the same service name silently masks the new one; the fix (0.3.1) was correct but was not what was running.

**One-command PyPI install.** Added `scripts/install-pypi.sh` + `install-pypi.ps1`: install uv (if missing), `uv tool install memex-chats`, then `memex setup -y`. The README leads with the `curl | sh` / `irm | iex` one-liner. This is as close to "one command installs everything" as a terminal can get; the only remaining manual step is the browser extension (install + paste token + Backfill), which a terminal cannot do. Not run locally on purpose (it would create a second, conflicting install next to the dev repo); to be tested on a fresh machine.

Frictionless-onboarding work. Mapped the real new-user critical path (~9 steps, one of them an async export of hours, one a token copy-paste between two installables) and attacked the install half of Phase 7.

- **New `memex setup` command** (`src/memex/cli/main.py`). One idempotent command that: checks the embedder, runs `claude mcp add --scope user memex -- ...` (or prints the manual command if the `claude` CLI is absent), installs the autostart service, indexes local Claude Code sessions, and prints the extension pairing token + Web Store link. Each step degrades to a WARN row instead of aborting the rest. Flags: `--no-mcp` / `--no-autostart` / `--no-ingest` / `--remote` / `-y`. Detects repo vs wheel install and builds the MCP invocation accordingly (`uv run --directory <repo> memex-mcp` vs bare `memex-mcp`).
- **macOS launchd implementation for `install-service`** (new `src/memex/cli/services.py`). The Darwin branch was a print-only stub; it now renders the plist templates (substituting `__REPO__`), writes them to `~/Library/LaunchAgents`, and `launchctl load`s them, with `uninstall` / `status` to match Linux/Windows. Default agents are `serve` + the ingest backstop; the remote connector is opt-in via `--remote` (it crash-loops without the `MEMEX_REMOTE_*` config, so installing it by default — as the old README one-liner did — was a latent bug, now fixed).
- **Design decision: Phase A is repo-anchored, deliberately.** The agents still run from the cloned repo and the DB default is still CWD-relative (`config.py:67`). Making a pure `uvx`/PyPI install self-sufficient (ship templates in the wheel + a stable user data dir) is **Phase B**, deferred because it changes the DB default and would move existing users' databases. `services.source_repo_root()` returns None on a wheel install so `setup` degrades (MCP + ingest + token work; autostart warns).
- **M1 (backfill endpoint discovery) done.** Verified live in claude.ai devtools: org-with-`chat` from `/api/organizations`, conversation list `GET .../chat_conversations?limit=&offset=` (flat array, offset pagination, `limit=1000` returns all), items carry `uuid` + `updated_at`, full conv via `?tree=True&rendering_mode=messages&render_all_tools=true`. The existing `inject.js` hook captures the backfill fetch (classifies by path), so M2 needs no pipe changes. Recorded in ROADMAP + `handoff.md`.
- **Tests:** rewrote the two macOS `install-service` tests (behavior changed from print to launchctl) and added hermetic coverage for launchd install/status/no-repo, `services.render_agent`, and `setup` (all-skipped prints token, MCP calls `claude mcp add`, missing `claude` CLI warns). 503 passed (+1 skipped), `ruff` clean.

Decision: split "simpler install" into onboarding (this) + auto-backfill (next).

**Then M2 (active backfill), same day, verified live.** Added `window.__memexBackfill()` to `chrome-extension/src/inject.js`: it reads the chat org from `/api/organizations` (the one with the `chat` capability), pages the conversation list, and fetches each full conversation through the already-patched `fetch`, so the existing capture pipe ingests it with ZERO changes to content.js/background/server. Concurrency 3 + 200 ms throttle + re-entrancy guard; org/list calls use `originalFetch` (not conversations, must not be captured), conv-full uses `patchedFetch` (captured). Reuses the claude.ai (no-redact) path by construction. Tested against the real DB: 94/94 fetched, 0 failed, claude.ai `conversations` 93 -> 98 (+5 brand-new chats pulled in, the rest deduped by uuid/content_hash). No automated test (extension JS is outside the pytest harness); `node --check` only. Next: M3 (incremental, skip-unchanged via a `GET /ingest/known`) and M4 (popup button + progress + resumability).

**Then M3 + M4 (incremental + popup), same day, verified live.** Server: new `POST /ingest/plan` (`http_ingest.py`) takes the conversation manifest and returns only the new/changed uuids, comparing `updated_at` by instant (the ingest normalizes fractional seconds, so a string compare is wrong). The indexed set is computed and never leaves the server. It is a POST, not a GET, on purpose: a first cut used `GET /ingest/known` and the extension's cross-origin GET did not carry the extension Origin, so the server 403'd and the backfill fell back to fetching all (which it did, safely). POST matches the proven `/ingest/conversation` path, so the Origin is always sent. Extension: inject.js asks background (via a new `control` message channel kept disjoint from the validated capture path) which uuids to fetch, then fetches only those; the popup gets a "Backfill history" button that triggers `window.__memexBackfill` in the MAIN world via `chrome.scripting.executeScript` (added the `scripting` permission, bumped the manifest to 0.2.4), and live progress flows back inject -> content -> background -> popup stats. Verified live: full account re-runs as "0 to fetch, 94 up to date" (M3), and the popup shows progress and the final tally (M4). 4 new server tests (`TestBackfillPlan`); 507 passing.

Gotcha logged: `launchctl kickstart -k` on `com.memex.serve` mid-backfill SIGTERMs (-15) the in-flight ingest subprocesses, so a handful of chats showed up as failed in the extension. No data loss (the killed ones were re-ingests of already-stored chats; the incremental confirmed "0 to fetch"), and a missing chat self-heals on the next backfill. Lesson: do not restart `memex serve` during a bulk ingest.

**M5 (docs + repackage).** README now leads onboarding with `memex setup`, the live-capture steps include the one-click "Backfill claude.ai history" button, and the autostart section covers macOS too (was Windows/Linux only). CHANGELOG `[Unreleased]` captures setup + cross-platform autostart + the backfill + `POST /ingest/plan`. Repackaged the extension to `chrome-extension/dist/memex-extension-0.2.4.zip` (manifest 0.2.4, gitignored). Remaining manual: upload 0.2.4 to the Web Store (the live store build is still 0.2.1 without backfill) and a fresh-machine e2e. Half B (M1-M5) is functionally done; next is closing + Phase B of the install (PyPI-first).

**Phase B step 1: stable data dir.** The DB/exports default was `./data/memex.db`, relative to the CWD, which only works because the daemons `cd` into the repo; a `uvx`/`pip` install would create the DB wherever the command happened to run. Now `config.py:_default_data_dir()` resolves it absolutely: `<repo>/data` when running from a cloned/editable repo (a `pyproject.toml` + `scripts/` two levels up from `config.py`), so existing installs are unchanged (verified: the 147-conversation DB still resolves), or an OS-conventional per-user dir otherwise (macOS `~/Library/Application Support/memex`, Windows `%LOCALAPPDATA%\memex`, XDG `~/.local/share/memex`). `connect_and_init` already `mkdir -p`s the parent, so a fresh wheel install just works. `MEMEX_DB_PATH` still overrides. 5 new tests (`test_config.py`).

**Phase B step 2: self-contained autostart for wheel installs.** The repo-mode autostart points at `scripts/` (absent in the wheel), so `cli.services` now also generates definitions that have no repo dependency: a launchd plist (macOS) and a systemd user unit (Linux) whose command is `sys.executable -m memex.cli.main serve` (the absolute interpreter + the `memex.cli.main` `__main__` guard, so no PATH dependency at boot), with the resolved `MEMEX_DB_PATH` pinned into the service env so it matches the user's CLI. `_run_install_service` dispatches to the repo path when `source_repo_root()` finds a clone (the proven, live-tested path is untouched) and to these generators otherwise. Windows wheel autostart is not generated yet (the Scheduled-Task path still needs the repo `.ps1`); it prints a clear message. Verified by generating the plist and parsing it with `plistlib` (valid: ProgramArguments, EnvironmentVariables, KeepAlive for serve / StartInterval 900 for ingest). 4 new/updated tests. 516 passing. Caveat: persistent autostart wants a `pip`/`pipx` install (stable `sys.executable`); transient `uvx` is not a good autostart target. Not live-tested end to end (would need a real wheel install); the generators are unit-covered. Remaining Phase B: Windows wheel autostart + a fresh-machine e2e.

---

## 2026-06-16 (later): demo GIF, registry published, Phase 7 planned, redaction verified

Distribution + launch session. Built on the 0.2.3 prep below.

- **Published to the official MCP Registry.** Installed `mcp-publisher`, `mcp-publisher validate` flagged the `server.json` description was over the 100-char limit (shortened it), then `mcp-publisher login github` + `publish`. Live as `io.github.dioniipereyraa/memex`, `status: active`. Learned the registry has no per-server web page (404); only the API responds, so the `io.github...` name is an identifier, not a link. Also submitted to mcp.so (status `created`, pending review). Glama/PulseMCP auto-index, no action.
- **0.2.3 to PyPI.** `uv build` + `uv publish`. Confirmed the `mcp-name` marker is in the published PyPI description (the registry needs it to verify package ownership). `uv.lock` synced.
- **Demo GIF in the README.** User recorded a screen capture, converted to GIF (ffmpeg palettegen/paletteuse path explored; the user's own 800x518 / 2.5 MB GIF was the one shipped). Replaced the static `session-memory-check.jpeg` reference; rewrote the caption to match (claude.ai recall from Claude Code). README opener also rewritten to lead with the pain, not the architecture.
- **Planned Phase 7 (claude.ai auto-backfill).** The product gap: a new user's claude.ai history is empty until a manual export, so the demo does not reproduce on a fresh install. Root cause confirmed by reading `chrome-extension/src/inject.js`: it is a passive `fetch` interceptor (keeps only `conv-full` / `conv-create` responses the app already makes), never backfills. Architectural constraint logged: the terminal cannot reach claude.ai, so the backfill must come from the extension (MAIN-world session) or a pasted `sessionKey`, NOT a Claude Code hook. Full milestone plan (M1 discovery -> M5 docs/e2e) is in `handoff.md`. Gate before Hacker News.
- **Verified `redact.py` covers PyPI tokens.** The user pasted a real PyPI token in chat; confirmed the dedicated `pypi-token` vendor rule (plus the assignment + labeled-value rules) masks it. Live check with a synthetic token: bare `pypi-...` -> `[REDACTED:pypi-token]`. Masked in the Memex index; the raw `.jsonl`/transcript still hold it, so revoking remains a user TODO.
- **Distribution reality:** Reddit direct posting is karma-gated (r/ClaudeAI, r/mcp, r/SideProject all blocked); got one showcase comment up. Plan: earn karma, then post for real with the backfill shipped. Discord + registries are the channels that worked.

---

## 2026-06-16: 0.2.3, list in the MCP registries

Distribution work, no code change. Added the official MCP Registry publishing artifacts: a `server.json` at the repo root (name `io.github.dioniipereyraa/memex`, pypi package `memex-chats`, stdio transport) and an `mcp-name: io.github.dioniipereyraa/memex` HTML-comment marker at the top of the README so the registry can verify the PyPI package is ours. The marker only lands on PyPI with a new release, so bumped to 0.2.3 (README leads with the pain now, not the architecture).

Publish runbook (manual, needs credentials / interactive auth): build + `twine upload` the 0.2.3 dist, then `mcp-publisher login github` + `mcp-publisher publish`. Glama auto-indexes from GitHub (no action). mcp.so and PulseMCP are manual web submissions.

Next step: cut the 0.2.3 PyPI release, publish to the official registry, submit to mcp.so + PulseMCP.

---

## 2026-06-12: capture server embeds in a subprocess (0.2.2)

Closed the capture-server RSS issue properly. The earlier in-process idle-release was reverted because `del + gc` does not return the onnxruntime arena to the OS on macOS (measured). The only reliable fix is to embed in a process that exits, so the OS reclaims everything. User pushed for it: ~0.5 GB resident is a real cost for low-RAM users of an always-on local tool.

Design: the `/ingest` handler, when `MEMEX_INGEST_EMBED_IN_SUBPROCESS` (default true), spawns `python -m memex.transports.ingest_worker <source>` via `asyncio.create_subprocess_exec`, pipes the conversation payload over stdin (nothing sensitive on disk), and reads the summary as one JSON line from stdout. The child loads the model, runs the full `ingest_single_conversation`, stores to the DB (own connection, WAL handles concurrency), and exits. The subprocess boundary is per CONVERSATION, not per embed() call (which is per-32-chunk-batch and would reload the model dozens of times). DB path passed absolute via env so the child does not depend on cwd. `False` keeps the in-process path (used by the existing capture tests with their injected in-memory DB + FakeEmbedder).

Verified live: restarted `memex serve`, baseline 0.06 GB; POSTed a synthetic capture; the parent stayed at **0.06 GB** the whole time while the transient child peaked at ~0.63 GB and then exited cleanly (no lingering worker); the conversation ingested (1/30/19). Cleaned up the test conversation via `delete_chunks_for_conversation` + delete row (chunks=vec=fts stayed in sync). New `ingest_worker.py` + tests (worker `run_ingest` with injected fakes; endpoint subprocess branch and worker-failure-503, both mocked). 496 tests green, ruff + mypy clean. Cost: ~3-5s model load per captured chat (background, fine). The MCP server is left resident (one query per search is cheap, and search wants low latency); the `ingest-claude-code` process already exits.

---

## 2026-06-12: redaction re-index of the live corpus + orphan-vector cleanup

Applied the round-4 redaction to already-indexed content (the new rules only mask on ingest going forward; secrets of the newly-covered shapes already in the store stay until re-ingested). Only the `claude_code` source is redacted (claude.ai chats are genuine conversation, not redacted by design), so the scope was the 20 `claude_code` conversations, not the 92 claude.ai ones.

Approach (low-risk, no raw deletes): online backup of `data/memex.db` first (`data/backups/memex.db.pre-reindex.bak`, via the sqlite3 backup API for a consistent snapshot), then `UPDATE conversations SET content_hash=NULL WHERE source='claude_code'` so the skip-unchanged check fails, then `memex ingest-claude-code` re-ingested all 20 (the pipeline's `delete_chunks_for_conversation` cleanly replaces vec+fts+chunks per conversation). Verified: 20 re-ingested, 0 errors, content_hash repopulated, claude.ai 92 untouched, search works across both sources.

**Found and fixed a pre-existing data-integrity bug:** the DB carried 3823 orphaned `vec_chunks`/`fts_chunks` entries (vec=fts=7355 vs chunks=3532), confirmed pre-existing by checking the backup (identical orphan count), so the re-index did NOT cause it (`delete_chunks_for_conversation` kept vec/chunks 1:1, orphans stayed exactly 3823 across the re-ingest). Root cause: a prior session cleared rows with a raw `DELETE FROM conversations`, which cascades to `chunks` but not to the virtual vec/fts tables. Cleaned them (`DELETE ... WHERE rowid NOT IN (SELECT id FROM chunks)`, batched), now chunks=vec=fts=3584, 0 orphans, search verified. Lesson logged in CLAUDE.md (never raw-delete conversations; there is no `delete_conversation` helper that cleans vec/fts yet).

---

## 2026-06-12: red-team round 4 (data-theft focus) + 0.2.1 security patch

User asked for one more big adversarial verification focused on what matters most: stealing sensitive info that could be sent through a chat, and stealing chat content from outside. Ran four parallel attackers, each executing REAL attacks (not theory): (A) redaction bypass, (B) the remote connector, (C) the local surface, (D) end-to-end data-flow / injection.

**Verdict: data theft from outside did NOT work.** The remote connector held against unauthenticated tool calls, forged/`alg:none` JWTs, Host-header/DNS-rebinding, redirect_uri theft, consent CSRF, and allow-list bypass; zero chat bytes leaked. The local capture server held (constant-time token, Origin/Host pinned, body caps); all of `data/` is 0600 in a 0700 dir and the token is never logged. The claude_code→cloud path leaks no new field un-redacted (the round-2 title fix still holds; every adjacent derived field is covered).

**Redaction bypasses found by execution and fixed (redact.py), all reproduced first:**
- **HIGH: 64-hex key exempted by a non-adjacent digest WORD.** `_DIGEST_CONTEXT` matched free-text `object`/`commit`/`hash`/`tree`/`oid` anywhere in the 160-char window, so `the commit message references <eth-key>` left a 256-bit key in cleartext. Tightened to adjacency (marker must abut the token, anchored `$` with a short separator), mirroring the round-3 `_INTEGRITY_PREFIX` fix. Real `sha256:HEX` / `commit HEX` / `etag: "HEX"` still preserved.
- **HIGH: dot/colon-segmented secrets leaked whole.** Discord (`id.ts.hmac`) and Telegram (`id:secret`) tokens, and any dot/colon-chunked secret, evaded the entropy pass (each segment under the 28-char floor). Added Discord + Telegram vendor rules and a general `_redact_dotted_secret` gated hard (joined body ≥40 chars, 3 char classes, entropy ≥4.0, not a camel identifier, `/` excluded so paths/URLs are not swallowed). Verified domains, semvers, Maven coords, Java package paths, k8s image refs all survive.
- **MED: zero-width / soft-hyphen split.** An invisible `Cf` char mid-secret split the token; each half survived, and the read-time strip was too late (already chunked/embedded). Now `_strip_format_chars` (Cf category + TAG block) runs first in `redact_secrets`.
- **MED: PEM ReDoS.** Packed unterminated `-----BEGIN ... PRIVATE KEY-----` markers forced a 16 KB lazy scan each (2.9 MB ≈ 10 s). Required a trailing newline after the header → junk BEGIN fails instantly, linear again (≈1 s). Real PEM/CRLF blocks still redacted (verified).

**Other fixes:** the untrusted-content envelope now names the `project` description/prompt_template surfaced by `get_chat` (a Project prompt_template reads like a system prompt; it was already control-char-stripped, this closes the framing gap). Chrome extension: `content.js` checks message origin + payload shape; corrected the misleading `scrubSensitive` "redaction" comment (it is not a security boundary); bumped manifest to 0.2.1. Honest call: a nonce handshake between the MAIN-world inject and the isolated content script can't be forge-proof (no un-observable cross-world channel; a nonce rides postMessage and is observable by a page-world attacker), so documented the limit instead of shipping security theater. That vector needs a claude.ai XSS and is index-poisoning (integrity), not exfiltration.

**Documented, not fixed:** unauthenticated DCR `/register` is unbounded (no TTL/cap in upstream FastMCP) → disk-exhaustion DoS. Availability only, no data exposure, behind the user's own Tailscale Funnel. Mitigate at the tunnel; proper fix is upstream. The round-4 attacker created ~60 throwaway encrypted client files (<1 MB total) proving it; harmless and left in place (can't distinguish them from the live claude.ai client by mtime without risking the real connection).

**State:** 493 tests green (+16 round-4 corpus/perf guards), ruff + format + mypy clean. Shipping as 0.2.1.

**RAM optimization (same session, user reported ~4 GB across memex processes):** measured the real driver instead of guessing. The persistent servers are cheap (serve ~0.17 GB, connector ~0.03 GB, each MCP ~0.1-0.25 GB, one per open Claude Code session, model loaded lazily on first search); the heavy process is the ingest's embedder. Benchmarked the embedder on 200 realistic 500-token chunks (threads=2, peak RUSAGE): batch=4 → 1556 MB / 45.6 s, batch=2 → 969 MB / 44.8 s, batch=1 → 671 MB / 44.7 s. Throughput is batch-independent because threads=2 caps parallelism, so a larger batch only inflates the onnxruntime per-batch arena. Also tested fastembed's `enable_cpu_mem_arena=False` (it exposes it via kwargs): it made things WORSE (batch=4 went 1556 → 2291 MB), so the arena stays on. Lowered the default `embed_batch_size` 4 → 2 (≈ -38% RAM, no speed cost) and set this machine's `.env` to `MEMEX_EMBED_BATCH_SIZE=1` (≈ 0.67 GB embedder peak; real ingest adds pipeline + longer chunks on top, so the live process lands around half of the old ~2.2 GB). Takes effect on the next ingest (uv run reads `.env` fresh); no restart needed.

**Capture server RSS (same session, user then reported the capture process at 1.79 GB):** root cause is structural, not the batch size. `memex serve` embeds each captured chat in-process and is always-on (launchd KeepAlive), so once it embedded one chat it held the model + arena resident forever. batch=1 already cuts that ~1.79 GB (old batch=4 + a large chat) down to ~0.48-0.66 GB, durable.

Tried an idle-release timer (drop `_embedder` + `gc.collect()` after N seconds of no captures) and MEASURED it: it does NOT work on macOS. Pure-Python probe: after embedding, RSS 0.48 GB; after `del model; gc.collect()`, 0.47 GB. The onnxruntime arena is freed at the C++ level but the process allocator keeps the pages (macOS does not return them to the OS), so RSS does not drop. Live test on a real `memex serve` (idle=5s) confirmed: RSS stayed at 0.65 GB, never released. Disabling the CPU arena (`enable_cpu_mem_arena=False`) did not change the retained RSS either (0.49 GB). So the only way to actually reclaim is to embed in a SHORT-LIVED SUBPROCESS that exits (the OS reclaims everything on exit). That is a real change with a per-capture model-reload cost (~3-5 s) and it complicates the test setup (the existing capture tests inject an in-memory DB + FakeEmbedder into the server process, which a child cannot share), so it was NOT done unilaterally. **Reverted the idle-release code** rather than ship a no-op feature; kept batch=1. Offered the subprocess approach to the user as the next step (gets the capture server to ~0.08 GB idle, with the latency tradeoff). The transient `ingest-claude-code` process already exits and frees RAM, so batch covers it; the MCP server embeds only one query per search (small) and is left resident for search latency.

Next: publish 0.2.1 to PyPI (maintainer token), tag, push. Repackage the Chrome ext (0.2.1) if resubmitting.

---

## 2026-06-12: credential rotation + 0.2.0 release

Closed the pending security actions and shipped the release.

- **GitHub OAuth client secret rotated** (it had appeared in audit logs). New secret in `.env`, connector restarted via `launchctl kickstart`. Side effect understood and verified: the restart invalidated the token claude.ai had stored (401 `invalid_token` in `serve-remote.log`), fixed by re-authorizing the connector from claude.ai; the log then shows the full OAuth dance (authorize, consent, callback, token) and a stream of 200s on `/mcp`. Rotation validated end to end.
- **Chrome extension re-paired** with the rotated capture token. Verified live: opening a chat on claude.ai produced `POST /ingest/conversation` 200s and the new messages were retrievable via MCP seconds later.
- **0.2.0 released.** CHANGELOG: `[Unreleased]` promoted to `[0.2.0]` and completed with the missing entries (auto-sync hook + backstop, always-on launchd agents, installers, process naming, embed settings, the flock, and a full Security section covering the audit + three red-team rounds). pyproject bumped and description updated (the remote connector is no longer "soon"). README: status header, PyPI note (now: package current, but extension/scripts only come with the repo), and the stale `install-service` macOS bullet now points to the launchd section. ROADMAP Phase 5: macOS launchd marked done, status header refreshed. 477 tests green, ruff + format + mypy clean. `uv build` artifacts inspected: wheel is package-only, sdist (112 files) has no `data/`, `.env`, or `MEMEX.md`.
- **Observation, not a blocker:** the overnight re-population ingest (full corpus re-index with the new redaction) ran ~9 h with a ~2.6 GB peak, above the ~1.6 GB expected at batch=4. It finished clean (0 errors) and freed the RAM on exit. If a routine incremental ingest ever shows that footprint again, profile it; for the one-off full re-index it is tolerable.

Next: publish to PyPI (maintainer token), tag `v0.2.0`, push.

---

## 2026-06-11 (resource audit, round 3): embedder respawn + redaction O(n^2)

Third-round performance/resource audit after the user hit a 20 GB RAM / machine-freeze during ingest. Measured everything for real (`uv run python`, peak tree-RSS polled across the whole process tree, `/usr/bin/time`-style accounting).

**Findings + fixes applied (all tests green, ruff + mypy clean):**

- **Embedder still spawned a worker subprocess (the round-2 "fix" was incomplete).** `parallel=1` does NOT mean single-process in fastembed: any non-None value (with a batch ≥ `batch_size`) takes the worker-pool branch, which spawns a subprocess that loads its OWN model copy (~+0.6 GB) and is re-created on every `embed()` call (the pipeline calls embed once per 32-chunk batch, so a 5000-chunk session re-loads the model ~156 times). Measured: parallel=1 = 164 s / 2.4–3.1 GB vs parallel=None = 117 s / 1.8–2.4 GB on 320 realistic 500-token chunks. Fix: `parallel=None` (inline, model resident). ~40% faster, ~0.6 GB less, and removes a latent bootstrap crash for any non-import-safe caller. RSS plateaus with chunk count (50→1000 chunks: 2.4→2.7 GB), so it is the arena, not accumulation.
- **The real memory driver is the onnxruntime per-batch arena, not the pipeline.** It scales with `batch_size * sequence_length`. At the default 500-token chunk size: ~0.7 GB (batch=1), ~1.0 GB (batch=2), ~1.6 GB (batch=4), ~2.4 GB (batch=8). The user's 2.4 GB on a real 16 MB session matches batch=8. Lowered the default to **batch=4** (~1.6 GB). The pipeline's own structures are cheap: full_text (16 MB) + chunks (9.5 MB, capped at 5000) + vectors (30 MB) ≈ 55 MB total; `chunk_text` and `_join_messages` on 16 MB run in ~3–4 ms at <110 MB RSS. So accumulating `prepared_chunks` is NOT the problem; per-batch streaming would save ~tens of MB, not worth the complexity.
- **Redaction quadratic on a newline-free blob (the round-2 `_line_before` fix did not cover this path).** Round 2 clamped the returned slice length but left `rfind("\n", 0, pos)` searching from 0, which is O(pos) per match on a line with no newline. With thousands of 64-hex tokens on one line (single-line JSON array of git SHAs, `npm ls` output) the hex64 + entropy passes were O(n^2): a 2 MB single-line hex blob took **9.1 s** (0.5→1→2 MB = 0.7→2.4→9.1 s). Fix: bound the `rfind` search to the window (`rfind("\n", pos-160, pos)`). Now linear, 2 MB = ~0.8 s (11x). 0 correctness mismatches vs the old function over 2000 random inputs; regression test added.

**Confirmed solid (no change):**
- **The flock works.** Two concurrent `ingest-claude-code`: the second skips (exit 0, "Another Memex ingest is already running"); the lock releases on process exit (a third run acquires cleanly). `LOCK_EX | LOCK_NB` is fd-bound, so it releases even on crash.
- **The round-2 single-line high-entropy fix holds** (alphanumeric-token blob stays linear: 200 KB ≈ 76 ms). FTS rebuild is a server-side `INSERT ... SELECT` (no Python-side materialization). Summary generation truncates to 12 K chars; the lazy path loads ≤3 full conversation texts transiently (bounded, ~tens of MB) before truncating — minor, not a freeze risk.

**Next step:**
- None blocking. Optional: truncate the lazy-summary text in the SQL/repo layer to avoid the transient full-text load. Consider a future smarter chunker so denser content (code/JSON) does not inflate per-batch sequence length.

---

## 2026-06-11 (final security audit): 4-auditor sweep + hardening

Ran a final security audit with four parallel auditors (internet-facing connector, sensitive-data/redaction/injection, local surface + daemons + hook, and correctness). User's call on the headline finding: **acceso total + redacción reforzada** (keep Claude Code reachable from the remote connector, but make redaction much stronger), and then attack it adversarially in rounds.

**Findings and fixes applied this pass (416 tests green, ruff + format + mypy clean):**

- **Redaction reinforced (HIGH-1/2/3/4 + the ReDoS bug):** `redact.py` rewritten. Added vendor prefixes (Stripe, Twilio, SendGrid, npm, PyPI, Square, Google OAuth, GitHub fine-grained, Slack webhooks, more AWS prefixes, OpenSSH keys), more sensitive assignment names (dsn, database_url, connection_string, credential, mnemonic, seed/pass/recovery phrase, signing key), quoted-value-with-spaces handling, and a **high-entropy fallback** (Shannon ≥ 4.0, mixed charset, 32–100 chars, skips pure-hex/UUIDs and huge base64 blobs) for secrets with no known prefix. Fixed a **quadratic `url-credentials` regex** (an 180 KB no-whitespace blob went from ~6 s to ~3 ms) by bounding all quantifiers. Redaction now also runs in `content_renderer` **before** tool input/result truncation, so a secret straddling the cut is masked whole. Removed the `pwd:` false positive.
- **Ingest token leaked into a world-readable daemon log (HIGH-5):** `memex serve` now only echoes the token when stdout `isatty()`; under launchd it points users to `memex token`. Added `umask 077` to the daemon/hook scripts, `Umask` (077) to the launchd plists, and chmod'd the existing `data/*.log` to 0600. The leaked token is being rotated.
- **Allow-list revocation gap (MEDIUM-1):** `AllowlistGitHubProvider` now re-reads the allow-list from `.env` when its mtime changes, so removing a login takes effect without a daemon restart (fail-safe to the last good set; never drops to empty). GitHub-side revocation was already immediate.
- **TrustedHost ordering (LOW-2):** moved `TrustedHostMiddleware` to outermost via `app.add_middleware`, so a bad Host is rejected before the GitHub API call the auth backend would otherwise make.
- **Concurrency (LOW-4) + migration robustness (OBS-1):** `busy_timeout` 15s → 30s for the multi-writer setup; the CHECK migration now `DROP TABLE IF EXISTS conversations_new` + `CREATE TABLE IF NOT EXISTS` to survive a crashed prior run.
- **Docs (DOC-1/2/3):** README status header corrected (Phases 0–4 + 6 closed; PyPI 0.1.0 predates the new features); `install-service` on macOS now points to the shipped launchd one-liner instead of claiming launchd is unimplemented; stale comment in `install-autostart.sh` fixed.

**Confirmed solid by the auditors (no change):** remote auth chain has no bypass (JWT unforgeable, allow-list per request, claims not client-spoofable); local capture token is constant-time compared and 0600; hook stdin handling is injection-safe (quoted, parsed via stdlib json); `.env` is 0600 and not in git; DB/sidecars 0600 in a 0700 dir.

**Adversarial red-team round 1 (4 attackers) + fixes:**

Four red-teamers attacked the reinforced redaction (vendor formats, evasion/obfuscation, false-pos/neg + ReDoS) and the connector (second independent pass). They executed real attacks against `redact_secrets`, not theory. Redaction leaks found and fixed (redact.py rewritten again): pure-hex keys (Twilio/Datadog/Ethereum — entropy can never catch 64-hex, added a dedicated `\b(0x)?[0-9a-fA-F]{64}\b` rule gated on non-digest context); JSON quoted-key creds (`{"password": "x"}` — allow optional quotes around the assignment name); empty-user URLs (`redis://:pass@` — user segment `{0,256}`); single-charset random tokens (relaxed the 2-class gate to allow single-class at higher entropy ≥ 4.3); cleartext tail of >100-char secrets (token candidate upper bound 100 → 4096); more vendors (GitLab, Vault, Stripe webhook, DigitalOcean, New Relic, age, otpauth). False positives fixed (over-redaction kills search): removed `/` from the token charset and skip path-segment / digest-context tokens, so file paths, S3 object keys, git SHAs, and lockfile SRI hashes survive. A 32-entry MUST_REDACT / MUST_PRESERVE adversarial corpus (`test_redact_adversarial.py`) now guards both directions. No ReDoS (bounded quantifiers hold; ~4 MB/s linear).

Connector (second pass): no auth bypass, no exfiltration; TrustedHost-outermost and the allow-list reload both verified correct against fastmcp source. Fixes: allow-list now read DIRECTLY from `.env` (not `Settings()`, which an exported env var would shadow), with `(mtime_ns, size)` change detection and a warn if the env var is set; regression test asserting the OAuth consent screen stays enabled (the confused-deputy defense). 451 tests green.

**GitHub push protection gotcha:** the push was blocked (GH013) because the redaction tests held realistic fake secrets (Twilio SID, New Relic key) — secret-scanning matches by shape. Fix: build vendor-shaped test secrets from fragments joined with `+` (adjacent string literals get re-merged by ruff format, so `+` is required). Logged in CLAUDE.md.

**Red-team round 2 (4 attackers) + fixes (2026-06-11, after the pause):**

Re-attacked with the round-1 vectors (regression) + new ones. All round-1 fixes held. New findings, all fixed (467 tests green):

- **End-to-end (highest value, only visible tracing the full path):** the conversation TITLE was the one ingested `claude_code` field that bypassed redaction — `aiTitle` (model-generated, can quote a secret), the cwd-derived fallback, and from there the summarizer forwarded it to the Anthropic API and re-stored it in the retrievable `summary`. One fix: `title = redact_secrets(...)` in `parse_session_file` closes all three.
- **Performance O(n^2) I introduced in round 1:** `_line_before` did an unbounded back-scan per candidate token; a 199 KB single-line minified bundle took 11 s. Bounded the look-back to a 160-char window (markers sit right before the token) — 7300 ms → 28 ms, identical output. Regression test added.
- **New redaction leaks:** AWS secret access key (40 base64 chars with `/`, split by the no-`/` token charset) → dedicated `b64-key` rule gated on mixed-class + not-a-path; short secrets with a space-separated label ("token is X") → labeled-value rule lowering the floor only in that context; `alg:none` JWT (empty signature) → third segment `{0,..}`.
- **New false positives fixed:** long PascalCase/camelCase code identifiers (length-tiered entropy: 40+ alpha-only needs ≥4.6, NOT a flat bump that would leak 32-char alpha secrets); Go module `h1:` hashes (added to integrity markers).
- **Connector:** `.env` allow-list parser used `startswith` (a bug I introduced) — `..._OLD=evil` could hijack the allow-list; now exact-key match, last-wins, strips `export`/inline-comments. `search_chats` query length capped (parity with find_related). Bidi/zero-width control chars stripped from every tool result string in `_serialize` (defense-in-depth vs disguised injection). Consent-stays-enabled + reload tests confirmed meaningful.
- **Left as documented/deferred:** DCR `/register` disk-growth (LOW, bounded by disk + TrustedHost; third-party fastmcp, no cleanup hook); nonce-fenced per-field injection delimiters (the `_meta` note + bidi strip are the current mitigation; full fencing is a larger change for marginal gain on a model-level problem).

## 2026-06-12: RAM/CPU blowup fix (ingest was unusable for release)

The user reported two `python3.13` processes eating 20 GB and freezing the machine. Root-caused it: (1) fastembed/onnxruntime loads a model copy per worker (fastembed parallelizes by default) and grows a memory arena with a thread-per-core (10 cores here), so embedding 100 chunks measured **4.5 GB** in one process; (2) the 15-min schedule, the manual command, and the SessionEnd hook could all run an ingest at once, multiplying that. Embedding a single text was only 0.4 GB, so it was the batch + parallelism, not the model.

Fixes:
- `FastEmbedEmbedder` now passes `threads=MEMEX_EMBED_THREADS` (default 2) to `TextEmbedding` and `parallel=1` + `batch_size=MEMEX_EMBED_BATCH_SIZE` (default 8) to `.embed()`. Measured: 200 chunks **4.5 GB → 0.47 GB**, and it no longer pins all 10 cores.
- `memex ingest-claude-code` takes a non-blocking `fcntl.flock` (`data/ingest.lock`); a second concurrent ingest skips (the schedule retries in 15 min). Released automatically on process exit, even on crash. Best-effort no-op where `fcntl` is absent (Windows).
- New settings `MEMEX_EMBED_THREADS` / `MEMEX_EMBED_BATCH_SIZE` (documented in `.env.example`). 468 tests green.

Disabled the launchd ingest agent while diagnosing; re-enabled after the fix.

---

## 2026-06-12: red-team round 3 + process naming

Three attackers (verification + fresh). All round-1/2 fixes held. New fixes (476 tests green):

- **Redaction (round-3 findings):** Azure Storage key (88 base64 chars with `/`) — generalized the `b64-key` rule to 40-512 chars, gated strict (3 char classes + entropy ≥4.3 + not-a-path: ≤2 slashes, no leading `/`, no SRI/digest prefix) so it catches AWS/Azure keys without redacting file paths or S3 object URLs. Tightened the integrity-skip: only an ADJACENT SRI/Go prefix (`sha256-`/`h1:`, detected on the token itself since the token includes `-`) skips the entropy pass — free-text `base64 <secret>` / `integrity <secret>` no longer dodges redaction (attacker-controllable). Replaced the over-eager labeled-value floor (3.7) and added a structural CamelCase-identifier check (humps + low digit ratio) so engineering prose/codenames (`the secret WidgetFactory2025Prod`, `RequestMappingHandlerAdapter24X`) survive while random tokens are still redacted. Added Spanish labels (`contraseña`/`clave`/`secreto`) and PGP `PRIVATE KEY BLOCK`. A consolidated MUST_REDACT/MUST_PRESERVE corpus (rounds 1-3) guards both directions.
- **Injection (B1):** `_strip_control_chars` now strips by Unicode `Cf` category + the TAG block (U+E0000-E007F) instead of a hardcoded list — closes an invisible-instruction vector (TAG chars can encode hidden ASCII) the old list missed.
- **Process naming:** added `setproctitle` and `memex.proctitle.set_process_title`; `serve`/`serve-remote`/`ingest`/`stdio` now show as `Memex capture`/`Memex connector`/`Memex ingest`/`Memex mcp` in Activity Monitor instead of bare `python3.13` (transparency, the user asked for it).

Round-3 connector/e2e + perf attackers confirmed: title redaction holds, no other field bypasses redaction (gitBranch isn't even ingested), `.env` parser solid, flock correct, tool args bounded. Connector has no auth bypass across three rounds.

**PAUSED here (2026-06-11). Picking up next:**
- Rotate the GitHub OAuth client secret (user action: GitHub > Developer settings > OAuth Apps > regenerate; then update `.env` + restart the connector). The current secret appeared in audit logs.
- Re-pair the Chrome extension with the rotated ingest token `EaMGdsZcvv0RjuCaC-DQFztCx5wrV6eMgb653Gv5KJk` (or run `memex token`).
- **Red-team round 2** (the user explicitly asked to keep attacking, same methods + new ones, until nothing or almost nothing leaks): re-run the four attack angles against the reinforced redaction and connector; tighten anything new.
- Optional defense-in-depth still open: DCR `/register` rate-limit, provenance flag (`untrusted_origin`), injection sentinels around untrusted fields.
- Then: the Activity Monitor process-naming (`setproctitle` → "Memex capture/connector/ingest", verified to work on macOS) that the user requested — deferred until after the security work so the auditors saw stable code.
- State: Phases 0-6 closed + audited; 451 tests green; three launchd daemons running (serve, serve-remote, ingest), hook active.

---

## 2026-06-11 (always-on): launchd agents for serve + serve-remote

Made the two long-lived servers persistent on macOS so the user never has to start them by hand (they used to die with the Claude Code session that launched them). Added `scripts/serve-daemon.sh` and `scripts/serve-remote-daemon.sh` (resolve the repo from their own location, `cd` into it so the relative DB path and `.env` resolve, then `exec uv run memex serve` / `serve-remote`), plus two launchd plist templates with `RunAtLoad` + `KeepAlive` + `ProcessType Background`. Installed and verified on this Mac: `launchctl list | grep memex` shows all three agents (serve, serve-remote, ingest-claude-code) at status 0; `serve` answers `/health` 200, `serve-remote` answers `/mcp` 401 (auth required) both on loopback and through the public Funnel.

Clarified the user's confusion in the process: `serve`/`serve-remote` are for claude.ai (browser capture + the connector), not for Claude Code; the Claude Code ingest (hook + 15-min launchd) was already automatic and only works when there is something new. Idle cost of both servers is low because the embedder is lazy (no model loaded until a real ingest/search), which is why always-on is fine. The Tailscale Funnel is expected to persist across reboots via its `--bg` serve config (Tailscale re-applies it), pairing with the now-persistent `serve-remote`.

Now persistent: ingest (hook + schedule), serve, serve-remote. The `.env`, the installed plists, and `~/.claude/settings.json` are machine config (not committed); the scripts and templates are.

---

## 2026-06-11 (auto-sync): SessionEnd hook + periodic backstop for Claude Code

Made Claude Code / terminal ingestion automatic (it was manual). Two complementary mechanisms, both background + low priority so they never block or slow a session:

- **SessionEnd hook** (`scripts/session-end-hook.sh`): Claude Code passes the closed session's `transcript_path` on stdin; the script ingests just that one session, detached (`nohup nice -n 19 ... &`), and returns 0 immediately. Confirmed against the docs (via claude-code-guide) that SessionEnd cannot block and a backgrounded command is the right pattern, and that the hook fires for both the terminal CLI and the VS Code extension (both write `.jsonl` under `~/.claude/projects`). Catches the "user says the important thing then closes" case with no delay.
- **Periodic backstop** (`scripts/scheduled-ingest.sh` + a launchd plist template, 15-min `StartInterval`, `ProcessType Background`): a full incremental scan that picks up anything the hook missed (crash, etc.).

**Resource fix (the user's main worry):** the old ingest loaded the fastembed model (hundreds of MB, ~1-2s) every run, even when nothing changed. Added `LazyEmbedder` (`core/embeddings/lazy.py`) wrapping the factory and building the real backend only on the first non-empty `embed()`. The pipeline only embeds when a session actually produced new chunks, and `embed([])` is a no-op, so an all-skipped scan never loads the model. Verified: re-scan of an unchanged corpus leaves `LazyEmbedder.loaded is False`. Also added single-file ingest to `ingest_claude_code_sessions` (pass a `.jsonl` path) so the hook embeds one session, not 432. CLI now builds `LazyEmbedder(get_default_embedder)` and no longer forces a load just to print the model name.

Installed on this Mac and validated end to end: the hook returns 0 instantly and ingests in the background (re-ingesting an already-indexed session → skipped, model not loaded); the launchd agent is loaded (`launchctl list | grep memex`) and ran at load. 5 new tests (lazy embedder + single-file + no-load-on-unchanged); **401 green**, ruff + format + mypy clean.

Honesty note: the hook script is bash, so the hook half is macOS/Linux for now; Windows needs a PowerShell equivalent (documented as not-yet-included; Windows users can still schedule the scan via Task Scheduler). `~/.claude/settings.json` and the installed plist are the user's machine config, not committed; the scripts and the plist template are.

---

## 2026-06-11 (install docs): one-command installers + per-OS install section

Rewrote the README install section to be per-OS (macOS / Linux / Windows / manual) and added `scripts/install.sh` + `scripts/install.ps1`: each installs uv (Astral's official installer, verified against docs.astral.sh) if missing, runs `uv sync` (which fetches the pinned Python 3.13, so no system Python is needed), and verifies with `memex doctor`. That is the closest honest thing to "one command" without shipping a binary: a click-installer (.dmg/.exe) is not feasible without heavy, fragile packaging (fastembed/onnxruntime/sqlite-vec), so it was not promised.

Two honesty notes baked into the docs:
- **PyPI is still 0.1.0**, which predates Phase 4 (claude.ai connector) and Phase 6 (Claude Code ingestion). The README now steers users to install from source for those features, with a note, instead of the old "install from PyPI (recommended)" that would hand them a stale build. Publishing a new release needs the maintainer's PyPI token (a bump + `uv build` + `uv publish`), tracked for later.
- **`install.sh` was tested on this macOS box** (uv detected, `uv sync`, `memex doctor` → Python/DB/embedder OK). **`install.ps1` was written from the verified official uv Windows command but NOT run on a Windows machine** (none available here); it mirrors the shell script step for step and needs a real-Windows smoke test before being called proven.

Ollama stays documented as optional (fastembed is the zero-config default); the install section no longer implies it is required.

---

## 2026-06-11 (later): Phase 6, Claude Code / terminal ingestion (CLOSED)

Same day, second push. The user wanted "one brain in different forms": claude.ai, Claude Code, and the terminal all searchable from anywhere. claude.ai → Memex already existed (export + live capture + the Phase 4 remote connector). The missing half was Claude Code / terminal → Memex. It had been parked as out-of-scope (deferring to Claude Historian), but the user explicitly prioritized a single unified store, so it moved in scope as Phase 6.

**Format study (real logs):** each session is `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`, one event per line. Relevant types: `user` (content is a str, or a list when it carries a `tool_result`), `assistant` (content is a list of `text`/`thinking`/`tool_use` blocks), `ai-title` (the `aiTitle`). Every line also has `cwd`, `timestamp`, `gitBranch`, `isMeta`, `isSidechain`. Across a 60-file sample: 1108 `isSidechain` lines (sub-agent noise) — confirms dropping them matters.

**User's three choices (all "go with your recommendation"):** index prompts + replies + tool markers, drop my `thinking` (volume/noise); no sensitive folders to exclude; drop sub-agent side threads.

**What was built (reuses a lot):**
- `Source.CLAUDE_CODE` in the model. The `conversations.source` had a CHECK constraint listing the three old sources; SQLite cannot ALTER a CHECK in place, so `_migrate_conversations_source_check` (in `storage/db.py`) recreates the table following SQLite's table-redefinition procedure: data + indexes preserved, copy is column-intersection so very old DBs without `ingested_at` still migrate, FKs toggled OFF for the swap. Idempotent (checks the live DDL for `claude_code`). Verified on the real prod DB: 97 convs intact, CHECK widened.
- `core/ingest/claude_code.py`: `parse_session_file` → `ParsedSession(conversation, messages, cwd)`. Reuses `content_renderer` verbatim, so `thinking` is dropped for free (unknown block type) and tool calls render as the same `[tool_use]`/`[result]` markers as the export. Filters `isMeta`, `isSidechain`, and harness plumbing (slash-command echoes, bash wrappers). Malformed lines skipped, not fatal.
- `ingest_claude_code_sessions` in the pipeline, driving the shared `_ingest_conversation`. Added two reusable knobs to that helper: `skip_unchanged` (short-circuit on matching `content_hash` before any embed → cheap incremental re-scan over hundreds of files) and `extra_repo_keys` (associate a session to the registered repo of its `cwd`, resolved via `resolve_repo_key`, confidence 1.0 — a stronger signal than text matching, and free because every line carries `cwd`). New `IngestSummary.skipped_unchanged_conversations`.
- CLI `memex ingest-claude-code [--path] [--db]`.
- Tests: 10 parser + 5 pipeline + 1 migration. Suite **379 green**, ruff + format + mypy clean.

**Gotcha logged (CLAUDE.md):** `PRAGMA foreign_keys` is a no-op inside a transaction. The first cut of the migration toggled it OFF while an implicit transaction was open, so FKs stayed ON and the table swap hit "no such table" against a forward-referenced parent. Fix: `conn.commit()` before toggling.

**Phase-close audit (two parallel auditors: security/privacy + correctness/docs).**

Correctness found one **shipping-blocker bug**: the marquee cwd→repo association did not fire for any repo with a git remote. Such a repo is keyed by the remote URL (`canonical_repo_key` prefers it), but `resolve_repo_key` only ever looked up by `key`, never by the `repos.path` column, so a working directory never matched. The test missed it because it registered the repo with `remote_url=None` (path-keyed, coincidentally working). Fix: added `repo.get_repo_by_path` and a 4th strategy in `resolve_repo_key` (match the normalized path against the `path` column); regression test with a real `git@github.com:...` remote that fails without the fix. Also fixed: `rglob` symlink containment (skip files resolving outside the scan root), derived title skips a leading `tool_result` line, min/max timestamps instead of first/last, stale `foreign_key_check` mention in the migration docstring, and the `source` tool docstring now lists `claude_code`.

Security found one **HIGH by-design risk**: the remote claude.ai connector now exposes the full local terminal history (commands, file contents, paths, and any secrets that appear in them) with no redaction; secrets that were never really "a conversation" (a third party's token in tool output) could leave the box on a remote search. The user chose **redact-secrets, full access**. Added `core/ingest/redact.py`: masks API keys (Anthropic/OpenAI/GitHub/Slack/Google/AWS), JWTs, PEM private-key blocks, `KEY=`/`SECRET=` assignments, `Bearer` tokens, and URL-embedded passwords as `[REDACTED:...]`, applied on the `claude_code` path before storage/embedding; `raw_content` is no longer persisted for this source (would store the unredacted blocks). Other security findings: the indirect-prompt-injection `_meta.untrusted_content` envelope already covers the new source for free (source-agnostic); CHECK migration verified correct (data + CASCADE children + FK toggle, f-string `col_csv` not injectable since it comes from `pragma_table_info` ∩ a hardcoded literal). Lessons logged in CLAUDE.md.

After the fixes: **396 unit tests green**, ruff + format + mypy clean. The partial bulk ingest (13 sessions) from before the audit was dropped and re-run clean with redaction + the association fix; unified search validated (one query returning both claude.ai and Claude Code hits).

**Phase 6 CLOSED.** Keep-fresh automation (launchd scan or a Claude Code SessionEnd hook) is deferred; MVP is the manual `memex ingest-claude-code`.

---

## 2026-06-11: Phase 4, remote MCP transport (CLOSED)

Goal of the phase: claude.ai consumes Memex as a remote MCP custom connector. Before coding, verified the current connector requirements against the official docs (support.claude.com + claude.com/docs/connectors, checked 2026-06): custom connectors exist on **all consumer plans** (Free is capped at one), one connector serves claude.ai web + Desktop + mobile, transport is **Streamable HTTP** (HTTP+SSE is deprecated), and the connection **originates from Anthropic's cloud**, never from the user's device. That settles the two open design questions from the Phase 1 notes:

- **Reachability:** the server needs a public HTTPS URL with a public IPv4 `A` record; loopback/private IPs are rejected at DNS validation. So: tunnel, non-negotiable. Chosen: **Tailscale Funnel** (stable URL, automatic TLS, free, no domain needed). Memex only cares about the resulting URL; ngrok/Cloudflare work identically.
- **Auth:** claude.ai supports exactly two modes for custom connectors: authless or full OAuth 2.0 with dynamic client registration (PKCE S256). There is **no UI field for a bearer token**, and tokens in the query string are prohibited by the MCP auth spec. The ROADMAP's original "local token in header" idea is therefore impossible for this client. Chosen: **OAuth proxy over a GitHub OAuth App** (FastMCP 3.x `GitHubProvider`, which implements DCR/CIMD + consent + token swap), narrowed by `AllowlistGitHubProvider`: `verify_token` re-checks the upstream GitHub `login` claim against `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS` on **every request** and fails closed. The OAuth dance succeeds for any GitHub account (GitHub does not know the allow-list), but a non-allowed user's token dies at verification, so they never reach a tool. Revoking the app on GitHub locks out immediately because the proxy validates the upstream token per request.

**What was built:**

- **Refactor first:** the FastMCP server + the 4 tool wrappers moved from `transports/stdio.py` to a shared `transports/mcp_server.py` with a `build_server(auth=None)` factory. `stdio.py` is now a thin entrypoint (logging-to-stderr + `server.run()`); `memex-mcp` and existing `.mcp.json` configs are untouched. `tests/unit/test_stdio_server.py` renamed to `test_mcp_server.py`, pointing at the shared module and additionally asserting the stdio entrypoint still serves the 4 tools.
- **`transports/http.py`:** `build_remote_app()` validates config (reports every missing var at once, refuses plain `http://`, refuses an empty allow-list), builds the allow-list provider, and mounts `build_server(auth=...)` via `server.http_app()` at `/mcp` with `TrustedHostMiddleware` pinned to the funnel hostname + loopback (same DNS-rebinding posture as `http_ingest.py`). The injection envelope lives in the shared `tools.py`, so remote results carry the same `_meta.untrusted_content` marker as stdio.
- **CLI `memex serve-remote`:** binds loopback (the tunnel does the public part), warns on non-loopback binds, prints the public `/mcp` URL and the allow-list. New settings in `config.py` + `.env.example`: `MEMEX_REMOTE_BASE_URL`, `MEMEX_REMOTE_PORT` (default 8377), `MEMEX_GITHUB_CLIENT_ID/SECRET`, `MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS`.
- **Restart behavior verified in the FastMCP source:** the OAuth proxy persists client registrations and token mappings Fernet-encrypted on disk, keyed deterministically from the client secret, and derives the JWT signing key from the same secret, so `serve-remote` restarts do not invalidate the claude.ai connection.
- **Tests:** 14 new in `tests/unit/test_http_remote.py` (config fail-closed matrix, allow-list accept/reject/missing-claim/invalid-upstream, app construction + Host pinning, CLI exit code on bad config). Suite: **362 unit tests green**, ruff + format + mypy clean.

Also today: registered Memex in Claude Code at user scope on the Mac (`claude mcp add --scope user memex -- uv run --directory ~/Dionisio/memex memex-mcp`, health check green).

**End-to-end validation (same day):** populated the Mac DB from a fresh official export (2 projects, 96 conversations, 1607 messages, 1064 chunks, 0 errors). Brought up Tailscale Funnel (`https://dionisios-macbook-air.tail2a5fa8.ts.net` → loopback 8377), created the GitHub OAuth App (callback `/auth/callback`), filled `.env`, started `memex serve-remote`. Confirmed `/mcp` returns 401 unauthenticated both locally and through the Funnel, and the `/.well-known/oauth-protected-resource/mcp` metadata is served publicly. Added the connector in claude.ai, completed the GitHub OAuth dance, and `search_chats("transnova")` from a real claude.ai chat returned real indexed history. Goal of the project (the context claude.ai has is now also reachable the other way: Memex feeds claude.ai) demonstrated live.

**Phase-close audit (two parallel auditors):**
- *Security (attacker mindset over the internet-exposed surface):* no critical/high. The auditor traced the full request path through the installed fastmcp 3.3.1 source and confirmed: the allow-list runs on **every** request (Starlette `AuthenticationMiddleware` → `BearerAuthBackend` → our `verify_token`), the `login` claim is set live from `api.github.com/user` keyed by the server-held upstream token (not client-spoofable, JWT `upstream_claims` cannot override top-level `login`), verification caching is disabled by default (immediate revocation), and there is no fail-open path (empty allow-list aborts startup; the only authless server is local stdio). One LOW worth acting on (LOW-1): username reuse/rename on GitHub means a `login`-only allow-list could grant access to a future holder of the handle.
- *Correctness/dead-code/docs:* no bugs. Refactor is behavior-preserving (singletons aligned, `server.tool(fn, run_in_thread=False)` loop registers identically to the old decorator, verified description + param schema). Docs consistent (port 8377, `/auth/callback`, setting names match `config.py` aliases). Two nitpicks: stale `stdio.py` references in docstrings, and a fragile `__mro__[1]` test patch.

**Fixes applied at close (363 tests green, ruff + format + mypy clean):**
- LOW-1: `AllowlistGitHubProvider` now matches an allow-list entry against the username (`login`) OR the immutable numeric account id (`sub`); param renamed `allowed_logins`→`allowed`. New test `test_allowed_numeric_id_passes`. `.env.example` documents the id option. Lesson logged in CLAUDE.md.
- Stale docstrings in `tools.py` and `test_tools.py` updated from `stdio.py` to `mcp_server.py`.
- Replaced `__mro__[1]` patching with a `patch_upstream` helper that patches `GitHubProvider.verify_token` directly. Lesson logged in CLAUDE.md.

**Phase 4 CLOSED.** Operational caveat: the connector responds only while the Mac is on, the Funnel is up, and `memex serve-remote` is running. Persistent service (macOS launchd) stays a 0.2.0 item; for now it is started manually.

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
