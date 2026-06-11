"""Tests for secret redaction (core/ingest/redact.py)."""

from __future__ import annotations

import pytest

from memex.core.ingest.redact import redact_secrets


class TestRedactSecrets:
    @pytest.mark.parametrize(
        "secret",
        [
            "AKIA1234567890ABCDEF",  # AWS access key id
            "sk-ant-api03-abcdefGHIJKLmnop1234567890",  # Anthropic key
            "sk-abcdefghijklmnopqrstuvwx",  # OpenAI key
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",  # GitHub PAT
            "xoxb-1234567890-abcdefghijkl",  # Slack token
            "AIzaSyA1234567890abcdefghij1234567890abc",  # Google API key
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
        assert "[REDACTED:private-key]" in out

    def test_jwt_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
        out = redact_secrets(f"Authorization cookie {jwt}")
        assert jwt not in out
        assert "[REDACTED:jwt]" in out

    def test_assignment_keeps_key_masks_value(self):
        out = redact_secrets('API_KEY="supersecretvalue123"')
        assert "supersecretvalue123" not in out
        assert "API_KEY=" in out  # the name/prefix survives
        assert "[REDACTED:assignment]" in out

    def test_url_credentials_keep_user_mask_password(self):
        out = redact_secrets("clone https://dioni:hunter2pass@github.com/x/y.git")
        assert "hunter2pass" not in out
        assert "dioni:" in out
        assert "@github.com" in out

    def test_bearer_token_masked(self):
        out = redact_secrets("curl -H 'Authorization: Bearer abc123def456ghi789' url")
        assert "abc123def456ghi789" not in out
        assert "[REDACTED:bearer]" in out

    def test_ordinary_text_untouched(self):
        text = "arreglá el login y corré los tests, el bug está en el parser"
        assert redact_secrets(text) == text

    def test_short_assignment_not_masked(self):
        # A short value (< 6 chars) is left alone to limit false positives.
        out = redact_secrets("mode=dev")
        assert out == "mode=dev"

    def test_empty_string(self):
        assert redact_secrets("") == ""
