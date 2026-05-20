"""Repositorio: CRUD sobre el schema SQLite.

Diseño:
- Funciones, no clases. Cada función toma una `sqlite3.Connection` y modelos
  pydantic. El llamador maneja el ciclo de vida de la conexión y de la transacción.
- Upserts (`ON CONFLICT DO UPDATE`) para que reingestar el mismo export no falle.
- Conversión datetime ↔ TEXT (ISO 8601 con sufijo Z) en los bordes.
- Chunks + vec_chunks se insertan juntos a través de `add_chunk()` para mantenerlos
  sincronizados. Si querés transacción, envolvé las llamadas en `with conn:`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

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


def _to_iso(dt: datetime) -> str:
    """Serializa datetime a ISO 8601 UTC con sufijo Z.

    Si `dt` no tiene tzinfo, asume UTC. Si tiene otra timezone, convierte a UTC
    primero. La conversión usa strftime explícito (no `replace("+00:00", "Z")`,
    que rompe con timezones no-UTC convertidas).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"


def _from_iso(s: str) -> datetime:
    """Parsea ISO 8601 a datetime. Acepta sufijo Z (lo convierte a +00:00)."""
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
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            title = excluded.title,
            summary = excluded.summary,
            source = excluded.source,
            project_uuid = excluded.project_uuid,
            account_uuid = excluded.account_uuid,
            updated_at = excluded.updated_at
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


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        uuid=row["uuid"],
        title=row["title"],
        summary=row["summary"],
        source=Source(row["source"]),
        project_uuid=row["project_uuid"],
        account_uuid=row["account_uuid"],
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
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
    """Inserta un chunk y su embedding en una sola operación lógica.

    Devuelve el `chunks.id` autoasignado, que es también el rowid en `vec_chunks`.
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
            raise RuntimeError("INSERT en chunks no devolvió lastrowid")
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

    # Reemplazar embedding existente si lo había (DELETE + INSERT, no hay UPSERT en vec0).
    conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(list(embedding))),
    )

    # Sincronizar el índice FTS lexical. fts5 tampoco soporta UPSERT directo.
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
    """Borra todos los chunks (con embeddings y FTS) de una conversación.

    Necesario antes de re-chunkear una conversación: las inserciones nuevas no
    reemplazan las viejas (los ids son AUTOINCREMENT en `chunks`), y ni
    `vec_chunks` ni `fts_chunks` cascadean automáticamente. Devuelve la cantidad
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
    """Búsqueda K-NN por similitud sobre `vec_chunks`. Une chunks y conversations.

    Devuelve `SearchHit` con el chunk, la conversación y la distancia (L2). Más bajo
    = más parecido. Para embeddings normalizados (como nomic-embed-text) el orden L2
    coincide con el orden por coseno.

    Si `dedupe_by_conversation` es True (default), devuelve a lo sumo un chunk por
    conversación: el más cercano. Esto evita que el top-N esté dominado por varios
    chunks del mismo chat. Para conseguir suficientes conversaciones únicas, se piden
    `limit * OVERSAMPLE_FACTOR` chunks a vec_chunks y se dedupea en Python.

    Si `dedupe_by_conversation` es False, devuelve los `limit` chunks más cercanos
    sin importar a qué chat pertenecen. Útil para análisis o debugging.

    Nota: cuando hay JOINs, sqlite-vec exige restringir `k` en el WHERE en vez de
    apoyarse en LIMIT (porque el LIMIT se evalúa después del join). Por eso se pasa
    `k = ?` además de `MATCH ?`.
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
    """Búsqueda lexical BM25 sobre `fts_chunks`. Une chunks y conversations.

    FTS5 devuelve la columna `rank` con el score BM25 (más negativo = mejor
    match). `SearchHit.distance` se llena con ese rank tal cual para que la
    semántica "menor = mejor" sea consistente con `vector_search`.

    El tokenizer del schema (unicode61, remove_diacritics=2) hace que la query
    se normalice igual que el texto indexado: "amarok" matchea "Amarók",
    "AMAROK", etc.

    Si la query no es válida para FTS5 (caracteres especiales sin escapar,
    operadores rotos), devuelve lista vacía en vez de propagar el error.
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
        # FTS5 rechaza queries malformadas (operadores raros, paréntesis sueltos).
        # Preferimos devolver vacío a que el cliente vea un error oscuro.
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
    """Combina `vector_search` y `text_search` con Reciprocal Rank Fusion.

    RRF asigna a cada chunk un score = Σ 1 / (rrf_k + rank_i) sumando sobre
    los rankings donde aparece. `rrf_k=60` es el default canónico (Cormack
    2009); valores entre 20 y 100 son razonables. El score combinado es
    robusto a las distintas escalas de distancia L2 vs rank BM25.

    Pide `limit * 5` candidatos a cada motor para tener buena cobertura antes
    de fusionar. Después aplica dedup por conversación si corresponde.

    El `distance` del `SearchHit` resultante es `-rrf_score` para mantener la
    convención "menor = mejor" del resto del repo.
    """
    if limit < 1:
        limit = 1

    oversample_factor = 5
    fetch_limit = limit * oversample_factor

    vec_hits = vector_search(conn, query_embedding, limit=fetch_limit, dedupe_by_conversation=False)
    text_hits = text_search(conn, query, limit=fetch_limit, dedupe_by_conversation=False)

    # RRF: cada chunk acumula score por aparecer en cada lista.
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

    # Ordenar por score descendente, dedupear por conversación si aplica.
    ordered_ids = sorted(scores, key=lambda cid: -scores[cid])
    result: list[SearchHit] = []
    seen_convs: set[str] = set()
    for cid in ordered_ids:
        base = hits_by_id[cid]
        if dedupe_by_conversation:
            if base.conversation.uuid in seen_convs:
                continue
            seen_convs.add(base.conversation.uuid)
        # Reemplazamos el distance original por -score para que "menor = mejor".
        merged = base.model_copy(update={"distance": -scores[cid]})
        result.append(merged)
        if len(result) >= limit:
            break
    return result


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Repuebla `fts_chunks` desde `chunks`. Útil para bases pre-existentes
    que se actualizaron al schema con FTS5 sin re-ingestar.

    Borra el contenido actual del índice FTS y vuelve a insertar todos los
    chunks. Devuelve la cantidad de filas indexadas. No toca `chunks` ni
    `vec_chunks`.

    Es una operación de mantenimiento auto-contenida, así que commitea al final
    (a diferencia de las funciones tipo `insert_*` que dejan el commit al
    llamador). Si vas a hacer más escrituras en la misma transacción, llamala
    explícitamente con `conn.in_transaction` apropiado y commit/rollback vos.
    """
    conn.execute("DELETE FROM fts_chunks")
    cursor = conn.execute("INSERT INTO fts_chunks(rowid, text) SELECT id, text FROM chunks")
    count = cursor.rowcount if cursor.rowcount is not None else 0
    conn.commit()
    return count


def _sanitize_fts_query(query: str) -> str:
    """Convierte una query libre en una válida para FTS5.

    FTS5 acepta una mini-lenguaje (AND, OR, NOT, NEAR, comillas, etc.) que se
    rompe si el usuario tipea cosas con paréntesis, comillas sueltas, etc.
    Para una búsqueda casual lo más seguro es quedarse con las palabras
    alfanuméricas y citarlas como frase implícita: "palabra1 palabra2 ..."

    Si todas las palabras se filtran, devuelve "".
    """
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    # Cada palabra entre comillas dobles para que FTS5 no las interprete como
    # operadores. Las quotes hacen match exacto sobre tokens.
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
