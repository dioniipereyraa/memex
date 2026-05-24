"""Summarizer determinístico para tests.

Genera un "resumen" de las primeras palabras del texto + el título. No tiene
ningún valor semántico, pero es estable y rápido. Usado por la suite de tests
para ejercer el pipeline de summary sin llamar a Anthropic.
"""

from __future__ import annotations

from memex.core.summaries.base import Summarizer, SummarizerError


class FakeSummarizer(Summarizer):
    """Devuelve las primeras N palabras del texto como pseudo-resumen.

    Si se construye con `fail=True`, levanta `SummarizerError` al primer
    `summarize`. Útil para tests que cubren el camino de fallo silencioso
    del pipeline.
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
            raise SummarizerError("FakeSummarizer configurado para fallar")
        words = text.split()[: self._max_words]
        head = " ".join(words)
        if title:
            return f"{title}: {head}".strip()
        return head
