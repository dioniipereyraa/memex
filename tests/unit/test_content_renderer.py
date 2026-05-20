"""Tests del content_renderer."""

from __future__ import annotations

from memex.core.ingest.content_renderer import (
    MAX_TOOL_INPUT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    has_tool_use,
    render_content,
)


class TestRenderText:
    def test_single_text_block(self) -> None:
        blocks = [{"type": "text", "text": "hola"}]
        assert render_content(blocks) == "hola"

    def test_multiple_text_blocks(self) -> None:
        blocks = [
            {"type": "text", "text": "primero"},
            {"type": "text", "text": "segundo"},
        ]
        assert render_content(blocks) == "primero\nsegundo"

    def test_text_block_without_text_field(self) -> None:
        blocks = [{"type": "text"}]
        assert render_content(blocks) == ""

    def test_empty_list(self) -> None:
        assert render_content([]) == ""


class TestRenderToolUse:
    def test_basic_tool_use(self) -> None:
        blocks = [{"type": "tool_use", "name": "search", "input": {"query": "memex"}}]
        out = render_content(blocks)
        assert out.startswith("[tool_use: search]")
        assert '"query": "memex"' in out

    def test_tool_use_no_input(self) -> None:
        blocks = [{"type": "tool_use", "name": "ping"}]
        out = render_content(blocks)
        assert out == "[tool_use: ping] {}"

    def test_tool_use_input_truncated(self) -> None:
        long_input = {"q": "x" * (MAX_TOOL_INPUT_CHARS * 2)}
        blocks = [{"type": "tool_use", "name": "search", "input": long_input}]
        out = render_content(blocks)
        assert out.endswith("…")
        # +30 da margen al prefix `[tool_use: search] ` y a la trunc marker.
        assert len(out) < MAX_TOOL_INPUT_CHARS + 30

    def test_tool_use_anonymous(self) -> None:
        blocks = [{"type": "tool_use", "input": {}}]
        out = render_content(blocks)
        assert out.startswith("[tool_use: ?]")


class TestRenderToolResult:
    def test_string_content(self) -> None:
        blocks = [{"type": "tool_result", "content": "el resultado"}]
        assert render_content(blocks) == "[result] el resultado"

    def test_list_content(self) -> None:
        blocks = [
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "parte uno"},
                    {"type": "text", "text": "parte dos"},
                ],
            }
        ]
        assert render_content(blocks) == "[result] parte uno parte dos"

    def test_error_flag(self) -> None:
        blocks = [{"type": "tool_result", "content": "boom", "is_error": True}]
        assert render_content(blocks) == "[result error] boom"

    def test_empty_content(self) -> None:
        blocks = [{"type": "tool_result", "content": ""}]
        assert render_content(blocks) == "[result]"

    def test_result_truncated(self) -> None:
        blocks = [{"type": "tool_result", "content": "x" * (MAX_TOOL_RESULT_CHARS * 2)}]
        out = render_content(blocks)
        assert out.endswith("…")
        assert len(out) < MAX_TOOL_RESULT_CHARS + 30


class TestMixed:
    def test_text_tool_use_text(self) -> None:
        blocks = [
            {"type": "text", "text": "voy a buscar"},
            {"type": "tool_use", "name": "search", "input": {"q": "memex"}},
            {"type": "tool_result", "content": "0 resultados"},
            {"type": "text", "text": "no encontré nada"},
        ]
        out = render_content(blocks)
        assert "voy a buscar" in out
        assert "[tool_use: search]" in out
        assert "[result] 0 resultados" in out
        assert "no encontré nada" in out
        assert out.index("voy a buscar") < out.index("[tool_use:")
        assert out.index("[tool_use:") < out.index("[result]")
        assert out.index("[result]") < out.index("no encontré nada")

    def test_unknown_block_type_ignored(self) -> None:
        blocks = [
            {"type": "text", "text": "ok"},
            {"type": "future_block_type_xyz", "data": "whatever"},
            {"type": "text", "text": "fin"},
        ]
        assert render_content(blocks) == "ok\nfin"

    def test_invalid_block_skipped(self) -> None:
        blocks = [None, "string suelto", 42, {"type": "text", "text": "ok"}]
        assert render_content(blocks) == "ok"  # type: ignore[arg-type]


class TestHasToolUse:
    def test_true_for_tool_use(self) -> None:
        assert has_tool_use([{"type": "tool_use", "name": "x"}]) is True

    def test_true_for_tool_result(self) -> None:
        assert has_tool_use([{"type": "tool_result", "content": "x"}]) is True

    def test_false_for_text_only(self) -> None:
        assert has_tool_use([{"type": "text", "text": "x"}]) is False

    def test_false_for_empty(self) -> None:
        assert has_tool_use([]) is False

    def test_mixed(self) -> None:
        blocks = [
            {"type": "text", "text": "a"},
            {"type": "tool_use", "name": "x"},
        ]
        assert has_tool_use(blocks) is True
