"""Persistent state for multi-device sync: the master on/off gate + sync history.

The whole sync feature is OFF by default. `memex sync enable` flips a flag stored
in a small JSON file next to the DB (like the peer registry), so the gate persists
across restarts without an env var, and a running `memex serve` picks it up on the
next request (the state is read fresh, not cached). When sync is disabled the
`/sync/*` endpoints 404 and the CLI data commands refuse, so a non-user has no new
network surface even if `memex serve` is bound beyond loopback.

The per-peer sync history (last time + counts) lives in a SEPARATE file from the
gate on purpose: the history is written on every sync (manual and the auto-sync
loop), so co-locating it with the rarely-written `enabled` flag would let a
frequent history write race-clobber a concurrent `enable`/`disable`. Keeping them
apart means the gate is only ever written by `enable`/`disable`. History writes can
still race each other (serve's auto-sync vs a manual CLI run), but they are
best-effort breadcrumbs for `status`, not authoritative data, so last-writer-wins
is acceptable there.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memex.config import settings

logger = logging.getLogger("memex.sync.state")


def _default_gate() -> dict[str, Any]:
    return {"enabled": False}


def state_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else settings.sync_state_path


def history_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else settings.sync_history_path


def _load_json_object(target: Path, what: str) -> dict[str, Any] | None:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        logger.warning("Sync %s file %s is not valid JSON; treating as default.", what, target)
        return None
    return data if isinstance(data, dict) else None


def _write_json_object(target: Path, data: dict[str, Any]) -> None:
    parent_existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o700)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    # Write to a per-process temp file then atomically rename over the target, so a
    # concurrent reader (the gate is read on every /sync request) never sees a
    # half-written file. Create 0600 from the start (consistent with the
    # DB-adjacent peer registry): the gate/history are not secret but share the
    # user-private data dir. The pid suffix keeps two processes from colliding.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def is_enabled(path: Path | str | None = None) -> bool:
    """Whether the sync feature is enabled on this device (default False)."""
    data = _load_json_object(state_path(path), "state") or _default_gate()
    return bool(data.get("enabled", False))


def set_enabled(enabled: bool, path: Path | str | None = None) -> None:
    """Persist the master gate (the only writer of the gate file)."""
    _write_json_object(state_path(path), {"enabled": bool(enabled)})


def record_sync(
    peer: str,
    *,
    pulled: int,
    pushed: int,
    failed: int,
    when: datetime | None = None,
    path: Path | str | None = None,
) -> None:
    """Record the last sync result for one peer (best-effort, for `status`)."""
    target = history_path(path)
    data = _load_json_object(target, "history") or {}
    peers = data.get("peers")
    if not isinstance(peers, dict):
        peers = {}
    peers[peer] = {
        "last_sync_at": (when or datetime.now(UTC)).isoformat(),
        "pulled": int(pulled),
        "pushed": int(pushed),
        "failed": int(failed),
    }
    _write_json_object(target, {"peers": peers})


def get_peer_history(peer: str, path: Path | str | None = None) -> dict[str, Any] | None:
    """The last recorded sync for one peer, or None if never synced."""
    data = _load_json_object(history_path(path), "history") or {}
    peers = data.get("peers")
    if isinstance(peers, dict):
        entry = peers.get(peer)
        if isinstance(entry, dict):
            return entry
    return None
