"""Factory + interfaces de summarization.

Punto único de entrada para obtener el summarizer default según config.
Los call sites NO instancian `AnthropicSummarizer` o `FakeSummarizer`
directamente; usan `get_default_summarizer()` que respeta el feature flag
y devuelve `None` cuando los summaries están desactivados.

Convención: si la feature está OFF, devuelve `None` (el pipeline lo
chequea y simplemente no genera resumen). Esto evita errores ruidosos
cuando el usuario no quiere la feature.
"""

from __future__ import annotations

from memex.config import settings
from memex.core.summaries.base import Summarizer, SummarizerError

__all__ = [
    "Summarizer",
    "SummarizerError",
    "get_default_summarizer",
]


def get_default_summarizer() -> Summarizer | None:
    """Devuelve el summarizer configurado, o `None` si la feature está OFF.

    Hoy soporta un único backend (Anthropic). El factory está pensado por
    si en el futuro hay alternativas (Ollama local, OpenAI, fake-via-env).

    No valida la API key acá: la validación ocurre al primer `summarize()`
    para que el factory sea barato y libre de side effects.
    """
    if not settings.summary_enabled:
        return None
    from memex.core.summaries.anthropic_summarizer import AnthropicSummarizer

    return AnthropicSummarizer()
