"""Deterministic embedder for tests.

`FakeEmbedder` produces vectors derived from the text hash. The same
text always returns the same vector. There is NO semantic relationship
to the content (similar texts do NOT produce similar vectors), but it
is enough for:

- Tests that exercise the ingest pipeline without talking to Ollama.
- Repo tests that need valid but unrealistic embeddings.
- Fast smoke tests without depending on an external service.

DO NOT use in production. L2 distance between vectors generated here
has no semantic meaning, so retrieval will be garbage.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

from memex.core.embeddings.base import Embedder, l2_normalize


class FakeEmbedder(Embedder):
    """Deterministic embedder based on SHA-256 hash of the text.

    Each text is hashed, the bytes are interpreted as floats, and the
    vector is sliced or padded to `dim`. Then L2-normalized.
    """

    def __init__(self, dim: int = 768, model_name: str = "fake") -> None:
        if dim <= 0:
            raise ValueError("dim must be > 0")
        self._dim = dim
        self._model_name = model_name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._fake_vector(t) for t in texts]

    def _fake_vector(self, text: str) -> list[float]:
        # Generate enough bytes: each float takes 4 bytes, we need `dim`
        # floats. SHA-256 gives 32 bytes. Iterate hashes with numeric
        # suffixes until we fill the buffer.
        needed_bytes = self._dim * 4
        buf = bytearray()
        counter = 0
        while len(buf) < needed_bytes:
            h = hashlib.sha256(f"{text}|{counter}".encode()).digest()
            buf.extend(h)
            counter += 1
        # Convert bytes to floats in [-1, 1].
        floats: list[float] = []
        for i in range(self._dim):
            # struct.unpack returns int32; we normalize to [-1, 1].
            (raw,) = struct.unpack_from(">i", buf, i * 4)
            floats.append(raw / 2_147_483_647.0)
        return l2_normalize(floats)
