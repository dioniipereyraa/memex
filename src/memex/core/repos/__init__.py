"""Repo identity, discovery, and chat ↔ repo matching.

Phase 3 sub-task 2: associate Claude.ai chats to local code repos so
`search_chats(repo=...)` can boost results that touch the active repo.

Public surface:
- `normalize_path(p)`: canonicalize a filesystem path to a stable string.
- `normalize_remote(url)`: canonicalize a git remote URL to host/owner/repo.
- `parse_repo(path)`: read a directory and produce a `RepoInfo` (path,
  remote_url, name, manifest_name).
- `match_conversation(text, title, repos)`: returns the repo keys that the
  given text matched, with confidence scores.
"""

from __future__ import annotations

from memex.core.repos.discovery import (
    ChatRepoAssociation,
    RepoInfo,
    find_repo_root,
    parse_repo,
)
from memex.core.repos.keys import (
    canonical_repo_key,
    normalize_path,
    normalize_remote,
)
from memex.core.repos.matcher import Match, match_text
from memex.core.repos.resolve import resolve_repo_key

__all__ = [
    "ChatRepoAssociation",
    "Match",
    "RepoInfo",
    "canonical_repo_key",
    "find_repo_root",
    "match_text",
    "normalize_path",
    "normalize_remote",
    "parse_repo",
    "resolve_repo_key",
]
