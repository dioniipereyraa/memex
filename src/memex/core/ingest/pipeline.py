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
from memex.core.ingest.claude_code import parse_session_file
from memex.core.ingest.claude_export import (
    parse_conversation_dict,
    parse_conversations_list,
    parse_design_chat,
    parse_memories,
    parse_project,
)
from memex.core.models import Chunk, Conversation, Message, Source
from memex.core.repos import match_text, resolve_repo_key
from memex.core.storage import repo

logger = logging.getLogger(__name__)


class IngestSummary(BaseModel):
    """Ingest counts. Useful to report to the user what was loaded."""

    projects: int = 0
    conversations: int = 0
    messages: int = 0
    chunks: int = 0
    skipped_empty_messages: int = 0
    # Conversations whose text exceeded `max_chunks_per_conversation` and were
    # truncated (tail dropped) to bound resource use.
    truncated_conversations: int = 0
    # Conversations skipped because their content was unchanged since the last
    # ingest (incremental re-scan, e.g. `ingest-claude-code`). No re-embed.
    skipped_unchanged_conversations: int = 0
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


def ingest_claude_code_sessions(
    conn: sqlite3.Connection,
    embedder: Embedder,
    root: Path | str,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    batch_size: int = 32,
) -> IngestSummary:
    """Ingest local Claude Code / terminal session logs under `root`.

    `root` is typically `~/.claude/projects`. Every `*.jsonl` below it is a
    session; each is parsed (see `claude_code.parse_session_file`) and ingested
    with the shared per-conversation pipeline.

    Incremental: unchanged sessions (same `content_hash`) are skipped without
    re-embedding, so a re-scan over hundreds of files is cheap. Live sessions
    grow append-only, so re-running picks up new turns and new sessions.

    Each session is associated with the registered repo of its `cwd` (resolved
    via `resolve_repo_key`) at full confidence, when that repo is registered.

    One bad file does not abort the scan: it is logged into `summary.errors`
    and the rest proceed. Each session commits independently.
    """
    cs = chunk_size if chunk_size is not None else settings.chunk_size
    co = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    summary = IngestSummary()

    root_path = Path(root).expanduser()
    if not root_path.exists():
        summary.errors.append(f"{root_path}: path does not exist")
        return summary

    root_resolved = root_path.resolve()
    for jsonl_path in sorted(root_path.rglob("*.jsonl")):
        try:
            # `rglob` follows directory symlinks; skip anything that resolves
            # outside the scan root so a symlinked subtree cannot pull in
            # `.jsonl` files from elsewhere on disk.
            if not jsonl_path.resolve().is_relative_to(root_resolved):
                continue
            parsed = parse_session_file(jsonl_path)
            if parsed is None:
                continue
            repo_key = resolve_repo_key(conn, parsed.cwd) if parsed.cwd else None
            _ingest_conversation(
                conn,
                embedder,
                parsed.conversation,
                parsed.messages,
                summary,
                cs,
                co,
                batch_size,
                skip_unchanged=True,
                extra_repo_keys=[repo_key] if repo_key else None,
            )
            conn.commit()
        except Exception as e:
            logger.exception("Error ingesting session %s", jsonl_path)
            summary.errors.append(f"{jsonl_path.name}: {e}")
            conn.rollback()

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
    *,
    skip_unchanged: bool = False,
    extra_repo_keys: list[str] | None = None,
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

    `skip_unchanged`: when True, if the conversation already exists with
    the same `content_hash`, return early without re-embedding (cheap
    incremental re-scan, used by `ingest-claude-code` over hundreds of
    session files). The export path leaves this False (full re-ingest).

    `extra_repo_keys`: registered repo keys to associate with this
    conversation in addition to text-matched ones, with confidence 1.0.
    Used to associate a Claude Code session with the repo of its `cwd`
    (a stronger signal than text matching).
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

    # Incremental short-circuit: if the conv already exists with the same
    # content hash, there is nothing to re-embed. Done before any write so the
    # bulk `ingest-claude-code` re-scan stays cheap over unchanged sessions.
    if skip_unchanged and content_hash is not None:
        existing = repo.get_conversation(conn, conv.uuid)
        if existing is not None and existing.content_hash == content_hash:
            summary.skipped_unchanged_conversations += 1
            return

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

    # ---- Read + compute phase (NO writes, so the WAL write lock is not held) ----
    # All embedding work and repo matching happen BEFORE the first write
    # statement. Under WAL the write lock is acquired on the first DML and held
    # until commit; embedding a long chat can take seconds, so doing it here
    # keeps the lock window down to the fast write phase below. This matters
    # because `memex serve` and `memex-mcp` write to the same DB from two
    # processes (audit: cross-process SQLITE_BUSY).
    repo_matches = _match_repos(conn, full_text) if full_text else []
    prepared_chunks: list[tuple[Chunk, list[float]]] = []
    if full_text:
        spans = chunk_text(
            full_text,
            max_tokens=chunk_size_tokens,
            overlap_tokens=chunk_overlap_tokens,
            max_chunks=settings.max_chunks_per_conversation,
        )
        if spans and spans[-1].char_end < len(full_text):
            summary.truncated_conversations += 1
            logger.warning(
                "Conversation %s exceeded max_chunks_per_conversation (%d); truncating the tail.",
                conv.uuid,
                settings.max_chunks_per_conversation,
            )
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
                prepared_chunks.append((chunk, vec))

    # ---- Write phase (holds the WAL write lock; kept tight, no embedding) ----
    repo.insert_conversation(conn, conv)
    summary.conversations += 1

    for msg in messages:
        repo.insert_message(conn, msg)
        summary.messages += 1

    # Clean up old chunks so re-ingest is idempotent.
    repo.delete_chunks_for_conversation(conn, conv.uuid)

    if not full_text:
        return

    # Persist auto-detected repo associations. Manual ('manual' source)
    # associations are preserved by `repo.associate_chat_repo` (it refuses to
    # overwrite a manual tag).
    for match in repo_matches:
        repo.associate_chat_repo(
            conn,
            conv.uuid,
            match.repo_key,
            source="auto",
            confidence=match.confidence,
        )
    # Sessions carry their own repo via `cwd`: associate it directly at full
    # confidence (stronger than text matching). De-duped against text matches.
    text_matched = {m.repo_key for m in repo_matches}
    for repo_key in extra_repo_keys or []:
        if repo_key not in text_matched:
            repo.associate_chat_repo(conn, conv.uuid, repo_key, source="auto", confidence=1.0)

    for chunk, vec in prepared_chunks:
        repo.add_chunk(conn, chunk, vec)
        summary.chunks += 1


def _hash_content(text: str) -> str:
    """SHA-256 hex of the canonical text. Stable, enough to detect changes.
    Not cryptographic: just a fingerprint to compare."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _match_repos(conn: sqlite3.Connection, text: str) -> list[Any]:
    """Read registered repos and run the matcher (READ-only, no writes).

    Returns the list of matches (each with `.repo_key` and `.confidence`).
    The caller persists them as `source='auto'` associations inside the write
    phase. Splitting the read (here) from the write (caller) keeps the WAL
    write lock held only during the fast write phase.

    Idempotent downstream: re-ingesting unchanged content re-asserts the same
    set of associations. Manual tags survive because `associate_chat_repo`
    refuses to overwrite a `manual` with `auto`.

    Returns `[]` when there are no repos registered yet (very common: a fresh
    user has not run `memex repos add`). Matcher errors are not caught here;
    they would indicate a bug, not a runtime condition.
    """
    repos = repo.list_repos(conn)
    if not repos:
        return []
    return list(match_text(text, repos))


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
