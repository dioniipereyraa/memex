"""Tests for repo key canonicalization (paths + git remote URLs)."""

from __future__ import annotations

import sys

import pytest

from memex.core.repos.keys import (
    canonical_repo_key,
    normalize_path,
    normalize_remote,
)


class TestNormalizePath:
    def test_forward_slashes(self, tmp_path) -> None:
        result = normalize_path(tmp_path)
        assert "\\" not in result
        # On Windows the path is lowercased.
        if sys.platform == "win32":
            assert result == result.lower()

    def test_trailing_slash_stripped(self, tmp_path) -> None:
        with_slash = str(tmp_path) + "/"
        assert normalize_path(with_slash) == normalize_path(tmp_path)

    def test_resolves_relative_to_absolute(self, tmp_path) -> None:
        result = normalize_path(".")
        # Should be absolute.
        assert len(result) > 1
        # First char should not be "." (was resolved away).
        assert not result.startswith("./")

    def test_nonexistent_path_does_not_raise(self) -> None:
        result = normalize_path("/this/path/does/not/exist/anywhere")
        # Did not crash. Returned a canonical form.
        assert "\\" not in result

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behavior")
    def test_lowercases_on_windows(self) -> None:
        # Mixed-case path collapses to lowercase canonical form.
        a = normalize_path(r"D:\Foo\Bar")
        b = normalize_path(r"d:/FOO/bar")
        assert a == b


class TestNormalizeRemote:
    def test_ssh_github(self) -> None:
        assert normalize_remote("git@github.com:user/repo.git") == "github.com/user/repo"

    def test_ssh_github_no_suffix(self) -> None:
        assert normalize_remote("git@github.com:user/repo") == "github.com/user/repo"

    def test_https_github(self) -> None:
        assert normalize_remote("https://github.com/user/repo.git") == "github.com/user/repo"

    def test_https_github_no_suffix(self) -> None:
        assert normalize_remote("https://github.com/user/repo") == "github.com/user/repo"

    def test_https_with_user(self) -> None:
        assert normalize_remote("https://oauth@github.com/user/repo.git") == "github.com/user/repo"

    def test_ssh_with_port(self) -> None:
        assert normalize_remote("ssh://git@github.com:22/user/repo.git") == "github.com/user/repo"

    def test_lowercases_host(self) -> None:
        assert normalize_remote("https://GitHub.com/User/Repo.git") == "github.com/User/Repo"

    def test_gitlab(self) -> None:
        assert (
            normalize_remote("https://gitlab.example.com/group/sub/project.git")
            == "gitlab.example.com/group/sub/project"
        )

    def test_self_hosted_with_port(self) -> None:
        assert normalize_remote("http://git.internal:8080/team/proj") == "git.internal/team/proj"

    def test_empty_string_returns_none(self) -> None:
        assert normalize_remote("") is None

    def test_garbage_returns_none(self) -> None:
        assert normalize_remote("not a url at all") is None

    def test_trailing_slash_stripped(self) -> None:
        assert normalize_remote("https://github.com/user/repo/") == "github.com/user/repo"


class TestCanonicalRepoKey:
    def test_prefers_remote_when_available(self, tmp_path) -> None:
        key = canonical_repo_key(tmp_path, "git@github.com:user/repo.git")
        assert key == "github.com/user/repo"

    def test_falls_back_to_path_when_no_remote(self, tmp_path) -> None:
        key = canonical_repo_key(tmp_path, None)
        assert "github.com" not in key
        assert "\\" not in key

    def test_falls_back_when_remote_is_unparseable(self, tmp_path) -> None:
        key = canonical_repo_key(tmp_path, "garbage-not-a-url")
        # Garbage remote falls back to path-based key.
        assert "garbage" not in key
        assert "\\" not in key

    def test_same_repo_two_paths_same_remote_collide(self) -> None:
        """Two checkouts of the same repo on the same machine collapse to one key."""
        k1 = canonical_repo_key("/home/me/work/repo", "git@github.com:org/repo.git")
        k2 = canonical_repo_key("/tmp/scratch/repo", "git@github.com:org/repo.git")
        assert k1 == k2 == "github.com/org/repo"
