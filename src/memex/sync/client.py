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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from memex.sync import records
from memex.sync.peers import Peer

logger = logging.getLogger("memex.sync.client")

# Header carrying the peer's per-install token (matches http_ingest._TOKEN_HEADER).
_TOKEN_HEADER = "X-Memex-Token"
# How many conversations to move per request. Kept under the server's
# `_MAX_SYNC_UUIDS` cap; a larger sync is split into several requests.
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


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


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
    batch_size: int = _DEFAULT_BATCH,
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

    to_fetch = records.select_to_transfer(records.local_manifest(conn), remote)
    inserted, failed = _fetch_and_insert(
        conn, peer, to_fetch, fetch_fn, local_model, local_dim, batch_size
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
    batch_size: int = _DEFAULT_BATCH,
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

    to_push = records.select_to_transfer(remote, records.local_manifest(conn))
    pushed, failed = _serialize_and_push(
        conn, peer, to_push, push_fn, local_model, local_dim, batch_size
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
    batch_size: int = _DEFAULT_BATCH,
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

    to_pull, to_push, forks = records.select_reconcile(records.local_manifest(conn), remote)
    pulled, pull_failed = _fetch_and_insert(
        conn, peer, to_pull, fetch_fn, local_model, local_dim, batch_size
    )
    pushed, push_failed = _serialize_and_push(
        conn, peer, to_push, push_fn, local_model, local_dim, batch_size
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
    )


def _fetch_and_insert(
    conn: sqlite3.Connection,
    peer: Peer,
    uuids: list[str],
    fetch_fn: FetchFn,
    local_model: str,
    local_dim: int,
    batch_size: int,
) -> tuple[int, int]:
    inserted = 0
    failed = 0
    for batch in _batched(uuids, batch_size):
        resp = fetch_fn(peer, batch)
        # The peer could re-embed under a new model between the manifest and this
        # call; re-check before trusting the vectors.
        if not _compatible(resp, local_model, local_dim):
            logger.warning("Peer %s changed model mid-sync; stopping fetch.", peer.name)
            break
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
    return inserted, failed


def _serialize_and_push(
    conn: sqlite3.Connection,
    peer: Peer,
    uuids: list[str],
    push_fn: PushFn,
    local_model: str,
    local_dim: int,
    batch_size: int,
) -> tuple[int, int]:
    pushed = 0
    failed = 0
    for batch in _batched(uuids, batch_size):
        payload = [
            rec for rec in (records.serialize_conversation(conn, uuid) for uuid in batch) if rec
        ]
        if not payload:
            continue
        resp = push_fn(peer, local_model, local_dim, payload)
        pushed += int(resp.get("inserted", 0))
        failed += int(resp.get("failed", 0))
    return pushed, failed
