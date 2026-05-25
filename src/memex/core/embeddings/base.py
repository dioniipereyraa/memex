"""Abstract Embedder interface + shared exception.

Everything that goes through the retrieval pipeline passes through an
`Embedder`. This interface lets us swap models (local Ollama, remote
API, fake in tests) without touching the rest of core.

Any new implementation must:
- Expose `dim` (vector dimension; must match the `vec_chunks` table).
- Expose `model_name` (informational string for logging and schema_meta).
- Implement `embed(texts)` to return a parallel list of vectors.

Convention: vectors are returned L2-normalized when possible, so that
sqlite-vec L2 distance matches cosine-similarity ranking. Implementations
may skip normalization if their model already returns unit vectors (rare)
or if the caller explicitly disables it.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbedderError(Exception):
    """Operational error of an Embedder.

    Implementations raise this with a clear user-facing message instead
    of propagating low-level exceptions (connection errors, timeouts,
    missing models). Designed so the CLI and the MCP server catch it
    and return an actionable message.
    """


class Embedder(ABC):
    """Converts texts to fixed-dimension vectors."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Vector dimension. Constant for a given instance."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier. Used in logging and stats."""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a sequence of texts. Returns a parallel list.

        - For `texts = []` must return `[]`.
        - The length of the result must equal that of `texts`.
        - Each vector must have length `self.dim`.
        """

    def embed_one(self, text: str) -> list[float]:
        """Shortcut: embed a single text."""
        return self.embed([text])[0]


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Return the vector scaled so its L2 norm is 1.

    If the vector is all zeros (norm 0), returns it as-is.
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]
