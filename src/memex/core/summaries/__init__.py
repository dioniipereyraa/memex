"""Factory + interfaces for summarization.

Single entry point to get the default summarizer based on config. Call
sites do NOT instantiate `AnthropicSummarizer` or `FakeSummarizer`
directly; they use `get_default_summarizer()` which respects the
feature flag and returns `None` when summaries are disabled.

Convention: if the feature is OFF, return `None` (the pipeline checks
this and simply skips summary generation). This avoids noisy errors
when the user does not want the feature.
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
    """Return the configured summarizer, or `None` if the feature is OFF.

    Today only one backend is supported (Anthropic). The factory exists
    in case future alternatives appear (local Ollama, OpenAI, env-driven
    fake).

    Does not validate the API key here: validation happens on the first
    `summarize()` call so the factory stays cheap and side-effect free.
    """
    if not settings.summary_enabled:
        return None
    from memex.core.summaries.anthropic_summarizer import AnthropicSummarizer

    return AnthropicSummarizer()
