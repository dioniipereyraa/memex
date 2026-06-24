"""Local HTTP server that receives payloads from the Chrome ext and ingests them.

Boots a Starlette app on `127.0.0.1:PORT` (default 5777). Expected payload is
the JSON returned by the Claude.ai API at `chat_conversations/{id}?tree=True`,
which has the same shape as an item in `conversations.json` from the official
export.

Endpoints:
- `GET /health`: ping. Returns `{"status": "ok"}` (no product fingerprint).
- `POST /ingest/conversation`: receives the JSON, parses it with
  `parse_conversation_dict`, ingests via `ingest_single_conversation`,
  returns counts.
- `POST /ingest/plan`: takes the claude.ai conversation manifest
  (`{"conversations": [{uuid, updated_at}, ...]}`) and returns the
  `{"toFetch": [...]}` subset that is new or changed, so the extension's
  backfill skips already-indexed chats. Same Origin + token auth as ingest.

Lifecycle:
- SQLite connections and the embedder are created lazily (one process for
  the whole server, one connection, one embedder).
- SQLite + WAL mode lets another process (the MCP server) read the same DB
  concurrently. Writes from both processes are serialized by SQLite's write
  lock + busy timeout (set in `storage/db.py`).

Security model (this is a LOCAL, single-user service):
- Binds to 127.0.0.1 by default. `TrustedHostMiddleware` pins the Host header
  to the loopback allow-list, so a DNS-rebinding request from a visited
  webpage is rejected at the framework level.
- `/ingest` requires BOTH an extension Origin AND a per-install access token
  (`X-Memex-Token`). The Origin prefix check blocks browser-page script; the
  token authenticates the caller, because the Origin header is forgeable by any
  non-browser local process. The token is generated on first use, stored
  user-only (0600) next to the DB, and shown by `memex serve` / `memex token`
  for pairing in the extension popup.
- The request body is capped (`MEMEX_INGEST_MAX_BODY_BYTES`) and rejected with
  413 before being buffered/parsed (memory-exhaustion DoS).
- No telemetry, no logging of payload contents.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from memex import ingest_lock
from memex.config import settings
from memex.core.embeddings import Embedder, EmbedderError, get_default_embedder
from memex.core.ingest.pipeline import ingest_single_conversation
from memex.core.models import Source
from memex.core.storage.db import connect_and_init

logger = logging.getLogger("memex.http_ingest")

# Lazy globals. Tests overwrite these to inject mocks or an in-memory DB.
_conn: sqlite3.Connection | None = None
_embedder: Embedder | None = None
# Per-install access token. Loaded lazily from disk (or generated). Tests set
# it directly to avoid filesystem I/O.
_token: str | None = None

# Only accept requests originating from a browser extension. Defense in depth
# on top of the token: blocks visited web pages (browsers force a truthful
# Origin) but NOT other local processes (which can forge it), hence the token.
_ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")

# Case-insensitive header carrying the per-install token.
_TOKEN_HEADER = "x-memex-token"

# Hard cap on the number of conversations accepted in one /ingest/plan request.
# The byte cap bounds the payload size, but 16 MB of tiny `{"uuid":...}` objects
# is ~1M items; without a count cap a token-holding caller could force a
# full-table scan plus millions of timestamp parses on the event loop. A real
# claude.ai org has at most a few thousand chats, so 50k is generous.
_MAX_PLAN_ITEMS = 50_000

# Secrets the short-lived ingest worker never needs. Stripped from its
# environment (defense in depth: the child must not carry the parent's API
# keys / OAuth secret, which only the main process uses).
_WORKER_ENV_DENY = frozenset(
    {"ANTHROPIC_API_KEY", "MEMEX_GITHUB_CLIENT_SECRET", "MEMEX_GITHUB_CLIENT_ID"}
)


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # check_same_thread=False because uvicorn runs sync code in a threadpool.
        # Today's async handlers run on the event loop and their SQLite queries
        # run inline, so a single conn sees a single thread. If background
        # tasks ever touch `conn`, we must serialize them explicitly (mutex or
        # queue).
        _conn = connect_and_init(check_same_thread=False)
        logger.info("DB opened for http_ingest")
    return _conn


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = get_default_embedder()
        logger.info("Embedder initialized: %s", _embedder.model_name)
    return _embedder


# Serializes this server's own capture workers (concurrent POSTs, e.g. a
# backfill burst or several claude.ai tabs) so they do not each spawn a worker
# and stack a ~0.5 GB model load. Created lazily to bind to the running loop.
_ingest_serialize_lock: asyncio.Lock | None = None


def _get_serialize_lock() -> asyncio.Lock:
    global _ingest_serialize_lock
    if _ingest_serialize_lock is None:
        _ingest_serialize_lock = asyncio.Lock()
    return _ingest_serialize_lock


async def _ingest_in_subprocess(payload: dict[str, Any], source: Source) -> dict[str, int]:
    """Ingest one conversation in a short-lived child process, then it exits.

    Keeps the always-on capture server from holding the embedding model (~0.5 GB)
    resident: the child loads the model, embeds, stores, and exits, so the OS
    reclaims everything. The payload goes to the child over stdin (not a temp
    file, so nothing sensitive touches disk); the summary comes back as one JSON
    line on stdout. The DB path is passed absolute via the environment so the
    child does not depend on the working directory.

    Single-flight on two fronts so at most one embedding model is resident at a
    time: an in-process `asyncio.Lock` serializes this server's own workers, and
    the cross-process `ingest.lock` (shared with the `ingest-claude-code` CLI /
    schedule / hook) is taken blocking-with-timeout so a live capture WAITS for
    an in-flight ingest instead of stacking a second load (and never hangs).
    """
    async with _get_serialize_lock():
        lock_handle = await asyncio.to_thread(ingest_lock.acquire_blocking)
        if lock_handle is None:
            # A long-running ingest still holds the lock past the timeout. Drop
            # the live chat rather than risk a stacked model load; the extension
            # re-sends and the incremental backfill re-fetches it later.
            logger.warning("ingest lock busy past timeout; skipping this capture")
            raise EmbedderError("ingest lock busy; capture deferred")
        try:
            return await _spawn_ingest_worker(payload, source)
        finally:
            ingest_lock.release(lock_handle)


async def _spawn_ingest_worker(payload: dict[str, Any], source: Source) -> dict[str, int]:
    env = {k: v for k, v in os.environ.items() if k not in _WORKER_ENV_DENY}
    env["MEMEX_DB_PATH"] = str(Path(settings.db_path).resolve())
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "memex.transports.ingest_worker",
        source.value,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate(json.dumps(payload).encode())
    if proc.returncode != 0:
        # Keep the worker's stderr (it may carry the absolute DB path or other
        # internals) in the server log only; the client gets the return code,
        # not the detail.
        detail = err.decode(errors="replace").strip()[-500:]
        logger.warning("ingest worker exited %s: %s", proc.returncode, detail)
        raise EmbedderError(f"ingest worker exited {proc.returncode}")
    lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise EmbedderError("ingest worker produced no output")
    try:
        result: dict[str, int] = json.loads(lines[-1])
    except ValueError as e:
        raise EmbedderError(f"ingest worker bad output: {e}") from e
    return result


def load_or_create_ingest_token(path: Path | str | None = None) -> str:
    """Return the per-install access token, creating it if it does not exist.

    Stored as a single line in `settings.ingest_token_path` (next to the DB),
    written user-only (0600). If the file cannot be persisted (read-only FS,
    odd permissions), falls back to an ephemeral in-process token and warns;
    pairing would then need `memex token` to read the live value.
    """
    target = Path(path) if path is not None else settings.ingest_token_path
    with contextlib.suppress(OSError):
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    try:
        parent_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            with contextlib.suppress(OSError):
                os.chmod(target.parent, 0o700)
        # Create with 0600 from the start (avoid a window where it is readable).
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except OSError:
        logger.warning(
            "Could not persist ingest token to %s; using an ephemeral token. "
            "Live capture pairing will not survive a restart.",
            target,
        )
    return token


def _get_token() -> str:
    global _token
    if _token is None:
        _token = load_or_create_ingest_token()
    return _token


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    return any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES)


def _token_valid(request: Request) -> bool:
    provided = request.headers.get(_TOKEN_HEADER, "")
    if not provided:
        return False
    # Constant-time compare so a wrong token cannot be brute-forced by timing.
    return hmac.compare_digest(provided, _get_token())


async def health(request: Request) -> JSONResponse:
    """Ping endpoint. The Chrome ext uses it to detect if the server is alive.

    Returns a minimal body with no product/service name so it is not a clean
    fingerprint oracle for an arbitrary caller.
    """
    return JSONResponse({"status": "ok"})


async def _read_body_capped(request: Request, max_bytes: int) -> bytes | None:
    """Read the request body, returning None if it exceeds `max_bytes`.

    Checks Content-Length up front (cheap reject) and then streams with a
    running byte counter so a missing/chunked length cannot bypass the cap.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        with contextlib.suppress(ValueError):
            if int(content_length) > max_bytes:
                return None

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


