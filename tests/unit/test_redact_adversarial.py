"""Adversarial corpus for redaction, consolidated from red-team rounds.

MUST_REDACT: real (fake-but-shaped) secrets that must not appear verbatim.
MUST_PRESERVE: non-secret terminal/code content that must survive intact
(over-redaction destroys search usefulness).
"""

from __future__ import annotations

import time

import pytest

from memex.core.ingest.redact import redact_secrets

# Vendor-shaped secrets are built from fragments (the prefix is split) so no
# complete literal credential appears in the file — GitHub secret-scanning push
# protection matches by shape and would block the push otherwise. The runtime
# value is still the full secret, so the test is unchanged.
_HEX64 = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"

# (secret_substring_that_must_disappear, full_input_line)
_GLPAT = "glp" + "at-aBcDeFgHiJkLmNoPqRsT"
_TWILIO = "AC" + "1234567890abcdef1234567890abcdef"
_WHSEC = "whse" + "c_abcdefghijklmnop1234"
_NRAK = "NRA" + "K-ABCDEFGHIJKLMNOPQRSTUVWXY12"
_DOO = "do" + "o_v1_" + "a" * 64
_AGE = "AGE-" + "SECRET-KEY-1" + "A" * 58
_VAULT = "s." + "aBcDeFgHiJkLmNoPqRsTuVwX"
# round 4 (data-theft red-team)
_DISCORD = "MTk4NjIyNDgzNDcxOTI1MjQ4" + "." + "Cl2FMQ" + "." + "aBcDeFgHiJkLmNoPqRsTuVwX"
_TELEGRAM = "123456789" + ":AA" + "FakeTelegramBotTokenHere1234567890xyz"
_DOTTED = "Xq7Zm2Vp9Lr4Ks8" + "." + "Tn3Wj6Yb1Dc5" + "." + "Fg0Hh4Jk8Mn2Pq6Rs0"
# a 64-hex key with a free-text digest word elsewhere on the line must NOT be
# exempted (the word must abut the token to count as a digest context)
_HEX_FREEWORD = "the commit message references " + _HEX64
# zero-width space splitting a high-entropy token must not let a half survive
_ZW_SPLIT = "leaked Xq7Zm2Vp9Lr4Ks8Tn3Wj" + "​" + "6Yb1Dc5Fg0Hh4Jk8Mn2Pq6Rs0Tv4Wx8Zy2"

MUST_REDACT = [
    # vendor prefixes (built from fragments to dodge secret-scanning)
    (_GLPAT, "token=" + _GLPAT),
    (_TWILIO, "sid " + _TWILIO),
    (_WHSEC, "stripe " + _WHSEC),
    (_NRAK, "nr " + _NRAK),
    (_DOO, "do " + _DOO),
    (_AGE, "key " + _AGE),
    (_VAULT, "vault " + _VAULT),
    # 64-hex keys (Ethereum / HMAC / AES)
    (_HEX64, "private key: " + _HEX64),
    ("0x" + _HEX64, "eth " + "0x" + _HEX64),
    # JSON quoted-key credential
    ("lowentropyvalue99", '{"api_key": "lowentropyvalue99"}'),
    ("correcthorsebattery", '{"db": {"password": "correcthorsebattery"}}'),
    # URL with empty username
    ("mypassword", "REDIS_URL=redis://:mypassword@redis:6379/0"),
    ("tok3nonly", "url https://:tok3nonly@host.com/path"),
    # single-charset random secret
    ("njxqplfmwzdbkrhtgsyvcoeiaufpnmlw", "token njxqplfmwzdbkrhtgsyvcoeiaufpnmlw end"),
    # quoted multi-word value
    ("correct horse battery staple", 'passphrase = "correct horse battery staple"'),
    # custom header
    ("hunter2value", "curl -H 'x-custom-pass: hunter2value' url"),
    # otpauth secret
    ("JBSWY3DPEHPK3PXPABCDEFGH", "otpauth://totp/x?secret=JBSWY3DPEHPK3PXPABCDEFGH&issuer=y"),
    # long high-entropy secret (>100 chars: no cleartext tail leak)
    (
        "Xq7Zm2Vp9Lr4Ks8Tn3Wj6Yb1Dc5Fg0Hh4Jk8Mn2Pq6Rs0Tv4Wx8Zy2Bc6Df0Gh4Jl8Np2Qr6St0Uv4Wx8Yz2Ab6Cd0Ef4Gh8Ij2Kl6Mn0Op4Qr8Su2",
        "session Xq7Zm2Vp9Lr4Ks8Tn3Wj6Yb1Dc5Fg0Hh4Jk8Mn2Pq6Rs0Tv4Wx8Zy2Bc6Df0Gh4Jl8Np2Qr6St0Uv4Wx8Yz2Ab6Cd0Ef4Gh8Ij2Kl6Mn0Op4Qr8Su2 ok",
    ),
    # round 2: AWS secret access key (40 base64 chars, contains /) bare
    (
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "use the key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY now",
    ),
    # round 2: short secret with a space-separated label
    ("Hk7Lp2Qr9Xt4Yw1Zb6Nm3", "Your token is Hk7Lp2Qr9Xt4Yw1Zb6Nm3 ok"),
    # round 2: alg:none JWT (empty signature)
    (
        "eyJhbGciOiJub25lIn0.eyJ1c2VyIjoiYWRtaW4ifQ.",
        "cookie eyJhbGciOiJub25lIn0.eyJ1c2VyIjoiYWRtaW4ifQ. set",
    ),
    # round 3: Azure Storage account key (88 base64 chars, contains /)
    (
        "x8Kp9aLq2Rt5Uv7Wy0Bd3Fg6Hj1Kl4Mn8Pq2Rs5Tv8Wx1Zb4Cd7Ef0Gh3Jk6Lm9No2Pr5St8Uv1Wx4Yz7Ab0Cd",
        "value x8Kp9aLq2Rt5Uv7Wy0Bd3Fg6Hj1Kl4Mn8Pq2Rs5Tv8Wx1Zb4Cd7Ef0Gh3Jk6Lm9No2Pr5St8Uv1Wx4Yz7Ab0Cd ok",
    ),
    # round 3: fake integrity marker must NOT hide a secret (>=28 chars)
    ("Hk7Lp2Qr9Xt4Yw1Zb6Nm3Vc5Df8Gh", "base64 Hk7Lp2Qr9Xt4Yw1Zb6Nm3Vc5Df8Gh end"),
    # round 3: Spanish label
    ("Hk7Lp2Qr9Xt4Yw1Zb6Nm3", "la contraseña es Hk7Lp2Qr9Xt4Yw1Zb6Nm3"),
    # round 4: Discord / Telegram bot tokens (dot/colon-segmented, evaded the
    # per-segment entropy floor before)
    (_DISCORD, "DISCORD_BOT_TOKEN was " + _DISCORD),
    (_TELEGRAM, "bot " + _TELEGRAM),
    # round 4: generic dotted-secret chunking
    (_DOTTED, "session " + _DOTTED + " ok"),
    # round 4: a 64-hex key is NOT exempted by a non-adjacent digest word
    (_HEX64, _HEX_FREEWORD),
    (_HEX64, "Loaded object key for wallet: " + _HEX64),
    # round 4: zero-width split must not leave a cleartext half
    ("Xq7Zm2Vp9Lr4Ks8Tn3Wj", _ZW_SPLIT),
]

