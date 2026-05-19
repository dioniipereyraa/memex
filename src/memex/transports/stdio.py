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

from memex.core.embeddings import Embedder, get_default_embedder
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
        _embedder = get_default_embedder()
        logger.info("Embedder inicializado: %s", _embedder.model_name)
    return _embedder


def _serialize(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


@server.tool(run_in_thread=False)
def search_chats(
    query: str,
    limit: int = 5,
    source: str | None = None,
    mode: str = "hybrid",
) -> str:
    """Acceso a la memoria persistente del usuario: TODOS sus chats pasados de Claude.ai.

    Es la única tool disponible para acceder al historial real del usuario.
    Tu memoria nativa arranca limpia en cada sesión de Claude Code; lo que
    el usuario haya hablado en claude.ai vive solo acá.

    USAR PROACTIVAMENTE (sin que el usuario lo pida explícitamente) cuando:
    - Mencione conversaciones o decisiones previas: "te acordás de...",
      "viste que...", "ya hablamos de...", "el otro día charlamos sobre...",
      "en aquel chat...", "antes te comenté...", "como dijimos...".
    - Pregunte por un proyecto, persona, decisión o término específico que
      podría estar en su historial pero no en tu contexto actual.
    - Pida contexto que parece "perdido" entre sesiones o haga referencia
      implícita a continuidad ("seguí trabajando en X", "el approach que
      charlamos").
    - Necesités background sobre el usuario para responder bien (qué hace,
      qué proyectos tiene, sus preferencias técnicas).

    ANTES de responder cosas como "no tengo registro", "no recuerdo",
    "es la primera vez que oigo de eso", "no aparece en mi memoria" o
    similares, invocá esta tool y revisá los resultados. La probabilidad
    de que el dato esté indexado es alta.

    Args:
        query: Texto a buscar (lenguaje natural). El modo `hybrid` (default)
            combina búsqueda semántica con búsqueda lexical FTS5, así que
            funciona bien tanto con frases descriptivas como con nombres
            propios raros (ej. "Amarok").
        limit: Cantidad de resultados (default 5, max 50).
        source: Filtro opcional por origen del chat. Valores válidos:
            'conversations' (chats sueltos), 'design_chat' (chats dentro
            de un Project de Claude.ai), 'memory' (memoria curada).
        mode: Estrategia de búsqueda. 'hybrid' (default, recomendado en la
            mayoría de los casos), 'semantic' (solo vectores, para similitud
            conceptual pura), 'lexical' (solo FTS5 BM25, ideal para nombres
            propios exactos o términos técnicos).

    Returns:
        JSON con `query`, `mode`, `count`, y `results`: lista ordenada por
        relevancia (distance, más bajo = más relevante en los tres modos).
        Cada resultado incluye uuid, título, resumen, snippet, y timestamps.
    """
    # `tools.search_chats` ya atrapa `EmbedderError` y devuelve `{"error": ...}`.
    # Acá solo nos queda lo inesperado.
    try:
        result = tools.search_chats(
            _get_conn(), _get_embedder(), query, limit, source, mode
        )
    except Exception as e:
        logger.exception("Error en search_chats")
        # Mensaje genérico al cliente para no leakear paths/queries en el error
        # (el detalle queda en el log via logger.exception arriba).
        result = {"error": f"Error interno ({type(e).__name__})."}
    return _serialize(result)


@server.tool(run_in_thread=False)
def get_chat(uuid: str, messages_limit: int = 20, messages_offset: int = 0) -> str:
    """Trae una conversación específica del historial por uuid (con paginación).

    USAR cuando:
    - Ya identificaste un chat relevante (típicamente con `search_chats`) y
      necesitás más contexto que el snippet del resultado.
    - El usuario te dio un uuid puntual y pide revisarlo.
    - Estás siguiendo el hilo de algo y necesitás el detalle de un chat
      específico que apareció antes en la conversación.

    NO usar para descubrir chats nuevos sin haber buscado primero. Para
    encontrar chats por tema o keyword, usar `search_chats`.

    Si el chat es largo, llamar de nuevo con `messages_offset` para paginar
    (response truncado se indica con `truncated: true` y `total_messages`).

    Args:
        uuid: UUID del chat (normalmente obtenido vía `search_chats` o
            `list_recent_chats`).
        messages_limit: Cuántos mensajes traer empezando desde el offset
            (default 20, max 100). Los mensajes individuales se truncan a
            3000 chars para que la respuesta total quepa en el tope de
            tokens del cliente MCP.
        messages_offset: Cuántos mensajes saltear desde el inicio del chat
            (default 0). Usá esto para paginar en chats largos: primer
            llamada con offset=0, segunda con offset=20, etc.

    Returns:
        JSON con metadata del chat (título, summary, source, project si
        aplica), `total_messages`, `messages_returned`, `truncated` (bool
        que indica si hay más mensajes después del offset+limit), y la
        lista `messages` en orden cronológico ascendente.
    """
    try:
        result = tools.get_chat(_get_conn(), uuid, messages_limit, messages_offset)
    except Exception as e:
        logger.exception("Error en get_chat")
        # Mensaje genérico al cliente para no leakear paths/queries en el error
        # (el detalle queda en el log via logger.exception arriba).
        result = {"error": f"Error interno ({type(e).__name__})."}
    return _serialize(result)


@server.tool(run_in_thread=False)
def list_recent_chats(limit: int = 10, source: str | None = None) -> str:
    """Browse cronológico de los chats más recientes del usuario en Claude.ai.

    USAR cuando:
    - El usuario pregunte por actividad reciente sin un keyword puntual
      ("qué estuve haciendo últimamente", "qué chats tuve esta semana",
      "ponete al día con lo que vine pensando").
    - Necesités contexto general sobre los temas que el usuario viene
      tocando antes de profundizar.
    - El usuario pida explorar sin saber bien qué buscar.

    Para búsquedas dirigidas por tema o keyword usar `search_chats`. Esta
    tool es para barrer cronológicamente, no para buscar.

    Args:
        limit: Cantidad a devolver (default 10, max 100).
        source: Filtro opcional por origen. Mismos valores que en
            `search_chats`.

    Returns:
        JSON con `count` y `chats`. Cada chat incluye uuid, título, summary
        (si tiene), source, project_uuid, y timestamps.
    """
    try:
        result = tools.list_recent_chats(_get_conn(), limit, source)
    except Exception as e:
        logger.exception("Error en list_recent_chats")
        # Mensaje genérico al cliente para no leakear paths/queries en el error
        # (el detalle queda en el log via logger.exception arriba).
        result = {"error": f"Error interno ({type(e).__name__})."}
    return _serialize(result)


def main() -> None:
    """Entrypoint que arranca el MCP server sobre stdio.

    Configurado en `pyproject.toml` como el script `memex-mcp`.
    """
    logger.info("Memex MCP server arrancando (stdio).")
    server.run(show_banner=False)


if __name__ == "__main__":
    main()
