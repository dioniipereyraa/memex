"""Secret redaction for ingested text.

Claude Code / terminal sessions capture real command output and file
contents, which routinely include credentials (a token printed in an
error, a `.env` cat'd to stdout, a git remote with embedded auth). Those
secrets would otherwise be chunked, embedded, and retrievable, including
through the remote claude.ai connector. `redact_secrets` masks the common
shapes before the text is stored or embedded.

Layers (in order):
1. Vendor-prefixed pattern rules (API keys with distinctive prefixes,
   JWTs, PEM blocks, Bearer headers, URL credentials).
2. `KEY=VALUE` / `"key": "value"` assignments for sensitive names (handles
   JSON quoted keys and quoted multi-word values).
3. Phrase-style secrets (labeled mnemonics / seed phrases).
4. Standalone 64-hex tokens (Ethereum/HMAC/AES keys), excluding digest
   contexts (git/SHA/integrity) to avoid masking hashes.
5. A high-entropy fallback for random tokens with no recognizable prefix,
   tuned to avoid mangling file paths, git hashes, UUIDs, lockfile
   integrity hashes, and large base64 blobs.

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
    # PEM / OpenSSH private key blocks (any header), bounded body.
    (
        # `(?: BLOCK)?` also covers PGP armored "PRIVATE KEY BLOCK".
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----[\s\S]{0,16384}?"
            r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    ("age-key", re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{58}\b")),
    # JWTs: three base64url segments separated by dots, header starts with eyJ.
    (
        # Third segment {0,..}: an `alg:none` JWT has an empty signature.
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{6,2048}\.eyJ[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{0,2048}"),
    ),
    # Provider API keys with distinctive prefixes.
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,256}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,256}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,255}")),
    ("vault-token", re.compile(r"\bhv[sb]\.[A-Za-z0-9_-]{20,255}")),
    ("vault-legacy-token", re.compile(r"\bs\.[A-Za-z0-9]{24}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,255}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,255}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,60}")),
    ("google-oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,512}")),
    ("stripe-key", re.compile(r"\b[rsp]k_(?:live|test)_[0-9A-Za-z]{16,255}")),
    ("stripe-webhook", re.compile(r"\bwhsec_[0-9A-Za-z]{16,255}")),
    ("twilio-sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{20,66}\.[A-Za-z0-9_-]{20,66}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,48}")),
    ("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,512}")),
    ("square-token", re.compile(r"\b(?:sq0atp|sq0csp|EAAA)[0-9A-Za-z_-]{20,128}")),
    ("digitalocean-token", re.compile(r"\bdo[oprt]_v1_[0-9a-f]{64}\b")),
    ("newrelic-key", re.compile(r"\bNRAK-[A-Z0-9]{27}\b")),
    # otpauth:// URIs carry the TOTP secret in the `secret=` query param.
    (
        "otp-secret",
        re.compile(r"(?P<pre>otpauth://[^\s]*?[?&]secret=)(?P<val>[A-Za-z0-9]{8,128})"),
    ),
    # Credentials embedded in a URL: scheme://[user]:pass@host -> keep user +
    # host, mask only the password. The user may be empty (`scheme://:pass@`,
    # common for Redis). Lookahead keeps the `@`. Bounded to stay linear.
    (
        "url-credentials",
        re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]{0,40}://[^\s:/@]{0,256}:)[^\s:/@]{1,256}(?=@)"),
    ),
    # Authorization: Bearer / Basic <token> headers.
    (
        "bearer",
        re.compile(r"(?P<pre>(?i:bearer|basic)\s+)(?P<val>[A-Za-z0-9._~+/-]{12,1024}=*)"),
    ),
]

# KEY=VALUE / "key": "value" assignments for sensitive-looking names. The name
# may be wrapped in quotes (JSON). A quoted value may contain spaces (captured
# whole); a bare value runs to whitespace. Bounded lengths keep it linear.
_ASSIGNMENT_RE = re.compile(
    r"(?P<pre>(?P<kq>['\"]?)(?i:api[_-]?key|secret|token|passwd|password|passphrase|"
    r"access[_-]?key|client[_-]?secret|auth[_-]?token|private[_-]?key|encryption[_-]?key|"
    r"db[_-]?pass|database[_-]?url|redis[_-]?url|conn(?:ection)?[_-]?string|dsn|credential|"
    r"account[_-]?key|app[_-]?key|private[_-]?token|auth[_-]?key|totp|otp[_-]?secret|"
    r"x-[a-z-]*?(?:key|token|secret|pass|auth)|signing[_-]?key)(?P=kq)\s*[:=]\s*)"
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


# Standalone 64-hex tokens are key-sized (Ethereum/HMAC/AES, 256-bit). A bare
# 64-hex run is almost always a SHA-256 digest OR a key; redact it UNLESS the
# line marks it as a digest (lockfiles, image refs, integrity hashes). 32-hex
# (MD5) and 40-hex (git SHA-1) are left alone: too many benign occurrences.
_HEX64_RE = re.compile(r"(?<![0-9a-fA-Fx])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_DIGEST_CONTEXT = re.compile(
    r"(?i:sha-?\d{2,3}|integrity|checksum|etag|digest|@sha\d|blob|oid|hash|"
    r"commit|object|tree|content[_-]?hash)"
)


# Look back at most this many chars for a digest/integrity marker. A full
# scan to line start is O(distance), which becomes O(n^2) when many candidate
# tokens share one line (a minified bundle / single-line JSON token array).
# The markers we look for (`integrity`, `sha256-`, `h1:`, `checksum`) are short
# and sit immediately before the token, so a fixed window is equivalent.
_CONTEXT_WINDOW = 160


def _line_before(text: str, pos: int) -> str:
    # Bound the back-scan to the window. `rfind` searches its [start, end) range,
    # so searching from 0 is O(pos) per call and becomes O(n^2) when many
    # candidate tokens share one newline-free line (a minified bundle, a
    # single-line JSON array of git SHAs). Starting the search at the window
    # bound keeps every call O(_CONTEXT_WINDOW); the markers we look for sit
    # immediately before the token, so a clamped search is equivalent.
    win_start = max(0, pos - _CONTEXT_WINDOW)
    nl = text.rfind("\n", win_start, pos)
    start = nl + 1 if nl != -1 else win_start
    return text[start:pos]


def _redact_hex64(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        if _DIGEST_CONTEXT.search(_line_before(text, m.start())):
            return m.group(0)  # a hash, not a key
        return "[REDACTED:hex-key]"

    return _HEX64_RE.sub(repl, text)


# A standard-base64 secret (AWS secret key = 40 chars; Azure Storage key = 88;
# others 44/60) often contains `/`, which the high-entropy pass excludes to
# protect paths, so such secrets survive. Catch a 40-512 char base64 run that
# contains `/` or `+` and looks like a secret (mixed character classes — a
# lowercase path segment does not), unless it is a path segment (preceded by `/`).
_B64_SLASH_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,512}(?![A-Za-z0-9+/=])")


def _redact_b64_secret(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        tok = m.group(0)
        if "/" not in tok and "+" not in tok:
            return tok  # plain base64 (no /, +) is handled by the entropy pass
        # Paths/URLs have several `/`-separated segments; a base64 key has at
        # most 1-2 `/` as part of the encoding. More than two, or a leading `/`,
        # means a path, not a secret.
        if tok.startswith("/") or tok.count("/") > 2:
            return tok
        if m.start() > 0 and text[m.start() - 1] == "/":
            return tok  # path segment (the run starts mid-path)
        if _INTEGRITY_PREFIX.search(_line_before(text, m.start())):
            return tok  # SRI / digest hash, not a secret
        # Strict: require all three character classes AND high entropy. A file
        # path or structured data is single/two-class or lower-entropy; a real
        # base64 secret (AWS/Azure key) is mixed-case+digits and near-random.
        body = tok.replace("/", "").replace("+", "")
        if not (
            any(c.islower() for c in body)
            and any(c.isupper() for c in body)
            and any(c.isdigit() for c in body)
        ):
            return tok
        if _shannon_entropy(body) < 4.3:
            return tok
        return "[REDACTED:b64-key]"

    return _B64_SLASH_RE.sub(repl, text)


# Sensitive label followed by whitespace and a high-entropy value with no
# `=`/`:` separator (e.g. "Your token is Hk7Lp2Qr9Xt4Yw1Zb6Nm3"). Lowers the
# 28-char floor only inside this labeled context, where false positives are
# unlikely (the value must still look random).
_LABELED_VALUE_RE = re.compile(
    r"(?P<pre>(?i:secret|token|api[_-]?key|password|passphrase|auth[_-]?token|"
    r"credential|access[_-]?key|contrase(?:ña|na)|clave|secreto)"
    r"\s+(?:is\s+|was\s+|es\s+|:\s+)?)(?P<val>[A-Za-z0-9+/_=-]{16,512})\b"
)


def _redact_labeled_values(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        if _looks_like_short_secret(m.group("val")):
            return f"{m.group('pre')}[REDACTED:labeled]"
        return m.group(0)

    return _LABELED_VALUE_RE.sub(repl, text)


def _is_camel_identifier(token: str) -> bool:
    """Heuristic: a CamelCase/PascalCase code identifier, not a random secret.

    Identifiers are alphanumeric with several lower->Upper humps and few digits;
    random secrets are mostly distinguished by a high digit ratio. Catches
    `RequestMappingHandlerAdapter24X`, `thisIsAVeryLongPropName` while leaving a
    digit-heavy random token (the usual secret shape) to be redacted.
    """
    if len(token) < 12 or not token.isalnum():
        return False
    humps = sum(1 for i in range(1, len(token)) if token[i - 1].islower() and token[i].isupper())
    digits = sum(c.isdigit() for c in token)
    return humps >= 2 and digits <= len(token) * 0.3


def _looks_like_short_secret(token: str) -> bool:
    """Like `_looks_like_secret` but for the 16+ labeled-context band."""
    if _PURE_HEX.fullmatch(token):
        return len(token) >= 32  # short hex is usually an id; long hex is a key
    if _is_camel_identifier(token):
        return False  # a CamelCase word/codename, not a random secret
    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if (has_lower + has_upper + has_digit) < 2:
        return False
    # Higher floor than the general pass: a labeled value is only flagged when
    # it really looks random, so engineering prose after "the secret …" survives.
    return _shannon_entropy(token) >= 4.0


# High-entropy fallback. Candidate tokens are runs of base64/token chars (NOT
# including '/', so file paths and URLs are split at slashes and judged per
# segment, not masked whole). Upper bound is generous so a long secret is
# consumed in ONE match (no cleartext tail). A token preceded by a digest/
# integrity marker on the same line is skipped (lockfile SRI hashes).
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+_=-]{28,4096}")
_PURE_HEX = re.compile(r"[0-9a-fA-F]+")
# Only an ADJACENT digest-token prefix (SRI `sha256-`, Go `h1:`) skips the
# entropy pass. Free-text words (`integrity`/`checksum`/`base64`) were too
# permissive: an attacker could write `base64 <secret>` to dodge redaction.
_INTEGRITY_PREFIX = re.compile(r"(?i:sha\d{3}-|h1:)\W{0,2}$")


def _shannon_entropy(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    """Heuristic: random-looking, not a hash/path/identifier."""
    # Pure hex is handled by the dedicated 64-hex rule; skip here (git SHAs,
    # MD5, shorter hex digests are common and benign).
    if _PURE_HEX.fullmatch(token):
        return False
    # CamelCase/PascalCase identifiers (with or without version digits) are
    # code, not secrets — a structural check catches them at any length, which
    # the entropy floor alone cannot (they sit at ~4.0-4.5, same as some real
    # secrets).
    if _is_camel_identifier(token):
        return False
    has_lower = has_upper = has_digit = False
    for ch in token:
        if ch.islower():
            has_lower = True
        elif ch.isupper():
            has_upper = True
        elif ch.isdigit():
            has_digit = True
    classes = has_lower + has_upper + has_digit
    ent = _shannon_entropy(token)
    if classes >= 2:
        return ent >= 4.0
    # Single character class: require near-maximal entropy so random tokens are
    # caught but words / ALLCAPS constants / repetitive identifiers are not.
    if classes == 1 and len(token) >= 32:
        return ent >= 4.3
    return False


# The token candidate includes `-`, so an SRI hash `sha512-<b64>` is captured
# as one token starting with the marker; detect it on the token itself.
_SRI_TOKEN = re.compile(r"(?i:sha\d{3}-|h1:)")


def _redact_high_entropy(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if _SRI_TOKEN.match(token):
            return token  # the token IS an SRI/Go content hash
        if not _looks_like_secret(token):
            return token
        # A token right after `/` is a path / URL segment (object key, cache
        # dir, generated filename), not a secret. Secrets rarely live in a path
        # segment; over-redacting paths destroys the searchability this index
        # exists for.
        if m.start() > 0 and text[m.start() - 1] == "/":
            return token
        # Skip a lockfile / SRI hash marked just before the token (e.g. `h1:`,
        # where `:` is not part of the captured token).
        if _INTEGRITY_PREFIX.search(_line_before(text, m.start())):
            return token
        return "[REDACTED:high-entropy]"

    return _TOKEN_CANDIDATE.sub(repl, text)


def _prefix_keeper(label: str) -> Callable[[re.Match[str]], str]:
    """Keep the `pre` group (and the opening quote `q`, if any) and mask the rest."""

    def repl(m: re.Match[str]) -> str:
        quote = m.group("q") if "q" in m.re.groupindex else ""
        return f"{m.group('pre')}{quote}[REDACTED:{label}]"

    return repl


def redact_secrets(text: str) -> str:
    """Return `text` with recognized secret shapes masked.

    Vendor rules first (specific, low false-positive), then sensitive-name
    assignments, then phrase secrets, then standalone 64-hex keys, then a
    high-entropy fallback for prefixless random tokens. Patterns that capture a
    `pre` group keep that prefix and mask only the secret value.
    """
    if not text:
        return text
    for label, pattern in _RULES:
        if "pre" in pattern.groupindex:
            text = pattern.sub(_prefix_keeper(label), text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    text = _redact_assignments(text)
    text = _redact_labeled_values(text)
    text = _redact_b64_secret(text)
    text = _redact_hex64(text)
    text = _redact_high_entropy(text)
    return text
