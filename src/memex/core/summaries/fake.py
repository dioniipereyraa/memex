"""Deterministic Summarizer for tests.

Generates a "summary" from the first words of the text plus the title.
No semantic value, but stable and fast. Used by the test suite to
exercise the summary pipeline without calling Anthropic.
"""

from __future__ import annotations

from memex.core.summaries.base import Summarizer, SummarizerError


class FakeSummarizer(Summarizer):
    """Returns the first N words of the text as a pseudo-summary.

    If constructed with `fail=True`, raises `SummarizerError` on the
    first `summarize` call. Useful for tests covering the pipeline's
    silent-fail path.
    """

    def __init__(
        self,
        max_words: int = 20,
        model_name: str = "fake-summarizer",
        fail: bool = False,
    ) -> None:
        self._max_words = max_words
        self._model_name = model_name
        self._fail = fail
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def summarize(self, text: str, *, title: str | None = None) -> str:
        self.calls += 1
        if self._fail:
            raise SummarizerError("FakeSummarizer configured to fail")
        words = text.split()[: self._max_words]
        head = " ".join(words)
        if title:
            return f"{title}: {head}".strip()
        return head
