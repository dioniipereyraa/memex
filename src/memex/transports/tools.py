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

# Límites duros para evitar payloads desproporcionados.
SEARCH_LIMIT_MAX = 50
LIST_LIMIT_MAX = 100


def search_chats(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    limit: int = 5,
    source: str | None = None,
) -> dict[str, Any]:
    """Búsqueda semántica sobre todos los chats indexados.

    Devuelve dict con `query`, `count`, `results`. Cada resultado tiene rank,
    conversation_uuid, title, summary, source, project_uuid, distance (L2),
    snippet, y timestamps.

    Si `source` está seteado y no es válido, devuelve `{"error": ...}`.
    Si la query está vacía, devuelve `{"error": ...}`.
    Si el embedder falla (Ollama caído / modelo faltante), devuelve `{"error": ...}`.
    """
    q = query.strip()
    if not q:
        return {"error": "La query no puede estar vacía."}

    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    src_filter: Source | None = None
    if source is not None:
        try:
            src_filter = Source(source)
        except ValueError:
            return _invalid_source_error(source)

    try:
        query_vec = embedder.embed_one(q)
    except EmbedderError as e:
        return {"error": str(e)}

    # Si pediremos filtrar por source, pedimos más candidatos para no quedarnos cortos
    # después del filtro en Python.
    fetch_limit = limit * 3 if src_filter is not None else limit
    hits = repo.vector_search(conn, query_vec, limit=fetch_limit)

    if src_filter is not None:
        hits = [h for h in hits if h.conversation.source == src_filter]

    hits = hits[:limit]

    return {
        "query": q,
        "count": len(hits),
        "results": [
            {
                "rank": i + 1,
                "conversation_uuid": h.conversation.uuid,
                "title": h.conversation.title,
                "summary": h.conversation.summary,
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


def get_chat(conn: sqlite3.Connection, uuid: str) -> dict[str, Any]:
    """Trae una conversación entera con todos sus mensajes en orden cronológico.

    Si la conversación tiene project, incluye sus metadatos (uuid, name,
    description, prompt_template) en la respuesta. Útil para que el cliente
    entienda el contexto del chat.

    Devuelve `{"error": ...}` si el uuid no existe.
    """
    if not uuid.strip():
        return {"error": "El uuid no puede estar vacío."}

    conv = repo.get_conversation(conn, uuid)
    if conv is None:
        return {"error": f"No se encontró conversación con uuid='{uuid}'."}

    messages = repo.get_messages_for_conversation(conn, uuid)
    project = repo.get_project(conn, conv.project_uuid) if conv.project_uuid else None

    return {
        "uuid": conv.uuid,
        "title": conv.title,
        "summary": conv.summary,
        "source": conv.source.value,
        "project": _project_dict(project) if project else None,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
        "message_count": len(messages),
        "messages": [
            {
                "uuid": m.uuid,
                "parent_uuid": m.parent_uuid,
                "sender": m.sender.value,
                "text": m.text,
                "created_at": m.created_at.isoformat(),
                "has_tool_use": m.has_tool_use,
                "has_attachments": m.has_attachments,
            }
            for m in messages
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
