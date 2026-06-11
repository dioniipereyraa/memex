"""Tests for the remote MCP transport (transports/http.py).

Cover the three security-relevant layers without real network or OAuth:
- config validation (fail closed, every problem reported at once),
- the GitHub login allow-list on token verification,
- app construction (endpoint mounted, Host header pinned).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from memex.config import Settings
from memex.transports.http import (
    AllowlistGitHubProvider,
    RemoteConfigError,
    _allowed_hosts,
    _validate_remote_settings,
    build_remote_app,
    parse_allowed_logins,
)


def make_settings(**overrides) -> Settings:
    """Settings isolated from the developer's real `.env`."""
    base = {
        "remote_base_url": "https://my-mac.tail1234.ts.net",
        "github_client_id": "Iv1.fake-client-id",
        "github_client_secret": "fake-secret-long-enough-for-jwt",
        "remote_allowed_github_logins": "dioniipereyraa",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestParseAllowedLogins:
    def test_normalizes_case_and_whitespace(self) -> None:
        assert parse_allowed_logins(" Dioni , otra ") == frozenset({"dioni", "otra"})

    def test_empty_and_separators_only(self) -> None:
        assert parse_allowed_logins("") == frozenset()
        assert parse_allowed_logins(" , ,") == frozenset()


class TestValidateRemoteSettings:
    def test_complete_config_passes(self) -> None:
        base_url, allowed = _validate_remote_settings(make_settings())
        assert base_url == "https://my-mac.tail1234.ts.net"
        assert allowed == frozenset({"dioniipereyraa"})

    def test_trailing_slash_is_stripped(self) -> None:
        cfg = make_settings(remote_base_url="https://my-mac.tail1234.ts.net/")
        base_url, _ = _validate_remote_settings(cfg)
        assert base_url == "https://my-mac.tail1234.ts.net"

    def test_all_missing_reported_at_once(self) -> None:
        cfg = make_settings(
            remote_base_url=None,
            github_client_id=None,
            github_client_secret=None,
            remote_allowed_github_logins="",
        )
        with pytest.raises(RemoteConfigError) as exc:
            _validate_remote_settings(cfg)
        msg = str(exc.value)
        assert "MEMEX_REMOTE_BASE_URL" in msg
        assert "MEMEX_GITHUB_CLIENT_ID" in msg
        assert "MEMEX_GITHUB_CLIENT_SECRET" in msg
        assert "MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS" in msg

    def test_http_url_rejected(self) -> None:
        cfg = make_settings(remote_base_url="http://my-mac.tail1234.ts.net")
        with pytest.raises(RemoteConfigError, match="https"):
            _validate_remote_settings(cfg)

    def test_empty_allowlist_fails_closed(self) -> None:
        cfg = make_settings(remote_allowed_github_logins=" , ")
        with pytest.raises(RemoteConfigError, match="ALLOWED_GITHUB_LOGINS"):
            _validate_remote_settings(cfg)


def make_provider(allowed: frozenset[str]) -> AllowlistGitHubProvider:
    return AllowlistGitHubProvider(
        allowed_logins=allowed,
        client_id="Iv1.fake-client-id",
        client_secret="fake-secret-long-enough-for-jwt",
        base_url="https://my-mac.tail1234.ts.net",
    )


def make_access_token(claims: dict | None):
    from fastmcp.server.auth import AccessToken

    return AccessToken(
        token="fastmcp-jwt",
        client_id="claude-ai",
        scopes=[],
        expires_at=None,
        claims=claims or {},
    )


class TestAllowlistProvider:
    @pytest.mark.asyncio
    async def test_allowed_login_passes(self) -> None:
        provider = make_provider(frozenset({"dioni"}))
        upstream = make_access_token({"login": "Dioni", "sub": "1"})
        with patch.object(
            AllowlistGitHubProvider.__mro__[1],
            "verify_token",
            new=AsyncMock(return_value=upstream),
        ):
            assert await provider.verify_token("x") is upstream

    @pytest.mark.asyncio
    async def test_other_login_rejected(self) -> None:
        provider = make_provider(frozenset({"dioni"}))
        upstream = make_access_token({"login": "intruso", "sub": "2"})
        with patch.object(
            AllowlistGitHubProvider.__mro__[1],
            "verify_token",
            new=AsyncMock(return_value=upstream),
        ):
            assert await provider.verify_token("x") is None

    @pytest.mark.asyncio
    async def test_missing_login_claim_rejected(self) -> None:
        provider = make_provider(frozenset({"dioni"}))
        upstream = make_access_token({"sub": "3"})
        with patch.object(
            AllowlistGitHubProvider.__mro__[1],
            "verify_token",
            new=AsyncMock(return_value=upstream),
        ):
            assert await provider.verify_token("x") is None

    @pytest.mark.asyncio
    async def test_invalid_upstream_token_stays_rejected(self) -> None:
        provider = make_provider(frozenset({"dioni"}))
        with patch.object(
            AllowlistGitHubProvider.__mro__[1],
            "verify_token",
            new=AsyncMock(return_value=None),
        ):
            assert await provider.verify_token("x") is None


class TestBuildRemoteApp:
    def test_incomplete_config_raises(self) -> None:
        with pytest.raises(RemoteConfigError):
            build_remote_app(make_settings(remote_base_url=None))

    def test_app_mounts_mcp_and_pins_hosts(self) -> None:
        app = build_remote_app(make_settings())
        paths = {getattr(r, "path", "") for r in app.routes}
        assert any(p.startswith("/mcp") for p in paths), paths
        # TrustedHost pinned to the public hostname plus loopback.
        assert _allowed_hosts("https://my-mac.tail1234.ts.net") == [
            "my-mac.tail1234.ts.net",
            "127.0.0.1",
            "localhost",
        ]


class TestServeRemoteCli:
    def test_missing_config_exits_1(self) -> None:
        from typer.testing import CliRunner

        from memex.cli.main import app as cli_app

        runner = CliRunner()
        with patch(
            "memex.transports.http.build_remote_app",
            side_effect=RemoteConfigError("Remote MCP transport is not configured"),
        ):
            result = runner.invoke(cli_app, ["serve-remote"])
        assert result.exit_code == 1
        assert "not configured" in result.output
