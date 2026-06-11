"""Render a Claude.ai message `content[]` to plain text.

Supported block types (seen in the real export):
- `type=text`: direct text.
- `type=tool_use`: marker `[tool_use: <name>] <input>`. Input is
  serialized to JSON, truncated to `MAX_TOOL_INPUT_CHARS`.
- `type=tool_result`: marker `[result] <text>` (or `[result error]` if
  `is_error=true`). Text truncated to `MAX_TOOL_RESULT_CHARS`.

Blocks with an unknown `type` are silently ignored. This leaves the door
open for new types in future exports without breaking ingest.

The resulting text is what goes to `messages.text` and to the chunks
for retrieval. The original `content[]` is kept in `messages.raw_content`
in case finer parsing on tool_use / tool_result is needed later.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

MAX_TOOL_INPUT_CHARS = 500
MAX_TOOL_RESULT_CHARS = 1000

# Optional transform applied to tool input/result text BEFORE truncation, so a
# secret that straddles the truncation boundary is masked whole rather than
# sliced (used by the claude_code path to pass `redact_secrets`).
Redactor = Callable[[str], str]


def render_content(blocks: Iterable[Any], redactor: Redactor | None = None) -> str:
    """Convert a list of content blocks into a single string.

    Joins rendered blocks with `\\n`. Invalid or unknown blocks are
    ignored (they return an empty string). If `redactor` is given, it is
    applied to tool input/result text before truncation.
    """
    parts: list[str] = []
    for block in blocks:
        rendered = _render_block(block, redactor)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def has_tool_use(blocks: Iterable[Any]) -> bool:
    """True if the list contains any tool_use or tool_result block."""
    return any(isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in blocks)


def _render_block(block: Any, redactor: Redactor | None = None) -> str:
    if not isinstance(block, dict):
        return ""
    btype = block.get("type")
    if btype == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    if btype == "tool_use":
        return _render_tool_use(block, redactor)
    if btype == "tool_result":
        return _render_tool_result(block, redactor)
    return ""


def _render_tool_use(block: dict[str, Any], redactor: Redactor | None = None) -> str:
    name = block.get("name") or "?"
    tool_input = block.get("input", {})
    if isinstance(tool_input, (dict, list)):
        input_str = json.dumps(tool_input, ensure_ascii=False)
    elif tool_input is None:
        input_str = ""
    else:
        input_str = str(tool_input)
    if redactor is not None:
        input_str = redactor(input_str)
    if len(input_str) > MAX_TOOL_INPUT_CHARS:
        input_str = input_str[:MAX_TOOL_INPUT_CHARS] + "…"
    return f"[tool_use: {name}] {input_str}".rstrip()


def _render_tool_result(block: dict[str, Any], redactor: Redactor | None = None) -> str:
    text = _extract_result_text(block.get("content"))
    if redactor is not None:
        text = redactor(text)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "…"
    prefix = "[result error]" if block.get("is_error") else "[result]"
    return f"{prefix} {text}".rstrip() if text else prefix


def _extract_result_text(content: Any) -> str:
    """tool_result.content may be a str or a list of blocks with a `text` field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""
