"""Parsers del export oficial de Claude.ai.

Cuatro funciones de parsing, una por fuente dentro del zip:
- `parse_project(data)`: dict de `projects/{uuid}.json` -> Project
- `parse_conversations_list(data)`: lista de `conversations.json` -> [(Conversation, [Message])]
- `parse_design_chat(data)`: dict de `design_chats/{uuid}.json` -> (Conversation, [Message])
- `parse_memories(data, now)`: contenido de `memories.json` -> (Conversation, Message) sintéticos, o None

El llamador maneja IO (abrir el zip, parsear JSON). Los parsers reciben dicts y
devuelven modelos pydantic. Errores estructurales se propagan como KeyError /
ValueError; el llamador decide si registrar y seguir o frenar todo.

Schema observado en el export real del 2026-05-18 (66 chats + 7 design_chats +
memories.json + 2 projects, 900 mensajes). Si futuros exports cambian el shape,
adaptar acá.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.core.ingest.content_renderer import has_tool_use, render_content
from memex.core.models import Conversation, Message, Project, Sender, Source


def parse_project(data: dict[str, Any]) -> Project:
    """Parsea un dict de `projects/{uuid}.json`."""
    creator = data.get("creator") or {}
    return Project(
        uuid=data["uuid"],
        name=data.get("name") or "",
        description=_nonempty(data.get("description")),
        prompt_template=_nonempty(data.get("prompt_template")),
        is_private=bool(data.get("is_private", True)),
        is_starter_project=bool(data.get("is_starter_project", False)),
        creator_uuid=creator.get("uuid"),
        creator_name=creator.get("full_name"),
        created_at=_parse_dt(data["created_at"]),
        updated_at=_parse_dt(data["updated_at"]),
    )


def parse_conversations_list(
    data: list[dict[str, Any]],
) -> list[tuple[Conversation, list[Message]]]:
    """Parsea `conversations.json` (lista al top-level)."""
    return [parse_conversation_dict(c, Source.CONVERSATIONS) for c in data]


def parse_design_chat(data: dict[str, Any]) -> tuple[Conversation, list[Message]]:
    """Parsea un `design_chats/{uuid}.json` (un chat dentro de un project)."""
    return parse_conversation_dict(data, Source.DESIGN_CHAT)


def parse_memories(
    data: list[dict[str, Any]] | dict[str, Any],
    now: datetime | None = None,
) -> tuple[Conversation, Message] | None:
    """Parsea `memories.json` y sintetiza una conversación con un solo mensaje.

    Devuelve None si la memoria está vacía o malformada.

    El `account_uuid` se usa para generar uuids estables (re-ingest es idempotente).
    Los timestamps se setean a `now` (por default `datetime.now(UTC)`) porque el
    export no los trae. Sucesivos re-ingests preservan `created_at` original gracias
    al upsert del repo, solo `updated_at` se sobreescribe.
    """
    if now is None:
        now = datetime.now(UTC)

    if isinstance(data, list):
        if not data:
            return None
        item = data[0]
    elif isinstance(data, dict):
        item = data
    else:
        return None
    if not isinstance(item, dict):
        return None

    memory_text = item.get("conversations_memory")
    if not isinstance(memory_text, str) or not memory_text:
        return None

    account_uuid = item.get("account_uuid")
    conv_uuid = f"memory-{account_uuid}" if account_uuid else "memory-default"
    msg_uuid = f"{conv_uuid}-msg"

    conv = Conversation(
        uuid=conv_uuid,
        title="Memoria curada de Claude.ai",
        summary=None,
        source=Source.MEMORY,
        project_uuid=None,
        account_uuid=account_uuid if isinstance(account_uuid, str) else None,
        created_at=now,
        updated_at=now,
    )
    msg = Message(
        uuid=msg_uuid,
        conversation_uuid=conv_uuid,
        parent_uuid=None,
        sender=Sender.ASSISTANT,
        text=memory_text,
        raw_content=None,
        has_tool_use=False,
        has_attachments=False,
        created_at=now,
        updated_at=now,
    )
    return conv, msg


# ---------- helpers privados ----------

def parse_conversation_dict(
    data: dict[str, Any], source: Source
) -> tuple[Conversation, list[Message]]:
    """Parsea un dict de conversación a `(Conversation, [Message])`.

    Útil para cualquier origen de chats que comparta el shape de Claude.ai:
    items de `conversations.json`, `design_chats/{uuid}.json`, o payloads
    capturados en vivo por la Chrome ext desde el endpoint
    `chat_conversations/{id}?tree=True`.

    Diferencias entre fuentes (vistas en el export real):
    - conversations.json: `name` para título, `chat_messages` para mensajes, sin project.
    - design_chats/*.json: `title` para título, `messages` para mensajes, con `project`.
    """
    if source is Source.CONVERSATIONS:
        title = data.get("name") or ""
        messages_raw = data.get("chat_messages") or []
        project_uuid: str | None = None
    else:
        title = data.get("title") or ""
        messages_raw = data.get("messages") or []
        project = data.get("project") or {}
        project_uuid = project.get("uuid")

    account = data.get("account") or {}

    conv = Conversation(
        uuid=data["uuid"],
        title=title,
        summary=_nonempty(data.get("summary")),
        source=source,
        project_uuid=project_uuid,
        account_uuid=account.get("uuid"),
        created_at=_parse_dt(data["created_at"]),
        updated_at=_parse_dt(data["updated_at"]),
    )

    messages: list[Message] = []
    for m in messages_raw:
        if isinstance(m, dict):
            messages.append(_parse_message_dict(m, conv.uuid))
    return conv, messages


def _parse_message_dict(data: dict[str, Any], conversation_uuid: str) -> Message:
    raw_content = data.get("content")
    if isinstance(raw_content, list):
        rendered = render_content(raw_content)
        tool_use_flag = has_tool_use(raw_content)
        raw_for_storage: list[dict[str, Any]] | None = [
            b for b in raw_content if isinstance(b, dict)
        ]
    else:
        rendered = ""
        tool_use_flag = False
        raw_for_storage = None

    # Fallback al campo `text` legacy si el rendering del content vino vacío.
    if not rendered:
        legacy = data.get("text")
        if isinstance(legacy, str):
            rendered = legacy

    sender = _parse_sender(data.get("sender"))

    attachments = data.get("attachments") or []
    files = data.get("files") or []
    has_atts = (isinstance(attachments, list) and bool(attachments)) or (
        isinstance(files, list) and bool(files)
    )

    created_at = _parse_dt(data["created_at"])
    # `updated_at` no siempre viene poblado en design_chats; usamos created_at como fallback.
    updated_raw = data.get("updated_at")
    updated_at = _parse_dt(updated_raw) if isinstance(updated_raw, str) and updated_raw else created_at

    return Message(
        uuid=data["uuid"],
        conversation_uuid=conversation_uuid,
        parent_uuid=_nonempty(data.get("parent_message_uuid")),
        sender=sender,
        text=rendered,
        raw_content=raw_for_storage,
        has_tool_use=tool_use_flag,
        has_attachments=has_atts,
        created_at=created_at,
        updated_at=updated_at,
    )


def _parse_sender(value: Any) -> Sender:
    """Parsea el sender. Valores desconocidos caen a HUMAN (defensivo)."""
    if isinstance(value, str):
        try:
            return Sender(value)
        except ValueError:
            pass
    return Sender.HUMAN


def _parse_dt(s: str) -> datetime:
    """Parsea ISO 8601 del export oficial. Acepta sufijo Z."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _nonempty(s: Any) -> str | None:
    """Devuelve `s` si es string no vacío, sino None.

    Útil para mapear `description: ""` y `summary: ""` del export a NULL en la DB.
    """
    if isinstance(s, str) and s:
        return s
    return None
