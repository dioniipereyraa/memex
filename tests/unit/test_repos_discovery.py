"""Tests for `core/repos/discovery.parse_repo`.

These tests build small fake repos under `tmp_path` (with optional
`.git/config`, `pyproject.toml`, `package.json`, `Cargo.toml`) and
verify that `parse_repo` reads the metadata correctly.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from memex.core.repos.discovery import parse_repo


def _make_repo(
    base: Path,
    *,
    name: str,
    git_remote: str | None = None,
    pyproject_name: str | None = None,
    package_json_name: str | None = None,
    cargo_name: str | None = None,
) -> Path:
    """Build a fake repo directory under `base` and return its path."""
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)

    if git_remote is not None:
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            textwrap.dedent(
                f"""\
                [core]
                \trepositoryformatversion = 0
                [remote "origin"]
                \turl = {git_remote}
                \tfetch = +refs/heads/*:refs/remotes/origin/*
                """
            ),
            encoding="utf-8",
        )

    if pyproject_name is not None:
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "{pyproject_name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )

    if package_json_name is not None:
        (repo / "package.json").write_text(
            f'{{"name": "{package_json_name}", "version": "0.1.0"}}',
            encoding="utf-8",
        )

    if cargo_name is not None:
        (repo / "Cargo.toml").write_text(
            f'[package]\nname = "{cargo_name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )

    return repo


def test_full_python_repo(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        name="memex",
        git_remote="git@github.com:user/memex.git",
        pyproject_name="memex",
    )
    info = parse_repo(repo)
    assert info.name == "memex"
    assert info.manifest_name == "memex"
    assert info.remote_url == "git@github.com:user/memex.git"
    # Key picks remote (normalized) over path.
    assert info.key == "github.com/user/memex"


def test_node_repo(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        name="my-app",
        git_remote="https://github.com/team/my-app",
        package_json_name="@team/my-app",
    )
    info = parse_repo(repo)
    assert info.manifest_name == "@team/my-app"
    assert info.name == "@team/my-app"  # manifest wins for display


def test_rust_repo(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        name="cratey",
        cargo_name="cratey",
    )
    info = parse_repo(repo)
    assert info.manifest_name == "cratey"
    assert info.remote_url is None


def test_repo_without_manifest_uses_dir_name(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, name="standalone")
    info = parse_repo(repo)
    assert info.manifest_name is None
    assert info.name == "standalone"
    assert info.remote_url is None
    # Key falls back to path-based.
    assert "github.com" not in info.key


def test_repo_with_only_git_no_manifest(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        name="just-git",
        git_remote="git@gitlab.com:org/just-git.git",
    )
    info = parse_repo(repo)
    assert info.manifest_name is None
    assert info.name == "just-git"  # falls back to dir name
    assert info.key == "gitlab.com/org/just-git"


def test_pyproject_without_project_section(tmp_path: Path) -> None:
    """Older `pyproject.toml` may have no `[project]` table."""
    repo = tmp_path / "old-style"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[tool.poetry]\nname = "old"\n', encoding="utf-8")
    info = parse_repo(repo)
    # We do not read `[tool.poetry]`. Manifest treated as missing.
    assert info.manifest_name is None
    assert info.name == "old-style"


def test_manifest_priority_pyproject_over_package_json(tmp_path: Path) -> None:
    """If both exist, pyproject wins (we check it first)."""
    repo = _make_repo(
        tmp_path,
        name="mixed",
        pyproject_name="py-name",
        package_json_name="js-name",
    )
    info = parse_repo(repo)
    assert info.manifest_name == "py-name"


def test_corrupted_git_config_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "broken-git"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("not valid ini @@", encoding="utf-8")
    info = parse_repo(repo)
    # Did not raise; remote stays None.
    assert info.remote_url is None


def test_corrupted_pyproject_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "broken-toml"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project\nname = broken", encoding="utf-8")
    info = parse_repo(repo)
    assert info.manifest_name is None


def test_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_repo(tmp_path / "does-not-exist")


def test_file_path_raises(tmp_path: Path) -> None:
    f = tmp_path / "afile.txt"
    f.write_text("not a dir", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        parse_repo(f)


class TestFindRepoRoot:
    def test_finds_at_current_dir(self, tmp_path: Path) -> None:
        from memex.core.repos.discovery import find_repo_root

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        assert find_repo_root(repo) == repo

    def test_finds_in_ancestor(self, tmp_path: Path) -> None:
        from memex.core.repos.discovery import find_repo_root

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        nested = repo / "src" / "memex"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == repo

    def test_returns_none_if_no_git(self, tmp_path: Path) -> None:
        from memex.core.repos.discovery import find_repo_root

        empty = tmp_path / "empty"
        empty.mkdir()
        assert find_repo_root(empty) is None

    def test_handles_git_as_file_for_worktree(self, tmp_path: Path) -> None:
        """Git worktrees have `.git` as a file (gitlink), not a directory."""
        from memex.core.repos.discovery import find_repo_root

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../main/.git/worktrees/wt", encoding="utf-8")
        assert find_repo_root(worktree) == worktree
