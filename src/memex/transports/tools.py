"""Implementaciones puras de las tools de Memex.

Cada función toma una conexión SQLite y/o un Embedder, hace el trabajo, y
devuelve un dict serializable. Estas funciones NO saben nada de MCP; son
testables directamente con DB in-memory y FakeEmbedder.

La capa MCP (`transports/stdio.py`) las envuelve, las decora con
`@server.tool`, y serializa el dict a JSON antes de devolverlo al cliente
del MCP.
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from memex.core.embeddings.base import Embedder, EmbedderError
from memex.core.models import Project, SearchHit, Source
from memex.core.storage import repo
from memex.core.summaries.base import Summarizer, SummarizerError

logger = logging.getLogger(__name__)

# Límites duros para evitar payloads desproporcionados. Los clientes MCP
# (incluido Claude Code) suelen tener un tope de ~25-30k tokens por respuesta;
# pasarse devuelve el resultado a un archivo aparte y rompe la experiencia.
SEARCH_LIMIT_MAX = 50
SEARCH_SUMMARY_MAX_CHARS = 500  # Algunos summaries pesan 2-3k chars, los recortamos.
# Tope de summaries generados lazy por call a `search_chats`. Acota latencia
# (más de 3 calls paralelas a la API agrega percepción de "lento") y costo
# por query. Los demás results vienen sin summary (Claude puede decidir
# llamar `get_chat` para profundizar en alguno específico).
SEARCH_SUMMARY_LAZY_CAP = 3
LIST_LIMIT_MAX = 100
# Default conservador: 10 mensajes x 1500 chars + overhead = ~17k chars de
# response, lejos del límite del cliente MCP (~25-30k tokens). Antes era
# 20 x 3000 -> ~62k chars, lo que ocasionalmente excedía el tope de Claude
# Code y derivaba el resultado a un archivo aparte (UX rota). Si Claude
# necesita más, pagina con messages_offset o pide messages_limit explícito.
GET_CHAT_MESSAGES_LIMIT_DEFAULT = 10
GET_CHAT_MESSAGES_LIMIT_MAX = 100
GET_CHAT_MESSAGE_TEXT_MAX_CHARS = 1500  # Code dumps largos explotan solos sin esto.

VALID_SEARCH_MODES = ("hybrid", "semantic", "lexical")

# When `search_chats(repo=...)` is provided, results associated to the
# given repo get their distance lowered by `REPO_BOOST_WEIGHT * confidence`.
# Weight chosen so that a high-confidence match (confidence ~= 1.0) clearly
# beats a slightly more semantically-similar but unrelated chat, without
# steamrolling matches that have a strong base score on their own.
REPO_BOOST_WEIGHT = 0.3
# How many extra candidates we fetch when boosting, so the reorder has room
# to surface relevant chats that were just outside the top-N before boosting.
REPO_BOOST_OVERSAMPLE = 5

# `find_related` accepts free-form context (potentially a whole file or
# message). We cap the embedded text to keep latency bounded and to stay
# well inside the embedding model's context window.
FIND_RELATED_MAX_INPUT_CHARS = 4000


def search_chats(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    limit: int = 5,
    source: str | None = None,
    mode: str = "hybrid",
    summarizer: Summarizer | None = None,
    repo_arg: str | None = None,
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

    Si `summarizer` es no-None, las conversaciones del top-N que no tengan
    `summary` cacheado en DB se enriquecen lazy: hasta `SEARCH_SUMMARY_LAZY_CAP`
    en paralelo, con silent fail si la API falla (el result queda sin summary,
    no aborta). Los summaries generados se persisten para que la próxima query
    pegue cache.

    Si `repo_arg` es no-None, los chats asociados a ese repo reciben un boost
    de ranking proporcional a su `confidence` de asociación. Chats fuera del
    repo siguen apareciendo. `repo_arg` puede ser un key canónico, una URL
    remote git, o un path absoluto; resolvemos cualquiera de los tres.

    Errores como dict con clave `error`:
    - source inválido
    - mode inválido
    - query vacía
    - embedder falla (Ollama caído / modelo faltante) en modos hybrid/semantic
    - `repo_arg` no corresponde a ningún repo registrado
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

    # Resolve the repo argument upfront so we fail fast on an unknown key.
    resolved_repo_key: str | None = None
    repo_boost_map: dict[str, float] = {}
    if repo_arg is not None:
        resolved_repo_key = _resolve_repo_key(conn, repo_arg)
        if resolved_repo_key is None:
            return {
                "error": (
                    f"No registered repo matches {repo_arg!r}. "
                    "Run `memex repos list` to see registered repos, "
                    "or `memex repos add <path>` to register one."
                ),
            }
        # Map of conversation_uuid -> confidence (used to boost ranking).
        for uuid, _src, conf in repo.list_conversations_for_repo(conn, resolved_repo_key):
            repo_boost_map[uuid] = conf if conf is not None else 1.0

    # Oversample when filtering by source or boosting by repo so we have
    # enough candidates after re-ranking.
    fetch_limit = limit
    if src_filter is not None:
        fetch_limit *= 3
    if resolved_repo_key is not None:
        fetch_limit = max(fetch_limit, limit * REPO_BOOST_OVERSAMPLE)

    if mode == "lexical":
        # En lexical puro, si la query se sanitiza a vacío (solo símbolos, CJK
        # sin tokens latinos, etc.) el repo devuelve [] silenciosamente.
        # Avisamos al cliente para que sepa que no es "no hay matches", es
        # "tu query no produjo tokens válidos para FTS5".
        from memex.core.storage.repo import _sanitize_fts_query

        if not _sanitize_fts_query(q):
            return {
                "error": (
                    f"La query {q!r} no produjo tokens utilizables para "
                    "búsqueda lexical (probá con palabras o cambiá a mode='hybrid')."
                ),
            }
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

    # Apply repo boost (if any) BEFORE truncating, so chats that ranked just
    # outside the top-N pre-boost can surface when they belong to the repo.
    if repo_boost_map and hits:
        hits = _apply_repo_boost(hits, repo_boost_map)

    hits = hits[:limit]

    # Enriquecer lazy con summaries si hay summarizer activo. Esto persiste
    # los summaries en DB para próximas queries y muta `hits` in-place con
    # las conversaciones actualizadas.
    if summarizer is not None and hits:
        hits = _generate_lazy_summaries(conn, hits, summarizer)

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


def _generate_lazy_summaries(
    conn: sqlite3.Connection,
    hits: list[SearchHit],
    summarizer: Summarizer,
) -> list[SearchHit]:
    """Genera summaries on-demand para las conversaciones de `hits` sin uno.

    Cap a `SEARCH_SUMMARY_LAZY_CAP` por call: si hay más conversaciones
    candidatas (sin summary), las que no entran al cap quedan sin summary en
    esta respuesta. La idea es acotar latencia y costo por query; con cap=3
    y Haiku ~2s/call en paralelo, la latencia agregada es ~2-3s para 3
    summaries (no 6-9s secuenciales).

    Persiste cada summary nuevo en DB (`UPDATE conversations SET summary=...`)
    así la siguiente query pega cache. Silent fail por chat: si la API tira
    error para uno, ese result queda sin summary y los demás siguen.

    Devuelve una lista nueva de `SearchHit` con las conversations enriquecidas
    (los pydantic models son inmutables, así que reemplazamos via `model_copy`).
    """
    # Identificar candidatos: conversaciones únicas sin summary, manteniendo
    # el orden de ranking (más relevante primero).
    candidates: list[str] = []
    seen: set[str] = set()
    title_by_uuid: dict[str, str] = {}
    for hit in hits:
        uuid = hit.conversation.uuid
        if uuid in seen:
            continue
        seen.add(uuid)
        title_by_uuid[uuid] = hit.conversation.title
        if not hit.conversation.summary:
            candidates.append(uuid)
    candidates = candidates[:SEARCH_SUMMARY_LAZY_CAP]
    if not candidates:
        return hits

    # Pre-cargar el texto de cada candidato en el thread main. SQLite ata
    # la conexión a su thread de creación, así que no podemos hacer queries
    # desde el ThreadPool. El pool solo se usa para el call slow al LLM.
    payloads: list[tuple[str, str, str]] = []
    for uuid in candidates:
        text = repo.get_conversation_text(conn, uuid)
        if not text:
            continue
        payloads.append((uuid, text, title_by_uuid.get(uuid, "")))

    if not payloads:
        return hits

    def _gen_one(item: tuple[str, str, str]) -> tuple[str, str | None]:
        uuid, text, title = item
        try:
            return uuid, summarizer.summarize(text, title=title or None)
        except SummarizerError as e:
            logger.warning("Lazy summary skipped for %s: %s", uuid, e)
            return uuid, None

    new_summaries: dict[str, str] = {}
    # ThreadPool: cada call al SDK de Anthropic libera el GIL durante la
    # request HTTP, así varios threads progresan en paralelo aunque Python
    # tenga GIL. max_workers = cantidad de candidatos para no crear threads
    # de más.
    with ThreadPoolExecutor(max_workers=len(payloads)) as ex:
        for uuid, gen_result in ex.map(_gen_one, payloads):
            if gen_result is not None:
                new_summaries[uuid] = gen_result

    if not new_summaries:
        return hits

    # Persistir. Importante: no usamos `repo.insert_conversation` (que
    # haría upsert y podría pisar otros campos), solo el UPDATE del summary.
    for uuid, summary_text in new_summaries.items():
        repo.update_conversation_summary(conn, uuid, summary_text)
    conn.commit()

    # Construir lista nueva de hits con conversations actualizadas. No mutamos
    # la lista original por convención pydantic (modelos inmutables).
    enriched: list[SearchHit] = []
    for hit in hits:
        new_summary = new_summaries.get(hit.conversation.uuid)
        if new_summary is not None:
            new_conv = hit.conversation.model_copy(update={"summary": new_summary})
            enriched.append(hit.model_copy(update={"conversation": new_conv}))
        else:
            enriched.append(hit)
    return enriched


def _resolve_repo_key(conn: sqlite3.Connection, repo_arg: str) -> str | None:
    """Thin shim around `core.repos.resolve_repo_key` for backwards-compat.

    The actual resolution logic lives in `core/repos/resolve.py` so the CLI
    can use it too. Tests import this symbol from `tools` for convenience.
    """
    from memex.core.repos import resolve_repo_key

    return resolve_repo_key(conn, repo_arg)


def _apply_repo_boost(
    hits: list[SearchHit],
    boost_map: dict[str, float],
) -> list[SearchHit]:
    """Lower the `distance` of hits whose conversation is associated to the
    target repo, then re-sort ascending.

    `boost_map` is `conversation_uuid -> confidence` (0.0-1.0). Hits not in
    the map keep their original distance. Returns a new list of `SearchHit`
    with potentially-updated `distance`, sorted ascending (lower = better).
    """
    boosted: list[SearchHit] = []
    for hit in hits:
        conf = boost_map.get(hit.conversation.uuid)
        if conf is not None:
            new_distance = hit.distance - REPO_BOOST_WEIGHT * conf
            boosted.append(hit.model_copy(update={"distance": new_distance}))
        else:
            boosted.append(hit)
    boosted.sort(key=lambda h: h.distance)
    return boosted


def get_chat(
    conn: sqlite3.Connection,
    uuid: str,
    messages_limit: int = GET_CHAT_MESSAGES_LIMIT_DEFAULT,
    messages_offset: int = 0,
) -> dict[str, Any]:
    """Trae una conversación con sus mensajes en orden cronológico.

    Por defecto devuelve los primeros 10 mensajes desde el inicio del chat
    (`GET_CHAT_MESSAGES_LIMIT_DEFAULT`). Cada mensaje se trunca a
    `GET_CHAT_MESSAGE_TEXT_MAX_CHARS` (1500 chars) para que el response total
    quepa en el tope de tokens del cliente MCP. Worst case: ~17k chars.
    Para chats largos, paginar con `messages_offset` o pedir
    `messages_limit` explícito (max 100).

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


