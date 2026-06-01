"""Simple chunker: splits text into even chunks with overlap.

Char-based strategy with a configurable `chars_per_token` factor. Does
not respect message, sentence, or paragraph boundaries. Enough for Phase
0; if retrieval results are poor we can build a smarter chunker later
(respect `\\n\\n`, or split by messages when possible).

Config (`MEMEX_CHUNK_SIZE`, `MEMEX_CHUNK_OVERLAP`) is expressed in
tokens. Conversion uses `chars_per_token=4` by default (reasonable for
mixed ES/EN with a typical BPE tokenizer).
"""

from __future__ import annotations

from typing import NamedTuple


class ChunkSpan(NamedTuple):
    """A text span identified by its offsets in the source text."""

    text: str
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
    chars_per_token: float = 4.0,
    max_chunks: int | None = None,
) -> list[ChunkSpan]:
    """Split `text` into chunks of ~max_tokens with overlap_tokens between neighbors.

    - Returns `[]` if `text` is empty.
    - Returns `[ChunkSpan(text, 0, len(text))]` if everything fits in one chunk.
    - Each chunk guarantees `text[span.char_start:span.char_end] == span.text`.
    - Consecutive chunks overlap by `overlap_tokens * chars_per_token` chars.
    - If `max_chunks` is set, stops after that many chunks (the tail of the
      text is dropped). Callers can detect truncation because the last span's
      `char_end` is then `< len(text)`. Use it to bound the chunk/embed/store
      amplification of a single oversized input.

    Raises `ValueError` if parameters are invalid.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens cannot be negative")
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be > 0")
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be less than max_tokens")
    if max_chunks is not None and max_chunks <= 0:
        raise ValueError("max_chunks must be > 0 when provided")

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
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
        pos += step
    return chunks
