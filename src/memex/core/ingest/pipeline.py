"""Orquestador end-to-end del ingest.

Toma un zip del export oficial de Claude.ai, una conexión SQLite (ya con schema)
y un `Embedder`. Hace parse → render → chunk → embed → store en orden correcto:

1. Projects primero (son FK target de conversations).
2. Design chats (referencian projects).
3. Conversations sueltas.
4. Memoria curada (memories.json) como conversación sintética.

Para cada conversación: inserta conv, inserta sus mensajes, junta el texto
renderizado en una sola string (con headers `[sender]\\n`), chunkea, embebe en
batches, guarda chunks + vectores. Antes de chunkear, borra los chunks viejos
de esa conversación para que el re-ingest sea idempotente.

Las inserciones se hacen dentro de una transacción por conversación, así un
error en una no rompe el progreso del resto.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from memex.config import settings
from memex.core.embeddings.base import Embedder
from memex.core.ingest.chunker import ChunkSpan, chunk_text
from memex.core.ingest.claude_export import (
    parse_conversation_dict,
    parse_conversations_list,
    parse_design_chat,
    parse_memories,
    parse_project,
)
from memex.core.models import Chunk, Conversation, Message, Source
from memex.core.storage import repo

logger = logging.getLogger(__name__)


class IngestSummary(BaseModel):
    """Counts del ingest. Útil para mostrarle al usuario qué se cargó."""

    projects: int = 0
    conversations: int = 0
    messages: int = 0
    chunks: int = 0
    skipped_empty_messages: int = 0
    errors: list[str] = Field(default_factory=list)


def ingest_export(
    conn: sqlite3.Connection,
    zip_path: Path | str,
    embedder: Embedder,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    batch_size: int = 32,
) -> IngestSummary:
    """Pipeline completo. Devuelve un `IngestSummary` con counts y errores.

    `chunk_size` y `chunk_overlap` se expresan en tokens. Si no se pasan, se
    toman del config.

    `batch_size` controla cuántos chunks se embeben por llamada a Ollama. 32
    suele dar buen balance latencia/throughput sin meter mucha presión al servicio.

    Nota sobre summaries: el pipeline NO genera summaries por LLM (Fase 3 los
    movió a generación on-demand en `tools.search_chats`). El `content_hash`
    se persiste igual, porque el lazy summarizer lo usa para detectar si una
    conv cambió y forzar regen aunque ya haya un summary guardado.
    """
    cs = chunk_size if chunk_size is not None else settings.chunk_size
    co = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    summary = IngestSummary()
    zp = Path(zip_path)

    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()

        # 1) Projects primero (FK target).
        for name in names:
            if name.startswith("projects/") and name.endswith(".json"):
                try:
                    with zf.open(name) as f:
                        project = parse_project(json.load(f))
                    repo.insert_project(conn, project)
                    summary.projects += 1
                    conn.commit()
                except Exception as e:
                    logger.exception("Error parseando %s", name)
                    summary.errors.append(f"{name}: {e}")
                    conn.rollback()

        # 2) Design chats (linkean a projects).
        for name in names:
            if name.startswith("design_chats/") and name.endswith(".json"):
                try:
                    with zf.open(name) as f:
                        conv, messages = parse_design_chat(json.load(f))
                    _ingest_conversation(
                        conn, embedder, conv, messages, summary, cs, co, batch_size
                    )
                    conn.commit()
                except Exception as e:
                    logger.exception("Error en %s", name)
                    summary.errors.append(f"{name}: {e}")
                    conn.rollback()

        # 3) Conversations sueltas.
        if "conversations.json" in names:
            try:
                with zf.open("conversations.json") as f:
                    parsed_list = parse_conversations_list(json.load(f))
                for conv, messages in parsed_list:
                    try:
                        _ingest_conversation(
                            conn,
                            embedder,
                            conv,
                            messages,
                            summary,
                            cs,
                            co,
                            batch_size,
                        )
                        conn.commit()
                    except Exception as e:
                        logger.exception("Error en conv %s", conv.uuid)
                        summary.errors.append(f"conversations.json/{conv.uuid}: {e}")
                        conn.rollback()
            except Exception as e:
                logger.exception("Error parseando conversations.json")
                summary.errors.append(f"conversations.json: {e}")
                conn.rollback()

        # 4) Memoria curada como conversación sintética.
        if "memories.json" in names:
            try:
                with zf.open("memories.json") as f:
                    result = parse_memories(json.load(f), now=datetime.now(UTC))
                if result is not None:
                    conv, msg = result
                    _ingest_conversation(conn, embedder, conv, [msg], summary, cs, co, batch_size)
                    conn.commit()
            except Exception as e:
                logger.exception("Error parseando memories.json")
                summary.errors.append(f"memories.json: {e}")
                conn.rollback()

    return summary


def ingest_single_conversation(
    conn: sqlite3.Connection,
    embedder: Embedder,
    conv_payload: dict[str, Any],
    source: Source = Source.CONVERSATIONS,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    batch_size: int = 32,
) -> IngestSummary:
    """Pipeline para UN solo chat (parsing + chunks + embeddings + storage).

    Útil para la captura en vivo: la Chrome ext captura el payload del API
    de Claude.ai (mismo shape que un item de `conversations.json`) y lo manda
    al endpoint HTTP local. Este endpoint llama a esta función con el dict ya
    parseado.

    Si `source` es `DESIGN_CHAT`, el payload tiene que tener `project` y
    `messages`. Si es `CONVERSATIONS`, tiene `name` y `chat_messages`. Si el
    `project_uuid` referenciado no existe en la base, se ingesta orphan
    (project_uuid=None) sin romper.

    Devuelve un `IngestSummary` con counts. Para una sola conv esperás
    `conversations=1` y `messages` + `chunks` segun el tamaño.

    Hace `conn.commit()` al final si todo salió bien; `conn.rollback()` si
    hubo error en el camino (mantiene la base consistente).
    """
    cs = chunk_size if chunk_size is not None else settings.chunk_size
    co = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap

    summary = IngestSummary()
    try:
        conv, messages = parse_conversation_dict(conv_payload, source)
        _ingest_conversation(conn, embedder, conv, messages, summary, cs, co, batch_size)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return summary


# ---------- helpers privados ----------


def _ingest_conversation(
    conn: sqlite3.Connection,
    embedder: Embedder,
    conv: Conversation,
    messages: list[Message],
    summary: IngestSummary,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    batch_size: int,
) -> None:
    """Inserta una conversación con sus mensajes y chunks/embeddings.

    Idempotente: re-ingestar la misma conversación reemplaza sus chunks viejos
    (upsert del conv y mensajes vía `ON CONFLICT`, delete + reinsert de chunks).

    Si `conv.project_uuid` referencia un project que no está en la base (sucede
    en el export real: design_chats apuntan a projects que el usuario tiene pero
    que no fueron exportados), se setea a None para no violar el FK.

    El `content_hash` (SHA-256 del texto canónico) se calcula y persiste acá
    aunque no se genere summary: lo consume el lazy summarizer en
    `tools.search_chats` para detectar si una conv cambió desde la última
    generación y forzar regen aunque ya haya un summary cacheado.
    """
    if conv.project_uuid is not None:
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE uuid = ?", (conv.project_uuid,)
        ).fetchone()
        if exists is None:
            logger.info(
                "Conversación %s referencia project %s que no está en el export; "
                "se ingesta sin asociar a project.",
                conv.uuid,
                conv.project_uuid,
            )
            conv = conv.model_copy(update={"project_uuid": None})

    # `_join_messages` no toca DB, solo procesa los objetos en memoria.
    # Lo llamamos temprano para tener el hash listo antes del upsert.
    full_text, msg_map = _join_messages(messages, summary)
    content_hash = _hash_content(full_text) if full_text else None

    # Preservar el summary cacheado si la conv ya existía y el contenido NO
    # cambió. Importante: el upsert pisa `summary` con `excluded.summary`,
    # así que si dejamos el `conv.summary` del parser (típicamente None o el
    # summary del export oficial), perdemos el summary lazy que pudo haberse
    # generado en queries previas. Acá lo restauramos si aplica.
    if content_hash is not None:
        existing = repo.get_conversation(conn, conv.uuid)
        if existing is not None and existing.summary and existing.content_hash == content_hash:
            conv = conv.model_copy(update={"summary": existing.summary})

    conv = conv.model_copy(update={"content_hash": content_hash})

    repo.insert_conversation(conn, conv)
    summary.conversations += 1

    for msg in messages:
        repo.insert_message(conn, msg)
        summary.messages += 1

    # Limpiar chunks viejos para que la re-ingesta sea idempotente.
    repo.delete_chunks_for_conversation(conn, conv.uuid)

    if not full_text:
        return

    spans = chunk_text(
        full_text,
        max_tokens=chunk_size_tokens,
        overlap_tokens=chunk_overlap_tokens,
    )
    if not spans:
        return

    for batch in _batched(spans, batch_size):
        texts = [s.text for s in batch]
        vectors = embedder.embed(texts)
        for span, vec in zip(batch, vectors, strict=True):
            msg_uuid, sender = _lookup_msg(msg_map, span.char_start)
            chunk = Chunk(
                conversation_uuid=conv.uuid,
                message_uuid=msg_uuid,
                sender=sender,
                text=span.text,
                char_start=span.char_start,
                char_end=span.char_end,
                created_at=conv.updated_at,
            )
            repo.add_chunk(conn, chunk, vec)
            summary.chunks += 1


def _hash_content(text: str) -> str:
    """SHA-256 hex del texto canónico. Estable, suficiente para detectar
    cambios. No es cripto: solo un fingerprint para comparar."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _join_messages(
    messages: list[Message], summary: IngestSummary
) -> tuple[str, list[tuple[int, int, str, str]]]:
    """Concatena el texto de los mensajes con headers `[sender]\\n`.

    Devuelve `(full_text, msg_map)` donde `msg_map` es una lista de
    `(body_start, body_end, msg_uuid, sender)` para poder mapear cada offset
    de chunk de vuelta a su mensaje.
    """
    parts: list[str] = []
    msg_map: list[tuple[int, int, str, str]] = []
    pos = 0
    for msg in messages:
        if not msg.text:
            summary.skipped_empty_messages += 1
            continue
        header = f"[{msg.sender.value}]\n"
        body_start = pos + len(header)
        body_end = body_start + len(msg.text)
        msg_map.append((body_start, body_end, msg.uuid, msg.sender.value))
        section = header + msg.text + "\n\n"
        parts.append(section)
        pos += len(section)
    return "".join(parts), msg_map


def _lookup_msg(
    msg_map: list[tuple[int, int, str, str]], pos: int
) -> tuple[str | None, str | None]:
    """Encuentra el mensaje cuyo body cubre `pos`. Si cae entre, usa el último anterior."""
    fallback: tuple[str, str] | None = None
    for start, end, uuid, sender in msg_map:
        if start <= pos < end:
            return uuid, sender
        if start <= pos:
            fallback = (uuid, sender)
    return fallback if fallback else (None, None)


def _batched(items: Iterable[ChunkSpan], n: int) -> Iterator[list[ChunkSpan]]:
    """Agrupa items en batches de tamaño hasta `n`. (Equivalente a itertools.batched.)"""
    batch: list[ChunkSpan] = []
    for item in items:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch
