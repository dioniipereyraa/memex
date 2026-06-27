"""Sync a peer's conversations with the local store.

Three operations, all over the peer's `memex serve` HTTP endpoints
(`/sync/manifest`, `/sync/conversations`, `/sync/push`):

- `pull`  (Phase 1): one-directional, take the peer's version of anything that
  differs (peer authoritative).
- `push`  (Phase 2): one-directional, send the local version of anything the
  peer is missing or has differently (local authoritative).
- `reconcile` (Phase 2): bidirectional last-writer-wins by `updated_at`, so a
  newer version is never overwritten by an older one; converges both devices.

All refuse on an embedding model/dim mismatch (the vectors would be
incompatible). Records carry their chunk vectors, so neither side re-embeds. The
record (de)serialization and the diff live in `sync.records`; the HTTP calls are
injectable so the logic is tested without a live socket.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memex.config import settings
from memex.sync import records
from memex.sync.peers import Peer

logger = logging.getLogger("memex.sync.client")

# Header carrying the peer's per-install token (matches http_ingest._TOKEN_HEADER).
_TOKEN_HEADER = "X-Memex-Token"
_MANIFEST_TIMEOUT = 30.0
_FETCH_TIMEOUT = 120.0

# A conversation carries its chunk vectors, so its transfer size scales with the
# chunk count, not the conversation count. Sync therefore batches by serialized
# BYTES (a budget under the peer's body cap), never by a fixed count, so a body
# cannot exceed the receiver's `ingest_max_body_bytes` (which would 413 / break
# the pipe). See `_resolve_budget` / `_serialize_and_push` / `_fetch_and_insert`.
#
# Fraction of the peer's advertised body cap we are willing to fill (headroom for
# the JSON envelope and any size estimate error).
_BUDGET_SAFETY = 0.8
# Fixed slack for the request envelope ({"embed_model", "embed_dim",
# "conversations": [...]}) added on top of the records' own size.
_ENVELOPE_OVERHEAD = 4096
# Hard ceiling on the per-request item count, regardless of the byte budget, so a
# batch of tiny records still stays under the server's `_MAX_SYNC_UUIDS` (1000).
_MAX_BATCH_COUNT = 500
# Estimating a remote record's size from its manifest `chunk_count` (the client
# has no local copy to measure). Vectors dominate: ~`dim` JSON floats per chunk.
_FLOAT_JSON_BYTES = 14
_CHUNK_TEXT_BYTES = 800
_CONV_BASE_BYTES = 8192
# Fallback chunk count for a peer too old to advertise it (estimate moderately so
# batches stay conservative rather than huge).
_DEFAULT_CHUNK_COUNT = 100


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


@dataclass
class PushSummary:
    """Result of one `push`."""

    peer: str
    to_push: int
    pushed: int
    failed: int
    local_model: str
    local_dim: int
    peer_model: str | None
    peer_dim: int | None
    refused_mismatch: bool = False
    # Conversations skipped because one record alone exceeds the peer's body cap
    # (counted in `failed` too). The peer must raise MEMEX_INGEST_MAX_BODY_BYTES.
    oversized: int = 0


@dataclass
class ReconcileSummary:
    """Result of one bidirectional `reconcile`."""

    peer: str
    pulled: int
    pushed: int
    failed: int
    # uuids that diverged on both sides with the SAME updated_at (a fork): left
    # untouched on both, surfaced so the user can force a side with pull/push.
    forks: int = 0
    refused_mismatch: bool = False
    # Conversations skipped because one record alone exceeds the peer's body cap
    # (counted in `failed` too). The peer must raise MEMEX_INGEST_MAX_BODY_BYTES.
    oversized: int = 0


ManifestFn = Callable[[Peer], dict[str, Any]]
FetchFn = Callable[[Peer, list[str]], dict[str, Any]]
PushFn = Callable[[Peer, str, int, list[dict[str, Any]]], dict[str, Any]]


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


def push_conversations(
    peer: Peer, model: str, dim: int, conversations: list[dict[str, Any]]
) -> dict[str, Any]:
    return _http_post_json(
        f"{peer.url}/sync/push",
        peer.token,
        {"embed_model": model, "embed_dim": dim, "conversations": conversations},
        _FETCH_TIMEOUT,
    )


def _resolve_budget(manifest: dict[str, Any], override: int | None) -> int:
    """Bytes one sync request may carry: a safe fraction of the peer's body cap.

    Starts from the local config (or an explicit override), then clamps to 80% of
    the peer's advertised `max_body_bytes` so a request never exceeds the
    receiver's cap. A peer too old to advertise it leaves the local budget as is
    (the default 8 MB already sits under the 16 MB default server cap).
    """
    base = override if (override is not None and override > 0) else settings.sync_max_batch_bytes
    peer_cap = manifest.get("max_body_bytes")
    if isinstance(peer_cap, int) and peer_cap > 0:
        return min(base, int(peer_cap * _BUDGET_SAFETY))
    return base


def _est_record_bytes(chunk_count: int, dim: int) -> int:
    """Estimate a record's serialized size from its chunk count (vectors dominate).

    Used only for the pull side, where the client has no local copy to measure;
    the push side measures the real serialized bytes instead.
    """
    per_chunk = dim * _FLOAT_JSON_BYTES + _CHUNK_TEXT_BYTES
    return chunk_count * per_chunk + _CONV_BASE_BYTES


def _size_hint(manifest_conversations: list[dict[str, Any]], dim: int) -> dict[str, int]:
    """Map uuid -> estimated transfer bytes, from a peer manifest's chunk counts."""
    hint: dict[str, int] = {}
    for item in manifest_conversations:
        if not isinstance(item, dict) or not item.get("uuid"):
            continue
        cc = item.get("chunk_count")
        cc = cc if isinstance(cc, int) and cc >= 0 else _DEFAULT_CHUNK_COUNT
        hint[item["uuid"]] = _est_record_bytes(cc, dim)
    return hint


def _compatible(manifest: dict[str, Any], local_model: str, local_dim: int) -> bool:
    return manifest.get("embed_model") == local_model and manifest.get("embed_dim") == local_dim


def _insert_record(conn: sqlite3.Connection, record: dict[str, Any], expected_dim: int) -> None:
    """Thin alias kept for the test suite; delegates to the shared insert path."""
    records.insert_record(conn, record, expected_dim)


def pull(
    conn: sqlite3.Connection,
    peer: Peer,
    *,
    local_model: str,
    local_dim: int,
    manifest_fn: ManifestFn | None = None,
    fetch_fn: FetchFn | None = None,
    max_batch_bytes: int | None = None,
) -> PullSummary:
    """Pull new or changed conversations from `peer` into `conn` (peer wins)."""
    manifest_fn = manifest_fn or fetch_manifest
    fetch_fn = fetch_fn or fetch_conversations

    manifest = manifest_fn(peer)
    peer_model = manifest.get("embed_model")
    peer_dim = manifest.get("embed_dim")
    remote = manifest.get("conversations") or []

    if not _compatible(manifest, local_model, local_dim):
        logger.warning(
            "Refusing pull from peer %s: model/dim mismatch (peer %s/%s, local %s/%s)",
            peer.name,
            peer_model,
            peer_dim,
            local_model,
            local_dim,
        )
        return PullSummary(
            peer=peer.name,
            manifest_total=len(remote),
            to_fetch=0,
            inserted=0,
            failed=0,
            local_model=local_model,
            local_dim=local_dim,
            peer_model=peer_model,
            peer_dim=peer_dim,
            refused_mismatch=True,
        )

    budget = _resolve_budget(manifest, max_batch_bytes)
    hint = _size_hint(remote, local_dim)
    to_fetch = records.select_to_transfer(records.local_manifest(conn), remote)
    inserted, failed = _fetch_and_insert(
        conn, peer, to_fetch, fetch_fn, local_model, local_dim, budget, hint
    )
    return PullSummary(
        peer=peer.name,
        manifest_total=len(remote),
        to_fetch=len(to_fetch),
        inserted=inserted,
        failed=failed,
        local_model=local_model,
        local_dim=local_dim,
        peer_model=peer_model,
        peer_dim=peer_dim,
    )


