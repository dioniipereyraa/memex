"""Embedder con Ollama local.

Usa el cliente Python oficial de Ollama. El modelo se elige por env var
`MEMEX_EMBED_MODEL` (default `nomic-embed-text`). El host por `OLLAMA_HOST`
(default `http://localhost:11434`).

Convención: los vectores se L2-normalizan antes de devolverse para que la
distancia L2 de sqlite-vec coincida con el ranking por coseno. Se puede
desactivar pasando `normalize=False` al constructor.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import ollama

from memex.config import settings
from memex.core.embeddings.base import Embedder, EmbedderError, l2_normalize

# Errores de red de httpx que el cliente ollama propaga sin envolver.
# Catch explícito antes del fallback por substring; menos frágil que adivinar
# por wording (que cambia entre versiones y locales).
_HTTPX_CONNECT_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

# Fallback por substring si llega un error que no es ningún httpx.* conocido.
_CONNECTION_HINT_KEYWORDS = ("connect", "refused", "timeout", "resolve", "unreachable")

DEFAULT_MODEL = "nomic-embed-text"


class OllamaEmbedder(Embedder):
    """Embedder que habla con un Ollama corriendo local (o remoto via host)."""

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
        # `dim` real se detecta al primer embed. Si nadie llamó embed todavía,
        # devolvemos el valor configurado.
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
                    f"Modelo '{self._model_name}' no encontrado en Ollama. "
                    f"Corré `ollama pull {self._model_name}` y reintentá."
                ) from e
            raise EmbedderError(f"Ollama devolvió error: {e}") from e
        except _HTTPX_CONNECT_EXCEPTIONS as e:
            raise EmbedderError(
                f"No se pudo conectar a Ollama en {self._host}. "
                f"¿Está corriendo el servicio?"
            ) from e
        except Exception as e:
            # Fallback: cualquier otra excepción cuyo mensaje sugiera conexión
            # / timeout (algún wrapper que no esté en _HTTPX_CONNECT_EXCEPTIONS).
            msg = str(e).lower()
            if any(k in msg for k in _CONNECTION_HINT_KEYWORDS):
                raise EmbedderError(
                    f"No se pudo conectar a Ollama en {self._host}. "
                    f"¿Está corriendo el servicio?"
                ) from e
            raise

        embeddings: list[list[float]] = [list(v) for v in response.embeddings]
        if self._normalize:
            embeddings = [l2_normalize(v) for v in embeddings]
        # Guardar dim real para futuras llamadas a self.dim.
        if embeddings and self._dim is None:
            self._dim = len(embeddings[0])
        return embeddings
