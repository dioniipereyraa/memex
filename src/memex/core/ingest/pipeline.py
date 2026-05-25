"""End-to-end ingest orchestrator.

Takes a zip of the official Claude.ai export, a SQLite connection (with
schema already applied) and an `Embedder`. Runs parse -> render -> chunk
-> embed -> store in the right order:

1. Projects first (they are the FK target of conversations).
2. Design chats (they reference projects).
3. Standalone conversations.
4. Curated memory (memories.json) as a synthetic conversation.

For each conversation: insert conv, insert its messages, join the
rendered text into a single string (with `[sender]\\n` headers), chunk,
embed in batches, store chunks + vectors. Before chunking we delete the
old chunks for that conversation so re-ingest is idempotent.

Inserts happen inside a per-conversation transaction, so an error in one
does not break the progress of the rest.
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
from memex.core.repos import match_text
from memex.core.storage import repo

logger = logging.getLogger(__name__)


class IngestSummary(BaseModel):
    """Ingest counts. Useful to report to the user what was loaded."""

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
    """Full pipeline. Returns an `IngestSummary` with counts and errors.

    `chunk_size` and `chunk_overlap` are expressed in tokens. If not
    provided, taken from config.

    `batch_size` controls how many chunks are embedded per Ollama call.
    32 tends to give a good latency/throughput balance without putting
    too much pressure on the service.

    Note on summaries: the pipeline does NOT generate LLM summaries
    (Phase 3 moved them to on-demand generation in
    `tools.search_chats`). The `content_hash` is still persisted because
    the lazy summarizer uses it to detect if a conv changed and force
    regeneration even when a summary is already cached.
    """
    cs = chunk_size if chunk_size is not None else settings.chunk_size
    co = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    summary = IngestSummary()
    zp = Path(zip_path)

    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()

        # 1) Projects first (FK target).
        for name in names:
            if name.startswith("projects/") and name.endswith(".json"):
                try:
                    with zf.open(name) as f:
                        project = parse_project(json.load(f))
                    repo.insert_project(conn, project)
                    summary.projects += 1
                    conn.commit()
                except Exception as e:
                    logger.exception("Error parsing %s", name)
                    summary.errors.append(f"{name}: {e}")
                    conn.rollback()

        # 2) Design chats (linked to projects).
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
                    logger.exception("Error in %s", name)
                    summary.errors.append(f"{name}: {e}")
                    conn.rollback()

        # 3) Standalone conversations.
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
                        logger.exception("Error in conv %s", conv.uuid)
                        summary.errors.append(f"conversations.json/{conv.uuid}: {e}")
                        conn.rollback()
            except Exception as e:
                logger.exception("Error parsing conversations.json")
                summary.errors.append(f"conversations.json: {e}")
                conn.rollback()

        # 4) Curated memory as a synthetic conversation.
        if "memories.json" in names:
            try:
                with zf.open("memories.json") as f:
                    result = parse_memories(json.load(f), now=datetime.now(UTC))
                if result is not None:
                    conv, msg = result
                    _ingest_conversation(conn, embedder, conv, [msg], summary, cs, co, batch_size)
                    conn.commit()
            except Exception as e:
                logger.exception("Error parsing memories.json")
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
    """Pipeline for ONE chat (parsing + chunks + embeddings + storage).

    Useful for live capture: the Chrome ext captures the Claude.ai API
    payload (same shape as an item in `conversations.json`) and posts it
    to the local HTTP endpoint. That endpoint calls this function with
    the already-parsed dict.

    If `source` is `DESIGN_CHAT`, the payload must have `project` and
    `messages`. If it is `CONVERSATIONS`, it has `name` and
    `chat_messages`. If the referenced `project_uuid` is not in the DB,
    the conversation is ingested orphan (project_uuid=None) without
    failing.

    Returns an `IngestSummary` with counts. For a single conv expect
    `conversations=1` plus `messages` and `chunks` depending on size.

    Calls `conn.commit()` at the end if everything went well;
    `conn.rollback()` if there was an error along the way (keeps the DB
    consistent).
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