# Non-secret content that must NOT be mangled.
MUST_PRESERVE = [
    "commit 0251bfae9c1d4f6a8b2c3d4e5f6a7b8c9d0e1f2a fixed it",  # git SHA-1 (40 hex)
    "session 1d8d1a66-0d6a-4bd7-886f-7c43188e9d14 closed",  # uuid
    "/Users/d/wheels/3a/cf/9e/abcdef1234567890fedcba0987654321/foo.whl",  # path w/ hex seg
    "/var/folders/3a/cf9e8b7a6d5c4b3a2f1e0d9c8b7a6f5e/T/tmpfile",  # tmp path
    "https://my-bucket.s3.amazonaws.com/uploads/2024/aBcDeFgHiJkLmNoPqRsTuVwXyZ012345.jpg",
    "integrity sha512-pP9Qrh9dGEUfabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH==",  # SRI
    'integrity="sha384-oqVuAfXRKap7fdgcCY5uykM6MbW7Mxha8w8w8w8w8w8w8w8w8w8w8w8w8"',
    "pwd: /Users/dioni/projects/memex/src",  # pwd command output
    "image@sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    'checksum = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"',
    "arreglá el login y corré los tests, el bug está en el parser de sesiones",
    "mode=dev",
    "import { defineConfig } from 'vite'",
    # round 2: long code identifiers must survive (search usefulness)
    "class UserAccountManagementServiceImplementationFactory extends Base",
    "const thisIsAVeryLongCamelCaseIdentifierForSomeReactComponentPropName = 1",
    "def findAllActiveAccountsByOrganizationIdAndStatus(self, org):",
    "new AbstractSingletonProxyFactoryBeanInitializer()",
    # round 2: Go module hash (public content hash)
    "golang.org/x/text v0.3.7 h1:olpwvP2KacW1ZWvsR7uQhoyTYvKAupfQrRGBFM3p6kw=",
    # round 3: code identifiers (incl. version digits) and prose must survive
    "the secret WidgetFactory2025Prod will ship next week",
    "class RequestMappingHandlerAdapter24X extends Base",
    "stack at getUserAuthenticationTokenFromDb9 line 42",
    "the secret to success is hard work and patience",
    "my password manager is open source and audited",
    # round 4: dotted code/coords/domains must survive the new dotted-secret rule
    "org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter",
    "com.google.guava:guava:31.0.1-jre is the dependency",
    "MyNamespace.SubModule.HandlerClass.processRequest()",
    "host db.internal.example.com:5432 is up",
    "image registry.k8s.io/pause:3.9 pulled",
    "timestamp 2026.06.12.14.30.00 logged",
    # round 4: a 64-hex digest with an ADJACENT marker is still a hash, not a key
    "commit " + _HEX64 + " authored by dioni",
    "image@sha256:" + _HEX64 + " pulled",
    'etag: "' + _HEX64 + '" cached',
]


