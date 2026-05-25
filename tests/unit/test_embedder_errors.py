"""Tests del manejo de errores en `OllamaEmbedder`.

Mockeamos el `Client.embed` del cliente Ollama para simular los modos de falla
sin depender de un servicio real.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import ollama
import pytest

from memex.core.embeddings.base import EmbedderError
from memex.core.embeddings.ollama import OllamaEmbedder


class _FakeResponseError(ollama.ResponseError):
    """ollama.ResponseError lleva un status_code que el cliente fija al detectar HTTP."""

    def __init__(self, error: str, status_code: int) -> None:
        super().__init__(error, status_code)


class TestOllamaEmbedderErrorHandling:
    def test_model_not_found_raises_friendly_error(self) -> None:
        embedder = OllamaEmbedder(model_name="modelo-inexistente", host="http://localhost:11434")
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = _FakeResponseError("model not found", 404)
            with pytest.raises(EmbedderError) as exc_info:
                embedder.embed(["hola"])
            msg = str(exc_info.value)
            assert "modelo-inexistente" in msg
            assert "ollama pull" in msg.lower()

    def test_connection_refused_raises_friendly_error(self) -> None:
        embedder = OllamaEmbedder(host="http://localhost:11435")
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = ConnectionError("Connection refused")
            with pytest.raises(EmbedderError) as exc_info:
                embedder.embed(["hola"])
            msg = str(exc_info.value)
            assert "localhost:11435" in msg
            assert "connect" in msg.lower() or "running" in msg.lower()

    def test_timeout_raises_friendly_error(self) -> None:
        embedder = OllamaEmbedder()
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = TimeoutError("Read timeout")
            with pytest.raises(EmbedderError):
                embedder.embed(["hola"])

    def test_other_response_error_wrapped(self) -> None:
        embedder = OllamaEmbedder()
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = _FakeResponseError("internal server error", 500)
            with pytest.raises(EmbedderError) as exc_info:
                embedder.embed(["hola"])
            assert "Ollama" in str(exc_info.value)

    def test_unknown_exception_propagates(self) -> None:
        """Errores no reconocidos pasan tal cual; no los envolvemos en EmbedderError."""
        embedder = OllamaEmbedder()
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = RuntimeError("algo raro")
            with pytest.raises(RuntimeError, match="algo raro"):
                embedder.embed(["hola"])

    def test_empty_list_short_circuits(self) -> None:
        """embed([]) no debe llamar al cliente (y por lo tanto no puede fallar)."""
        embedder = OllamaEmbedder()
        with patch.object(embedder._client, "embed") as mock_embed:
            mock_embed.side_effect = ConnectionError("would fail if called")
            result: list[list[float]] = embedder.embed([])
            assert result == []
            assert mock_embed.call_count == 0


class TestEmbedderErrorIsPublic:
    def test_can_be_imported_from_base(self) -> None:
        """EmbedderError vive en base.py para que cualquier consumidor lo atrape."""
        from memex.core.embeddings.base import EmbedderError as ImportedHere

        assert ImportedHere is EmbedderError

    def test_inherits_from_exception(self) -> None:
        assert issubclass(EmbedderError, Exception)


def _unused_marker(_: Any) -> None:
    """Placeholder para evitar warnings de imports en algunos entornos."""
