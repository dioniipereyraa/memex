"""Secret redaction for ingested text.

Claude Code / terminal sessions capture real command output and file
contents, which routinely include credentials (a token printed in an
error, a `.env` cat'd to stdout, a git remote with embedded auth). Those
secrets would otherwise be chunked, embedded, and retrievable, including
through the remote claude.ai connector. `redact_secrets` masks the common
shapes before the text is stored or embedded.

Two layers:
1. Pattern rules for known shapes (vendor-prefixed API keys, JWTs, PEM
   blocks, `KEY=VALUE` assignments, `Bearer` tokens, URL credentials).
2. A high-entropy fallback that masks long, random-looking tokens with no
   recognizable prefix (the class of secret the pattern rules miss),
   tuned to avoid mangling git hashes, UUIDs, and large base64 blobs.

This is best-effort defense, not a guarantee. The goal is to keep
third-party credentials that were never really part of a conversation out
of the index. Matches are replaced with `[REDACTED:<label>]`.

All patterns use bounded quantifiers so redaction is linear in input size
(no catastrophic backtracking on long whitespace-free blobs).

Applied on the `claude_code` ingest path (see `claude_code`). Not applied
to claude.ai chats, which are genuine conversation, not terminal capture.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

# Each rule: (label, compiled pattern). Patterns are specific and use bounded
# quantifiers to stay linear-time.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    # PEM private key blocks (any header), including the body. Bounded body.
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
    # OpenSSH private key one-liners / bodies without PEM dashes.
    (
        "openssh-key",
        re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----[\s\S]{0,8192}?-----END[^\n]{0,40}"),
    ),
    # JWTs: three base64url segments separated by dots, header starts with eyJ.
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{6,2048}\.eyJ[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{6,2048}"),
    ),
    # Provider API keys with distinctive prefixes.
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,256}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,256}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,255}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,255}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,60}")),
    ("google-oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,512}")),
    ("stripe-key", re.compile(r"\b[rsp]k_(?:live|test)_[0-9A-Za-z]{16,255}")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{20,66}\.[A-Za-z0-9_-]{20,66}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,48}")),
    ("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,512}")),
    ("square-token", re.compile(r"\b(?:sq0atp|sq0csp|EAAA)[0-9A-Za-z_-]{20,128}")),
    (
        "private-key-id",
        re.compile(r"-----BEGIN [A-Z ]{0,40}KEY-----[\s\S]{0,8192}?-----END [A-Z ]{0,40}KEY-----"),
    ),
    # Credentials embedded in a URL: scheme://user:pass@host -> keep user +
    # host, mask only the password. Lookahead keeps the `@`. Bounded so a long
    # blob with no `@` cannot trigger quadratic backtracking.
    (
        "url-credentials",
        re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]{0,40}://[^\s:/@]{1,256}:)[^\s:/@]{1,256}(?=@)"),
    ),
    # Authorization: Bearer / Basic <token> headers.
    (
        "bearer",
        re.compile(r"(?P<pre>(?i:bearer|basic)\s+)(?P<val>[A-Za-z0-9._~+/-]{12,1024}=*)"),
    ),
]

# KEY=VALUE / "key": "value" assignments for sensitive-looking names. A quoted
# value may contain spaces (captured whole); a bare value runs to whitespace.
# Bounded lengths keep it linear. Handled separately from `_RULES` because the
# replacement must restore the opening quote.
_ASSIGNMENT_RE = re.compile(
    r"(?P<pre>(?i:api[_-]?key|secret|token|passwd|password|passphrase|access[_-]?key|"
    r"client[_-]?secret|auth[_-]?token|private[_-]?key|encryption[_-]?key|"
    r"db[_-]?pass|database[_-]?url|conn(?:ection)?[_-]?string|dsn|credential|"
    r"mnemonic|seed[_-]?phrase|signing[_-]?key)\s*[:=]\s*)"
    r"(?:(?P<q>['\"])(?P<qval>(?:(?!(?P=q)).){6,512})(?P=q)|(?P<bval>[^\s'\"]{6,512}))"
)


# Phrase-style secrets (mnemonics, seed/pass phrases) are multiple words with
# no quotes; their value runs to end of line. Run before _ASSIGNMENT_RE.
_PHRASE_RE = re.compile(
    r"(?P<pre>(?i:mnemonic|seed[_-]?phrase|pass[_-]?phrase|recovery[_-]?phrase)\s*[:=]\s*)"
    r"(?P<val>\S[^\n]{5,512})"
)


def _redact_assignments(text: str) -> str:
    text = _PHRASE_RE.sub(lambda m: f"{m.group('pre')}[REDACTED:phrase]", text)

    def repl(m: re.Match[str]) -> str:
        if m.group("q") is not None:
            return f"{m.group('pre')}{m.group('q')}[REDACTED:assignment]{m.group('q')}"
        return f"{m.group('pre')}[REDACTED:assignment]"

    return _ASSIGNMENT_RE.sub(repl, text)


# High-entropy fallback. Candidate tokens are runs of base64/hex/token chars in
# a length band that fits real secrets (32..100). Longer runs are usually data
# (base64 images, hashes of files) and are left alone to avoid mangling.
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{32,100}")
_PURE_HEX = re.compile(r"[0-9a-fA-F]+")
_ENTROPY_BITS_THRESHOLD = 4.0


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    """Heuristic: high-entropy, mixed charset, not a plain hex hash/UUID."""
    # Pure hex (git SHAs, MD5/SHA hex digests) and dash-separated UUIDs are
    # common, non-secret, and would be false positives — skip them.
    if _PURE_HEX.fullmatch(token):
        return False
    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    # Require at least two character classes (random tokens mix them; English
    # words and single-class strings do not).
    classes = sum((has_lower, has_upper, has_digit))
    if classes < 2:
        return False
    return _shannon_entropy(token) >= _ENTROPY_BITS_THRESHOLD


def _redact_high_entropy(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        return "[REDACTED:high-entropy]" if _looks_like_secret(token) else token

    return _TOKEN_CANDIDATE.sub(repl, text)


def _prefix_keeper(label: str) -> Callable[[re.Match[str]], str]:
    """Keep the `pre` group (and the opening quote `q`, if any) and mask the rest."""

    def repl(m: re.Match[str]) -> str:
        quote = m.group("q") if "q" in m.re.groupindex else ""
        return f"{m.group('pre')}{quote}[REDACTED:{label}]"

    return repl


def redact_secrets(text: str) -> str:
    """Return `text` with recognized secret shapes masked.

    Pattern rules run first (specific, low false-positive), then a
    high-entropy fallback masks long random-looking tokens the rules missed.
    Patterns that capture a `pre` group keep that prefix and mask only the
    secret value, so the line stays readable.
    """
    if not text:
        return text
    for label, pattern in _RULES:
        if "pre" in pattern.groupindex:
            text = pattern.sub(_prefix_keeper(label), text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    text = _redact_assignments(text)
    text = _redact_high_entropy(text)
    return text
