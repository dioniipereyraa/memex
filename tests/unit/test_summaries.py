"""Tests del módulo `core/summaries/`.

Cubre:
- `FakeSummarizer`: determinístico, modo fail.
- Factory `get_default_summarizer()`: respeta `MEMEX_SUMMARY_ENABLED`.
- `AnthropicSummarizer`: errores accionables sin API key, sin SDK (import lazy).

NO testea la API real de Anthropic (eso sería integration test). El client se
ejerce vía monkeypatch o vía verificación de error paths.
"""

from __future__ import annotations

import pytest

from memex.core.summaries import get_default_summarizer
from memex.core.summaries.anthropic_summarizer import AnthropicSummarizer
from memex.core.summaries.base import SummarizerError
from memex.core.summaries.fake import FakeSummarizer


class TestFakeSummarizer:
    def test_returns_first_words(self) -> None:
        s = FakeSummarizer(max_words=3)
        out = s.summarize("uno dos tres cuatro cinco")
        assert out == "uno dos tres"

    def test_uses_title_as_prefix(self) -> None:
        s = FakeSummarizer(max_words=2)
        out = s.summarize("hola mundo extra", title="Saludo")
        assert out.startswith("Saludo:")
        assert "hola mundo" in out

    def test_deterministic_same_input(self) -> None:
        s1 = FakeSummarizer(max_words=5)
        s2 = FakeSummarizer(max_words=5)
        assert s1.summarize("texto repetible") == s2.summarize("texto repetible")

    def test_fail_mode_raises(self) -> None:
        s = FakeSummarizer(fail=True)
        with pytest.raises(SummarizerError):
            s.summarize("no importa")

    def test_counts_calls(self) -> None:
        s = FakeSummarizer()
        s.summarize("a b c")
        s.summarize("d e f")
        assert s.calls == 2

    def test_model_name(self) -> None:
        s = FakeSummarizer(model_name="custom-fake")
        assert s.model_name == "custom-fake"


class TestFactory:
    def test_returns_none_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Forzar OFF aunque el .env del repo tenga otro valor.
        from memex.config import settings

        monkeypatch.setattr(settings, "summary_enabled", False)
        assert get_default_summarizer() is None

    def test_returns_anthropic_when_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memex.config import settings

        monkeypatch.setattr(settings, "summary_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-not-real")
        result = get_default_summarizer()
        assert isinstance(result, AnthropicSummarizer)


class TestAnthropicSummarizer:
    def test_empty_text_raises(self) -> None:
        s = AnthropicSummarizer(api_key="sk-fake")
        with pytest.raises(SummarizerError, match="vacío"):
            s.summarize("")

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Si no se pasa api_key y settings tampoco la tiene, debería avisar.
        from memex.config import settings

        monkeypatch.setattr(settings, "anthropic_api_key", None)
        s = AnthropicSummarizer(api_key=None)
        with pytest.raises(SummarizerError, match="ANTHROPIC_API_KEY"):
            s.summarize("hay texto")

    def test_truncates_long_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifica que el user_msg incluye texto truncado al límite, sin enviar
        chats gigantes a la API. Reemplaza el cliente con un stub que captura el
        body del mensaje y lo expone para inspección.
        """
        from memex.core.summaries import anthropic_summarizer as mod

        captured: dict[str, object] = {}

        class _StubMessages:
            def create(self, **kwargs: object) -> object:
                captured.update(kwargs)
                # Respuesta mínima válida.
                return type(
                    "R",
                    (),
                    {"content": [type("B", (), {"text": "resumen falso"})()]},
                )()

        class _StubClient:
            def __init__(self) -> None:
                self.messages = _StubMessages()

        s = AnthropicSummarizer(api_key="sk-fake")
        # Inyectar el stub saltándonos _ensure_client.
        s._client = _StubClient()

        # Usamos un caracter que no aparece en el template (`SYSTEM_PROMPT`,
        # `USER_TEMPLATE_WITH_TITLE`) ni en el title de abajo, para contar
        # solo cuántos chars del body llegan al payload final.
        marker = "Ω"
        long_text = marker * (mod.MAX_INPUT_CHARS + 5_000)
        out = s.summarize(long_text, title="Test")
        assert out == "resumen falso"
        sent_messages = captured["messages"]
        assert isinstance(sent_messages, list)
        user_content = sent_messages[0]["content"]
        body_chars = user_content.count(marker)
        assert body_chars == mod.MAX_INPUT_CHARS

    def test_empty_response_raises(self) -> None:
        """Si la API devuelve content vacío, levantamos error explícito."""

        class _EmptyMessages:
            def create(self, **kwargs: object) -> object:
                return type("R", (), {"content": []})()

        class _EmptyClient:
            def __init__(self) -> None:
                self.messages = _EmptyMessages()

        s = AnthropicSummarizer(api_key="sk-fake")
        s._client = _EmptyClient()
        with pytest.raises(SummarizerError, match="sin texto"):
            s.summarize("hay texto", title="T")

    def test_model_name_uses_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = AnthropicSummarizer(api_key="sk-fake", model="claude-test-model")
        assert s.model_name == "claude-test-model"
