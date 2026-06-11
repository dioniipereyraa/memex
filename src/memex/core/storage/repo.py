"""Repository: CRUD over the SQLite schema.

Design:
- Functions, not classes. Each function takes a `sqlite3.Connection` and
  pydantic models. The caller manages the connection and transaction
  lifecycles.
- Upserts (`ON CONFLICT DO UPDATE`) so re-ingesting the same export does
  not fail.
- datetime <-> TEXT conversion (ISO 8601 with Z suffix) at the boundaries.
- Chunks + vec_chunks are inserted together through `add_chunk()` to
  keep them in sync. To wrap in a transaction, use `with conn:`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlite_vec

from memex.core.models import (
    Chunk,
    Conversation,
    Message,
    Project,
    SearchHit,
    Sender,
    Source,
)

if TYPE_CHECKING:
    from memex.core.repos.discovery import ChatRepoAssociation, RepoInfo


def _to_iso(dt: datetime) -> str:
    """Serialize datetime to ISO 8601 UTC with a Z suffix.

    If `dt` has no tzinfo, assume UTC. If it has a different timezone,
    convert to UTC first. The conversion uses explicit strftime (not
    `replace("+00:00", "Z")`, which breaks for non-UTC timezones after
    conversion).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"


def _from_iso(s: str) -> datetime:
    """Parse ISO 8601 to datetime. Accepts Z suffix (converts to +00:00)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- Projects ----------


def insert_project(conn: sqlite3.Connection, project: Project) -> None:
    conn.execute(
        """
        INSERT INTO projects (
            uuid, name, description, prompt_template,
            is_private, is_starter_project,
            creator_uuid, creator_name,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            prompt_template = excluded.prompt_template,
            is_private = excluded.is_private,
            is_starter_project = excluded.is_starter_project,
            creator_uuid = excluded.creator_uuid,
            creator_name = excluded.creator_name,
            updated_at = excluded.updated_at
        """,
        (
            project.uuid,
            project.name,
            project.description,
            project.prompt_template,
            int(project.is_private),
            int(project.is_starter_project),
            project.creator_uuid,
            project.creator_name,
            _to_iso(project.created_at),
            _to_iso(project.updated_at),
        ),
    )


def get_project(conn: sqlite3.Connection, uuid: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE uuid = ?", (uuid,)).fetchone()
    return _row_to_project(row) if row else None


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    return [_row_to_project(r) for r in rows]


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        uuid=row["uuid"],
        name=row["name"],
        description=row["description"],
        prompt_template=row["prompt_template"],
        is_private=bool(row["is_private"]),
        is_starter_project=bool(row["is_starter_project"]),
        creator_uuid=row["creator_uuid"],
        creator_name=row["creator_name"],
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
    )


# ---------- Conversations ----------


def insert_conversation(conn: sqlite3.Connection, conv: Conversation) -> None:
    conn.execute(
        """
        INSERT INTO conversations (
            uuid, title, summary, source, project_uuid, account_uuid,
            created_at, updated_at, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            title = excluded.title,
            summary = excluded.summary,
            source = excluded.source,
            project_uuid = excluded.project_uuid,
            account_uuid = excluded.account_uuid,
            updated_at = excluded.updated_at,
            content_hash = excluded.content_hash
        """,
        (
            conv.uuid,
            conv.title,
            conv.summary,
            conv.source.value,
            conv.project_uuid,
            conv.account_uuid,
            _to_iso(conv.created_at),
            _to_iso(conv.updated_at),
            conv.content_hash,
        ),
    )


def get_conversation(conn: sqlite3.Connection, uuid: str) -> Conversation | None:
    row = conn.execute("SELECT * FROM conversations WHERE uuid = ?", (uuid,)).fetchone()
    return _row_to_conversation(row) if row else None


def list_recent_conversations(
    conn: sqlite3.Connection,
    limit: int = 20,
    source: Source | None = None,
) -> list[Conversation]:
    if source is None:
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM conversations
            WHERE source = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (source.value, limit),
        ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def update_conversation_summary(
    conn: sqlite3.Connection,
    uuid: str,
    summary: str,
) -> bool:
    """Update only the `summary` field of a conversation.

    Designed for the lazy summarizer in `tools.search_chats`: once the
    summary is generated against the LLM, we persist it without
    touching the rest of the row. Does not change `content_hash` or
    `updated_at` because the conv did not change, only a new derivative
    was added.

    Returns True if a row was updated, False if the uuid did not exist.
    """
    cursor = conn.execute(
        "UPDATE conversations SET summary = ? WHERE uuid = ?",
        (summary, uuid),
    )
    return cursor.rowcount > 0


def get_conversation_text(conn: sqlite3.Connection, uuid: str) -> str:
    """Rebuild the canonical text of a conversation from its messages.

    Returns the same shape the pipeline uses when chunking: each message
    preceded by `[sender]\\n`, separated by a blank line. If the
    conversation does not exist or has no messages with text, returns "".

    Útil para el lazy summarizer en `tools.search_chats`: dado el uuid de un
    chat candidato, reconstruye el texto sin meter pipeline en el call site.
    """
    rows = conn.execute(
        """
        SELECT sender, text
        FROM messages
        WHERE conversation_uuid = ? AND text != ''
        ORDER BY created_at ASC
        """,
        (uuid,),
    ).fetchall()
    if not rows:
        return ""
    parts = [f"[{row['sender']}]\n{row['text']}" for row in rows]
    return "\n\n".join(parts) + "\n\n"


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    # `content_hash` may not be present on very old DBs if the migration
    # has not run yet; defensive just in case.
    try:
        content_hash = row["content_hash"]
    except (IndexError, KeyError):
        content_hash = None
    return Conversation(
        uuid=row["uuid"],
        title=row["title"],
        summary=row["summary"],
        source=Source(row["source"]),
        project_uuid=row["project_uuid"],
        account_uuid=row["account_uuid"],
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
        content_hash=content_hash,
    )


# ---------- Messages ----------


def insert_message(conn: sqlite3.Connection, msg: Message) -> None:
    raw_json = json.dumps(msg.raw_content, ensure_ascii=False) if msg.raw_content else None
    conn.execute(
        """
        INSERT INTO messages (
            uuid, conversation_uuid, parent_uuid, sender, text, raw_content,
            has_tool_use, has_attachments, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            parent_uuid = excluded.parent_uuid,
            sender = excluded.sender,
            text = excluded.text,
            raw_content = excluded.raw_content,
            has_tool_use = excluded.has_tool_use,
            has_attachments = excluded.has_attachments,
            updated_at = excluded.updated_at
        """,
        (
            msg.uuid,
            msg.conversation_uuid,
            msg.parent_uuid,
            msg.sender.value,
            msg.text,
            raw_json,
            int(msg.has_tool_use),
            int(msg.has_attachments),
            _to_iso(msg.created_at),
            _to_iso(msg.updated_at),
        ),
    )


def get_messages_for_conversation(
    conn: sqlite3.Connection, conversation_uuid: str
) -> list[Message]:
    rows = conn.execute(
        """
        SELECT * FROM messages
        WHERE conversation_uuid = ?
        ORDER BY created_at ASC
        """,
        (conversation_uuid,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def _row_to_message(row: sqlite3.Row) -> Message:
    raw = row["raw_content"]
    parsed: list[dict[str, Any]] | None = json.loads(raw) if raw else None
    return Message(
        uuid=row["uuid"],
        conversation_uuid=row["conversation_uuid"],
        parent_uuid=row["parent_uuid"],
        sender=Sender(row["sender"]),
        text=row["text"],
        raw_content=parsed,
        has_tool_use=bool(row["has_tool_use"]),
        has_attachments=bool(row["has_attachments"]),
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
    )


# ---------- Chunks + embeddings ----------


def add_chunk(
    conn: sqlite3.Connection,
    chunk: Chunk,
    embedding: Sequence[float],
) -> int:
    """Insert a chunk and its embedding in a single logical operation.

    Returns the auto-assigned `chunks.id`, which is also the rowid in
    `vec_chunks`.
    Si el chunk ya tiene id seteado (re-ingest), lo respeta y hace UPSERT.
    """
    if chunk.id is None:
        cursor = conn.execute(
            """
            INSERT INTO chunks (
                conversation_uuid, message_uuid, sender, text,
                char_start, char_end, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.conversation_uuid,
                chunk.message_uuid,
                chunk.sender,
                chunk.text,
                chunk.char_start,
                chunk.char_end,
                _to_iso(chunk.created_at),
            ),
        )
        chunk_id = cursor.lastrowid
        if chunk_id is None:
            raise RuntimeError("INSERT into chunks did not return lastrowid")
    else:
        conn.execute(
            """
            INSERT INTO chunks (
                id, conversation_uuid, message_uuid, sender, text,
                char_start, char_end, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                char_start = excluded.char_start,
                char_end = excluded.char_end
            """,
            (
                chunk.id,
                chunk.conversation_uuid,
                chunk.message_uuid,
                chunk.sender,
                chunk.text,
                chunk.char_start,
                chunk.char_end,
                _to_iso(chunk.created_at),
            ),
        )
        chunk_id = chunk.id

    # Replace any existing embedding (DELETE + INSERT; vec0 does not support UPSERT).
    conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(list(embedding))),
    )

    # Sync the lexical FTS index. fts5 does not support direct UPSERT either.
    conn.execute("DELETE FROM fts_chunks WHERE rowid = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO fts_chunks(rowid, text) VALUES (?, ?)",
        (chunk_id, chunk.text),
    )
    return chunk_id


