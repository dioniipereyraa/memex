"""Tests for LazyEmbedder (defers model load until first real embed)."""

from __future__ import annotations

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.embeddings.lazy import LazyEmbedder


class TestLazyEmbedder:
    def test_not_loaded_until_embed(self):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return FakeEmbedder(dim=768)

        lazy = LazyEmbedder(factory)
        assert lazy.loaded is False
        assert calls["n"] == 0

        # Empty input must not trigger a load.
        assert lazy.embed([]) == []
        assert lazy.loaded is False
        assert calls["n"] == 0

        # A real embed loads the backend exactly once.
        out = lazy.embed(["hola"])
        assert len(out) == 1
        assert lazy.loaded is True
        assert calls["n"] == 1

        # Subsequent calls reuse the same instance.
        lazy.embed(["otra"])
        assert calls["n"] == 1

    def test_dim_and_model_name_forward(self):
        lazy = LazyEmbedder(lambda: FakeEmbedder(dim=768))
        assert lazy.dim == 768
        assert lazy.loaded is True  # accessing dim builds the backend
        assert isinstance(lazy.model_name, str)

    def test_embed_one(self):
        lazy = LazyEmbedder(lambda: FakeEmbedder(dim=768))
        vec = lazy.embed_one("texto")
        assert len(vec) == 768
