"""Implementaciones puras de las tools de Memex.

Cada función toma una conexión SQLite y/o un Embedder, hace el trabajo, y
devuelve un dict serializable. Estas funciones NO saben nada de MCP; son
testables directamente con DB in-memory y FakeEmbedder.

La capa MCP (`transports/stdio.py`) las envuelve, las decora con
`@server.tool`, y serializa el dict a JSON antes de devolverlo al cliente
del MCP.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from memex.core.embeddings.base import Embedder, EmbedderError
from memex.core.models import Project, Source
from memex.core.storage import repo

# Límites duros para evitar payloads desproporcionados. Los clientes MCP
# (incluido Claude Code) suelen tener un tope de ~25-30k tokens por respuesta;
# pasarse devuelve el resultado a un archivo aparte y rompe la experiencia.
SEARCH_LIMIT_MAX = 50
SEARCH_SUMMARY_MAX_CHARS = 500  # Algunos summaries pesan 2-3k chars, los recortamos.
LIST_LIMIT_MAX = 100
GET_CHAT_MESSAGES_LIMIT_DEFAULT = 20
GET_CHAT_MESSAGES_LIMIT_MAX = 100
GET_CHAT_MESSAGE_TEXT_MAX_CHARS = 3000  # Code dumps largos explotan solos sin esto.

VALID_SEARCH_MODES = ("hybrid", "semantic", "lexical")


def search_chats(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    limit: int = 5,
    source: str | None = None,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Búsqueda sobre todos los chats indexados.

    Modos:
    - `hybrid` (default): combina vector search + FTS5 lexical con Reciprocal
      Rank Fusion. Mejor por default; atrapa tanto significado como palabras
      exactas (resuelve el caso "Amarok").
    - `semantic`: solo vector search. Útil cuando importa la similitud
      conceptual y no las palabras literales.
    - `lexical`: solo FTS5 BM25. Útil para buscar nombres propios o términos
      técnicos exactos.

    Devuelve dict con `query`, `mode`, `count`, `results`. Cada resultado tiene
    rank, conversation_uuid, title, summary, source, project_uuid, distance
    (semántica menor = mejor en los tres modos), snippet y timestamps.

    Errores como dict con clave `error`:
    - source inválido
    - mode inválido
    - query vacía
    - embedder falla (Ollama caído / modelo faltante) en modos hybrid/semantic
    """
    q = query.strip()
    if not q:
        return {"error": "La query no puede estar vacía."}

    if mode not in VALID_SEARCH_MODES:
        return {
            "error": f"Mode inválido: {mode!r}. Válidos: {', '.join(VALID_SEARCH_MODES)}.",
        }

    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    src_filter: Source | None = None
    if source is not None:
        try:
            src_filter = Source(source)
        except ValueError:
            return _invalid_source_error(source)

    # Para filtrar por source en Python (no lo soporta el repo todavía), pedimos
    # más candidatos para no quedarnos cortos.
    fetch_limit = limit * 3 if src_filter is not None else limit

    if mode == "lexical":
        hits = repo.text_search(conn, q, limit=fetch_limit)
    else:
        try:
            query_vec = embedder.embed_one(q)
        except EmbedderError as e:
            return {"error": str(e)}
        if mode == "semantic":
            hits = repo.vector_search(conn, query_vec, limit=fetch_limit)
        else:  # hybrid
            hits = repo.hybrid_search(conn, q, query_vec, limit=fetch_limit)

    if src_filter is not None:
        hits = [h for h in hits if h.conversation.source == src_filter]

    hits = hits[:limit]

    return {
        "query": q,
        "mode": mode,
        "count": len(hits),
        "results": [
            {
                "rank": i + 1,
                "conversation_uuid": h.conversation.uuid,
                "title": h.conversation.title,
                "summary": _truncate(h.conversation.summary, SEARCH_SUMMARY_MAX_CHARS),
                "source": h.conversation.source.value,
                "project_uuid": h.conversation.project_uuid,
                "distance": round(h.distance, 4),
                "snippet": h.snippet,
                "created_at": h.conversation.created_at.isoformat(),
                "updated_at": h.conversation.updated_at.isoformat(),
            }
            for i, h in enumerate(hits)
        ],
    }