async def ingest_conversation_endpoint(request: Request) -> JSONResponse:
    """Receive a conversation payload from the Chrome ext and ingest it."""
    if not _origin_allowed(request):
        logger.warning(
            "Request rejected: Origin '%s' not allowed",
            request.headers.get("origin", "<missing>"),
        )
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)

    if not _token_valid(request):
        logger.warning("Request rejected: missing or invalid X-Memex-Token")
        return JSONResponse(
            {"error": "Missing or invalid access token. Pair the extension with `memex token`."},
            status_code=401,
        )

    max_bytes = settings.ingest_max_body_bytes
    raw = await _read_body_capped(request, max_bytes)
    if raw is None:
        return JSONResponse(
            {"error": f"Payload too large (max {max_bytes} bytes)."},
            status_code=413,
        )

    try:
        payload: Any = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "Body is not valid JSON"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "Payload must be a JSON object containing one chat"},
            status_code=400,
        )

    # `source` is optional in the query string to force 'design_chat'. Default
    # is 'conversations' (a standalone user chat).
    source_param = request.query_params.get("source", "conversations")
    try:
        source = Source(source_param)
    except ValueError:
        return JSONResponse(
            {"error": f"Invalid source: {source_param!r}"},
            status_code=400,
        )
    if source is Source.MEMORY:
        return JSONResponse(
            {"error": "source=memory is not supported via live capture"},
            status_code=400,
        )

    try:
        if settings.ingest_embed_in_subprocess:
            # Embed in a child process that exits, so the always-on server does
            # not hold the model resident. See `_ingest_in_subprocess`.
            counts = await _ingest_in_subprocess(payload, source)
        else:
            # In-process path (lower per-capture latency, higher steady RSS).
            # Tests use this with an injected DB + embedder.
            summary = ingest_single_conversation(
                _get_conn(),
                _get_embedder(),
                payload,
                source=source,
            )
            counts = {
                "conversations": summary.conversations,
                "messages": summary.messages,
                "chunks": summary.chunks,
                "skipped_empty_messages": summary.skipped_empty_messages,
            }
    except EmbedderError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except KeyError as e:
        return JSONResponse(
            {"error": f"Missing required field in payload: {e}"},
            status_code=400,
        )
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"Malformed payload: {e}"}, status_code=400)
    except Exception as e:
        logger.exception("Unexpected error in ingest_conversation")
        return JSONResponse(
            {"error": f"Internal error: {type(e).__name__}"},
            status_code=500,
        )

    return JSONResponse({"status": "ok", "uuid": payload.get("uuid"), **counts})