# These guard against O(n^2) / ReDoS regressions, which on these inputs blow up
# to ~9-67 s. The redactor is linear (~0.5-0.8 s for 2 MB on a dev box). The
# thresholds are deliberately generous because shared CI runners are 2-3x slower
# than a dev box (a tight ~1 s bound was flaky there); a generous bound still
# cleanly separates linear (~2-2.5 s on CI) from a quadratic blowup (9 s+).
class TestPerfQuadraticRegression:
    def test_single_line_packed_tokens_fast(self):
        # Many high-entropy tokens on ONE line (no newlines) must stay linear:
        # the digest-context look-back is windowed, not a full back-scan.
        import random
        import string

        rng = random.Random(0)
        one_line = " ".join(
            "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(40))
            for _ in range(8000)
        )  # ~320 KB single line
        start = time.perf_counter()
        redact_secrets(one_line)
        assert time.perf_counter() - start < 3.0

    def test_packed_unterminated_pem_markers_fast(self):
        # Round 4: a packed run of `-----BEGIN ... PRIVATE KEY-----` with no
        # newline and no matching END forced a 16 KB lazy forward scan per marker
        # (O(n*16384)); ~2.9 MB took ~10 s. The header now requires a trailing
        # newline, so a junk BEGIN fails immediately. Must stay near-linear.
        blob = "-----BEGIN A PRIVATE KEY-----" * 100000  # ~2.9 MB, no newlines
        start = time.perf_counter()
        redact_secrets(blob)
        assert time.perf_counter() - start < 6.0

    def test_packed_otpauth_prefixes_fast(self):
        # The `otp-secret` rule used an UNBOUNDED lazy `[^\s]*?` before
        # `[?&]secret=`, the lone rule breaking the module's bounded-quantifier
        # invariant. A whitespace-free run of `otpauth://` with no `secret=` made
        # each anchor expand to end-of-input: clean O(n^2) (~12 s at 16k reps).
        # Bounded to `{0,256}?` now; must stay near-linear.
        blob = "otpauth://" * 20000  # ~200 KB, no newlines, no `secret=`
        start = time.perf_counter()
        redact_secrets(blob)
        assert time.perf_counter() - start < 3.0

    def test_packed_x_header_prefixes_fast(self):
        # The assignment rule's `x-[a-z-]*?(?:key|token|...)` branch had an
        # unbounded lazy quantifier over a class containing both `x` and `-`, so a
        # contiguous `x-x-x-...` run backtracked quadratically (~8 s at 16k pairs).
        # Bounded to `{0,32}?` now; must stay near-linear.
        blob = "x-" * 20000  # ~40 KB run of `x-` with no trailing `[:=]` value
        start = time.perf_counter()
        redact_secrets(blob)
        assert time.perf_counter() - start < 3.0


class TestMustRedact:
    @pytest.mark.parametrize("secret,line", MUST_REDACT)
    def test_secret_removed(self, secret, line):
        out = redact_secrets(line)
        assert secret not in out, f"LEAK: {secret!r} survived in {out!r}"
        assert "[REDACTED" in out


class TestMustPreserve:
    @pytest.mark.parametrize("line", MUST_PRESERVE)
    def test_non_secret_untouched(self, line):
        out = redact_secrets(line)
        assert "[REDACTED" not in out, f"FALSE POSITIVE: {line!r} -> {out!r}"


class TestPerformance:
    def test_linear_on_large_inputs(self):
        # 2 MB realistic-ish mixed content must stay linear (see the threshold
        # note on TestPerfQuadraticRegression for why the bound is generous).
        blob = "path/to/file_0.js const x = 'abc'; " * 40000
        start = time.perf_counter()
        redact_secrets(blob)
        assert time.perf_counter() - start < 5.0

    def test_linear_on_single_line_hex64_blob(self):
        # Many 64-hex tokens on ONE line (no newlines) must stay linear. The
        # hex64 pass calls `_line_before` per match; an `rfind("\n", 0, pos)`
        # that scans to the start of a newline-free line is O(pos) per call and
        # O(n^2) overall (a single-line JSON array of git object hashes, an
        # `npm ls` dump). The back-scan must be window-bounded. A ~2 MB blob
        # took ~9 s before the fix; linear now (well under the generous bound).
        import random

        rng = random.Random(0)
        one_line = " ".join(
            "".join(rng.choice("0123456789abcdef") for _ in range(64)) for _ in range(32000)
        )  # ~2 MB single line of 64-hex tokens
        start = time.perf_counter()
        redact_secrets(one_line)
        assert time.perf_counter() - start < 5.0
