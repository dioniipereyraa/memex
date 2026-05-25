"""Embedder backed by a local Ollama.

Uses the official Ollama Python client. The model is picked via the
`MEMEX_EMBED_MODEL` env var (default `nomic-embed-text`). The host via
`OLLAMA_HOST` (default `http://localhost:11434`).

Convention: vectors are L2-normalized before being returned so that
sqlite-vec L2 distance matches cosine ranking. Can be disabled by
passing `normalize=False` to the constructor.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import ollama

from memex.config import settings
from memex.core.embeddings.base import Embedder, EmbedderError, l2_normalize

# httpx network errors that the ollama client propagates unwrapped. Explicit
# catch before the substring fallback; less fragile than guessing by wording
# (which changes between versions and locales).
_HTTPX_CONNECT_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

# Substring fallback if we get an error that is not a known httpx.* type.
_CONNECTION_HINT_KEYWORDS = ("connect", "refused", "timeout", "resolve", "unreachable")

DEFAULT_MODEL = "nomic-embed-text"


class OllamaEmbedder(Embedder):
    """Embedder that talks to a local Ollama (or remote via host)."""

    def __init__(
        self,
        model_name: str | None = None,
        host: str | None = None,
        normalize: bool = True,
        timeout: float = 120.0,
    ) -> None:
        self._model_name = model_name or settings.embed_model or DEFAULT_MODEL
        self._host = host or settings.ollama_host
        self._client = ollama.Client(host=self._host, timeout=timeout)
        self._normalize = normalize
        # Real `dim` is detected on the first embed call. If nobody has
        # called embed yet, we return the configured value.
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        return self._dim if self._dim is not None else settings.embed_dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embed(model=self._model_name, input=list(texts))
        except ollama.ResponseError as e:
            status = getattr(e, "status_code", None)
            if status == 404:
                raise EmbedderError(
                    f"Model '{self._model_name}' not found in Ollama. "
                    f"Run `ollama pull {self._model_name}` and try again."
                ) from e
            raise EmbedderError(f"Ollama returned an error: {e}") from e
        except _HTTPX_CONNECT_EXCEPTIONS as e:
            raise EmbedderError(
                f"Could not connect to Ollama at {self._host}. Is the service running?"
            ) from e
        except Exception as e:
            # Fallback: any other exception whose message hints at a
            # connection / timeout problem (some wrapper not in
            # _HTTPX_CONNECT_EXCEPTIONS).
            msg = str(e).lower()
            if any(k in msg for k in _CONNECTION_HINT_KEYWORDS):
                raise EmbedderError(
                    f"Could not connect to Ollama at {self._host}. Is the service running?"
                ) from e
            raise

        embeddings: list[list[float]] = [list(v) for v in response.embeddings]
        if self._normalize:
            embeddings = [l2_normalize(v) for v in embeddings]
        # Cache real dim for future self.dim calls.
        if embeddings and self._dim is None:
            self._dim = len(embeddings[0])
        return embeddings
