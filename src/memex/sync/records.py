"""Wire format for multi-device sync: serialize, insert, and diff conversations.

Shared by both sides so the server (`transports/http_ingest` endpoints) and the
client (`sync/client`) agree on the record shape and the insert path:

- `serialize_conversation`: a full transferable record (conversation row +
  messages + chunks + their vectors + the Project row for a design_chat).
- `insert_record`: insert one such record through the repo so
  `chunks`/`vec_chunks`/`fts_chunks` stay consistent and chunk ids are
  reassigned locally (never the peer's, which would collide).
- `local_manifest` + the diff helpers: decide what to pull/push.

Diff policy:
- `select_to_transfer` (one-directional pull or push): take the other side's
  version whenever the content hash differs. The caller's direction IS the
  intent (pull = peer authoritative, push = local authoritative).
- `select_reconcile` (bidirectional / auto-sync): last-writer-wins by
  `updated_at`, so a newer version is never overwritten by an older one. The
  rare same-timestamp-different-content fork is never overwritten and is reported
  back as `forks` for the caller to surface (the user resolves it with an
  explicit one-directional `pull`/`push`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from memex.config import settings
from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage import repo


def to_epoch(value: object) -> float:
    """Parse a stored ISO timestamp to epoch seconds (-inf if unparseable).

    Unparseable sorts oldest, so a record with a bad timestamp is treated as the
    least recent (it will not win a last-writer-wins comparison by accident).
    """
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def local_manifest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The local conversation manifest: uuid + content_hash + updated_at + source.

    Also carries `chunk_count` per conversation so a pulling peer can estimate a
    record's transfer size (the vectors dominate it) and batch by bytes instead
    of by a fixed count. No message bodies; still cheap to build and to send
    (the count uses the `idx_chunks_conversation` index). Used by the server's
    `/sync/manifest` and by the client when reconciling (it needs both sides).
    """
    rows = conn.execute(
        """
        SELECT c.uuid, c.content_hash, c.updated_at, c.source,
               (SELECT COUNT(*) FROM chunks k WHERE k.conversation_uuid = c.uuid)
                   AS chunk_count
        FROM conversations c
        """
    ).fetchall()
    return [
        {
            "uuid": row["uuid"],
            "content_hash": row["content_hash"],
            "updated_at": row["updated_at"],
            "source": row["source"],
            "chunk_count": row["chunk_count"],
        }
        for row in rows
    ]