def _instant(value: object) -> float:
    """Parse a claude.ai ISO timestamp to epoch seconds (-inf if unparseable).

    The ingest normalizes fractional seconds (`.000Z` -> `.000000Z`), so a raw
    string compare would be wrong; compare by instant. An unparseable value
    sorts oldest, so the conversation is re-fetched (fail safe toward keeping the
    index complete rather than skipping a possibly-changed chat).
    """
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


async def backfill_plan_endpoint(request: Request) -> JSONResponse:
    """Return the subset of a conversation manifest that is new or changed.

    POST body `{"conversations": [{"uuid", "updated_at"}, ...]}` (the claude.ai
    list). Compares each item's `updated_at` against what is indexed and returns
    `{"toFetch": [uuid, ...]}` for the new or changed ones, so the extension's
    backfill skips conversations already stored unchanged. The indexed set is
    computed and compared here and never leaves the server.

    Same auth as `/ingest` (Origin + token): it reveals which conversations are
    indexed, so it must not be reachable by a visited web page or an unpaired
    local process. `claude_code` sessions are excluded (backfill is claude.ai
    only). A POST is used (not GET) so the request reliably carries the
    extension Origin, matching the proven `/ingest/conversation` path.
    """
    if not _origin_allowed(request):
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)
    if not _token_valid(request):
        return JSONResponse({"error": "Missing or invalid access token."}, status_code=401)

    raw = await _read_body_capped(request, settings.ingest_max_body_bytes)
    if raw is None:
        return JSONResponse({"error": "Payload too large."}, status_code=413)
    try:
        body: Any = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "Body is not valid JSON"}, status_code=400)
    conversations = body.get("conversations") if isinstance(body, dict) else None
    if not isinstance(conversations, list):
        return JSONResponse(
            {"error": "Expected {'conversations': [{uuid, updated_at}, ...]}"},
            status_code=400,
        )
    if len(conversations) > _MAX_PLAN_ITEMS:
        return JSONResponse(
            {"error": f"Too many conversations in one plan request (max {_MAX_PLAN_ITEMS})."},
            status_code=413,
        )
    if not conversations:
        # Nothing to compare; skip the full-table scan entirely.
        return JSONResponse({"toFetch": []})

    conn = _get_conn()
    rows = conn.execute(
        "SELECT uuid, updated_at FROM conversations WHERE source != ?",
        (Source.CLAUDE_CODE.value,),
    ).fetchall()
    known = {row["uuid"]: row["updated_at"] for row in rows}

    to_fetch: list[str] = []
    seen: set[str] = set()
    for item in conversations:
        if not isinstance(item, dict):
            continue
        uuid = item.get("uuid")
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        stored = known.get(uuid)
        if stored is None or _instant(stored) < _instant(item.get("updated_at")):
            to_fetch.append(uuid)
    return JSONResponse({"toFetch": to_fetch})


def _allowed_hosts() -> list[str]:
    hosts = [h.strip() for h in settings.ingest_allowed_hosts.split(",") if h.strip()]
    return hosts or ["127.0.0.1", "localhost"]


def build_app() -> Starlette:
    """Factory for the Starlette app. Tests use it to reuse the instance."""
    return Starlette(
        debug=False,
        # Pin the Host header to loopback (DNS-rebinding defense). The
        # middleware strips the port before matching, so list bare hosts.
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())],
        routes=[
            Route("/health", health, methods=["GET"]),
            Route(
                "/ingest/conversation",
                ingest_conversation_endpoint,
                methods=["POST"],
            ),
            Route("/ingest/plan", backfill_plan_endpoint, methods=["POST"]),
        ],
    )


app = build_app()
