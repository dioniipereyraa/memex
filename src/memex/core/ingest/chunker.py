"""Chunker simple: corta texto en chunks parejos con overlap.

Estrategia char-based con factor `chars_per_token` configurable. No respeta bordes
de mensajes, oraciones ni párrafos. Para Fase 0 es suficiente; si los resultados
de retrieval son pobres podemos hacer un chunker smart después (respetar `\\n\\n`,
o partir por mensajes cuando se pueda).

El config (`MEMEX_CHUNK_SIZE`, `MEMEX_CHUNK_OVERLAP`) se expresa en tokens. La
conversión usa `chars_per_token=4` por default (razonable para mezcla ES/EN con
un tokenizer BPE típico).
"""

from __future__ import annotations

from typing import NamedTuple


class ChunkSpan(NamedTuple):
    """Tramo de texto identificado por sus offsets dentro del texto fuente."""

    text: str
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
    chars_per_token: float = 4.0,
) -> list[ChunkSpan]:
    """Corta `text` en chunks de ~max_tokens con overlap_tokens entre vecinos.

    - Retorna `[]` si `text` es vacío.
    - Retorna `[ChunkSpan(text, 0, len(text))]` si todo cabe en un solo chunk.
    - Cada chunk garantiza `text[span.char_start:span.char_end] == span.text`.
    - Chunks consecutivos se solapan en `overlap_tokens * chars_per_token` chars.

    Lanza `ValueError` si los parámetros son inválidos.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens debe ser > 0")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens no puede ser negativo")
    if chars_per_token <= 0:
        raise ValueError("chars_per_token debe ser > 0")
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens debe ser menor que max_tokens")

    if not text:
        return []

    max_chars = max(1, int(max_tokens * chars_per_token))
    overlap_chars = int(overlap_tokens * chars_per_token)
    step = max_chars - overlap_chars

    if len(text) <= max_chars:
        return [ChunkSpan(text=text, char_start=0, char_end=len(text))]

    chunks: list[ChunkSpan] = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        chunks.append(ChunkSpan(text=text[pos:end], char_start=pos, char_end=end))
        if end >= len(text):
            break
        pos += step
    return chunks
