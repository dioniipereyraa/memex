"""Tests for the Claude Code session parser (core/ingest/claude_code.py)."""

from __future__ import annotations

import json

from memex.core.ingest.claude_code import parse_session_file
from memex.core.models import Sender, Source


def write_session(tmp_path, session_id: str, lines: list[dict]):
    """Write a list of event dicts as a `.jsonl` session file."""
    p = tmp_path / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return p


def user(text, **extra):
    base = {
        "type": "user",
        "uuid": extra.pop("uuid", "u1"),
        "timestamp": extra.pop("timestamp", "2026-06-10T10:00:00.000Z"),
        "cwd": extra.pop("cwd", "/Users/d/proj"),
        "message": {"role": "user", "content": text},
    }
    base.update(extra)
    return base


def assistant(content, **extra):
    base = {
        "type": "assistant",
        "uuid": extra.pop("uuid", "a1"),
        "timestamp": extra.pop("timestamp", "2026-06-10T10:00:05.000Z"),
        "cwd": extra.pop("cwd", "/Users/d/proj"),
        "message": {"role": "assistant", "content": content},
    }
    base.update(extra)
    return base


class TestParseSessionFile:
    def test_basic_user_assistant(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-1",
            [
                {"type": "mode", "mode": "normal"},
                user("hola, arreglá el bug", uuid="u1"),
                assistant([{"type": "text", "text": "listo, lo arreglé"}], uuid="a1"),
            ],
        )
        parsed = parse_session_file(p)
        assert parsed is not None
        assert parsed.conversation.uuid == "sess-1"
        assert parsed.conversation.source is Source.CLAUDE_CODE
        assert parsed.cwd == "/Users/d/proj"
        assert [m.sender for m in parsed.messages] == [Sender.HUMAN, Sender.ASSISTANT]
        assert parsed.messages[0].text == "hola, arreglá el bug"
        assert "listo" in parsed.messages[1].text

    def test_title_from_ai_title(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-2",
            [
                {"type": "ai-title", "aiTitle": "Fix del parser", "sessionId": "sess-2"},
                user("algo"),
                assistant([{"type": "text", "text": "ok"}]),
            ],
        )
        parsed = parse_session_file(p)
        assert parsed.conversation.title == "Fix del parser"

    def test_title_derived_from_first_prompt_when_no_ai_title(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-3",
            [
                user("implementá la búsqueda híbrida por favor"),
                assistant([{"type": "text", "text": "ok"}]),
            ],
        )
        parsed = parse_session_file(p)
        assert parsed.conversation.title == "implementá la búsqueda híbrida por favor"

    def test_timestamps_span_first_and_last(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-4",
            [
                user("a", timestamp="2026-06-10T10:00:00.000Z"),
                assistant([{"type": "text", "text": "b"}], timestamp="2026-06-10T10:05:00.000Z"),
            ],
        )
        parsed = parse_session_file(p)
        assert parsed.conversation.created_at.isoformat().startswith("2026-06-10T10:00:00")
        assert parsed.conversation.updated_at.isoformat().startswith("2026-06-10T10:05:00")

    def test_sidechain_and_meta_dropped(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-5",
            [
                user("real prompt", uuid="u1"),
                user("subagent noise", uuid="u2", isSidechain=True),
                user("meta noise", uuid="u3", isMeta=True),
                assistant([{"type": "text", "text": "respuesta"}], uuid="a1"),
            ],
        )
        parsed = parse_session_file(p)
        texts = [m.text for m in parsed.messages]
        assert "real prompt" in texts
        assert "respuesta" in texts
        assert "subagent noise" not in texts
        assert "meta noise" not in texts

    def test_local_command_noise_dropped(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-6",
            [
                user("<local-command-caveat>Caveat: ...</local-command-caveat>", uuid="u1"),
                user("pregunta real", uuid="u2"),
                assistant([{"type": "text", "text": "ok"}], uuid="a1"),
            ],
        )
        parsed = parse_session_file(p)
        texts = [m.text for m in parsed.messages]
        assert "pregunta real" in texts
        assert all("local-command" not in t for t in texts)

    def test_thinking_dropped_but_tool_use_kept(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-7",
            [
                user("hacelo"),
                assistant(
                    [
                        {"type": "thinking", "thinking": "secreto interno"},
                        {"type": "text", "text": "ya está"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]
                ),
            ],
        )
        parsed = parse_session_file(p)
        atext = parsed.messages[1].text
        assert "secreto interno" not in atext
        assert "ya está" in atext
        assert "[tool_use: Bash]" in atext
        assert parsed.messages[1].has_tool_use is True

    def test_tool_result_in_user_role_rendered(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-8",
            [
                user("corré los tests"),
                assistant(
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}],
                    uuid="a1",
                ),
                user(
                    [{"type": "tool_result", "content": "5 passed", "is_error": False}],
                    uuid="u2",
                ),
            ],
        )
        parsed = parse_session_file(p)
        last = parsed.messages[-1]
        assert last.sender is Sender.HUMAN
        assert "[result]" in last.text
        assert "5 passed" in last.text

    def test_derived_title_skips_leading_tool_result(self, tmp_path):
        # A post-/resume session can open with a tool_result user line; the
        # derived title must use the first REAL prompt, not "[result] ...".
        p = write_session(
            tmp_path,
            "sess-r",
            [
                user([{"type": "tool_result", "content": "salida vieja"}], uuid="u1"),
                user("esta es la pregunta real", uuid="u2"),
                assistant([{"type": "text", "text": "ok"}], uuid="a1"),
            ],
        )
        parsed = parse_session_file(p)
        assert parsed.conversation.title == "esta es la pregunta real"

    def test_secrets_redacted_in_messages(self, tmp_path):
        # A token printed in tool output must be masked in the stored text,
        # and raw_content must not be persisted (no unredacted secret in DB).
        p = write_session(
            tmp_path,
            "sess-sec",
            [
                user("mostrame el env"),
                assistant(
                    [
                        {"type": "text", "text": "export API_KEY=topsecretvalue12345"},
                        {
                            "type": "tool_result",
                            "content": "AKIA1234567890ABCDEF leaked",
                        },
                    ],
                    uuid="a1",
                ),
            ],
        )
        parsed = parse_session_file(p)
        joined = " ".join(m.text for m in parsed.messages)
        assert "topsecretvalue12345" not in joined
        assert "AKIA1234567890ABCDEF" not in joined
        assert "[REDACTED:" in joined
        assert all(m.raw_content is None for m in parsed.messages)

    def test_empty_session_returns_none(self, tmp_path):
        p = write_session(
            tmp_path,
            "sess-9",
            [{"type": "mode", "mode": "normal"}, {"type": "file-history-snapshot"}],
        )
        assert parse_session_file(p) is None

    def test_malformed_line_skipped(self, tmp_path):
        p = tmp_path / "sess-10.jsonl"
        p.write_text(
            "not json\n"
            + json.dumps(user("buena línea"))
            + "\n"
            + json.dumps(assistant([{"type": "text", "text": "ok"}])),
            encoding="utf-8",
        )
        parsed = parse_session_file(p)
        assert parsed is not None
        assert any("buena línea" in m.text for m in parsed.messages)
