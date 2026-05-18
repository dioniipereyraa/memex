"""MCP server stdio entrypoint para Memex.

Levanta un FastMCP que expone las 3 tools (`search_chats`, `get_chat`,
`list_recent_chats`) sobre stdio. Comunicación JSON-RPC sobre stdin/stdout.

Las funciones acá decoradas con `@server.tool(...)` son wrappers thin sobre las
implementaciones puras de `memex.transports.tools`. La separación permite
testear la lógica sin spinnear el server.

La conexión SQLite y el embedder se crean lazy (en la primera llamada) y se
reutilizan durante toda la sesión del proceso. Cada cliente MCP arranca su
propio proceso, así que no hay concurrencia compartida.

`run_in_thread=False` en cada `@server.tool`: por default FastMCP corre las
tools sync en un thread pool, pero el módulo `sqlite3` ata cada conexión al
thread que la creó. Con tools rápidas de I/O conviene correrlas en el event
loop directamente y mantener una sola conexión por proceso.

Para conectar desde Claude Code, agregá en tu `.mcp.json`:

    {
      "mcpServers": {
        "memex": {
          "command": "uv",
          "args": ["run", "memex-mcp"],
          "cwd": "/path/al/repo/de/memex"
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from typing import Any

from fastmcp import FastMCP

from memex.core.embeddings.base import Embedder, EmbedderError
from memex.core.embeddings.ollama import OllamaEmbedder
from memex.core.storage.db import connect_and_init
from memex.transports import tools

# Logging a stderr (stdout está reservado para JSON-RPC).
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("memex.mcp")

server: FastMCP = FastMCP("memex")

_conn: sqlite3.Connection | None = None
_embedder: Embedder | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect_and_init()
        logger.info("DB abierta: %s", _conn)
    return _conn


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = OllamaEmbedder()
        logger.info("Embedder inicializado: %s", _embedder.model_name)
    return _embedder


def _serialize(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


@server.tool(run_in_thread=False)
def search_chats(query: str, limit: int = 5, source: str | None = None) -> str:
    """Búsqueda semántica sobre todos los chats indexados de Claude.ai.

    Args:
        query: Texto a buscar (lenguaje natural). Funciona mejor con frases
            descriptivas (ej. "decisión sobre arquitectura del proyecto") que
            con palabras sueltas o nombres propios raros (limitación de la
            búsqueda semántica pura; planeado para Fase 2 con FTS5 híbrido).
        limit: Cantidad de resultados (default 5, max 50).
        source: Filtro opcional por origen del chat. Valores válidos:
            'conversations' (chats sueltos), 'design_chat' (chats dentro
            de un Project de Claude.ai), 'memory' (memoria curada).

    Returns:
        JSON con `query`, `count`, y `results`: lista ordenada por similitud
        (distance, más bajo = más relevante). Cada resultado incluye uuid,
        título, resumen, snippet, y timestamps de la conversación.
    """
    try:
        result = tools.search_chats(_get_conn(), _get_embedder(), query, limit, source)
    except EmbedderError as e:
        result = {"error": str(e)}
    except Exception as e:
        logger.exception("Error en search_chats")
        result = {"error": f"Error interno: {e}"}
    return _serialize(result)


@server.tool(run_in_thread=False)
def get_chat(uuid: str) -> str:
    """Trae una conversación entera de Claude.ai por uuid.

    Args:
        uuid: UUID del chat (normalmente obtenido vía `search_chats` o
            `list_recent_chats`).

    Returns:
        JSON con metadata, project asociado (si aplica), y todos los mensajes
        en orden cronológico. Si la conversación es muy larga (cientos de
        mensajes), el payload puede ser grande.
    """
    try:
        result = tools.get_chat(_get_conn(), uuid)
    except Exception as e:
        logger.exception("Error en get_chat")
        result = {"error": f"Error interno: {e}"}
    return _serialize(result)


@server.tool(run_in_thread=False)
def list_recent_chats(limit: int = 10, source: str | None = None) -> str:
    """Lista los chats más recientes, ordenados por última actualización.

    Args:
        limit: Cantidad a devolver (default 10, max 100).
        source: Filtro opcional por origen. Mismos valores que en
            `search_chats`.

    Returns:
        JSON con `count` y `chats`. Útil para browse cronológico cuando no
        hay un keyword claro para buscar.
    """
    try:
        result = tools.list_recent_chats(_get_conn(), limit, source)
    except Exception as e:
        logger.exception("Error en list_recent_chats")
        result = {"error": f"Error interno: {e}"}
    return _serialize(result)


def main() -> None:
    """Entrypoint que arranca el MCP server sobre stdio.

    Configurado en `pyproject.toml` como el script `memex-mcp`.
    """
    logger.info("Memex MCP server arrancando (stdio).")
    server.run(show_banner=False)


if __name__ == "__main__":
    main()
