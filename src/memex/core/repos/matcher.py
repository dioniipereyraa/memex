"""Detect which registered repos a chat conversation references.

Given the canonical text of a chat (the same stream the chunker sees)
and the list of repos registered in the DB, produce zero or more
`Match` entries: `(repo_key, confidence)`.

Signals (highest confidence wins per repo):

| signal                          | confidence |
|---------------------------------|------------|
| `remote_url` literal in text    | 1.00       |
| absolute path literal in text   | 0.90       |
| `manifest_name` (word-bounded)  | 0.80       |
| `name` (word-bounded)           | 0.50       |

Anything below 0.50 is dropped (the implicit threshold). The threshold
exists because the `name` of a repo is often a common word (e.g.
"docs", "memex") and we do not want every chat that mentions it to
get tagged. Manifest names are usually more specific (e.g. python
package names) so they earn a higher signal.

The matcher does NOT normalize the chat text; it generates candidate
strings to look for. This lets us catch the user typing the path with
backslashes on Windows even when the canonical form uses forward
slashes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from memex.core.repos.discovery import RepoInfo

# Anything below this never produces a match. Tuning knob.
CONFIDENCE_THRESHOLD = 0.5

# Per-signal confidence values.
CONFIDENCE_REMOTE = 1.0
CONFIDENCE_PATH = 0.9
CONFIDENCE_MANIFEST = 0.8
CONFIDENCE_NAME = 0.5


@dataclass(frozen=True)
class Match:
    """A repo that the matcher associated to a piece of text."""

    repo_key: str
    confidence: float


def _path_candidates(path: str) -> list[str]:
    """Generate alternate string forms of a path that a human might type.

    A user on Windows might write the path with backslashes, with forward
    slashes, or in lowercase / mixed case. We try a small set of common
    variants. Search is done case-insensitively for path matching anyway,
    so case variants are absorbed at match time.
    """
    if not path:
        return []
    forward = path.replace("\\", "/").rstrip("/")
    backward = forward.replace("/", "\\")
    candidates = {forward, backward}
    return list(candidates)


def _word_bounded_match(needle: str, haystack: str) -> bool:
    """Substring match with word boundaries on alphanumeric edges.

    Avoids matching `memex` inside `gimemext`. Boundaries are at start
    of string, end of string, or any non-word character. Case-insensitive.
    """
    if not needle:
        return False
    pattern = r"(?<![a-zA-Z0-9_])" + re.escape(needle) + r"(?![a-zA-Z0-9_])"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _has_substring_ci(needle: str, haystack: str) -> bool:
    """Case-insensitive substring check. Empty needle returns False."""
    if not needle:
        return False
    return needle.lower() in haystack.lower()


def match_text(
    text: str,
    repos: Iterable[RepoInfo],
    *,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> list[Match]:
    """Return matches above the confidence threshold, sorted by confidence desc.

    The same `repo_key` will appear at most once in the result. If
    multiple signals fire for the same repo, the highest-confidence one
    wins; we do not sum signals (would distort the 0.0-1.0 scale and
    interact weirdly with the boost formula in `search_chats`).
    """
    if not text:
        return []

    best: dict[str, float] = {}

    for repo in repos:
        score = 0.0

        # Signal 1: remote URL literal. Highest confidence.
        if repo.remote_url and _has_substring_ci(repo.remote_url, text):
            score = max(score, CONFIDENCE_REMOTE)

        # Signal 2: absolute path literal (any common form).
        if repo.path:
            for candidate in _path_candidates(repo.path):
                if _has_substring_ci(candidate, text):
                    score = max(score, CONFIDENCE_PATH)
                    break

        # Signal 3: manifest name (word-bounded).
        if repo.manifest_name and _word_bounded_match(repo.manifest_name, text):
            score = max(score, CONFIDENCE_MANIFEST)

        # Signal 4: display name (word-bounded). Lowest confidence; only
        # earns the threshold if nothing else fired.
        if repo.name and _word_bounded_match(repo.name, text):
            score = max(score, CONFIDENCE_NAME)

        if score >= threshold:
            best[repo.key] = score

    # Stable sort: highest confidence first, ties broken by repo_key.
    return sorted(
        (Match(repo_key=k, confidence=v) for k, v in best.items()),
        key=lambda m: (-m.confidence, m.repo_key),
    )
