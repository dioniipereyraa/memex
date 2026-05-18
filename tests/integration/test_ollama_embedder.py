"""Integration tests del `OllamaEmbedder`.

Estos tests hablan con un Ollama real corriendo en `OLLAMA_HOST`. Si Ollama
no está corriendo o el modelo `nomic-embed-text` no está descargado, se hace
skip (no falla la suite).

Para correr solo estos:
    uv run pytest tests/integration -m integration

Para correr todo MENOS estos:
    uv run pytest -m 'not integration'
"""

from __future__ import annotations

import math
import urllib.error
import urllib.request

import pytest

from memex.config import settings
from memex.core.embeddings.ollama import OllamaEmbedder


def _ollama_available() -> bool:
    """Devuelve True si Ollama responde en el host configurado."""
    try:
        with urllib.request.urlopen(f"{settings.ollama_host}/api/tags", timeout=2.0) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ollama_available(), reason="Ollama no responde en OLLAMA_HOST"),
]


@pytest.fixture
def embedder() -> OllamaEmbedder:
    return OllamaEmbedder()


class TestOllamaEmbedder:
    def test_model_name_default(self, embedder: OllamaEmbedder) -> None:
        assert embedder.model_name == settings.embed_model

    def test_embed_single_text(self, embedder: OllamaEmbedder) -> None:
        out = embedder.embed(["hola mundo"])
        assert len(out) == 1
        assert len(out[0]) == 768  # nomic-embed-text
        assert embedder.dim == 768

    def test_embed_batch(self, embedder: OllamaEmbedder) -> None:
        texts = ["uno", "dos", "tres", "cuatro"]
        out = embedder.embed(texts)
        assert len(out) == len(texts)
        for v in out:
            assert len(v) == 768

    def test_embed_empty_returns_empty(self, embedder: OllamaEmbedder) -> None:
        assert embedder.embed([]) == []

    def test_output_is_normalized(self, embedder: OllamaEmbedder) -> None:
        v = embedder.embed_one("texto de prueba")
        norm = math.sqrt(sum(x * x for x in v))
        assert math.isclose(norm, 1.0, rel_tol=1e-5)

    def test_deterministic_for_same_input(self, embedder: OllamaEmbedder) -> None:
        v1 = embedder.embed_one("repetido")
        v2 = embedder.embed_one("repetido")
        for a, b in zip(v1, v2, strict=True):
            assert math.isclose(a, b, abs_tol=1e-5)

    def test_similar_texts_are_closer_than_unrelated(self, embedder: OllamaEmbedder) -> None:
        """Sanity check: textos relacionados deberían tener vectores más cercanos."""
        vecs = embedder.embed(
            [
                "perro labrador marrón",
                "labrador chocolate jugando",
                "fórmulas matemáticas avanzadas",
            ]
        )
        a, b, c = vecs
        # Distancia coseno = 1 - dot product para vectores normalizados.
        d_ab = 1 - sum(x * y for x, y in zip(a, b, strict=True))
        d_ac = 1 - sum(x * y for x, y in zip(a, c, strict=True))
        assert d_ab < d_ac, (
            f"Esperaba que 'labrador' esté más cerca de 'labrador' que de "
            f"'matemáticas'. d_ab={d_ab:.4f}, d_ac={d_ac:.4f}"
        )
