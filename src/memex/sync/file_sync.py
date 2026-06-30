"""File-based multi-device sync: a transport that uses a shared folder, not HTTP.

The network sync (`sync/client.py`) needs both devices online at once: each runs
`memex serve` and the source dials the destination. That assumption breaks on a
dual-boot machine (Linux and Windows on the same disk, never powered on at the
same time). File sync drops the live connection: each device EXPORTS a full
snapshot of its store to a gzip file in a shared directory, and IMPORTS the other
devices' snapshot files from that same directory. No server, no port, no token.

It reuses the transport-agnostic core unchanged:
- `records.local_manifest` / `select_reconcile` decide what to pull (same
  last-writer-wins by `updated_at` as the network reconcile).
- `records.serialize_conversation` / `insert_record` are the wire format and the
  consistent insert path (chunk ids reassigned locally, vec/fts kept in step,
  the chunk-count cap re-asserted), so a file record and an HTTP record are
  identical and cannot drift.

Ownership and convergence:
- Each device owns exactly ONE file, `<slug(device_name)>.memexsync.gz`, and only
  ever writes that one. It reads every OTHER `*.memexsync.gz` in the directory.
  So two devices never write the same file (and on a dual-boot only one OS runs
  at a time anyway).
- Sync is pull-only against each peer file; the "push" half is emergent. My
  snapshot is my full state, so the peer pulls my newer conversations when IT
  next runs. Over two boots the two devices converge (eventual consistency).
- A fork (same `updated_at`, different content hash) is left untouched and
  reported, exactly like the network reconcile.

Trust model: the shared folder is on the user's own disk (the other OS's
partition, a USB drive, a synced cloud folder). A snapshot file is therefore
attacker-shaped data with the same trust as a paired peer ("only share folders
you control"): `insert_record` re-asserts the chunk cap, the list-type guards and
the embedding-dimension check, and the reader bounds each decompressed line so a
crafted gzip cannot exhaust memory. No re-redaction happens (the vectors already
encode the text, same as the network path).
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memex.config import settings
from memex.sync import records
from memex.sync import state as sync_state

logger = logging.getLogger("memex.sync.file_sync")

# Snapshot files are gzipped JSON-lines: a header line (format, device, embed
# model/dim, a fingerprint, and the full manifest) followed by one serialized
# conversation record per line. The header carries the manifest so a reader can
# compute the diff without parsing every record, and only stream the records it
# actually needs to pull.
SNAPSHOT_SUFFIX = ".memexsync.gz"
FORMAT_VERSION = 1

# Anti-gzip-bomb guard: the most a single decompressed line (one record) may grow
# to while streaming. Set well above the largest legitimate single-conversation
# record (max_chunks_per_conversation chunks * a vector + text each, ~tens of MB),
# but far below "inflate a tiny gzip into GBs". Unlike the network path, file sync
# has no per-request body cap, so a big record is allowed; this only stops a
# crafted/absurd line from exhausting memory.
_MAX_LINE_BYTES = 256 * 1024 * 1024
# Read the decompressed stream in fixed blocks (bounds the buffer while splitting
# lines, independent of how the gzip is framed).
_READ_BLOCK = 1024 * 1024


@dataclass
class FileSyncSummary:
    """Result of one file `sync_once`, for the CLI / auto-sync loop to report."""

    device: str
    peers_seen: int  # peer snapshot files found in the directory
    pulled: int  # conversations inserted/updated from peer files
    failed: int  # records that errored on insert
    would_push: int  # local conversations a peer will pull on its next run (info)
    forks: int  # same updated_at, different content: left untouched on both
    incompatible: int  # peer files refused for an embed model/dim mismatch
    exported: bool  # whether this device (re)wrote its own snapshot this run


def _slug(name: str) -> str:
    """A filesystem-safe device label (keep alnum/dash/underscore, collapse rest)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned or "device"


