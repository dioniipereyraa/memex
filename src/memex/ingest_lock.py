"""Shared single-flight lock for any path that loads the embedding model.

Loading the embedder costs ~0.5 GB (model weights + the onnxruntime arena). Two
independent paths can trigger it:

- the ``ingest-claude-code`` CLI (manual run, the 15-min schedule, the SessionEnd
  hook); and
- the live-capture worker that ``memex serve`` spawns per captured conversation.

If two of them run at once the model loads stack and RAM/CPU roughly double. This
module gives both an OS advisory file lock (``fcntl.flock`` on
``<db_dir>/ingest.lock``) so only one embed runs at a time, coordinated across
independent processes.

Two acquire modes:

- :func:`acquire_nonblocking` for the CLI backstop: a second ingest simply skips
  (the 15-min schedule re-runs later), so it must not wait.
- :func:`acquire_blocking` for live capture: it should WAIT for an in-flight
  ingest to finish rather than drop the freshest chat, but never hang forever.

Best-effort where ``fcntl`` is unavailable (Windows) or the lock file cannot be
created: the acquire returns a non-``None`` sentinel so ingest is never blocked
by the lock layer itself. Closing the returned handle (see :func:`release`)
releases the lock; it is also released automatically when the process exits.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import IO

from memex.config import settings


def _lock_path(db_path: Path | str | None = None) -> Path:
    base = Path(db_path) if db_path is not None else Path(settings.db_path)
    return base.parent / "ingest.lock"


def _open_handle(db_path: Path | str | None) -> IO[str] | None:
    path = _lock_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("w")
    except OSError:
        return None


def acquire_nonblocking(db_path: Path | str | None = None) -> object | None:
    """Take the lock without waiting.

    Returns an open handle to keep referenced for the lock to hold, or ``None``
    if another holder has it. Returns a bare sentinel (never ``None``) where the
    lock cannot be enforced (no ``fcntl``, or the file cannot be created), so the
    caller proceeds rather than being blocked by the lock layer.
    """
    handle = _open_handle(db_path)
    if handle is None:
        return object()
    try:
        import fcntl
    except ImportError:
        return handle  # no flock on this platform; best-effort
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def acquire_blocking(db_path: Path | str | None = None, timeout: float = 60.0) -> object | None:
    """Wait up to ``timeout`` seconds for the lock.

    Returns an open handle on success, or ``None`` if the timeout elapsed while
    another holder kept it. Used by the live-capture path so a capture waits for
    an in-flight ingest instead of stacking a second model load, but never hangs.
    """
    handle = _open_handle(db_path)
    if handle is None:
        return object()
    try:
        import fcntl
    except ImportError:
        return handle  # no flock on this platform; best-effort
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.25)


def release(handle: object | None) -> None:
    """Release a lock taken by one of the acquire helpers (a no-op for sentinels)."""
    if handle is None or not hasattr(handle, "close"):
        return
    with contextlib.suppress(Exception):
        handle.close()  # closing the fd drops the flock
