"""Modelos de dominio de Memex.

Pydantic v2. Reflejan el schema relacional (`storage/schema.sql`) pero son la
representación canónica usada por core. Los parsers de `ingest/` producen estos
modelos. El repo (`storage/repo.py`) los serializa a SQLite y los reconstruye al
leer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Source(StrEnum):
    """Origen de una conversación dentro del export de Claude.ai."""

    CONVERSATIONS = "conversations"
    DESIGN_CHAT = "design_chat"
    MEMORY = "memory"


class Sender(StrEnum):
    """Quién emitió un mensaje."""

    HUMAN = "human"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uuid: str
    name: str
    description: str | None = None
    prompt_template: str | None = None
    is_private: bool = True
    is_starter_project: bool = False
    creator_uuid: str | None = None
    creator_name: str | None = None
    created_at: datetime
    updated_at: datetime


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uuid: str
    title: str
    summary: str | None = None
    source: Source
    project_uuid: str | None = None
    account_uuid: str | None = None
    created_at: datetime
    updated_at: datetime
    # Hash SHA-256 (hex) del texto canónico de la conversación al momento del
    # último ingest. Lo usa el pipeline para decidir si re-generar derivados
    # caros (ej. summary con LLM) cuando se re-ingesta el mismo chat.
    content_hash: str | None = None


class Message(BaseModel):
    """Mensaje individual dentro de una conversación.

    `text` es el texto canónico ya renderizado (incluye tool markers cuando aplica).
    `raw_content` conserva el `content[]` original (lista de bloques) por si algún
    día queremos hacer análisis más fino sobre tool_use / tool_result.
    """

    model_config = ConfigDict(extra="forbid")

    uuid: str
    conversation_uuid: str
    parent_uuid: str | None = None
    sender: Sender
    text: str = ""
    raw_content: list[dict[str, Any]] | None = None
    has_tool_use: bool = False
    has_attachments: bool = False
    created_at: datetime
    updated_at: datetime


class Chunk(BaseModel):
    """Unidad de retrieval. Cubre ~500 tokens del texto de una conversación.

    `id` es nullable antes del INSERT (lo asigna el AUTOINCREMENT). El embedder
    no necesita el id, pero el repo lo necesita para asociar el vector.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    conversation_uuid: str
    message_uuid: str | None = None
    sender: str | None = None
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    created_at: datetime


class SearchHit(BaseModel):
    """Resultado de búsqueda enriquecido (chunk + datos del chat + score)."""

    model_config = ConfigDict(extra="forbid")

    chunk: Chunk
    conversation: Conversation
    distance: float
    snippet: str
