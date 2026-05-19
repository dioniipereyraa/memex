"""Factory + interfaces de embedding.

Punto único de entrada para obtener el embedder default según config.
Los call sites NO deben instanciar `OllamaEmbedder` o `FastEmbedEmbedder`
directamente; usá `get_default_embedder()` así el backend es configurable
via `MEMEX_EMBED_BACKEND` sin tocar código.
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
    """Devuelve el embedder configurado vía `MEMEX_EMBED_BACKEND`.

    - `fastembed` (default): embedding embebido vía ONNX. Zero-config.
    - `ollama`: cliente que habla con un Ollama corriendo local.

    Si el backend es inválido, levanta `EmbedderError` con mensaje accionable.
    """
    backend = (settings.embed_backend or "fastembed").strip().lower()

    if backend == "fastembed":
        # Import lazy: evita pagar el costo de cargar onnxruntime/numpy si el
        # usuario eligió Ollama.
        from memex.core.embeddings.fastembed_embedder import FastEmbedEmbedder

        return FastEmbedEmbedder()

    if backend == "ollama":
        from memex.core.embeddings.ollama import OllamaEmbedder

        return OllamaEmbedder()

    raise EmbedderError(
        f"MEMEX_EMBED_BACKEND inválido: {backend!r}. Valores válidos: 'fastembed', 'ollama'."
    )
