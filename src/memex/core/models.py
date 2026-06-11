"""Memex domain models.

Pydantic v2. They mirror the relational schema (`storage/schema.sql`) but
are the canonical representation used by core. The `ingest/` parsers
produce these models. The repo (`storage/repo.py`) serializes them to
SQLite and reconstructs them on read.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Source(StrEnum):
    """Origin of a conversation.

    The first three come from the Claude.ai export (or live capture).
    `CLAUDE_CODE` comes from local Claude Code / terminal session logs
    (`~/.claude/projects/**/*.jsonl`), unifying both halves of the user's
    history into one searchable store.
    """

    CONVERSATIONS = "conversations"
    DESIGN_CHAT = "design_chat"
    MEMORY = "memory"
    CLAUDE_CODE = "claude_code"


class Sender(StrEnum):
    """Who emitted a message."""

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
    # SHA-256 hash (hex) of the conversation's canonical text at the
    # last ingest. The pipeline uses it to decide whether to regenerate
    # expensive derivatives (e.g. LLM summary) when the same chat is
    # re-ingested.
    content_hash: str | None = None


class Message(BaseModel):
    """A single message inside a conversation.

    `text` is the canonical already-rendered text (includes tool markers
    when applicable). `raw_content` keeps the original `content[]` (list
    of blocks) in case we want finer analysis of tool_use / tool_result
    one day.
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
    """Retrieval unit. Covers ~500 tokens of a conversation's text.

    `id` is nullable before the INSERT (assigned by AUTOINCREMENT). The
    embedder does not need the id, but the repo needs it to associate
    the vector.
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
    """Enriched search result (chunk + chat metadata + score)."""

    model_config = ConfigDict(extra="forbid")

    chunk: Chunk
    conversation: Conversation
    distance: float
    snippet: str