def get_chat(
    conn: sqlite3.Connection,
    uuid: str,
    messages_limit: int = GET_CHAT_MESSAGES_LIMIT_DEFAULT,
    messages_offset: int = 0,
) -> dict[str, Any]:
    """Trae una conversación con sus mensajes en orden cronológico.

    Por defecto devuelve los primeros 20 mensajes desde el inicio del chat.
    Para chats largos, paginar con `messages_offset`. Cada mensaje se trunca
    a `GET_CHAT_MESSAGE_TEXT_MAX_CHARS` para que el response total quepa en
    el tope de tokens típico de un cliente MCP.

    Si el chat referencia un project, se incluyen sus metadatos
    (uuid, name, description, prompt_template).

    El campo `raw_content` de los mensajes (JSON original con bloques tool_use
    y tool_result) se omite siempre: pesa mucho y solo es útil para análisis
    especializado. Si en el futuro hace falta, se agrega via parámetro
    `include_raw_content=True`.

    Devuelve `{"error": ...}` si el uuid no existe o está vacío.
    """
    if not uuid.strip():
        return {"error": "El uuid no puede estar vacío."}

    messages_limit = max(1, min(messages_limit, GET_CHAT_MESSAGES_LIMIT_MAX))
    messages_offset = max(0, messages_offset)

    conv = repo.get_conversation(conn, uuid)
    if conv is None:
        return {"error": f"No se encontró conversación con uuid='{uuid}'."}

    all_messages = repo.get_messages_for_conversation(conn, uuid)
    total = len(all_messages)
    end = messages_offset + messages_limit
    window = all_messages[messages_offset:end]
    truncated = end < total

    project = repo.get_project(conn, conv.project_uuid) if conv.project_uuid else None

    return {
        "uuid": conv.uuid,
        "title": conv.title,
        "summary": conv.summary,
        "source": conv.source.value,
        "project": _project_dict(project) if project else None,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
        "total_messages": total,
        "messages_offset": messages_offset,
        "messages_returned": len(window),
        "truncated": truncated,
        "messages": [
            {
                "uuid": m.uuid,
                "parent_uuid": m.parent_uuid,
                "sender": m.sender.value,
                "text": _truncate(m.text, GET_CHAT_MESSAGE_TEXT_MAX_CHARS),
                "created_at": m.created_at.isoformat(),
                "has_tool_use": m.has_tool_use,
                "has_attachments": m.has_attachments,
            }
            for m in window
        ],
    }


def list_recent_chats(
    conn: sqlite3.Connection,
    limit: int = 10,
    source: str | None = None,
) -> dict[str, Any]:
    """Lista los chats más recientes ordenados por updated_at descendente."""
    limit = max(1, min(limit, LIST_LIMIT_MAX))

    src_filter: Source | None = None
    if source is not None:
        try:
            src_filter = Source(source)
        except ValueError:
            return _invalid_source_error(source)

    chats = repo.list_recent_conversations(conn, limit=limit, source=src_filter)
    return {
        "count": len(chats),
        "chats": [
            {
                "uuid": c.uuid,
                "title": c.title,
                "summary": c.summary,
                "source": c.source.value,
                "project_uuid": c.project_uuid,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in chats
        ],
    }


# ---------- helpers privados ----------

def _invalid_source_error(value: str | None) -> dict[str, Any]:
    valid = ", ".join(s.value for s in Source)
    return {
        "error": f"Source inválido: {value!r}. Válidos: {valid}.",
    }


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "uuid": p.uuid,
        "name": p.name,
        "description": p.description,
        "prompt_template": p.prompt_template,
    }


def _truncate(s: str | None, max_chars: int) -> str | None:
    """Recorta el string a `max_chars` y agrega `…[truncated]` si se pasó.

    Devuelve `None` tal cual. Útil para summaries y mensajes largos que
    explotan el límite de tokens del cliente MCP.
    """
    if s is None:
        return None
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "…[truncated]"
