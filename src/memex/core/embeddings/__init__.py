"""Factory + interfaces for embedding.

Single entry point to get the default embedder based on config. Call
sites do NOT instantiate `OllamaEmbedder` or `FastEmbedEmbedder`
directly; use `get_default_embedder()` so the backend is configurable
via `MEMEX_EMBED_BACKEND` without touching code.
"""

from __future__ import annotations

from memex.config import settings
from memex.core.embeddings.base import Embedder, EmbedderError, l2_normalize

__all__ = [
    "Embedder",
    "EmbedderError",
    "get_default_embedder",
    "l2_normalize",
]


def get_default_embedder() -> Embedder:
    """Return the embedder configured via `MEMEX_EMBED_BACKEND`.

    - `fastembed` (default): in-process embedding via ONNX. Zero-config.
    - `ollama`: client that talks to a local Ollama daemon.

    If the backend is invalid, raises `EmbedderError` with an actionable
    message.
    """
    backend = (settings.embed_backend or "fastembed").strip().lower()

    if backend == "fastembed":
        # Lazy import: avoids paying the cost of loading onnxruntime/numpy
        # if the user chose Ollama.
        from memex.core.embeddings.fastembed_embedder import FastEmbedEmbedder

        return FastEmbedEmbedder()

    if backend == "ollama":
        from memex.core.embeddings.ollama import OllamaEmbedder

        return OllamaEmbedder()

    raise EmbedderError(
        f"Invalid MEMEX_EMBED_BACKEND: {backend!r}. Valid values: 'fastembed', 'ollama'."
    )
