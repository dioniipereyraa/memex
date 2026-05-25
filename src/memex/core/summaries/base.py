"""Abstract Summarizer interface + shared exception.

A `Summarizer` takes the canonical text of a conversation (concatenated
messages with `[sender]\n` headers) and returns a short natural-language
summary. Same pattern as `Embedder`: core depends on the interface;
concrete implementations (Anthropic, fake) live in sibling modules. The
factory in `__init__.py` picks one based on config.

Conventions:
- If the underlying model fails (missing API key, rate limit, network
  down), the implementation raises `SummarizerError` with an actionable
  message. The pipeline catches it and continues without a summary
  (silent fail), it does not abort the chat ingest.
- The summary text does not include external quotes, prefixes like
  "Summary:" or markdown. Plain text ready to use in `list_recent_chats`
  or as extra retrieval context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SummarizerError(Exception):
    """Operational error of a Summarizer.

    Implementations raise this with a clear user-facing message instead
    of propagating low-level exceptions (HTTPError, AuthError, timeouts).
    The pipeline catches it, logs a warning, and persists the chat
    without a summary.
    """


class Summarizer(ABC):
    """Turns the text of a conversation into a short summary."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier. Used in logging and, eventually, in
        conversation metadata to detect model changes."""

    @abstractmethod
    def summarize(self, text: str, *, title: str | None = None) -> str:
        """Generate a summary of the conversation text.

        - `text`: canonical text of the conversation (messages concatenated).
        - `title`: chat title (optional, helps the model anchor the topic).

        Returns the summary as a string. If the implementation cannot
        produce a summary (rate limit, missing key, network error), it
        raises `SummarizerError` with an actionable message.
        """