def _by_uuid(manifest: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in manifest:
        # A manifest is attacker-shaped on both transports (a pushed body, a
        # snapshot header). The uuid must be a non-empty string: anything else
        # (a list, a number) would be unhashable here or poison the diff, and a
        # raise from the diff escapes the callers' per-file/per-peer guards.
        if isinstance(item, dict) and isinstance(item.get("uuid"), str) and item["uuid"]:
            out[item["uuid"]] = item
    return out


def select_to_transfer(have: list[dict[str, Any]], offered: list[dict[str, Any]]) -> list[str]:
    """uuids in `offered` that `have` is missing or has at a different content hash.

    One-directional: the offered side's version is taken whenever it differs.
    """
    mine = {item["uuid"]: item.get("content_hash") for item in _by_uuid(have).values()}
    out: list[str] = []
    for item in _by_uuid(offered).values():
        uuid = item["uuid"]
        if uuid not in mine or mine[uuid] != item.get("content_hash"):
            out.append(uuid)
    return out


def select_reconcile(
    local: list[dict[str, Any]], remote: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Bidirectional last-writer-wins split: (to_pull, to_push, forks).

    For each uuid: pull if the remote is absent-here or strictly newer; push if
    the local is absent-there or strictly newer; skip if the content hash matches.
    A uuid whose two sides differ in content but share the same `updated_at` is a
    fork: neither side is demonstrably newer, so it is NEVER overwritten and is
    returned in `forks` so the caller can surface it (the user resolves it with an
    explicit one-directional `pull`/`push --peer`). This is the documented v1
    conflict policy; a message-level merge is intentionally not attempted (it would
    require re-chunking and re-embedding the merged conversation, which the no
    re-embed design forbids).
    """
    local_by = _by_uuid(local)
    remote_by = _by_uuid(remote)
    to_pull: list[str] = []
    to_push: list[str] = []
    forks: list[str] = []
    for uuid in set(local_by) | set(remote_by):
        loc = local_by.get(uuid)
        rem = remote_by.get(uuid)
        if rem is None:
            to_push.append(uuid)
            continue
        if loc is None:
            to_pull.append(uuid)
            continue
        if loc.get("content_hash") == rem.get("content_hash"):
            continue
        local_t = to_epoch(loc.get("updated_at"))
        remote_t = to_epoch(rem.get("updated_at"))
        if remote_t > local_t:
            to_pull.append(uuid)
        elif local_t > remote_t:
            to_push.append(uuid)
        else:
            # Same timestamp, different content: a fork. Leave both untouched and
            # report it; the user picks the winning side with `pull`/`push --peer`.
            forks.append(uuid)
    return to_pull, to_push, forks


def serialize_conversation(conn: sqlite3.Connection, uuid: str) -> dict[str, Any] | None:
    """Full transferable record for one conversation: row + messages + chunks.

    Chunks carry their stored embedding so the receiver never re-embeds. When the
    conversation belongs to a Project (a `design_chat`), its project row travels
    too, so the receiver can satisfy the `conversations.project_uuid` foreign key
    without a separate project sync. Returns None if the uuid is unknown.
    """
    conv = repo.get_conversation(conn, uuid)
    if conv is None:
        return None
    messages = repo.get_messages_for_conversation(conn, uuid)
    chunks = repo.get_chunks_with_embeddings_for_conversation(conn, uuid)
    project: dict[str, Any] | None = None
    if conv.project_uuid is not None:
        proj = repo.get_project(conn, conv.project_uuid)
        if proj is not None:
            project = {
                "uuid": proj.uuid,
                "name": proj.name,
                "description": proj.description,
                "prompt_template": proj.prompt_template,
                "is_private": proj.is_private,
                "is_starter_project": proj.is_starter_project,
                "creator_uuid": proj.creator_uuid,
                "creator_name": proj.creator_name,
                "created_at": proj.created_at.isoformat(),
                "updated_at": proj.updated_at.isoformat(),
            }
    return {
        "uuid": conv.uuid,
        "title": conv.title,
        "summary": conv.summary,
        "source": conv.source.value,
        "project_uuid": conv.project_uuid,
        "project": project,
        "account_uuid": conv.account_uuid,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
        "content_hash": conv.content_hash,
        "messages": [
            {
                "uuid": m.uuid,
                "conversation_uuid": m.conversation_uuid,
                "parent_uuid": m.parent_uuid,
                "sender": m.sender.value,
                "text": m.text,
                "raw_content": m.raw_content,
                "has_tool_use": m.has_tool_use,
                "has_attachments": m.has_attachments,
                "created_at": m.created_at.isoformat(),
                "updated_at": m.updated_at.isoformat(),
            }
            for m in messages
        ],
        "chunks": [
            {
                "message_uuid": chunk.message_uuid,
                "sender": chunk.sender,
                "text": chunk.text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "created_at": chunk.created_at.isoformat(),
                "embedding": embedding,
            }
            for chunk, embedding in chunks
        ],
    }


def insert_record(conn: sqlite3.Connection, record: dict[str, Any], expected_dim: int) -> None:
    """Insert one synced conversation (row + messages + chunks + vectors).

    Replaces any existing chunks for the uuid so a re-sync of a changed
    conversation is a clean swap (chunk ids are local and reassigned, never the
    peer's, so two devices never collide on ids). Wrapped in a transaction.
    """
    project_data = record.get("project")
    project = Project(**project_data) if isinstance(project_data, dict) else None
    project_uuid = record.get("project_uuid")
    # A design_chat references a project via a foreign key. If the peer did not
    # ship the project row (older peer, or a dangling reference), drop the link
    # rather than fail the whole insert on the FK; the conversation still syncs.
    if project_uuid is not None and project is None:
        project_uuid = None

    conv = Conversation(
        uuid=record["uuid"],
        title=record["title"],
        summary=record.get("summary"),
        source=Source(record["source"]),
        project_uuid=project_uuid,
        account_uuid=record.get("account_uuid"),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        content_hash=record.get("content_hash"),
    )
    # A pushed record is attacker-shaped (the peer controls every field). Reject a
    # non-list messages/chunks rather than iterating it into a confusing crash.
    raw_messages = record.get("messages", [])
    chunk_rows = record.get("chunks", [])
    if not isinstance(raw_messages, list) or not isinstance(chunk_rows, list):
        raise ValueError("record 'messages'/'chunks' must be lists")
    # Bound the chunk fan-out: the sync insert path does not go through the ingest
    # pipeline, so it would otherwise skip the per-conversation chunk cap that
    # bounds the embed/store amplification of one record. The request body cap is
    # the outer bound; this keeps one record from inserting an unbounded number of
    # rows even within that.
    max_chunks = settings.max_chunks_per_conversation
    if len(chunk_rows) > max_chunks:
        raise ValueError(f"record has {len(chunk_rows)} chunks > cap {max_chunks}")
    messages = [
        Message(
            uuid=m["uuid"],
            conversation_uuid=conv.uuid,
            parent_uuid=m.get("parent_uuid"),
            sender=Sender(m["sender"]),
            text=m.get("text", ""),
            raw_content=m.get("raw_content"),
            has_tool_use=bool(m.get("has_tool_use", False)),
            has_attachments=bool(m.get("has_attachments", False)),
            created_at=m["created_at"],
            updated_at=m["updated_at"],
        )
        for m in raw_messages
    ]
    # Validate every embedding before writing anything: a wrong dimension means
    # the peer's model drifted (the model/dim guard should have caught it) and
    # inserting it would corrupt vector search.
    for ch in chunk_rows:
        emb = ch.get("embedding")
        if not isinstance(emb, list) or len(emb) != expected_dim:
            got = len(emb) if isinstance(emb, list) else "missing"
            raise ValueError(f"chunk embedding dim {got} != expected {expected_dim}")

    with conn:
        if project is not None:
            repo.insert_project(conn, project)
        repo.insert_conversation(conn, conv)
        for msg in messages:
            repo.insert_message(conn, msg)
        # Clear old chunks (vec + fts together) before re-adding, so a changed
        # conversation does not leave stale vectors behind.
        repo.delete_chunks_for_conversation(conn, conv.uuid)
        for ch in chunk_rows:
            chunk = Chunk(
                conversation_uuid=conv.uuid,
                message_uuid=ch.get("message_uuid"),
                sender=ch.get("sender"),
                text=ch["text"],
                char_start=ch["char_start"],
                char_end=ch["char_end"],
                created_at=ch["created_at"],
            )
            repo.add_chunk(conn, chunk, ch["embedding"])
