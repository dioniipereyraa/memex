"""Interfaz Summarizer abstracta + excepción común.

Un `Summarizer` recibe el texto canónico de una conversación (mensajes
concatenados con headers `[sender]\n`) y devuelve un resumen corto en
lenguaje natural. Mismo patrón que `Embedder`: el core depende de la
interfaz, las implementaciones concretas (Anthropic, fake) viven en
módulos hermanos. El factory en `__init__.py` elige según config.

Convenciones:
- Si el modelo subyacente falla (sin API key, rate limit, red caída),
  la implementación levanta `SummarizerError` con un mensaje accionable.
  El pipeline atrapa eso y sigue sin resumen (silent fail), no aborta el
  ingest del chat.
- El texto del summary no incluye comillas externas, prefijos tipo
  "Resumen:" ni markdown. Es texto plano listo para usar en
  `list_recent_chats` o como contexto extra de retrieval.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SummarizerError(Exception):
    """Error operativo de un Summarizer.

    Las implementaciones la levantan con un mensaje claro al usuario en
    lugar de propagar excepciones de bajo nivel (HTTPError, AuthError,
    timeouts). El pipeline la atrapa, logea un warning, y persiste el
    chat sin summary.
    """


class Summarizer(ABC):
    """Convierte el texto de una conversación en un resumen corto."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identificador del modelo. Usado en logging y, eventualmente, en
        metadata de la conversación para detectar cambios de modelo."""

    @abstractmethod
    def summarize(self, text: str, *, title: str | None = None) -> str:
        """Genera un resumen del texto de la conversación.

        - `text`: texto canónico de la conversación (mensajes concatenados).
        - `title`: título del chat (opcional, ayuda al modelo a anclar el tema).

        Devuelve el resumen como string. Si la implementación no puede
        producir un resumen (rate limit, sin key, error de red), levanta
        `SummarizerError` con un mensaje accionable.
        """