# ---------- private helpers ----------


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
    """Insert a conversation with its messages and chunks/embeddings.

    Idempotent: re-ingesting the same conversation replaces its old
    chunks (upsert of conv and messages via `ON CONFLICT`, delete +
    reinsert of chunks).

    If `conv.project_uuid` references a project not in the DB (happens
    in real exports: design_chats point to projects the user has but
    that were not exported), it is set to None to avoid FK violations.

    The `content_hash` (SHA-256 of the canonical text) is computed and
    persisted here even when no summary is generated: the lazy
    summarizer in `tools.search_chats` consumes it to detect whether a
    conv has changed since the last generation and force regen even
    when a summary is already cached.
    """
    if conv.project_uuid is not None:
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE uuid = ?", (conv.project_uuid,)
        ).fetchone()
        if exists is None:
            logger.info(
                "Conversation %s references project %s which is not in the export; "
                "ingesting without project association.",
                conv.uuid,
                conv.project_uuid,
            )
            conv = conv.model_copy(update={"project_uuid": None})

    # `_join_messages` does not touch the DB, only processes the objects
    # in memory. We call it early to have the hash ready before the upsert.
    full_text, msg_map = _join_messages(messages, summary)
    content_hash = _hash_content(full_text) if full_text else None

    # Preserve the cached summary if the conv already existed and the
    # content did NOT change. Important: the upsert overwrites `summary`
    # with `excluded.summary`, so if we leave the parser's `conv.summary`
    # (typically None or the official export summary), we lose the lazy
    # summary that may have been generated by previous queries. Restore
    # it here if applicable.
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

    # Clean up old chunks so re-ingest is idempotent.
    repo.delete_chunks_for_conversation(conn, conv.uuid)

    if not full_text:
        return

    # Auto-detect repo associations. No-op if no repos are registered.
    # Manual ('manual' source) associations are preserved by
    # `repo.associate_chat_repo` (it refuses to overwrite a manual tag).
    _scan_repos(conn, conv.uuid, full_text)

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
    """SHA-256 hex of the canonical text. Stable, enough to detect changes.
    Not cryptographic: just a fingerprint to compare."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan_repos(conn: sqlite3.Connection, conversation_uuid: str, text: str) -> None:
    """Run the repo matcher and persist auto-associations.

    Called from `_ingest_conversation` after the conv + messages are
    persisted. Reads the registered repos from DB, runs the matcher, and
    upserts each match as a `source='auto'` association.

    Idempotent: re-ingesting unchanged content re-asserts the same set of
    associations. Manual tags survive because `associate_chat_repo`
    refuses to overwrite a `manual` with `auto`.

    No-op when there are no repos registered yet (very common: a fresh
    user has not run `memex repos add` yet). Errors from the matcher are
    not caught here; they would indicate a bug, not a runtime condition.
    """
    repos = repo.list_repos(conn)
    if not repos:
        return
    matches = match_text(text, repos)
    for match in matches:
        repo.associate_chat_repo(
            conn,
            conversation_uuid,
            match.repo_key,
            source="auto",
            confidence=match.confidence,
        )


def _join_messages(
    messages: list[Message], summary: IngestSummary
) -> tuple[str, list[tuple[int, int, str, str]]]:
    """Concatenate message text with `[sender]\\n` headers.

    Returns `(full_text, msg_map)` where `msg_map` is a list of
    `(body_start, body_end, msg_uuid, sender)` so each chunk offset can
    be mapped back to its message.
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
    """Find the message whose body covers `pos`. If `pos` falls between bodies, use the last prior one."""
    fallback: tuple[str, str] | None = None
    for start, end, uuid, sender in msg_map:
        if start <= pos < end:
            return uuid, sender
        if start <= pos:
            fallback = (uuid, sender)
    return fallback if fallback else (None, None)


def _batched(items: Iterable[ChunkSpan], n: int) -> Iterator[list[ChunkSpan]]:
    """Group items into batches of size up to `n`. (Equivalent to itertools.batched.)"""
    batch: list[ChunkSpan] = []
    for item in items:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch
