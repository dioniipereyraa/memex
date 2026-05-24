"""Filesystem discovery: read a directory and infer repo metadata.

Used by `memex repos add <path>` and by the auto-scan at ingest. Reads:

- `.git/config` for the remote URL (origin or first remote it finds).
- `pyproject.toml [project] name` (Python).
- `package.json "name"` (Node).
- `Cargo.toml [package] name` (Rust).

Whatever we can read goes into a `RepoInfo`. Anything missing (no git,
no manifest) is left as `None` and the matcher just uses the path-based
signals instead.
"""

from __future__ import annotations

import configparser
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoInfo:
    """Canonical representation of a registered repo.

    The same shape is produced by `parse_repo` (reading the filesystem)
    and by `repo.list_repos` (reading the DB). The matcher only consumes
    `RepoInfo`, decoupled from where it came from.

    `key` is the canonical id (see `canonical_repo_key`). It is what
    `chat_repos.repo_key` references.
    """

    key: str
    path: str | None
    remote_url: str | None
    name: str
    manifest_name: str | None


@dataclass(frozen=True)
class ChatRepoAssociation:
    """One row of `chat_repos`, with the related `RepoInfo` already hydrated.

    `source` is either `'auto'` (the matcher detected it at ingest or scan
    time) or `'manual'` (the user added it with `memex tag`). `confidence`
    is the matcher score for auto-associations and `None` for manual ones.
    """

    repo: RepoInfo
    source: str
    confidence: float | None


def _read_git_remote(repo_path: Path) -> str | None:
    """Return the URL of `origin` (or the first remote found) from `.git/config`.

    Returns `None` if `.git/config` does not exist or has no remote.
    """
    config_path = repo_path / ".git" / "config"
    if not config_path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        # Git's config syntax is INI-compatible enough for our purposes.
        parser.read(config_path, encoding="utf-8")
    except configparser.Error:
        return None

    # Try `origin` first, then any other remote.
    candidates: list[str] = []
    for section in parser.sections():
        if section.startswith('remote "') and section.endswith('"'):
            url = parser.get(section, "url", fallback=None)
            if url:
                if section == 'remote "origin"':
                    return url
                candidates.append(url)
    return candidates[0] if candidates else None


def _read_manifest_name(repo_path: Path) -> str | None:
    """Try `pyproject.toml`, `package.json`, `Cargo.toml`. First hit wins.

    Returns `None` if no manifest is present or none of them declare a name.
    """
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            name = data.get("project", {}).get("name")
            if isinstance(name, str) and name:
                return name
        except (tomllib.TOMLDecodeError, OSError):
            pass

    package_json = repo_path / "package.json"
    if package_json.is_file():
        try:
            with package_json.open(encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name")
            if isinstance(name, str) and name:
                return name
        except (json.JSONDecodeError, OSError):
            pass

    cargo = repo_path / "Cargo.toml"
    if cargo.is_file():
        try:
            with cargo.open("rb") as f:
                data = tomllib.load(f)
            name = data.get("package", {}).get("name")
            if isinstance(name, str) and name:
                return name
        except (tomllib.TOMLDecodeError, OSError):
            pass

    return None


def parse_repo(path: str | Path) -> RepoInfo:
    """Read a directory and produce a `RepoInfo`.

    Raises `FileNotFoundError` if the path does not exist; `NotADirectoryError`
    if it points to a file. The caller (CLI) translates these into actionable
    user-facing errors.

    Best-effort on the rest: missing `.git`, missing manifests, unreadable
    files all just leave the corresponding field as `None`.
    """
    from memex.core.repos.keys import canonical_repo_key, normalize_path

    repo_path = Path(path)
    if not repo_path.exists():
        raise FileNotFoundError(f"Repo path not found: {repo_path}")
    if not repo_path.is_dir():
        raise NotADirectoryError(f"Repo path is not a directory: {repo_path}")

    remote_url = _read_git_remote(repo_path)
    manifest_name = _read_manifest_name(repo_path)
    # Display name: prefer manifest, fall back to the last path segment.
    display_name = manifest_name if manifest_name else repo_path.name

    key = canonical_repo_key(repo_path, remote_url)

    return RepoInfo(
        key=key,
        path=normalize_path(repo_path),
        remote_url=remote_url,
        name=display_name,
        manifest_name=manifest_name,
    )
