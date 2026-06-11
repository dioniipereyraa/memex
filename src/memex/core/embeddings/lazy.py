"""An Embedder that defers building the real backend until first use.

The fastembed model is a few hundred MB of RAM and a second or two to load.
The incremental `ingest-claude-code` re-scan skips unchanged sessions without
embedding anything, so on a quiet run (the common case for a 15-minute
backstop schedule) there is nothing to embed at all. `LazyEmbedder` wraps a
factory and only instantiates the real embedder on the first non-empty
`embed()` call, so a no-op scan loads no model and costs almost nothing.

`embed([])` returns `[]` without instantiating, and the pipeline only calls
`embed` when a conversation actually produced chunks, so an all-skipped scan
never touches the backend.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from memex.core.embeddings.base import Embedder


class LazyEmbedder(Embedder):
    """Defers `factory()` until a real embedding is needed."""

    def __init__(self, factory: Callable[[], Embedder]) -> None:
        self._factory = factory
        self._real: Embedder | None = None

    def _get(self) -> Embedder:
        if self._real is None:
            self._real = self._factory()
        return self._real

    @property
    def loaded(self) -> bool:
        """True once the real embedder has been built (model loaded)."""
        return self._real is not None

    @property
    def dim(self) -> int:
        return self._get().dim

    @property
    def model_name(self) -> str:
        return self._get().model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Empty input never needs the model, so do not pay to load it.
        if not texts:
            return []
        return self._get().embed(texts)