def push(
    conn: sqlite3.Connection,
    peer: Peer,
    *,
    local_model: str,
    local_dim: int,
    manifest_fn: ManifestFn | None = None,
    push_fn: PushFn | None = None,
    max_batch_bytes: int | None = None,
) -> PushSummary:
    """Push local conversations the peer is missing or has differently (local wins)."""
    manifest_fn = manifest_fn or fetch_manifest
    push_fn = push_fn or push_conversations

    manifest = manifest_fn(peer)
    peer_model = manifest.get("embed_model")
    peer_dim = manifest.get("embed_dim")
    remote = manifest.get("conversations") or []

    if not _compatible(manifest, local_model, local_dim):
        logger.warning("Refusing push to peer %s: model/dim mismatch", peer.name)
        return PushSummary(
            peer=peer.name,
            to_push=0,
            pushed=0,
            failed=0,
            local_model=local_model,
            local_dim=local_dim,
            peer_model=peer_model,
            peer_dim=peer_dim,
            refused_mismatch=True,
        )

    budget = _resolve_budget(manifest, max_batch_bytes)
    to_push = records.select_to_transfer(remote, records.local_manifest(conn))
    pushed, failed, oversized = _serialize_and_push(
        conn, peer, to_push, push_fn, local_model, local_dim, budget
    )
    return PushSummary(
        peer=peer.name,
        to_push=len(to_push),
        pushed=pushed,
        failed=failed,
        local_model=local_model,
        local_dim=local_dim,
        peer_model=peer_model,
        peer_dim=peer_dim,
        oversized=oversized,
    )


def reconcile(
    conn: sqlite3.Connection,
    peer: Peer,
    *,
    local_model: str,
    local_dim: int,
    manifest_fn: ManifestFn | None = None,
    fetch_fn: FetchFn | None = None,
    push_fn: PushFn | None = None,
    max_batch_bytes: int | None = None,
) -> ReconcileSummary:
    """Bidirectional reconcile (last-writer-wins by updated_at); leaves both equal."""
    manifest_fn = manifest_fn or fetch_manifest
    fetch_fn = fetch_fn or fetch_conversations
    push_fn = push_fn or push_conversations

    manifest = manifest_fn(peer)
    remote = manifest.get("conversations") or []
    if not _compatible(manifest, local_model, local_dim):
        logger.warning("Refusing reconcile with peer %s: model/dim mismatch", peer.name)
        return ReconcileSummary(peer=peer.name, pulled=0, pushed=0, failed=0, refused_mismatch=True)

    budget = _resolve_budget(manifest, max_batch_bytes)
    hint = _size_hint(remote, local_dim)
    to_pull, to_push, forks = records.select_reconcile(records.local_manifest(conn), remote)
    pulled, pull_failed = _fetch_and_insert(
        conn, peer, to_pull, fetch_fn, local_model, local_dim, budget, hint
    )
    pushed, push_failed, oversized = _serialize_and_push(
        conn, peer, to_push, push_fn, local_model, local_dim, budget
    )
    if forks:
        logger.warning(
            "reconcile with %s: %d conversation(s) forked (same updated_at, different "
            "content); left untouched. Resolve with `memex sync pull`/`push --peer %s`.",
            peer.name,
            len(forks),
            peer.name,
        )
    return ReconcileSummary(
        peer=peer.name,
        pulled=pulled,
        pushed=pushed,
        failed=pull_failed + push_failed,
        forks=len(forks),
        oversized=oversized,
    )