def get_chunk(conn: sqlite3.Connection, chunk_id: int) -> Chunk | None:
    row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return _row_to_chunk(row) if row else None


def count_chunks(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    return int(row["n"])


def delete_chunks_for_conversation(conn: sqlite3.Connection, conversation_uuid: str) -> int:
    """Delete all chunks (with embeddings and FTS) of a conversation.

    Needed before re-chunking a conversation: new inserts do not replace
    old ones (chunks ids are AUTOINCREMENT), and neither `vec_chunks` nor
    `fts_chunks` cascade automatically. Returns the count of
    de chunks borrados.
    """
    rows = conn.execute(
        "SELECT id FROM chunks WHERE conversation_uuid = ?",
        (conversation_uuid,),
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({placeholders})", ids)
    conn.execute("DELETE FROM chunks WHERE conversation_uuid = ?", (conversation_uuid,))
    return len(ids)


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=row["id"],
        conversation_uuid=row["conversation_uuid"],
        message_uuid=row["message_uuid"],
        sender=row["sender"],
        text=row["text"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        created_at=_from_iso(row["created_at"]),
    )


def vector_search(
    conn: sqlite3.Connection,
    query_embedding: Sequence[float],
    limit: int = 10,
    dedupe_by_conversation: bool = True,
) -> list[SearchHit]:
    """K-NN similarity search over `vec_chunks`. Joins chunks and conversations.

    Returns `SearchHit` with the chunk, the conversation, and the L2
    distance (lower = more similar). For normalized embeddings (like
    nomic-embed-text), L2 order matches cosine order.

    If `dedupe_by_conversation` is True (default), returns at most one
    chunk per conversation: the closest one. This prevents the top-N
    from being dominated by several chunks of the same chat. To get
    enough unique conversations, we request `limit * OVERSAMPLE_FACTOR`
    chunks from vec_chunks and dedupe in Python.

    If `dedupe_by_conversation` is False, returns the `limit` closest
    chunks regardless of which chat they belong to. Useful for analysis
    or debugging.

    Note: when JOINs are involved, sqlite-vec requires restricting `k`
    in the WHERE clause instead of relying on LIMIT (because LIMIT is
    evaluated after the join). That is why `k = ?` is passed in addition
    to `MATCH ?`.
    """
    serialized = sqlite_vec.serialize_float32(list(query_embedding))
    oversample_factor = 5
    k = limit * oversample_factor if dedupe_by_conversation else limit
    rows = conn.execute(
        """
        SELECT
            c.id AS chunk_id, c.conversation_uuid, c.message_uuid, c.sender,
            c.text AS chunk_text, c.char_start, c.char_end,
            c.created_at AS chunk_created_at,
            v.distance AS distance,
            conv.title AS conv_title, conv.summary AS conv_summary,
            conv.source AS conv_source, conv.project_uuid AS conv_project_uuid,
            conv.account_uuid AS conv_account_uuid,
            conv.created_at AS conv_created_at, conv.updated_at AS conv_updated_at
        FROM vec_chunks v
        JOIN chunks c ON c.id = v.rowid
        JOIN conversations conv ON conv.uuid = c.conversation_uuid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (serialized, k),
    ).fetchall()

    hits: list[SearchHit] = []
    seen_convs: set[str] = set()
    for row in rows:
        if dedupe_by_conversation:
            conv_uuid = row["conversation_uuid"]
            if conv_uuid in seen_convs:
                continue
            seen_convs.add(conv_uuid)
        hits.append(_row_to_search_hit(row))
        if len(hits) >= limit:
            break
    return hits


def text_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    dedupe_by_conversation: bool = True,
) -> list[SearchHit]:
    """Lexical BM25 search over `fts_chunks`. Joins chunks and conversations.

    FTS5 returns the `rank` column with the BM25 score (more negative =
    better match). `SearchHit.distance` is set to that rank as-is so the
    "lower = better" semantics stays consistent with `vector_search`.

    The schema tokenizer (unicode61, remove_diacritics=2) normalizes the
    query the same way as the indexed text: "amarok" matches "Amarók",
    "AMAROK", etc.

    If the query is not valid for FTS5 (special unescaped chars, broken
    operators), returns an empty list instead of propagating the error.
    """
    if not query.strip():
        return []

    oversample_factor = 5
    fts_limit = limit * oversample_factor if dedupe_by_conversation else limit
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []

    try:
        rows = conn.execute(
            """
            SELECT
                c.id AS chunk_id, c.conversation_uuid, c.message_uuid, c.sender,
                c.text AS chunk_text, c.char_start, c.char_end,
                c.created_at AS chunk_created_at,
                f.rank AS distance,
                conv.title AS conv_title, conv.summary AS conv_summary,
                conv.source AS conv_source, conv.project_uuid AS conv_project_uuid,
                conv.account_uuid AS conv_account_uuid,
                conv.created_at AS conv_created_at, conv.updated_at AS conv_updated_at
            FROM fts_chunks f
            JOIN chunks c ON c.id = f.rowid
            JOIN conversations conv ON conv.uuid = c.conversation_uuid
            WHERE fts_chunks MATCH ?
            ORDER BY f.rank
            LIMIT ?
            """,
            (fts_query, fts_limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 rejects malformed queries (odd operators, unbalanced parens).
        # We prefer to return empty rather than show the client an obscure error.
        return []

    hits: list[SearchHit] = []
    seen_convs: set[str] = set()
    for row in rows:
        if dedupe_by_conversation:
            conv_uuid = row["conversation_uuid"]
            if conv_uuid in seen_convs:
                continue
            seen_convs.add(conv_uuid)
        hits.append(_row_to_search_hit(row))
        if len(hits) >= limit:
            break
    return hits


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    query_embedding: Sequence[float],
    limit: int = 10,
    dedupe_by_conversation: bool = True,
    rrf_k: int = 60,
) -> list[SearchHit]:
    """Combine `vector_search` and `text_search` with Reciprocal Rank Fusion.

    RRF assigns each chunk a score = sum of 1 / (rrf_k + rank_i) over
    the rankings where it appears. `rrf_k=60` is the canonical default
    (Cormack 2009); values between 20 and 100 are reasonable. The
    combined score is robust to the different scales of L2 distance vs
    BM25 rank.

    Requests `limit * 5` candidates from each engine to ensure good
    coverage before fusing. Then applies per-conversation dedup if
    applicable.

    The resulting `SearchHit.distance` is `-rrf_score` to keep the
    "lower = better" convention used elsewhere in the repo.
    """
    if limit < 1:
        limit = 1

    oversample_factor = 5
    fetch_limit = limit * oversample_factor

    vec_hits = vector_search(conn, query_embedding, limit=fetch_limit, dedupe_by_conversation=False)
    text_hits = text_search(conn, query, limit=fetch_limit, dedupe_by_conversation=False)

    # RRF: each chunk accumulates score by appearing in each list.
    scores: dict[int, float] = {}
    hits_by_id: dict[int, SearchHit] = {}
    for rank, hit in enumerate(vec_hits, start=1):
        if hit.chunk.id is None:
            continue
        scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + 1.0 / (rrf_k + rank)
        hits_by_id.setdefault(hit.chunk.id, hit)
    for rank, hit in enumerate(text_hits, start=1):
        if hit.chunk.id is None:
            continue
        scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + 1.0 / (rrf_k + rank)
        hits_by_id.setdefault(hit.chunk.id, hit)

    if not scores:
        return []

    # Sort by descending score, dedupe by conversation if applicable.
    ordered_ids = sorted(scores, key=lambda cid: -scores[cid])
    result: list[SearchHit] = []
    seen_convs: set[str] = set()
    for cid in ordered_ids:
        base = hits_by_id[cid]
        if dedupe_by_conversation:
            if base.conversation.uuid in seen_convs:
                continue
            seen_convs.add(base.conversation.uuid)
        # Replace the original distance with -score so "lower = better".
        merged = base.model_copy(update={"distance": -scores[cid]})
        result.append(merged)
        if len(result) >= limit:
            break
    return result


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Repopulate `fts_chunks` from `chunks`. Useful for pre-existing DBs
    upgraded to the FTS5 schema without re-ingesting.

    Deletes the current FTS index content and re-inserts all chunks.
    Returns the number of rows indexed. Does not touch `chunks` or
    `vec_chunks`.

    Self-contained maintenance operation, so it commits at the end
    (unlike `insert_*` functions which leave the commit to the caller).
    If you plan more writes in the same transaction, call it explicitly
    with the appropriate `conn.in_transaction` handling and commit or
    rollback yourself.
    """
    conn.execute("DELETE FROM fts_chunks")
    cursor = conn.execute("INSERT INTO fts_chunks(rowid, text) SELECT id, text FROM chunks")
    count = cursor.rowcount if cursor.rowcount is not None else 0
    conn.commit()
    return count


def _sanitize_fts_query(query: str) -> str:
    """Convert a free-form query into one valid for FTS5.

    FTS5 accepts a mini-language (AND, OR, NOT, NEAR, quotes, etc.) that
    breaks if the user types things with parens, stray quotes, etc. For
    casual search the safest path is to keep alphanumeric words and
    quote them as an implicit phrase: "word1 word2 ..."

    If all words are filtered out, returns "".
    """
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    # Each word in double quotes so FTS5 does not interpret them as
    # operators. The quotes force exact-token matching.
    return " ".join(f'"{t}"' for t in tokens)


def _row_to_search_hit(row: sqlite3.Row) -> SearchHit:
    chunk = Chunk(
        id=row["chunk_id"],
        conversation_uuid=row["conversation_uuid"],
        message_uuid=row["message_uuid"],
        sender=row["sender"],
        text=row["chunk_text"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        created_at=_from_iso(row["chunk_created_at"]),
    )
    conv = Conversation(
        uuid=row["conversation_uuid"],
        title=row["conv_title"],
        summary=row["conv_summary"],
        source=Source(row["conv_source"]),
        project_uuid=row["conv_project_uuid"],
        account_uuid=row["conv_account_uuid"],
        created_at=_from_iso(row["conv_created_at"]),
        updated_at=_from_iso(row["conv_updated_at"]),
    )
    snippet = chunk.text[:280] + ("…" if len(chunk.text) > 280 else "")
    return SearchHit(chunk=chunk, conversation=conv, distance=row["distance"], snippet=snippet)


# ---------- Repos and chat-to-repo associations ----------


def insert_repo(conn: sqlite3.Connection, info: RepoInfo) -> None:
    """Upsert a repo by its canonical key.

    Re-registering an existing key refreshes its mutable fields (path,
    remote_url, name, manifest_name) but keeps `registered_at`. Cascade
    deletes are handled by the FK with `chat_repos.repo_key`.
    """
    conn.execute(
        """
        INSERT INTO repos (key, path, remote_url, name, manifest_name, registered_at)
        VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            path = excluded.path,
            remote_url = excluded.remote_url,
            name = excluded.name,
            manifest_name = excluded.manifest_name
        """,
        (info.key, info.path, info.remote_url, info.name, info.manifest_name),
    )


def get_repo(conn: sqlite3.Connection, key: str) -> RepoInfo | None:
    row = conn.execute("SELECT * FROM repos WHERE key = ?", (key,)).fetchone()
    return _row_to_repo_info(row) if row else None


def get_repo_by_path(conn: sqlite3.Connection, path: str) -> RepoInfo | None:
    """Find a registered repo by its local `path`.

    A repo with a git remote is keyed by the remote URL, not its path, so a
    plain path lookup (`get_repo`) misses it. This resolves a working
    directory to its repo regardless of how the repo was keyed.
    """
    row = conn.execute("SELECT * FROM repos WHERE path = ?", (path,)).fetchone()
    return _row_to_repo_info(row) if row else None


def list_repos(conn: sqlite3.Connection) -> list[RepoInfo]:
    """All registered repos, ordered by name (case-insensitive)."""
    rows = conn.execute("SELECT * FROM repos ORDER BY LOWER(name)").fetchall()
    return [_row_to_repo_info(r) for r in rows]


def delete_repo(conn: sqlite3.Connection, key: str) -> bool:
    """Remove a repo. Returns True if a row was deleted.

    `chat_repos` rows are removed by the ON DELETE CASCADE FK.
    """
    cursor = conn.execute("DELETE FROM repos WHERE key = ?", (key,))
    return cursor.rowcount > 0


def associate_chat_repo(
    conn: sqlite3.Connection,
    conversation_uuid: str,
    repo_key: str,
    *,
    source: str = "auto",
    confidence: float | None = None,
) -> None:
    """Upsert one association row.

    On conflict (same conv + same repo), the new `source` and `confidence`
    win. Typical case: a manual `memex tag` overrides an auto association
    by re-calling with `source='manual'`, and the next ingest re-asserts
    auto without losing the manual record (we keep the more authoritative
    one, see `_priority_source`).
    """
    if source not in ("auto", "manual"):
        raise ValueError(f"Invalid source: {source!r}. Expected 'auto' or 'manual'.")

    # If a manual association exists, do not let an `auto` rewrite it back.
    existing = conn.execute(
        "SELECT source FROM chat_repos WHERE conversation_uuid = ? AND repo_key = ?",
        (conversation_uuid, repo_key),
    ).fetchone()
    if existing is not None and existing["source"] == "manual" and source == "auto":
        return

    conn.execute(
        """
        INSERT INTO chat_repos (conversation_uuid, repo_key, source, confidence, associated_at)
        VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(conversation_uuid, repo_key) DO UPDATE SET
            source = excluded.source,
            confidence = excluded.confidence
        """,
        (conversation_uuid, repo_key, source, confidence),
    )


def dissociate_chat_repo(
    conn: sqlite3.Connection,
    conversation_uuid: str,
    repo_key: str,
) -> bool:
    """Remove an association. Returns True if a row was deleted."""
    cursor = conn.execute(
        "DELETE FROM chat_repos WHERE conversation_uuid = ? AND repo_key = ?",
        (conversation_uuid, repo_key),
    )
    return cursor.rowcount > 0


def list_repos_for_conversation(
    conn: sqlite3.Connection, conversation_uuid: str
) -> list[ChatRepoAssociation]:
    """Repos associated to a given conv, with source + confidence per row.

    Hydrates the full `RepoInfo` from the `repos` table via JOIN.
    """
    rows = conn.execute(
        """
        SELECT
            r.key, r.path, r.remote_url, r.name, r.manifest_name,
            cr.source, cr.confidence
        FROM chat_repos cr
        JOIN repos r ON r.key = cr.repo_key
        WHERE cr.conversation_uuid = ?
        ORDER BY cr.confidence DESC NULLS LAST, r.name
        """,
        (conversation_uuid,),
    ).fetchall()
    from memex.core.repos.discovery import ChatRepoAssociation

    return [
        ChatRepoAssociation(
            repo=_row_to_repo_info(r),
            source=r["source"],
            confidence=r["confidence"],
        )
        for r in rows
    ]


def list_conversations_for_repo(
    conn: sqlite3.Connection, repo_key: str
) -> list[tuple[str, str, float | None]]:
    """List `(conversation_uuid, source, confidence)` tuples for one repo.

    Used by `search_chats` boost: given the active repo, we get the set of
    conv uuids to score higher.
    """
    rows = conn.execute(
        """
        SELECT conversation_uuid, source, confidence
        FROM chat_repos
        WHERE repo_key = ?
        """,
        (repo_key,),
    ).fetchall()
    return [(r["conversation_uuid"], r["source"], r["confidence"]) for r in rows]


def _row_to_repo_info(row: sqlite3.Row) -> RepoInfo:
    """Reconstruct a `RepoInfo` from a `repos` table row.

    The import is local to avoid a circular import: `core/repos/` does
    not import from `core/storage/` either, so this keeps the deps a tree
    instead of a graph.
    """
    from memex.core.repos.discovery import RepoInfo

    return RepoInfo(
        key=row["key"],
        path=row["path"],
        remote_url=row["remote_url"],
        name=row["name"],
        manifest_name=row["manifest_name"],
    )
