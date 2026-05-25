"""Local HTTP server that receives payloads from the Chrome ext and ingests them.

Boots a Starlette app on `127.0.0.1:PORT` (default 5777). Accepts requests
only from a browser extension (Origin `chrome-extension://...` or
`moz-extension://...`). Expected payload is the JSON returned by the
Claude.ai API at `chat_conversations/{id}?tree=True`, which has the same
shape as an item in `conversations.json` from the official export.

Endpoints:
- `GET /health`: ping. Returns `{"status": "ok"}`.
- `POST /ingest/conversation`: receives the JSON, parses it with
  `parse_conversation_dict`, ingests via `ingest_single_conversation`,
  returns counts.

Lifecycle:
- SQLite connections and the embedder are created lazily (one process for
  the whole server, one connection, one embedder).
- SQLite + WAL mode lets another process (the MCP server) read the same DB
  concurrently.

Security:
- Listens on 127.0.0.1 by default (not network-accessible).
- Origin check: rejects requests that do not come from an extension.
- Shape validation: if `uuid`/`created_at`/`updated_at` is missing, returns 400.
- No telemetry, no logging of payload contents.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from memex.core.embeddings import Embedder, EmbedderError, get_default_embedder
from memex.core.ingest.pipeline import ingest_single_conversation
from memex.core.models import Source
from memex.core.storage.db import connect_and_init

logger = logging.getLogger("memex.http_ingest")

# Lazy globals. Tests overwrite these to inject mocks or an in-memory DB.
_conn: sqlite3.Connection | None = None
_embedder: Embedder | None = None

# Only accept requests originating from a browser extension. Prevents any
# visited web page from talking to the local endpoint.
_ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")


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


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    return any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES)


async def health(request: Request) -> JSONResponse:
    """Ping endpoint. The Chrome ext uses it to detect if the server is alive."""
    return JSONResponse({"status": "ok", "service": "memex-ingest"})


async def ingest_conversation_endpoint(request: Request) -> JSONResponse:
    """Receive a conversation payload from the Chrome ext and ingest it."""
    if not _origin_allowed(request):
        logger.warning(
            "Request rejected: Origin '%s' not allowed",
            request.headers.get("origin", "<missing>"),
        )
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)

    try:
        payload: Any = await request.json()
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
        summary = ingest_single_conversation(
            _get_conn(),
            _get_embedder(),
            payload,
            source=source,
        )
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

    return JSONResponse(
        {
            "status": "ok",
            "uuid": payload.get("uuid"),
            "conversations": summary.conversations,
            "messages": summary.messages,
            "chunks": summary.chunks,
            "skipped_empty_messages": summary.skipped_empty_messages,
        }
    )


def build_app() -> Starlette:
    """Factory for the Starlette app. Tests use it to reuse the instance."""
    return Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route(
                "/ingest/conversation",
                ingest_conversation_endpoint,
                methods=["POST"],
            ),
        ],
    )


app = build_app()
