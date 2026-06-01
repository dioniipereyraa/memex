"""Embedder backed by `fastembed` (embedded ONNX model, no external daemon).

Default backend (zero-config). The model is downloaded automatically the
first time `embed` is called (to `~/.cache/fastembed/`). To run fully
offline after the first run, no Internet is required.

Trade-off vs `OllamaEmbedder`:
- + Does not require installing Ollama or keeping a daemon running.
- + Single Python process.
- + Quantized model by default (130 MB vs 520 MB for the full one).
- - Model is downloaded on the first run (~30s with a good connection).
- - Embeddings are not bit-exact with Ollama's. If you switch backends
    over an already-indexed DB, it is worth re-ingesting so every vector
    comes from the same model.

Convention: vectors are L2-normalized before being returned (aligned
with `OllamaEmbedder` and the repo-wide convention).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from memex.config import settings
from memex.core.embeddings.base import Embedder, EmbedderError, l2_normalize

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5-Q"


class FastEmbedEmbedder(Embedder):
    """Embedder embedded via `fastembed` (ONNX in-process). Zero-config."""

    def __init__(
        self,
        model_name: str | None = None,
        normalize: bool = True,
    ) -> None:
        self._model_name = model_name or settings.embed_model or DEFAULT_MODEL
        if self._model_name != DEFAULT_MODEL:
            logger.warning(
                "Using a non-default embedding model %r. It is downloaded from the "
                "model hub on first use without a pinned revision or integrity check; "
                "only set MEMEX_EMBED_MODEL to a source you trust.",
                self._model_name,
            )
        self._normalize = normalize
        self._dim: int | None = None
        # Lazy import: fastembed pulls in numpy + onnxruntime (~30 MB); we
        # do not want to pay that import cost if the user picked Ollama.
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise EmbedderError(
                    "fastembed is not installed. Run `uv sync` or switch "
                    "MEMEX_EMBED_BACKEND to 'ollama'."
                ) from e
            try:
                self._model = TextEmbedding(model_name=self._model_name)
            except Exception as e:
                raise EmbedderError(
                    f"Could not load model {self._model_name!r}. "
                    "Check the name or your connection (first run downloads it). "
                    f"Detail: {e}"
                ) from e
        return self._model

    @property
    def dim(self) -> int:
        return self._dim if self._dim is not None else settings.embed_dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        # fastembed returns a generator of numpy arrays; convert to lists.
        try:
            results = list(model.embed(list(texts)))
        except Exception as e:
            raise EmbedderError(f"fastembed failed to embed: {e}") from e

        embeddings: list[list[float]] = [vec.tolist() for vec in results]
        if self._normalize:
            embeddings = [l2_normalize(v) for v in embeddings]
        if embeddings and self._dim is None:
            self._dim = len(embeddings[0])
        return embeddings
