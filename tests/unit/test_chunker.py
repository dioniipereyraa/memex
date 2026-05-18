"""Tests del chunker."""

from __future__ import annotations

import pytest

from memex.core.ingest.chunker import ChunkSpan, chunk_text


class TestEdgeCases:
    def test_empty_text_returns_empty_list(self) -> None:
        assert chunk_text("") == []

    def test_short_text_single_chunk(self) -> None:
        out = chunk_text("hola", max_tokens=500, chars_per_token=4.0)
        assert out == [ChunkSpan(text="hola", char_start=0, char_end=4)]

    def test_text_exactly_max_chars(self) -> None:
        text = "x" * 2000
        out = chunk_text(text, max_tokens=500, overlap_tokens=50, chars_per_token=4.0)
        assert len(out) == 1
        assert out[0] == ChunkSpan(text=text, char_start=0, char_end=2000)


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_tokens": 0},
            {"max_tokens": -10},
            {"overlap_tokens": -1},
            {"chars_per_token": 0},
            {"chars_per_token": -1},
            {"max_tokens": 100, "overlap_tokens": 100},
            {"max_tokens": 100, "overlap_tokens": 200},
        ],
    )
    def test_invalid_params_raise(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            chunk_text("texto", **kwargs)


class TestChunkingBehavior:
    def test_long_text_multiple_chunks(self) -> None:
        text = "a" * 5000
        out = chunk_text(text, max_tokens=500, overlap_tokens=50, chars_per_token=4.0)
        assert len(out) >= 2
        for span in out:
            assert text[span.char_start : span.char_end] == span.text
            assert len(span.text) <= 2000

    def test_overlap_correct(self) -> None:
        text = "a" * 5000
        out = chunk_text(text, max_tokens=500, overlap_tokens=50, chars_per_token=4.0)
        # overlap esperado = 50 * 4 = 200 chars
        for i in range(len(out) - 1):
            current = out[i]
            nxt = out[i + 1]
            if current.char_end < len(text):
                assert nxt.char_start == current.char_end - 200
            else:
                assert nxt.char_start == current.char_end

    def test_no_overlap(self) -> None:
        text = "b" * 5000
        out = chunk_text(text, max_tokens=500, overlap_tokens=0, chars_per_token=4.0)
        for i in range(len(out) - 1):
            assert out[i + 1].char_start == out[i].char_end

    def test_coverage_complete(self) -> None:
        """La unión de los chunks (descontando overlaps) cubre todo el texto."""
        text = "abc" * 1000  # 3000 chars
        out = chunk_text(text, max_tokens=500, overlap_tokens=50, chars_per_token=4.0)
        # El último chunk debe terminar en len(text).
        assert out[-1].char_end == len(text)
        # El primero debe arrancar en 0.
        assert out[0].char_start == 0

    def test_last_chunk_can_be_short(self) -> None:
        """El último chunk puede ser más corto que max_chars si el texto no llega."""
        text = "c" * 2200
        out = chunk_text(text, max_tokens=500, overlap_tokens=50, chars_per_token=4.0)
        assert len(out) == 2
        assert out[0].char_end == 2000
        # El segundo arranca en 2000 - 200 = 1800 y termina en 2200.
        assert out[1].char_start == 1800
        assert out[1].char_end == 2200
        assert len(out[1].text) == 400

    def test_text_offsets_round_trip(self) -> None:
        text = "lorem ipsum dolor sit amet " * 200
        out = chunk_text(text, max_tokens=300, overlap_tokens=30, chars_per_token=4.0)
        for span in out:
            assert text[span.char_start : span.char_end] == span.text


class TestCustomCharsPerToken:
    def test_higher_ratio_for_spanish(self) -> None:
        text = "x" * 2500
        out_en = chunk_text(text, max_tokens=500, overlap_tokens=0, chars_per_token=4.0)
        out_es = chunk_text(text, max_tokens=500, overlap_tokens=0, chars_per_token=5.0)
        # Con 5 chars/token, max_chars = 2500 → un solo chunk para este texto.
        assert len(out_es) == 1
        # Con 4 chars/token, max_chars = 2000 → dos chunks.
        assert len(out_en) == 2
