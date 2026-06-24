"""On-disk registry of sync peers.

A peer is another device running memex that this device can pull from. The
registry is a small JSON list next to the DB, written user-only (0600) because
each entry holds the peer's access token, the shared secret used to authenticate
to its `/sync/*` endpoints (same sensitivity as the local ingest token).

Stored as a plain file (not a DB table) on purpose for Phase 1: it needs no
schema migration, is trivially inspectable, and a handful of peers never grows
large. If sync gains richer per-peer state later (last-sync cursors, counts), a
table can replace this without changing the public functions here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, field_validator

from memex.config import settings

logger = logging.getLogger("memex.sync.peers")


class Peer(BaseModel):
    """A paired device this one can pull from."""

    name: str
    # Base URL of the peer's `memex serve`, e.g. "http://100.x.y.z:5777"
    # (a Tailscale address or LAN IP). No trailing slash.
    url: str
    # The peer's per-install access token (its `memex token`), sent as the
    # `X-Memex-Token` header so the peer authorizes the pull.
    token: str

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("peer name must not be blank")
        return value

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("peer url must start with http:// or https://")
        return value


def peers_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else settings.sync_peers_path


def load_peers(path: Path | str | None = None) -> list[Peer]:
    target = peers_path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError:
        return []
    except ValueError:
        logger.warning("Sync peers file %s is not valid JSON; treating as empty.", target)
        return []
    if not isinstance(data, list):
        return []
    peers: list[Peer] = []
    for item in data:
        try:
            peers.append(Peer(**item))
        except (TypeError, ValueError):
            logger.warning("Skipping a malformed peer entry in %s.", target)
    return peers


def _write_peers(peers: list[Peer], path: Path | str | None = None) -> None:
    target = peers_path(path)
    parent_existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o700)
    payload = json.dumps([p.model_dump() for p in peers], ensure_ascii=False, indent=2)
    # Create with 0600 from the start so the tokens are never world-readable,
    # even for the brief window between create and chmod.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    # If the file pre-existed with looser bits, tighten them.
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)


def get_peer(name: str, path: Path | str | None = None) -> Peer | None:
    for peer in load_peers(path):
        if peer.name == name:
            return peer
    return None


def add_peer(peer: Peer, path: Path | str | None = None) -> None:
    """Add or replace a peer (keyed by name)."""
    others = [p for p in load_peers(path) if p.name != peer.name]
    _write_peers([*others, peer], path)


def remove_peer(name: str, path: Path | str | None = None) -> bool:
    """Remove a peer by name. Returns True if one was removed."""
    peers = load_peers(path)
    kept = [p for p in peers if p.name != name]
    if len(kept) == len(peers):
        return False
    _write_peers(kept, path)
    return True
