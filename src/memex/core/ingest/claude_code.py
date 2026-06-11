"""Parser for local Claude Code / terminal session logs.

Claude Code (in VS Code or the terminal) records each session as a JSONL
file at `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. One line per
event; the relevant ones are `type=user` and `type=assistant`. Every line
also carries `cwd`, `sessionId`, `timestamp`, and `gitBranch`.

`parse_session_file(path)` turns one such file into a
`ParsedSession(conversation, messages, cwd)`:

- The conversation `uuid` is the `sessionId`; the title comes from the
  `ai-title` line (`aiTitle`) when present, else it is derived from the
  first real user prompt, else from the working directory.
- `created_at` / `updated_at` are the first and last event timestamps.
- `cwd` is returned so the pipeline can associate the session with a
  registered repo (a much stronger signal than text matching).

What is intentionally dropped (per the user's choices for this feature):
- `thinking` blocks (handled for free: `render_content` ignores unknown
  block types).
- Sub-agent side threads (`isSidechain=true`): noise from parallel agents.
- Harness-injected meta lines (`isMeta=true`) and local-command plumbing
  (slash-command stdout, bash I/O wrappers) that are not real dialogue.

Tool calls survive as the same `[tool_use: ...]` / `[result]` markers the
Claude.ai export uses, via the shared `content_renderer`.

A malformed line is skipped, not fatal: a single corrupt event must not
lose a whole session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from memex.core.ingest.content_renderer import has_tool_use, render_content
from memex.core.ingest.redact import redact_secrets
from memex.core.models import Conversation, Message, Sender, Source

logger = logging.getLogger(__name__)

# Substrings that mark a `type=user` line as harness plumbing rather than a
# real user turn (slash-command echoes, bash wrappers, hook output). These are
# injected into the transcript by the CLI, not typed by the user.
_HARNESS_USER_MARKERS = (
    "<local-command-",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
)

# Title derived from the first user prompt is capped to keep it readable.
_DERIVED_TITLE_MAX_CHARS = 80

# Deterministic, timezone-aware fallback timestamp for the rare session with no
# parseable timestamps. Aware (UTC) so it never mixes with the naive epoch.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ParsedSession(NamedTuple):
    conversation: Conversation
    messages: list[Message]
    cwd: str | None


def parse_session_file(path: Path | str) -> ParsedSession | None:
    """Parse one Claude Code session `.jsonl` into a `ParsedSession`.

    Returns `None` if the file has no usable user/assistant messages (an
    empty or pure-metadata session), so the caller can skip it.
    """
    p = Path(path)
    session_id = p.stem  # filename is "<sessionId>.jsonl"

    messages: list[Message] = []
    ai_title: str | None = None
    cwd: str | None = None
    first_dt: datetime | None = None
    last_dt: datetime | None = None
    first_user_text: str | None = None

    for raw_line in _iter_lines(p):
        obj = _load_line(raw_line)
        if obj is None:
            continue

        if cwd is None and isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]

        ltype = obj.get("type")
        if ltype == "ai-title":
            title_val = obj.get("aiTitle")
            if isinstance(title_val, str) and title_val.strip():
                ai_title = title_val.strip()
            continue

        if ltype not in ("user", "assistant"):
            continue
        # Drop harness-injected and sub-agent lines.
        if obj.get("isMeta") or obj.get("isSidechain"):
            continue

        dt = _parse_dt(obj.get("timestamp"))
        msg = _build_message(obj, session_id, ltype)
        if msg is None:
            continue

        # min/max rather than first/last seen, in case lines are ever
        # out of order (sessions are append-only, but be robust).
        if dt is not None:
            first_dt = dt if first_dt is None else min(first_dt, dt)
            last_dt = dt if last_dt is None else max(last_dt, dt)
        # Pick the first genuine prompt for the title fallback: skip a leading
        # tool_result user line (post-/resume sessions can open with one), so
        # the derived title is real text, never "[result] ...".
        if (
            first_user_text is None
            and msg.sender is Sender.HUMAN
            and msg.text
            and not msg.has_tool_use
        ):
            first_user_text = msg.text
        messages.append(msg)

    if not messages:
        return None

    created = first_dt or _EPOCH
    updated = last_dt or created
    title = ai_title or _derive_title(first_user_text, cwd)

    conv = Conversation(
        uuid=session_id,
        title=title,
        summary=None,
        source=Source.CLAUDE_CODE,
        project_uuid=None,
        account_uuid=None,
        created_at=created,
        updated_at=updated,
    )
    return ParsedSession(conversation=conv, messages=messages, cwd=cwd)


# ---------- private helpers ----------


def _iter_lines(path: Path) -> Iterator[str]:
    """Yield raw lines, tolerating a missing/unreadable file."""
    try:
        with path.open("r", encoding="utf-8") as f:
            yield from f
    except OSError as e:
        logger.warning("Could not read session file %s: %s", path, e)


def _load_line(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _build_message(obj: dict[str, Any], session_id: str, ltype: str) -> Message | None:
    """Build a Message from a user/assistant line, or None to skip it."""
    inner = obj.get("message")
    if not isinstance(inner, dict):
        return None
    content = inner.get("content")

    if isinstance(content, str):
        text = content
        # Skip harness plumbing dressed up as a user turn.
        if _is_harness_noise(text):
            return None
        tool_flag = False
    elif isinstance(content, list):
        text = render_content(content)
        tool_flag = has_tool_use(content)
    else:
        return None

    if not text.strip():
        return None

    # Mask credentials that leak into terminal output / file contents before
    # they are stored or embedded. `raw_content` is deliberately not persisted
    # for this source: the rendered+redacted `text` is the source of truth, and
    # keeping the raw blocks would store unredacted secrets in the DB.
    text = redact_secrets(text)

    sender = Sender.HUMAN if ltype == "user" else Sender.ASSISTANT
    created = _parse_dt(obj.get("timestamp")) or _EPOCH
    uuid = obj.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None

    return Message(
        uuid=uuid,
        conversation_uuid=session_id,
        parent_uuid=obj.get("parentUuid") if isinstance(obj.get("parentUuid"), str) else None,
        sender=sender,
        text=text,
        raw_content=None,
        has_tool_use=tool_flag,
        has_attachments=False,
        created_at=created,
        updated_at=created,
    )


def _is_harness_noise(text: str) -> bool:
    head = text.lstrip()[:200]
    return any(marker in head for marker in _HARNESS_USER_MARKERS)


def _derive_title(first_user_text: str | None, cwd: str | None) -> str:
    """Title fallback when the session has no `ai-title` line."""
    if first_user_text:
        flat = " ".join(first_user_text.split())
        if len(flat) > _DERIVED_TITLE_MAX_CHARS:
            flat = flat[:_DERIVED_TITLE_MAX_CHARS].rstrip() + "..."
        if flat:
            return flat
    if cwd:
        return f"Claude Code session in {Path(cwd).name}"
    return "Claude Code session"


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp (with optional Z), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
