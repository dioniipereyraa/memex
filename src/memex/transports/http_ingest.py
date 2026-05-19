"""HTTP server local que recibe payloads de la Chrome ext y los ingesta.

Levanta un Starlette en `127.0.0.1:PORT` (default 5777). Solo acepta requests
desde una extensión de browser (Origin `chrome-extension://...` o
`moz-extension://...`). El payload esperado es el JSON tal cual lo devuelve el
API de Claude.ai en `chat_conversations/{id}?tree=True`, que tiene el mismo
shape que un item de `conversations.json` del export oficial.

Endpoints:
- `GET /health`: ping. Devuelve `{"status": "ok"}`.
- `POST /ingest/conversation`: recibe el JSON, lo parsea con
  `parse_conversation_dict`, lo ingesta vía `ingest_single_conversation`,
  devuelve counts.

Lifecycle:
- Las conexiones SQLite y el embedder se crean lazy (un solo proceso para todo
  el server, una sola conn y un solo embedder).
- SQLite + WAL mode permite que otro proceso (el MCP server) lea de la misma
  base concurrentemente.

Seguridad:
- Listen solo en 127.0.0.1 por default (no accesible de la red).
- Origin check: rechaza requests que no vengan de una extensión.
- Validación de shape: si falta `uuid`/`created_at`/`updated_at`, devuelve 400.
- Sin telemetría, sin logging del contenido del payload.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from memex.core.embeddings.base import Embedder, EmbedderError
from memex.core.embeddings.ollama import OllamaEmbedder
from memex.core.ingest.pipeline import ingest_single_conversation
from memex.core.models import Source
from memex.core.storage.db import connect_and_init

logger = logging.getLogger("memex.http_ingest")

# Globals lazy. Tests sobrescriben estos para inyectar mocks o DB in-memory.
_conn: sqlite3.Connection | None = None
_embedder: Embedder | None = None

# Solo aceptamos requests originadas en una extensión de browser. Evita que
# cualquier página web visitada pueda hablarle al endpoint local.
_ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # check_same_thread=False: Starlette + uvicorn corren handlers en thread
        # pool. Asegurate de no ejecutar queries concurrentes sobre la misma
        # conn (el async-event-loop ya las serializa en producción).
        _conn = connect_and_init(check_same_thread=False)
        logger.info("DB abierta para http_ingest")
    return _conn


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = OllamaEmbedder()
        logger.info("Embedder inicializado: %s", _embedder.model_name)
    return _embedder


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    return any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES)


async def health(request: Request) -> JSONResponse:
    """Ping endpoint. La Chrome ext lo usa para detectar si el server está vivo."""
    return JSONResponse({"status": "ok", "service": "memex-ingest"})


async def ingest_conversation_endpoint(request: Request) -> JSONResponse:
    """Recibe un payload de conversación de la Chrome ext, lo ingesta."""
    if not _origin_allowed(request):
        logger.warning(
            "Request rechazada: Origin '%s' no permitido",
            request.headers.get("origin", "<missing>"),
        )
        return JSONResponse({"error": "Origin no permitido"}, status_code=403)

    try:
        payload: Any = await request.json()
    except ValueError:
        return JSONResponse({"error": "Body no es JSON válido"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "El payload tiene que ser un objeto JSON con un chat"},
            status_code=400,
        )

    # `source` opcional en query string para forzar 'design_chat'. Por defecto
    # asumimos 'conversations' (chat suelto del usuario).
    source_param = request.query_params.get("source", "conversations")
    try:
        source = Source(source_param)
    except ValueError:
        return JSONResponse(
            {"error": f"source inválido: {source_param!r}"},
            status_code=400,
        )
    if source is Source.MEMORY:
        return JSONResponse(
            {"error": "source=memory no soportado en captura en vivo"},
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
            {"error": f"Falta campo obligatorio en el payload: {e}"},
            status_code=400,
        )
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"Payload mal formado: {e}"}, status_code=400)
    except Exception as e:
        logger.exception("Error inesperado en ingest_conversation")
        return JSONResponse(
            {"error": f"Error interno: {type(e).__name__}"},
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
    """Factory de la app Starlette. Tests la usan para reusar la instancia."""
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
