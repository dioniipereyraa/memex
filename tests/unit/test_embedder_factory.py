"""Tests de `get_default_embedder()` (factory).

No instanciamos el embedder real (eso lo cubren tests de integración). Solo
chequeamos que el factory devuelva la clase correcta según `embed_backend`
y maneje backends inválidos.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memex.core.embeddings import EmbedderError, get_default_embedder
from memex.core.embeddings.fastembed_embedder import FastEmbedEmbedder
from memex.core.embeddings.ollama import OllamaEmbedder


class TestFactoryBackends:
    @patch("memex.core.embeddings.settings")
    def test_default_backend_is_fastembed(self, mock_settings) -> None:
        mock_settings.embed_backend = "fastembed"
        mock_settings.embed_model = None
        emb = get_default_embedder()
        assert isinstance(emb, FastEmbedEmbedder)

    @patch("memex.core.embeddings.settings")
    def test_ollama_backend(self, mock_settings) -> None:
        mock_settings.embed_backend = "ollama"
        mock_settings.embed_model = None
        mock_settings.ollama_host = "http://localhost:11434"
        emb = get_default_embedder()
        assert isinstance(emb, OllamaEmbedder)

    @patch("memex.core.embeddings.settings")
    def test_backend_case_insensitive(self, mock_settings) -> None:
        mock_settings.embed_backend = "FastEmbed"
        mock_settings.embed_model = None
        emb = get_default_embedder()
        assert isinstance(emb, FastEmbedEmbedder)

    @patch("memex.core.embeddings.settings")
    def test_backend_with_whitespace(self, mock_settings) -> None:
        mock_settings.embed_backend = "  fastembed  "
        mock_settings.embed_model = None
        emb = get_default_embedder()
        assert isinstance(emb, FastEmbedEmbedder)

    @patch("memex.core.embeddings.settings")
    def test_invalid_backend_raises(self, mock_settings) -> None:
        mock_settings.embed_backend = "openai"
        mock_settings.embed_model = None
        with pytest.raises(EmbedderError) as exc_info:
            get_default_embedder()
        assert "openai" in str(exc_info.value)
        assert "fastembed" in str(exc_info.value)
        assert "ollama" in str(exc_info.value)

    @patch("memex.core.embeddings.settings")
    def test_empty_backend_defaults_to_fastembed(self, mock_settings) -> None:
        mock_settings.embed_backend = ""
        mock_settings.embed_model = None
        emb = get_default_embedder()
        assert isinstance(emb, FastEmbedEmbedder)


class TestFastEmbedEmbedderConstruction:
    """Tests de construcción sin tocar el modelo real (lazy load)."""

    @patch("memex.core.embeddings.fastembed_embedder.settings")
    def test_default_model_when_settings_empty(self, mock_settings) -> None:
        mock_settings.embed_model = None
        mock_settings.embed_dim = 768
        emb = FastEmbedEmbedder()
        assert emb.model_name == "nomic-ai/nomic-embed-text-v1.5-Q"

    def test_explicit_model_overrides_settings(self) -> None:
        emb = FastEmbedEmbedder(model_name="nomic-ai/nomic-embed-text-v1.5")
        assert emb.model_name == "nomic-ai/nomic-embed-text-v1.5"

    def test_empty_input_short_circuits(self) -> None:
        """embed([]) no debería tocar el modelo (que requiere descarga)."""
        emb = FastEmbedEmbedder(model_name="modelo-que-no-existe")
        assert emb.embed([]) == []

    def test_dim_fallback_before_first_embed(self) -> None:
        emb = FastEmbedEmbedder()
        # Antes del primer embed real, dim viene de settings.embed_dim (default 768).
        assert emb.dim == 768
