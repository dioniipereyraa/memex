"""Parsers for the official Claude.ai export.

Four parsing functions, one per source inside the zip:
- `parse_project(data)`: dict from `projects/{uuid}.json` -> Project
- `parse_conversations_list(data)`: list from `conversations.json` -> [(Conversation, [Message])]
- `parse_design_chat(data)`: dict from `design_chats/{uuid}.json` -> (Conversation, [Message])
- `parse_memories(data, now)`: content of `memories.json` -> synthetic (Conversation, Message), or None

The caller handles IO (open the zip, parse JSON). Parsers receive dicts
and return pydantic models. Structural errors propagate as KeyError /
ValueError; the caller decides whether to log-and-continue or stop.

Schema observed in the real 2026-05-18 export (66 chats + 7 design_chats
+ memories.json + 2 projects, 900 messages). If future exports change
the shape, adjust here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.core.ingest.content_renderer import has_tool_use, render_content
from memex.core.models import Conversation, Message, Project, Sender, Source


def parse_project(data: dict[str, Any]) -> Project:
    """Parse a dict from `projects/{uuid}.json`."""
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
    """Parse `conversations.json` (top-level list)."""
    return [parse_conversation_dict(c, Source.CONVERSATIONS) for c in data]


def parse_design_chat(data: dict[str, Any]) -> tuple[Conversation, list[Message]]:
    """Parse a `design_chats/{uuid}.json` (a chat inside a project)."""
    return parse_conversation_dict(data, Source.DESIGN_CHAT)


def parse_memories(
    data: list[dict[str, Any]] | dict[str, Any],
    now: datetime | None = None,
) -> tuple[Conversation, Message] | None:
    """Parse `memories.json` and synthesize a conversation with a single message.

    Returns None if the memory is empty or malformed.

    `account_uuid` is used to generate stable uuids (re-ingest is
    idempotent). Timestamps are set to `now` (default
    `datetime.now(UTC)`) because the export does not bring them.
    Successive re-ingests preserve the original `created_at` thanks to
    the repo's upsert, only `updated_at` is overwritten.
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
        title="Curated Claude.ai memory",
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


# ---------- private helpers ----------


def parse_conversation_dict(
    data: dict[str, Any], source: Source
) -> tuple[Conversation, list[Message]]:
    """Parse a conversation dict into `(Conversation, [Message])`.

    Useful for any chat source that shares the Claude.ai shape: items
    in `conversations.json`, `design_chats/{uuid}.json`, or payloads
    captured live by the Chrome ext from the
    `chat_conversations/{id}?tree=True` endpoint.

    Differences between sources (observed in the real export):
    - conversations.json: `name` for title, `chat_messages` for messages, no project.
    - design_chats/*.json: `title` for title, `messages` for messages, with `project`.
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

    # Fallback to the legacy `text` field if rendering the content came up empty.
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
    # `updated_at` is not always populated in design_chats; use created_at as fallback.
    updated_raw = data.get("updated_at")
    updated_at = (
        _parse_dt(updated_raw) if isinstance(updated_raw, str) and updated_raw else created_at
    )

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
    """Parse the sender. Unknown values fall back to HUMAN (defensive)."""
    if isinstance(value, str):
        try:
            return Sender(value)
        except ValueError:
            pass
    return Sender.HUMAN


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 from the official export. Accepts a Z suffix."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _nonempty(s: Any) -> str | None:
    """Return `s` if it is a non-empty string, else None.

    Useful for mapping `description: ""` and `summary: ""` from the
    export to NULL in the DB.
    """
    if isinstance(s, str) and s:
        return s
    return None