def resolve_sync_dir() -> Path | None:
    """The configured shared directory: the env override, else the persisted choice.

    `MEMEX_SYNC_DIR` wins (tests / advanced); otherwise the value `memex setup
    --sync-dir` persisted in the gate file. None means file sync is not set up.
    """
    if settings.sync_dir is not None:
        return settings.sync_dir
    persisted = sync_state.get_sync_dir()
    return Path(persisted) if persisted else None


def resolve_device_name() -> str:
    """This device's snapshot name: the persisted choice, else the hostname.

    `memex setup --sync-dir` persists the chosen name; absent that, fall back to
    `settings.device_name` (which defaults to the hostname). Two devices whose
    hostnames collide must be given distinct `--device-name` values so their
    snapshot files do not overwrite each other.
    """
    persisted = sync_state.get_device_name()
    return persisted or settings.device_name


def snapshot_path(sync_dir: Path, device_name: str) -> Path:
    return sync_dir / f"{_slug(device_name)}{SNAPSHOT_SUFFIX}"


def _manifest_fingerprint(manifest: list[dict[str, Any]]) -> str:
    """A stable hash of the manifest so an unchanged store skips the re-export.

    Keyed on (uuid, content_hash, updated_at), the exact fields the diff uses, so
    a no-op tick does not rewrite tens of MB of vectors.
    """
    key = sorted(
        [str(m.get("uuid")), str(m.get("content_hash")), str(m.get("updated_at"))]
        for m in manifest
        if isinstance(m, dict) and m.get("uuid")
    )
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def _iter_decompressed_lines(fh: gzip.GzipFile) -> Iterator[str]:
    """Yield decompressed lines, capping any single line at `_MAX_LINE_BYTES`.

    Reads the gzip stream in fixed blocks and splits on newlines manually rather
    than trusting `for line in gzip_file`, so a snapshot with no newline (or one
    enormous line) cannot inflate into unbounded memory.
    """
    buf = b""
    while True:
        block = fh.read(_READ_BLOCK)
        if not block:
            break
        buf += block
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line = buf[:nl]
            buf = buf[nl + 1 :]
            if line:
                yield line.decode("utf-8")
        if len(buf) > _MAX_LINE_BYTES:
            raise ValueError(f"snapshot line exceeds {_MAX_LINE_BYTES} bytes; refusing")
    if buf:
        yield buf.decode("utf-8")


def read_header(path: Path) -> dict[str, Any] | None:
    """Read and parse only the header (first line) of a snapshot file.

    Returns the header dict (format, device, embed_model, embed_dim, fingerprint,
    manifest), or None if the file is missing/empty/corrupt. Fail-soft: a bad peer
    file is skipped, never fatal. A missing file is the normal first-export case
    (export reads its own not-yet-written snapshot to check the fingerprint), so it
    is silent; only a real read/corruption error warns.
    """
    try:
        with gzip.open(path, "rb") as fh:
            for line in _iter_decompressed_lines(fh):
                data = json.loads(line)
                return data if isinstance(data, dict) else None
            return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Could not read snapshot header %s: %s", path, e)
        return None


def _iter_records(path: Path, wanted: set[str]) -> Iterator[dict[str, Any]]:
    """Stream the records (after the header) whose uuid is in `wanted`."""
    with gzip.open(path, "rb") as fh:
        first = True
        for line in _iter_decompressed_lines(fh):
            if first:  # skip the header line
                first = False
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                logger.warning("Skipping a malformed record line in %s.", path)
                continue
            if isinstance(rec, dict) and rec.get("uuid") in wanted:
                yield rec


