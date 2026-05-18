"""Tests unitarios de la capa de embeddings.

Cubre la interfaz `Embedder` (vía `FakeEmbedder` que es la implementación de
referencia para tests). Los tests que hablan con Ollama de verdad viven en
`tests/integration/test_ollama_embedder.py` y están marcados con
`@pytest.mark.integration`.
"""

from __future__ import annotations

import math

import pytest

from memex.core.embeddings.base import l2_normalize
from memex.core.embeddings.fake import FakeEmbedder


class TestL2Normalize:
    def test_unit_vector_stays_unit(self) -> None:
        vec = [1.0, 0.0, 0.0]
        assert l2_normalize(vec) == [1.0, 0.0, 0.0]

    def test_arbitrary_vector_becomes_unit(self) -> None:
        vec = [3.0, 4.0]
        out = l2_normalize(vec)
        assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)
        assert out == [0.6, 0.8]

    def test_zero_vector_unchanged(self) -> None:
        vec = [0.0, 0.0, 0.0]
        assert l2_normalize(vec) == [0.0, 0.0, 0.0]

    def test_negative_values(self) -> None:
        vec = [-3.0, 4.0]
        out = l2_normalize(vec)
        assert out == [-0.6, 0.8]


class TestFakeEmbedder:
    def test_dim_default(self) -> None:
        e = FakeEmbedder()
        assert e.dim == 768

    def test_dim_custom(self) -> None:
        e = FakeEmbedder(dim=128)
        assert e.dim == 128

    def test_invalid_dim_raises(self) -> None:
        with pytest.raises(ValueError):
            FakeEmbedder(dim=0)
        with pytest.raises(ValueError):
            FakeEmbedder(dim=-5)

    def test_model_name(self) -> None:
        e = FakeEmbedder(model_name="my-fake")
        assert e.model_name == "my-fake"

    def test_embed_returns_correct_dim(self) -> None:
        e = FakeEmbedder(dim=64)
        out = e.embed(["hola"])
        assert len(out) == 1
        assert len(out[0]) == 64

    def test_embed_batch_parallel(self) -> None:
        e = FakeEmbedder(dim=128)
        out = e.embed(["a", "b", "c"])
        assert len(out) == 3
        for v in out:
            assert len(v) == 128

    def test_embed_empty_list_returns_empty(self) -> None:
        e = FakeEmbedder()
        assert e.embed([]) == []

    def test_embed_is_deterministic(self) -> None:
        e = FakeEmbedder()
        v1 = e.embed_one("texto idéntico")
        v2 = e.embed_one("texto idéntico")
        assert v1 == v2

    def test_different_texts_different_vectors(self) -> None:
        e = FakeEmbedder()
        v1 = e.embed_one("uno")
        v2 = e.embed_one("dos")
        assert v1 != v2

    def test_embed_one_matches_first_of_batch(self) -> None:
        e = FakeEmbedder()
        assert e.embed_one("x") == e.embed(["x"])[0]

    def test_output_is_unit_vector(self) -> None:
        e = FakeEmbedder(dim=64)
        v = e.embed_one("texto cualquiera")
        norm = math.sqrt(sum(x * x for x in v))
        assert math.isclose(norm, 1.0, rel_tol=1e-9)
