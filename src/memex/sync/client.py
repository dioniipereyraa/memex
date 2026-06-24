"""Pull a peer's conversations into the local store (Phase 1, one-directional).

Flow: fetch the peer's manifest, refuse if its embedding model/dim differ from
this device's (the vectors would be incompatible), diff the manifest against the
local store by uuid + content_hash, request only the new/changed records, and
insert each through the repo so `chunks`/`vec_chunks`/`fts_chunks` stay
consistent. Vectors travel inside each record, so this side never re-embeds and
never loads the model.

Idempotent: a re-pull with nothing new fetches and inserts nothing. The HTTP
calls are injectable (`manifest_fn` / `fetch_fn`) so the diff/insert logic is
tested without a live socket; the CLI wires them to the urllib helpers here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage import repo
from memex.sync.peers import Peer

logger = logging.getLogger("memex.sync.client")

# Header carrying the peer's per-install token (matches http_ingest._TOKEN_HEADER).
_TOKEN_HEADER = "X-Memex-Token"
# How many uuids to request per /sync/conversations call. Kept under the
# server's `_MAX_SYNC_UUIDS` cap; a larger pull is split into several requests.
_DEFAULT_BATCH = 500
_MANIFEST_TIMEOUT = 30.0
_FETCH_TIMEOUT = 120.0


@dataclass
class PullSummary:
    """Result of one `pull`, for the CLI to report."""

    peer: str
    manifest_total: int
    to_fetch: int
    inserted: int
    failed: int
    local_model: str
    local_dim: int
    peer_model: str | None
    peer_dim: int | None
    refused_mismatch: bool = False


ManifestFn = Callable[[Peer], dict[str, Any]]
FetchFn = Callable[[Peer, list[str]], dict[str, Any]]


def _http_get_json(url: str, token: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={_TOKEN_HEADER: token}, method="GET")
    # The peer url is validated http(s) at pairing time (Peer._normalize_url).
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{url} returned a non-object JSON body")
    return data


def _http_post_json(url: str, token: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={_TOKEN_HEADER: token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{url} returned a non-object JSON body")
    return data


def fetch_manifest(peer: Peer) -> dict[str, Any]:
    return _http_get_json(f"{peer.url}/sync/manifest", peer.token, _MANIFEST_TIMEOUT)


def fetch_conversations(peer: Peer, uuids: list[str]) -> dict[str, Any]:
    return _http_post_json(
        f"{peer.url}/sync/conversations", peer.token, {"uuids": uuids}, _FETCH_TIMEOUT
    )


def _batched(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _insert_record(conn: sqlite3.Connection, record: dict[str, Any], expected_dim: int) -> None:
    """Insert one synced conversation (row + messages + chunks + vectors).

    Replaces any existing chunks for the uuid so a re-pull of a changed
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
        logger.warning(
            "Synced conversation %s references project %s with no project row; "
            "dropping the project link.",
            record.get("uuid"),
            project_uuid,
        )
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
        for m in record.get("messages", [])
    ]
    chunk_rows = record.get("chunks", [])
    # Validate every embedding before writing anything: a wrong dimension means
    # the peer's model drifted (the manifest guard should have caught it) and
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


def pull(
    conn: sqlite3.Connection,
    peer: Peer,
    *,
    local_model: str,
    local_dim: int,
    manifest_fn: ManifestFn | None = None,
    fetch_fn: FetchFn | None = None,
    batch_size: int = _DEFAULT_BATCH,
) -> PullSummary:
    """Pull new/changed conversations from `peer` into `conn`."""
    manifest_fn = manifest_fn or fetch_manifest
    fetch_fn = fetch_fn or fetch_conversations

    manifest = manifest_fn(peer)
    peer_model = manifest.get("embed_model")
    peer_dim = manifest.get("embed_dim")
    convs = manifest.get("conversations") or []

    if peer_model != local_model or peer_dim != local_dim:
        logger.warning(
            "Refusing sync with peer %s: model/dim mismatch (peer %s/%s, local %s/%s)",
            peer.name,
            peer_model,
            peer_dim,
            local_model,
            local_dim,
        )
        return PullSummary(
            peer=peer.name,
            manifest_total=len(convs),
            to_fetch=0,
            inserted=0,
            failed=0,
            local_model=local_model,
            local_dim=local_dim,
            peer_model=peer_model,
            peer_dim=peer_dim,
            refused_mismatch=True,
        )

    known = {
        row["uuid"]: row["content_hash"]
        for row in conn.execute("SELECT uuid, content_hash FROM conversations").fetchall()
    }
    to_fetch: list[str] = []
    for item in convs:
        uuid = item.get("uuid") if isinstance(item, dict) else None
        if not uuid:
            continue
        if uuid not in known or known[uuid] != item.get("content_hash"):
            to_fetch.append(uuid)

    inserted = 0
    failed = 0
    for batch in _batched(to_fetch, batch_size):
        resp = fetch_fn(peer, batch)
        # The peer could in principle re-embed under a new model between the
        # manifest and this call; re-check before trusting the vectors.
        if resp.get("embed_model") != local_model or resp.get("embed_dim") != local_dim:
            logger.warning("Peer %s changed model mid-pull; stopping.", peer.name)
            break
        for record in resp.get("conversations") or []:
            try:
                _insert_record(conn, record, local_dim)
                inserted += 1
            except Exception:
                logger.exception(
                    "Failed to insert synced conversation %s",
                    record.get("uuid") if isinstance(record, dict) else "<?>",
                )
                failed += 1

    return PullSummary(
        peer=peer.name,
        manifest_total=len(convs),
        to_fetch=len(to_fetch),
        inserted=inserted,
        failed=failed,
        local_model=local_model,
        local_dim=local_dim,
        peer_model=peer_model,
        peer_dim=peer_dim,
    )
