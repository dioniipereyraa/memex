"""Embedder con `fastembed` (modelo ONNX embebido, sin daemon externo).

Es el backend default desde la opción 4 (zero-config). El modelo se baja
automáticamente la primera vez que se invoca embed (a `~/.cache/fastembed/`).
Para correr completamente offline después de la primera ejecución, no
necesita Internet.

Trade-off vs `OllamaEmbedder`:
- + No requiere instalar Ollama ni mantener un daemon corriendo.
- + Un único proceso Python.
- + Modelo cuantizado por default (130 MB vs 520 MB del full).
- - El modelo se baja la primera vez (~30s con buena conexión).
- - Embeddings no son bit-exactos a los de Ollama. Si cambiás de backend
    sobre una base ya indexada, conviene re-ingestar para que todos los
    vectores sean del mismo modelo.

Convención: vectores L2-normalizados antes de devolverse (alineado con
`OllamaEmbedder` y la convención del repo).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memex.config import settings
from memex.core.embeddings.base import Embedder, EmbedderError, l2_normalize

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5-Q"


class FastEmbedEmbedder(Embedder):
    """Embedder embebido vía `fastembed` (ONNX en proceso). Zero-config."""

    def __init__(
        self,
        model_name: str | None = None,
        normalize: bool = True,
    ) -> None:
        self._model_name = model_name or settings.embed_model or DEFAULT_MODEL
        self._normalize = normalize
        self._dim: int | None = None
        # Lazy import: fastembed arrastra numpy + onnxruntime (~30 MB), no
        # queremos pagar ese costo de import si el usuario eligió Ollama.
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise EmbedderError(
                    "fastembed no está instalado. Corré `uv sync` o cambiá "
                    "MEMEX_EMBED_BACKEND a 'ollama'."
                ) from e
            try:
                self._model = TextEmbedding(model_name=self._model_name)
            except Exception as e:
                raise EmbedderError(
                    f"No se pudo cargar el modelo {self._model_name!r}. "
                    "Verificá el nombre o tu conexión (la primera vez se baja). "
                    f"Detalle: {e}"
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
        # fastembed devuelve un generator de numpy arrays; los convertimos a list.
        try:
            results = list(model.embed(list(texts)))
        except Exception as e:
            raise EmbedderError(f"fastembed falló al embedear: {e}") from e

        embeddings: list[list[float]] = [vec.tolist() for vec in results]
        if self._normalize:
            embeddings = [l2_normalize(v) for v in embeddings]
        if embeddings and self._dim is None:
            self._dim = len(embeddings[0])
        return embeddings