def find_related(
    conn: sqlite3.Connection,
    embedder: Embedder,
    context: str,
    limit: int = 5,
    repo_arg: str | None = None,
) -> dict[str, Any]:
    """Find chats semantically related to a free-form context blob.

    Unlike `search_chats`, this is intended for longer inputs (file
    contents, message snippets, current discussion). It runs pure vector
    search (no lexical / FTS5) because the input length makes word-by-word
    BM25 less informative than embedding similarity.

    Args:
        conn: SQLite connection.
        embedder: Embedder to convert `context` into a query vector.
        context: Free-form text. Capped to `FIND_RELATED_MAX_INPUT_CHARS`
            before embedding so latency stays bounded.
        limit: Max results (1-50).
        repo_arg: Optional repo path / remote URL / canonical key. Same
            boost semantics as `search_chats`.

    Returns dict with `count`, `context_chars` (how much we actually
    embedded), and `results`. Error dict on empty context, embedder
    failure, or unregistered repo.
    """
    ctx = context.strip()
    if not ctx:
        return {"error": "El contexto no puede estar vacío."}

    if len(ctx) > FIND_RELATED_MAX_INPUT_CHARS:
        ctx = ctx[:FIND_RELATED_MAX_INPUT_CHARS]

    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    resolved_repo_key: str | None = None
    repo_boost_map: dict[str, float] = {}
    if repo_arg is not None:
        resolved_repo_key = _resolve_repo_key(conn, repo_arg)
        if resolved_repo_key is None:
            return {
                "error": (
                    f"No registered repo matches {repo_arg!r}. "
                    "Run `memex repos list` to see registered repos."
                ),
            }
        for uuid, _src, conf in repo.list_conversations_for_repo(conn, resolved_repo_key):
            repo_boost_map[uuid] = conf if conf is not None else 1.0

    try:
        query_vec = embedder.embed_one(ctx)
    except EmbedderError as e:
        return {"error": str(e)}

    fetch_limit = limit * (REPO_BOOST_OVERSAMPLE if resolved_repo_key else 1)
    hits = repo.vector_search(conn, query_vec, limit=fetch_limit)

    if repo_boost_map and hits:
        hits = _apply_repo_boost(hits, repo_boost_map)

    hits = hits[:limit]

    return {
        "context_chars": len(ctx),
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
