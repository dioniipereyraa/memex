"""Remote MCP transport over Streamable HTTP, for claude.ai connectors.

Mounts the shared Memex server (see `transports/mcp_server.py`) as a
Streamable HTTP app at `/mcp`, protected by OAuth. claude.ai (and Claude
Desktop / mobile, which share the same connector infrastructure) connects
from Anthropic's cloud, so the server must be reachable on a public HTTPS
URL; the intended deployment is a tunnel (e.g. Tailscale Funnel) that
terminates TLS and proxies to this process on loopback.

Auth model (claude.ai supports only "no auth" or full OAuth 2.0 with
dynamic client registration; bearer tokens cannot be configured in its UI):

- FastMCP's `GitHubProvider` acts as an OAuth proxy in front of a GitHub
  OAuth App: claude.ai registers itself dynamically (DCR), the user
  authorizes via GitHub, and the proxy issues its own JWTs bound to the
  upstream GitHub token.
- `AllowlistGitHubProvider` narrows that to a fixed set of GitHub
  usernames: token verification fails (401) for anyone else, even after a
  successful OAuth dance. Every MCP request re-validates the upstream
  token, so revoking access on GitHub takes effect immediately.
- OAuth state (client registrations, token mappings) is persisted by
  FastMCP encrypted on disk, keyed deterministically from the client
  secret, so server restarts do not break the claude.ai connection.

Defense in depth, same spirit as `http_ingest.py`:
- The process binds to loopback; only the tunnel exposes it.
- `TrustedHostMiddleware` pins the Host header to the public hostname (plus
  loopback for local smoke tests), so a DNS-rebinding page cannot reach the
  endpoint even though OAuth would already reject it.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from memex.config import Settings, settings
from memex.transports.mcp_server import build_server

logger = logging.getLogger("memex.http")

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class RemoteConfigError(ValueError):
    """Raised when the remote transport is started with incomplete config."""


class AllowlistGitHubProvider(GitHubProvider):
    """GitHubProvider that only accepts a fixed set of GitHub usernames.

    The OAuth dance itself succeeds for any GitHub account (GitHub does not
    know about our allow-list), but the token issued to a non-allowed user
    fails verification on every MCP request, so they never reach a tool.
    """

    def __init__(self, *, allowed_logins: frozenset[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._allowed_logins = frozenset(login.lower() for login in allowed_logins)

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        login = (access.claims or {}).get("login")
        if not isinstance(login, str) or login.lower() not in self._allowed_logins:
            # Do not log the token; the login is enough to diagnose.
            logger.warning(
                "Rejected GitHub user %r: not in MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS.",
                login,
            )
            return None
        return access


def parse_allowed_logins(raw: str) -> frozenset[str]:
    """Parse the comma-separated allow-list into a normalized set."""
    return frozenset(login.strip().lower() for login in raw.split(",") if login.strip())


def _validate_remote_settings(cfg: Settings) -> tuple[str, frozenset[str]]:
    """Check the remote config, returning (base_url, allowed_logins).

    Raises `RemoteConfigError` listing every missing/invalid setting at
    once, so the user fixes the `.env` in one pass instead of one error
    per restart.
    """
    problems: list[str] = []

    base_url = (cfg.remote_base_url or "").strip().rstrip("/")
    if not base_url:
        problems.append("MEMEX_REMOTE_BASE_URL is not set (public https:// URL).")
    else:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            problems.append(
                f"MEMEX_REMOTE_BASE_URL must be a public https:// URL, got {base_url!r}. "
                "claude.ai only connects to public HTTPS endpoints."
            )

    if not (cfg.github_client_id or "").strip():
        problems.append("MEMEX_GITHUB_CLIENT_ID is not set (GitHub OAuth App client id).")
    if not (cfg.github_client_secret or "").strip():
        problems.append("MEMEX_GITHUB_CLIENT_SECRET is not set (GitHub OAuth App client secret).")

    allowed = parse_allowed_logins(cfg.remote_allowed_github_logins)
    if not allowed:
        # Fail closed: without an allow-list, ANY GitHub account could read
        # the whole chat corpus after a successful OAuth dance.
        problems.append(
            "MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS is empty. List the GitHub "
            "username(s) allowed to access your chats (comma-separated)."
        )

    if problems:
        raise RemoteConfigError(
            "Remote MCP transport is not configured:\n- " + "\n- ".join(problems)
        )
    return base_url, allowed


def _allowed_hosts(base_url: str) -> list[str]:
    """Host header allow-list: the public hostname plus loopback."""
    public_host = urlparse(base_url).hostname
    hosts = [h for h in (public_host,) if h]
    hosts.extend(_LOOPBACK_HOSTS)
    return hosts


def build_remote_app(cfg: Settings | None = None) -> Starlette:
    """Factory for the remote MCP Starlette app (endpoint at `/mcp`)."""
    cfg = cfg if cfg is not None else settings
    base_url, allowed_logins = _validate_remote_settings(cfg)

    auth = AllowlistGitHubProvider(
        allowed_logins=allowed_logins,
        client_id=(cfg.github_client_id or "").strip(),
        client_secret=(cfg.github_client_secret or "").strip(),
        base_url=base_url,
    )
    server = build_server(auth=auth)
    return server.http_app(
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts(base_url))],
    )
