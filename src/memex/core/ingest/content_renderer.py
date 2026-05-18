"""Renderiza el `content[]` de un mensaje de Claude.ai a texto plano.

Tipos de bloque soportados (vistos en el export real):
- `type=text`: texto directo.
- `type=tool_use`: marker `[tool_use: <name>] <input>`. Input se serializa a JSON,
  truncado a `MAX_TOOL_INPUT_CHARS`.
- `type=tool_result`: marker `[result] <texto>` (o `[result error]` si is_error=true).
  Texto truncado a `MAX_TOOL_RESULT_CHARS`.

Bloques con `type` desconocido se ignoran silenciosamente. Esto deja la puerta
abierta a tipos nuevos en futuros exports sin romper el ingest.

El texto resultante es lo que va a `messages.text` y a los chunks para retrieval.
El `content[]` original se conserva en `messages.raw_content` por si después se
quiere parsing más fino sobre tool_use / tool_result.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

MAX_TOOL_INPUT_CHARS = 500
MAX_TOOL_RESULT_CHARS = 1000


def render_content(blocks: Iterable[Any]) -> str:
    """Convierte una lista de content blocks a un solo string.

    Une los bloques renderizados con `\\n`. Bloques inválidos o desconocidos
    se ignoran (devuelven cadena vacía).
    """
    parts: list[str] = []
    for block in blocks:
        rendered = _render_block(block)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def has_tool_use(blocks: Iterable[Any]) -> bool:
    """True si la lista contiene algún bloque tool_use o tool_result."""
    return any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
        for b in blocks
    )


def _render_block(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    btype = block.get("type")
    if btype == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    if btype == "tool_use":
        return _render_tool_use(block)
    if btype == "tool_result":
        return _render_tool_result(block)
    return ""


def _render_tool_use(block: dict[str, Any]) -> str:
    name = block.get("name") or "?"
    tool_input = block.get("input", {})
    if isinstance(tool_input, (dict, list)):
        input_str = json.dumps(tool_input, ensure_ascii=False)
    elif tool_input is None:
        input_str = ""
    else:
        input_str = str(tool_input)
    if len(input_str) > MAX_TOOL_INPUT_CHARS:
        input_str = input_str[:MAX_TOOL_INPUT_CHARS] + "…"
    return f"[tool_use: {name}] {input_str}".rstrip()


def _render_tool_result(block: dict[str, Any]) -> str:
    text = _extract_result_text(block.get("content"))
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "…"
    prefix = "[result error]" if block.get("is_error") else "[result]"
    return f"{prefix} {text}".rstrip() if text else prefix


def _extract_result_text(content: Any) -> str:
    """tool_result.content puede ser str o list de blocks con campo `text`."""
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
