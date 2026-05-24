"""Tests for the chat ↔ repo matcher.

Each test focuses on one signal in isolation, then a couple of combined
cases at the end.
"""

from __future__ import annotations

import pytest

from memex.core.repos.discovery import RepoInfo
from memex.core.repos.matcher import (
    CONFIDENCE_MANIFEST,
    CONFIDENCE_NAME,
    CONFIDENCE_PATH,
    CONFIDENCE_REMOTE,
    match_text,
)


@pytest.fixture
def repo_memex() -> RepoInfo:
    return RepoInfo(
        key="github.com/dioniipereyraa/memex",
        path="d:/dionisio/memex",
        remote_url="github.com/dioniipereyraa/memex",
        name="memex",
        manifest_name="memex",
    )


@pytest.fixture
def repo_other() -> RepoInfo:
    return RepoInfo(
        key="github.com/some/other",
        path="d:/work/other",
        remote_url="github.com/some/other",
        name="other",
        manifest_name="other-package",
    )


class TestRemoteSignal:
    def test_remote_url_in_text_high_confidence(self, repo_memex: RepoInfo) -> None:
        text = "We discussed github.com/dioniipereyraa/memex yesterday."
        matches = match_text(text, [repo_memex])
        assert len(matches) == 1
        assert matches[0].repo_key == repo_memex.key
        assert matches[0].confidence == CONFIDENCE_REMOTE

    def test_remote_match_is_case_insensitive(self, repo_memex: RepoInfo) -> None:
        text = "GitHub.com/Dioniipereyraa/Memex is the repo."
        matches = match_text(text, [repo_memex])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_REMOTE


class TestPathSignal:
    def test_path_forward_slashes(self, repo_memex: RepoInfo) -> None:
        text = "Open d:/dionisio/memex and check pyproject.toml"
        matches = match_text(text, [repo_memex])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_PATH

    def test_path_backslashes(self, repo_memex: RepoInfo) -> None:
        text = r"Open d:\dionisio\memex and check pyproject.toml"
        matches = match_text(text, [repo_memex])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_PATH


class TestManifestSignal:
    def test_manifest_name_word_bounded(self) -> None:
        repo = RepoInfo(
            key="local/some-pkg",
            path="d:/dev/some-pkg",
            remote_url=None,
            name="some-pkg",
            manifest_name="my-very-specific-pkg-name",
        )
        text = "I am working on my-very-specific-pkg-name today."
        matches = match_text(text, [repo])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_MANIFEST

    def test_manifest_name_not_matched_inside_other_word(self) -> None:
        """`memexXY` should NOT match the manifest name `memex` (word boundary)."""
        repo = RepoInfo(
            key="local/memex",
            path="d:/local/memex",
            remote_url=None,
            name="zzz-no-name-match-zzz",
            manifest_name="memex",
        )
        text = "I was using memexXY which is a different thing"
        matches = match_text(text, [repo])
        assert matches == []


class TestNameSignal:
    def test_name_match_threshold(self) -> None:
        """The name signal alone reaches the default 0.5 threshold."""
        repo = RepoInfo(
            key="local/uniqueproj",
            path="d:/dev/uniqueproj",
            remote_url=None,
            name="uniqueproj",
            manifest_name=None,
        )
        text = "Reviewing uniqueproj after the refactor."
        matches = match_text(text, [repo])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_NAME

    def test_name_not_matched_inside_word(self) -> None:
        """`extramemex` should NOT trip the name signal for `memex`."""
        repo = RepoInfo(
            key="local/memex",
            path="d:/local/memex",
            remote_url=None,
            name="memex",
            manifest_name=None,
        )
        text = "The extramemex library does foobar."
        matches = match_text(text, [repo])
        assert matches == []


class TestCombinedSignals:
    def test_highest_signal_wins(self, repo_memex: RepoInfo) -> None:
        """Path + name both fire, but the result has the higher one (path)."""
        text = "d:/dionisio/memex contains the memex code"
        matches = match_text(text, [repo_memex])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_PATH

    def test_multiple_repos_independent(self, repo_memex: RepoInfo, repo_other: RepoInfo) -> None:
        text = "I touched github.com/dioniipereyraa/memex and the other-package today."
        matches = match_text(text, [repo_memex, repo_other])
        keys = {m.repo_key for m in matches}
        assert keys == {repo_memex.key, repo_other.key}

    def test_sorted_by_confidence_desc(self, repo_memex: RepoInfo, repo_other: RepoInfo) -> None:
        # memex hits remote URL (1.0), other hits manifest_name (0.8).
        text = "github.com/dioniipereyraa/memex is one repo, other-package is the second."
        matches = match_text(text, [repo_memex, repo_other])
        assert matches[0].repo_key == repo_memex.key
        assert matches[0].confidence > matches[1].confidence

    def test_below_threshold_not_returned(self) -> None:
        """If we raise the threshold, lower-signal matches drop off."""
        repo = RepoInfo(
            key="local/x",
            path="d:/dev/x",
            remote_url=None,
            name="x",
            manifest_name=None,
        )
        text = "the symbol x is used in math"
        # Default threshold (0.5): name match reaches it.
        matches_default = match_text(text, [repo])
        assert len(matches_default) == 1
        # Raised threshold above name signal: nothing returned.
        matches_strict = match_text(text, [repo], threshold=0.6)
        assert matches_strict == []


class TestEdgeCases:
    def test_empty_text_returns_no_matches(self, repo_memex: RepoInfo) -> None:
        assert match_text("", [repo_memex]) == []

    def test_no_repos_returns_empty(self) -> None:
        assert match_text("text here", []) == []

    def test_repo_with_only_path_still_matches(self) -> None:
        """A repo without git remote or manifest still matches via path/name."""
        repo = RepoInfo(
            key="d:/dev/standalone",
            path="d:/dev/standalone",
            remote_url=None,
            name="standalone",
            manifest_name=None,
        )
        text = "Check d:/dev/standalone for the script."
        matches = match_text(text, [repo])
        assert len(matches) == 1
        assert matches[0].confidence == CONFIDENCE_PATH