def _fetch_and_insert(
    conn: sqlite3.Connection,
    peer: Peer,
    uuids: list[str],
    fetch_fn: FetchFn,
    local_model: str,
    local_dim: int,
    budget: int,
    size_hint: dict[str, int],
) -> tuple[int, int]:
    """Fetch the uuids from the peer and insert them, batching by estimated bytes.

    The request body (a list of uuids) is tiny, but the RESPONSE carries full
    records with vectors, so an over-large batch makes the peer materialize a
    huge response in memory. Group uuids until their estimated transfer size
    (from the manifest's chunk counts) would exceed `budget`, then fetch.
    """
    inserted = 0
    failed = 0
    batch: list[str] = []
    batch_bytes = 0

    def flush() -> bool:
        """Fetch+insert the current batch. Returns False to stop (model drift)."""
        nonlocal inserted, failed, batch, batch_bytes
        if not batch:
            return True
        resp = fetch_fn(peer, batch)
        batch, batch_bytes = [], 0
        # The peer could re-embed under a new model between the manifest and this
        # call; re-check before trusting the vectors.
        if not _compatible(resp, local_model, local_dim):
            logger.warning("Peer %s changed model mid-sync; stopping fetch.", peer.name)
            return False
        for record in resp.get("conversations") or []:
            try:
                records.insert_record(conn, record, local_dim)
                inserted += 1
            except Exception:
                logger.exception(
                    "Failed to insert synced conversation %s",
                    record.get("uuid") if isinstance(record, dict) else "<?>",
                )
                failed += 1
        return True

    for uuid in uuids:
        est = size_hint.get(uuid, _est_record_bytes(_DEFAULT_CHUNK_COUNT, local_dim))
        full = batch_bytes + est > budget or len(batch) >= _MAX_BATCH_COUNT
        # flush() (side-effecting) only runs once the batch is non-empty and full;
        # it returns False on peer model drift, which stops the whole fetch.
        if batch and full and not flush():
            return inserted, failed
        batch.append(uuid)
        batch_bytes += est
    flush()
    return inserted, failed


def _serialize_and_push(
    conn: sqlite3.Connection,
    peer: Peer,
    uuids: list[str],
    push_fn: PushFn,
    local_model: str,
    local_dim: int,
    budget: int,
) -> tuple[int, int, int]:
    """Serialize the uuids and push them, batching by real serialized bytes.

    Each record carries its chunk vectors, so the batch is filled by measured
    JSON size (not a fixed count) and flushed before it would exceed `budget`
    (which is already under the peer's body cap). A single record that alone
    exceeds the budget can never fit in any request, so it is skipped with a
    clear log and counted as both failed and oversized rather than triggering a
    413 / broken pipe that would abort the whole sync.

    Returns (pushed, failed, oversized).
    """
    pushed = 0
    failed = 0
    oversized = 0
    batch: list[dict[str, Any]] = []
    batch_bytes = _ENVELOPE_OVERHEAD

    def flush() -> None:
        nonlocal pushed, failed, batch, batch_bytes
        if not batch:
            return
        resp = push_fn(peer, local_model, local_dim, batch)
        pushed += int(resp.get("inserted", 0))
        failed += int(resp.get("failed", 0))
        batch, batch_bytes = [], _ENVELOPE_OVERHEAD

    for uuid in uuids:
        record = records.serialize_conversation(conn, uuid)
        if record is None:
            continue
        rec_bytes = len(json.dumps(record).encode("utf-8"))
        if rec_bytes + _ENVELOPE_OVERHEAD > budget:
            logger.error(
                "Conversation %s is %d bytes, over the %d-byte sync budget; skipping. "
                "Raise MEMEX_INGEST_MAX_BODY_BYTES on %s (or lower "
                "MEMEX_MAX_CHUNKS_PER_CONVERSATION) to transfer it.",
                uuid,
                rec_bytes,
                budget,
                peer.name,
            )
            failed += 1
            oversized += 1
            continue
        if batch and (batch_bytes + rec_bytes > budget or len(batch) >= _MAX_BATCH_COUNT):
            flush()
        batch.append(record)
        batch_bytes += rec_bytes
    flush()
    return pushed, failed, oversized
