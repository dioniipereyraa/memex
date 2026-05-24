"""Resolve a user-provided repo argument to a registered repo key.

Shared by `tools.search_chats(repo=...)` and the `memex session-context`
CLI command. Lives in `core/repos/` because both consumers are downstream
of the storage and key-canonicalization modules; placing it here keeps
the dependency direction clean (core does not import from transports
or cli).
"""

from __future__ import annotations

import sqlite3

from memex.core.repos.keys import normalize_path, normalize_remote
from memex.core.storage import repo


def resolve_repo_key(conn: sqlite3.Connection, repo_arg: str) -> str | None:
    """Map a path / remote URL / canonical key to a registered repo key.

    Strategy:
    1. Try the argument as the canonical key directly.
    2. Normalize as a git remote URL and try again.
    3. Normalize as a filesystem path and try again.

    Returns the matching `key` on success, `None` if no registered repo
    matches any of the three forms.
    """
    if repo.get_repo(conn, repo_arg) is not None:
        return repo_arg

    candidate_remote = normalize_remote(repo_arg)
    if candidate_remote and repo.get_repo(conn, candidate_remote) is not None:
        return candidate_remote

    candidate_path = normalize_path(repo_arg)
    if repo.get_repo(conn, candidate_path) is not None:
        return candidate_path

    return None
