"""Canonicalization of repo identifiers (filesystem paths + git remotes).

A repo is identified by a stable `key` in the `repos` table. The key is
the remote URL if available (normalized to `host/owner/repo`), otherwise
the absolute path normalized to forward slashes (and lowercased on
Windows because the filesystem is case-insensitive there).

Why both: a single repo can be cloned at different paths on different
machines, but the remote URL is stable across machines. We prefer the
remote when present so that re-registering a moved repo collapses to
the same key.

The matcher (`matcher.py`) uses both representations as candidate
signals when scanning chat text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def normalize_path(p: str | Path) -> str:
    """Return a canonical string representation of a filesystem path.

    - Resolves to absolute (best effort, follows symlinks if they resolve).
    - Forward slashes regardless of OS.
    - Lowercased on Windows (the filesystem is case-insensitive there;
      this avoids `D:/Foo` and `d:/foo` being treated as different repos).
    - No trailing slash.

    Falls back gracefully if the path does not exist: uses `Path.absolute()`
    instead of `Path.resolve()` to avoid raising. The caller may want a
    canonical form for paths that no longer exist on disk (e.g., archived
    repos referenced in old chats).
    """
    path = Path(p)
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    s = str(resolved).replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        s = s.lower()
    return s


# Common forms we want to normalize. Examples handled:
#   git@github.com:user/repo.git           -> github.com/user/repo
#   https://github.com/user/repo.git       -> github.com/user/repo
#   https://github.com/user/repo           -> github.com/user/repo
#   ssh://git@github.com:22/user/repo.git  -> github.com/user/repo
#   http://gitlab.example.com/foo/bar.git  -> gitlab.example.com/foo/bar
_REMOTE_PATTERNS = [
    # SCP-style: git@host:owner/repo(.git)
    re.compile(r"^\w+@(?P<host>[^:]+):(?P<rest>.+?)(?:\.git)?/?$"),
    # URL-style: scheme://[user@]host[:port]/owner/repo(.git)
    re.compile(r"^[a-z]+://(?:[^@]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<rest>.+?)(?:\.git)?/?$"),
]


def normalize_remote(url: str) -> str | None:
    """Convert a git remote URL to `host/owner/repo` form.

    Returns `None` for strings that do not look like a recognizable git
    remote (callers can fall back to path-based identification).
    """
    if not url:
        return None
    url = url.strip()
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(url)
        if match is not None:
            host = match.group("host").lower()
            rest = match.group("rest").strip("/")
            return f"{host}/{rest}"
    return None


def canonical_repo_key(path: str | Path, remote_url: str | None = None) -> str:
    """Pick a stable canonical key for a repo.

    Strategy: prefer the normalized remote URL (stable across machines
    and clones); fall back to the normalized absolute path when the repo
    has no remote configured.

    The same repo cloned at two paths on the same machine will collide
    on the remote-based key, which is the intended behavior: we want one
    logical repo per `key`.
    """
    if remote_url:
        normalized = normalize_remote(remote_url)
        if normalized is not None:
            return normalized
    return normalize_path(path)
