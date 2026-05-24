"""Summarizer real usando Claude Haiku via Anthropic API.

Import lazy del SDK (extra opcional `summaries`). Si el SDK no está
instalado, o falta `ANTHROPIC_API_KEY`, o la API falla, levanta
`SummarizerError` con un mensaje accionable. El pipeline lo atrapa y
sigue sin summary (silent fail).

El nombre del archivo es `anthropic_summarizer.py` (no `anthropic.py`)
para evitar el shadowing del paquete `anthropic` del SDK al hacer
`from anthropic import Anthropic` desde adentro.
"""

from __future__ import annotations

from typing import Any

from memex.config import settings
from memex.core.summaries.base import Summarizer, SummarizerError

SYSTEM_PROMPT = (
    "Sos un asistente que genera resúmenes muy cortos de conversaciones técnicas. "
    "Tu salida es texto plano, una a tres oraciones, sin markdown ni listas, "
    "sin prefijos como 'Resumen:'. El objetivo es que un buscador pueda decidir "
    "rápido si el chat es relevante. Foco en: qué problema o tema concreto se "
    "trata, qué decisiones o conclusiones aparecen. Sin alabanzas ni meta-"
    "comentarios."
)

USER_TEMPLATE_WITH_TITLE = (
    "Título del chat: {title}\n\nContenido (mensajes concatenados):\n{body}\n\nGenerá el resumen."
)

USER_TEMPLATE_NO_TITLE = "Contenido del chat (mensajes concatenados):\n{body}\n\nGenerá el resumen."

# Tope conservador para el input. El modelo soporta mucho más, pero queremos
# acotar costo en chats largos: los primeros ~12k chars suelen tener la idea
# central, y el resumen no necesita el cierre exacto.
MAX_INPUT_CHARS = 12_000


class AnthropicSummarizer(Summarizer):
    """Cliente del SDK oficial de Anthropic para generar resúmenes.

    Levantamos `SummarizerError` en lugar de propagar errores del SDK
    (auth, rate limit, red) para que el pipeline tenga un solo punto de
    fallo manejado.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._model = model or settings.summary_model
        self._max_tokens = max_tokens or settings.summary_max_tokens
        self._client: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise SummarizerError(
                "ANTHROPIC_API_KEY no está seteada. Exportá la key o "
                "desactivá MEMEX_SUMMARY_ENABLED."
            )
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise SummarizerError(
                "El SDK `anthropic` no está instalado. Instalá el extra: "
                "`uv sync --extra summaries`."
            ) from e
        try:
            self._client = Anthropic(api_key=self._api_key)
        except Exception as e:
            raise SummarizerError(f"No se pudo crear el cliente Anthropic: {e}") from e
        return self._client

    def summarize(self, text: str, *, title: str | None = None) -> str:
        if not text or not text.strip():
            raise SummarizerError("Texto vacío, no hay nada que resumir.")
        client = self._ensure_client()
        body = text[:MAX_INPUT_CHARS]
        if title:
            user_msg = USER_TEMPLATE_WITH_TITLE.format(title=title, body=body)
        else:
            user_msg = USER_TEMPLATE_NO_TITLE.format(body=body)

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as e:
            raise SummarizerError(f"Anthropic API falló: {e}") from e

        # `response.content` es una lista de bloques. Para summary esperamos
        # un único bloque de texto; concatenamos por las dudas (defensive).
        parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            text_attr = getattr(block, "text", None)
            if isinstance(text_attr, str):
                parts.append(text_attr)
        result = "".join(parts).strip()
        if not result:
            raise SummarizerError("Anthropic devolvió respuesta sin texto.")
        return result
