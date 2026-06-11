"""Tests for secret redaction (core/ingest/redact.py)."""

from __future__ import annotations

import time

import pytest

from memex.core.ingest.redact import redact_secrets


class TestRedactSecrets:
    # Vendor prefixes are built from fragments with `+` so no complete
    # credential literal appears in the file (GitHub secret-scanning push
    # protection matches by shape; ruff would re-merge adjacent literals, so
    # use explicit `+`). Python concatenates at runtime, so the value is the
    # full secret.
    @pytest.mark.parametrize(
        "secret",
        [
            "AKI" + "A1234567890ABCDEF",  # AWS access key id
            "ASI" + "A1234567890ABCDEF",  # AWS temporary
            "sk-" + "ant-api03-abcdefGHIJKLmnop1234567890",  # Anthropic key
            "sk" + "-abcdefghijklmnopqrstuvwx",  # OpenAI key
            "sk-" + "proj-abcdefghijklmnopqrstuvwx",  # OpenAI project key
            "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789",  # GitHub PAT
            "github" + "_pat_11ABCDE0123456789_abcdefghijklmnopqrstuvwxyz",  # fine-grained
            "xox" + "b-1234567890-abcdefghijkl",  # Slack token
            "AIz" + "aSyA1234567890abcdefghij1234567890abc",  # Google API key
            "ya2" + "9.a0ARrdaM-abcdefghijklmnopqrstuvwxyz0123456789",  # Google OAuth
            "sk_" + "live_abcdefghijklmnop1234",  # Stripe secret
            "rk_" + "test_abcdefghijklmnop1234",  # Stripe restricted
            "SG" + ".abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyz0123456789012",  # SendGrid
            "npm" + "_abcdefghijklmnopqrstuvwxyz0123456789",  # npm token
        ],
    )
    def test_known_token_shapes_are_masked(self, secret):
        out = redact_secrets(f"the value is {secret} ok")
        assert secret not in out
        assert "[REDACTED:" in out

    def test_pem_private_key_block_masked(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890\nabcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_secrets(text)
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED:" in out

    def test_jwt_masked(self):
        jwt = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
        out = redact_secrets(f"Authorization cookie {jwt}")
        assert jwt not in out
        assert "[REDACTED:jwt]" in out

    def test_assignment_keeps_key_masks_value(self):
        out = redact_secrets('API_KEY="supersecretvalue123"')
        assert "supersecretvalue123" not in out
        assert "API_KEY=" in out  # the name/prefix survives
        assert "[REDACTED:assignment]" in out

    def test_assignment_quoted_value_with_spaces(self):
        # A quoted secret containing spaces must be fully masked (HIGH-4).
        out = redact_secrets('passphrase = "correct horse battery staple"')
        assert "correct horse battery staple" not in out
        assert "battery" not in out

    def test_assignment_extra_sensitive_names(self):
        for line in [
            "DATABASE_URL=postgres://u:p@host/db",
            "client_secret=abcdef123456789",
            "mnemonic=word1 word2 word3 word4 word5 word6",
        ]:
            out = redact_secrets(line)
            assert "[REDACTED:" in out, line

    def test_url_credentials_keep_user_mask_password(self):
        out = redact_secrets("clone https://dioni:hunter2pass@github.com/x/y.git")
        assert "hunter2pass" not in out
        assert "dioni:" in out
        assert "@github.com" in out

    def test_bearer_token_masked(self):
        out = redact_secrets("curl -H 'Authorization: Bearer abc123def456ghi789' url")
        assert "abc123def456ghi789" not in out
        assert "[REDACTED:bearer]" in out

    def test_high_entropy_token_without_prefix_masked(self):
        # A random secret with no recognizable prefix, assigned to an innocuous
        # name, must still be caught by the entropy fallback.
        secret = "Xq7Zm2Vp9Lr4Ks8Tn3Wj6Yb1Dc5Fg0Hh"  # mixed-case+digits, 33 chars
        out = redact_secrets(f"config value {secret} end")
        assert secret not in out
        assert "[REDACTED:high-entropy]" in out

    def test_ordinary_text_untouched(self):
        text = "arreglá el login y corré los tests, el bug está en el parser de sesiones"
        assert redact_secrets(text) == text

    def test_git_sha_not_redacted(self):
        # Pure-hex (git SHA, md5) is not a secret and must survive.
        text = "commit 0251bfae9c1d4f6a8b2c3d4e5f6a7b8c9d0e1f2a fixed it"
        assert "0251bfae9c1d4f6a8b2c3d4e5f6a7b8c9d0e1f2a" in redact_secrets(text)

    def test_uuid_not_redacted(self):
        text = "session 1d8d1a66-0d6a-4bd7-886f-7c43188e9d14 closed"
        assert "1d8d1a66-0d6a-4bd7-886f-7c43188e9d14" in redact_secrets(text)

    def test_pwd_command_output_not_redacted(self):
        # `pwd` is a shell command; its path output must not be mangled (BUG-2).
        text = "pwd: /Users/dioni/projects/memex/src"
        out = redact_secrets(text)
        assert "/Users/dioni/projects/memex/src" in out

    def test_short_value_not_masked(self):
        out = redact_secrets("mode=dev")
        assert out == "mode=dev"

    def test_no_redos_on_large_blob(self):
        # A 180 KB whitespace-free blob (minified JS / base64 shape) must redact
        # in well under a second (regression for the quadratic url-credentials
        # pattern).
        blob = "a1b2c3d4." * 20000  # ~180 KB, no whitespace, no '@'
        start = time.perf_counter()
        redact_secrets(blob)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"redaction took {elapsed:.2f}s on a large blob"

    def test_empty_string(self):
        assert redact_secrets("") == ""
