"""Secret redaction for ingested text.

Claude Code / terminal sessions capture real command output and file
contents, which routinely include credentials (a token printed in an
error, a `.env` cat'd to stdout, a git remote with embedded auth). Those
secrets would otherwise be chunked, embedded, and retrievable, including
through the remote claude.ai connector. `redact_secrets` masks the common
shapes before the text is stored or embedded.

This is best-effort defense, not a guarantee: regexes catch well-known
formats, not every possible secret. The goal is to keep third-party
credentials that were never really part of a conversation out of the
index. Matches are replaced with `[REDACTED:<label>]` so the surrounding
text (and its searchability) is preserved.

Applied on the `claude_code` ingest path (see `claude_code._build_message`).
Not applied to claude.ai chats, which are genuine conversation, not
terminal capture.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Each rule: (label, compiled pattern). Order matters only for overlapping
# matches; patterns are intentionally specific to limit false positives.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    # PEM private key blocks (any header), including the body.
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # JWTs: three base64url segments separated by dots, header starts with eyJ.
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")),
    # Provider API keys with distinctive prefixes.
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    # Credentials embedded in a URL: scheme://user:pass@host -> keep user +
    # host, mask only the password. Lookahead keeps the `@` in place.
    (
        "url-credentials",
        re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s:/@]+(?=@)"),
    ),
    # KEY=VALUE / "key": "value" assignments for sensitive-looking names.
    (
        "assignment",
        re.compile(
            r"(?P<pre>(?i:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|"
            r"client[_-]?secret|auth[_-]?token)\s*[:=]\s*['\"]?)(?P<val>[^\s'\"]{6,})"
        ),
    ),
    # Authorization: Bearer <token> headers.
    (
        "bearer",
        re.compile(r"(?P<pre>(?i:bearer)\s+)(?P<val>[A-Za-z0-9._~+/-]{12,}=*)"),
    ),
]


def redact_secrets(text: str) -> str:
    """Return `text` with recognized secret shapes masked.

    Patterns that capture a `pre` group (an assignment prefix, a URL
    user-info head, a `Bearer ` keyword) keep that prefix and mask only the
    secret value, so the line stays readable. Others mask the whole match.
    """
    if not text:
        return text
    for label, pattern in _RULES:
        if "pre" in pattern.groupindex:
            text = pattern.sub(_prefix_keeper(label), text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def _prefix_keeper(label: str) -> Callable[[re.Match[str]], str]:
    """Build a substitution that keeps the `pre` group and masks the rest."""

    def repl(m: re.Match[str]) -> str:
        return f"{m.group('pre')}[REDACTED:{label}]"

    return repl