def export_snapshot(
    conn: sqlite3.Connection,
    sync_dir: Path,
    device_name: str,
    model: str,
    dim: int,
) -> tuple[Path, bool]:
    """Write this device's full store to its snapshot file (atomic gzip).

    Skips the rewrite when the local manifest fingerprint matches the existing
    file's, so a tick where nothing changed does not rewrite the whole snapshot.
    Returns (path, exported) where `exported` is False if the write was skipped.
    """
    sync_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_path(sync_dir, device_name)
    manifest = records.local_manifest(conn)
    fingerprint = _manifest_fingerprint(manifest)

    existing = read_header(target)
    if existing is not None and existing.get("fingerprint") == fingerprint:
        return target, False

    header = {
        "format": FORMAT_VERSION,
        "device": device_name,
        "embed_model": model,
        "embed_dim": dim,
        "written_at": datetime.now(UTC).isoformat(),
        "fingerprint": fingerprint,
        "manifest": manifest,
    }
    # Write to a per-process temp file then atomically replace, so a concurrent
    # reader (another device on a synced folder) never sees a half-written file.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            for item in manifest:
                uuid = item["uuid"]
                record = records.serialize_conversation(conn, uuid)
                if record is None:
                    continue
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
    return target, True


def list_peer_snapshots(sync_dir: Path, device_name: str) -> list[Path]:
    """Every `*.memexsync.gz` in the directory except this device's own file."""
    own = snapshot_path(sync_dir, device_name).name
    try:
        entries = sorted(sync_dir.glob(f"*{SNAPSHOT_SUFFIX}"))
    except OSError:
        return []
    return [p for p in entries if p.name != own and p.is_file()]


def import_once(
    conn: sqlite3.Connection,
    sync_dir: Path,
    device_name: str,
    model: str,
    dim: int,
) -> FileSyncSummary:
    """Import from every peer snapshot file in the directory (no export).

    For each peer file: refuse on an embed model/dim mismatch (the vectors would
    be incompatible), then `select_reconcile` and insert the records the peer has
    newer-or-only. Records this device has newer are NOT pushed here: the peer
    will pull them from this device's own snapshot on its next run.
    """
    pulled = 0
    failed = 0
    would_push = 0
    forks = 0
    incompatible = 0
    peer_files = list_peer_snapshots(sync_dir, device_name)

    for path in peer_files:
        header = read_header(path)
        if header is None:
            continue
        if header.get("embed_model") != model or header.get("embed_dim") != dim:
            logger.warning(
                "Skipping snapshot %s: embed mismatch (file %s/%s, local %s/%s).",
                path.name,
                header.get("embed_model"),
                header.get("embed_dim"),
                model,
                dim,
            )
            incompatible += 1
            continue
        remote_manifest = header.get("manifest")
        if not isinstance(remote_manifest, list):
            logger.warning("Snapshot %s has no usable manifest; skipping.", path.name)
            continue
        to_pull, to_push, file_forks = records.select_reconcile(
            records.local_manifest(conn), remote_manifest
        )
        would_push += len(to_push)
        forks += len(file_forks)
        if not to_pull:
            continue
        wanted = set(to_pull)
        # Stream the wanted records. A read error (a truncated/over-large line, an
        # I/O fault on a synced folder) aborts THIS file's import with a logged
        # error rather than crashing the whole sync; other peer files still run.
        try:
            for record in _iter_records(path, wanted):
                try:
                    records.insert_record(conn, record, dim)
                    pulled += 1
                except Exception:
                    logger.exception(
                        "Failed to insert conversation %s from snapshot %s",
                        record.get("uuid") if isinstance(record, dict) else "<?>",
                        path.name,
                    )
                    failed += 1
        except (OSError, ValueError) as e:
            logger.warning("Stopped reading snapshot %s early: %s", path.name, e)

    return FileSyncSummary(
        device=device_name,
        peers_seen=len(peer_files),
        pulled=pulled,
        failed=failed,
        would_push=would_push,
        forks=forks,
        incompatible=incompatible,
        exported=False,
    )


def sync_once(
    conn: sqlite3.Connection,
    sync_dir: Path,
    device_name: str,
    model: str,
    dim: int,
) -> FileSyncSummary:
    """The file-mode equivalent of `reconcile`: import peers, then export self.

    Import first so this device's freshly-written snapshot already includes what
    it just learned (a third device then converges faster, pulling the merged
    state from either peer file).
    """
    summary = import_once(conn, sync_dir, device_name, model, dim)
    _, exported = export_snapshot(conn, sync_dir, device_name, model, dim)
    summary.exported = exported
    return summary
