"""Memex configuration.

Reads from environment variables and optionally from a `.env` file.
Default values are safe for local development.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _platform_data_dir() -> Path:
    """Per-user data directory for the DB and exports on a wheel/PyPI install.

    Follows each OS convention: macOS `~/Library/Application Support`, Windows
    `%LOCALAPPDATA%`, and the XDG `~/.local/share` elsewhere. Used only when
    Memex is not running from a cloned repo (see `_default_data_dir`).
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "memex"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "memex"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "memex"


def _default_data_dir() -> Path:
    """Directory for the DB and exports when neither is set in the environment.

    From a cloned or editable repo (a `pyproject.toml` + `scripts/` two levels
    up from this file), use `<repo>/data` so existing installs are unchanged,
    resolved absolutely rather than relative to the current working directory.
    From a wheel/PyPI install (no repo alongside the package), use the
    platform's per-user data directory, so the DB has a stable home no matter
    where the `memex` command is run from.
    """
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").is_file() and (repo_root / "scripts").is_dir():
        return repo_root / "data"
    return _platform_data_dir()


class Settings(BaseSettings):
    """Unified project settings.

    Env var names follow `.env.example`. Some use the `MEMEX_` prefix to
    avoid collisions, others (like OLLAMA_HOST) are the dependency's
    standard names and are respected as-is.
    """

    # No `populate_by_name`: with it, pydantic-settings ALSO reads each field's
    # bare snake_case name from the environment (e.g. `db_path`, `anthropic_api_key`,
    # `ingest_allowed_hosts`), not only the declared `MEMEX_*`/standard aliases. A
    # generic inherited env var (`DB_PATH`, etc.) could then silently relocate the
    # DB or weaken a security setting. Only the explicit aliases below are honored.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Embeddings backend. Defaults to `fastembed` (zero-config, model is
    # downloaded automatically the first time). Switch to `ollama` if
    # you prefer to coordinate the model with your local Ollama instance.
    embed_backend: str = Field(default="fastembed", alias="MEMEX_EMBED_BACKEND")

    # Model name. If `None`, each backend uses its default:
    # - fastembed: `nomic-ai/nomic-embed-text-v1.5-Q` (~130 MB, quantized).
    # - ollama: `nomic-embed-text`.
    # If set, it must be a valid model for the chosen backend.
    embed_model: str | None = Field(default=None, alias="MEMEX_EMBED_MODEL")
    embed_dim: int = Field(default=768, alias="MEMEX_EMBED_DIM")

    # fastembed resource caps. The defaults keep a background ingest from
    # hogging the machine. Two independent drivers:
    #  - `embed_threads` caps onnxruntime intra-op parallelism (it otherwise
    #    spawns a thread per core, 10+, each with its own arena).
    #  - `embed_batch_size` caps the onnxruntime per-batch arena, which is the
    #    real memory driver and scales with batch_size * sequence_length. At the
    #    default 500-token chunk size, measured peak RSS is ~0.67 GB at batch=1,
    #    ~0.97 GB at batch=2, ~1.56 GB at batch=4, ~2.4 GB at batch=8.
    #    The embedder runs inference inline (`parallel=None`), so there is no
    #    extra per-worker model copy (a subprocess would add ~0.6 GB).
    #    batch=2 is the default. Because `embed_threads` caps parallelism at 2,
    #    a larger batch buys NO throughput (measured ~45 s for 200 chunks at
    #    batch 1/2/4 alike), it only inflates the arena; so a small batch is a
    #    near-free RAM win for a background ingest. Drop to 1 (~0.67 GB) to
    #    minimize RAM further; raise it only if you also raise `embed_threads`.
    embed_threads: int = Field(default=2, alias="MEMEX_EMBED_THREADS", ge=1, le=64)
    embed_batch_size: int = Field(default=2, alias="MEMEX_EMBED_BATCH_SIZE", ge=1, le=256)

    # Ollama-specific config (only used if embed_backend == "ollama").
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")

    db_path: Path = Field(
        default_factory=lambda: _default_data_dir() / "memex.db", alias="MEMEX_DB_PATH"
    )
    exports_dir: Path = Field(
        default_factory=lambda: _default_data_dir() / "exports", alias="MEMEX_EXPORTS_DIR"
    )

    chunk_size: int = Field(default=500, alias="MEMEX_CHUNK_SIZE", ge=64, le=4096)
    chunk_overlap: int = Field(default=50, alias="MEMEX_CHUNK_OVERLAP", ge=0, le=512)

    # Hard ceiling on chunks produced per conversation. Bounds the
    # chunk/embed/store amplification of one ingest call (an oversized chat
    # cannot blow up memory/CPU/DB without limit). 5000 chunks is far above any
    # real conversation (~10 MB of text at the default chunk size).
    max_chunks_per_conversation: int = Field(
        default=5000, alias="MEMEX_MAX_CHUNKS_PER_CONVERSATION", ge=1
    )

    # Embed each captured chat in a short-lived subprocess that exits, instead
    # of in the always-on `memex serve` process. The capture server embeds
    # in-process, so once it embedded one chat it held the model + onnxruntime
    # arena (~0.5 GB) resident forever (dropping the model + gc does NOT return
    # the arena to the OS on macOS). Spawning a child per capture keeps the
    # server itself at its ~0.08 GB baseline; the child reclaims everything on
    # exit. Cost: a ~3-5s model load per captured chat (background, not latency
    # critical). Set False to embed in-process (lower per-capture latency, higher
    # steady RSS). Tests run with this False to use injected mocks.
    ingest_embed_in_subprocess: bool = Field(default=True, alias="MEMEX_INGEST_EMBED_IN_SUBPROCESS")

    # Live-capture HTTP server hardening (transports/http_ingest.py).
    # Max request body accepted by the ingest endpoint, in bytes. A single
    # Claude.ai conversation is well under a few MB; the cap rejects oversized
    # bodies before they are buffered/parsed (memory-exhaustion DoS).
    ingest_max_body_bytes: int = Field(
        default=16 * 1024 * 1024, alias="MEMEX_INGEST_MAX_BODY_BYTES", ge=1024
    )
    # Comma-separated Host allow-list for the ingest server (defense against
    # DNS-rebinding). Defaults to loopback only; add a value here if you
    # deliberately bind `memex serve` to another interface.
    ingest_allowed_hosts: str = Field(
        default="127.0.0.1,localhost", alias="MEMEX_INGEST_ALLOWED_HOSTS"
    )

    # Remote MCP transport (transports/http.py). Lets claude.ai consume Memex
    # as a custom connector. All four settings below are required to start
    # `memex serve-remote`; there are no insecure defaults on purpose.
    #
    # Public HTTPS base URL where the server is reachable from the internet
    # (e.g. the Tailscale Funnel URL "https://my-mac.tailnet.ts.net"). OAuth
    # redirects and token audiences are derived from it.
    remote_base_url: str | None = Field(default=None, alias="MEMEX_REMOTE_BASE_URL")
    # Local port the remote MCP server binds to (loopback). The tunnel
    # (Tailscale Funnel) terminates TLS publicly and proxies to this port.
    remote_port: int = Field(default=8377, alias="MEMEX_REMOTE_PORT", ge=1, le=65535)
    # GitHub OAuth App credentials. The app's authorization callback URL must
    # be {MEMEX_REMOTE_BASE_URL}/auth/callback.
    github_client_id: str | None = Field(default=None, alias="MEMEX_GITHUB_CLIENT_ID")
    github_client_secret: str | None = Field(default=None, alias="MEMEX_GITHUB_CLIENT_SECRET")
    # Comma-separated GitHub usernames allowed to access the remote MCP.
    # Any other GitHub account that completes the OAuth flow gets its token
    # rejected on every request (fail closed). Case-insensitive.
    remote_allowed_github_logins: str = Field(
        default="", alias="MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS"
    )

    log_level: str = Field(default="INFO", alias="MEMEX_LOG_LEVEL")

    # Multi-device sync (experimental). OFF by default so the feature adds no
    # new network surface for non-users. When on (and at least one peer is
    # paired), `memex serve` reconciles with each peer on startup and every
    # `sync_interval_seconds`, skipping a tick while an ingest holds the lock and
    # skipping any peer that is offline. The manual `memex sync` commands work
    # regardless of this flag.
    sync_auto: bool = Field(default=False, alias="MEMEX_SYNC_AUTO")
    sync_interval_seconds: int = Field(
        default=900, alias="MEMEX_SYNC_INTERVAL_SECONDS", ge=60, le=86_400
    )
    # Target byte size of one pull/push request when syncing. A conversation
    # carries its chunk vectors (~11 KB per chunk at dim 768), so batching by a
    # fixed conversation COUNT could build a body far past the peer's
    # `ingest_max_body_bytes` cap and get rejected (413 / broken pipe). The sync
    # client instead accumulates serialized records up to this budget, then
    # flushes, so every request stays under the cap. Kept comfortably below the
    # 16 MB default body cap; the client also never exceeds 80% of the peer's
    # advertised cap (see `sync.client._resolve_budget`).
    sync_max_batch_bytes: int = Field(
        default=8 * 1024 * 1024, alias="MEMEX_SYNC_MAX_BATCH_BYTES", ge=64 * 1024
    )

    # Auto-summaries with Claude Haiku. Opt-in: OFF by default to avoid
    # API calls during bulk ingest. Enable with MEMEX_SUMMARY_ENABLED=true
    # plus a set ANTHROPIC_API_KEY.
    summary_enabled: bool = Field(default=False, alias="MEMEX_SUMMARY_ENABLED")
    summary_model: str = Field(default="claude-haiku-4-5-20251001", alias="MEMEX_SUMMARY_MODEL")
    summary_max_tokens: int = Field(default=200, alias="MEMEX_SUMMARY_MAX_TOKENS", ge=32, le=2048)
    # API key uses the standard Anthropic name (no MEMEX_ prefix) so the
    # key already exported in the shell can be reused.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    @property
    def ingest_token_path(self) -> Path:
        """Where the per-install live-capture access token is stored.

        Sits next to the DB so it shares the same user-private directory. The
        Chrome extension must send this token as the `X-Memex-Token` header on
        `/ingest`; the Origin check alone does not authenticate non-browser
        local clients.
        """
        return self.db_path.parent / "ingest_token"

    @property
    def sync_peers_path(self) -> Path:
        """Where the multi-device sync peer registry is stored.

        A small JSON file next to the DB (user-only, 0600) holding each paired
        peer's address + access token (the shared secret used to authenticate
        to its `/sync/*` endpoints), the same sensitivity as `ingest_token`.
        """
        return self.db_path.parent / "sync_peers.json"

    @property
    def sync_state_path(self) -> Path:
        """Where the multi-device sync master gate (enabled flag) is stored.

        A small JSON file next to the DB. The whole feature is OFF by default;
        `memex sync enable`/`disable` are the only writers. The server reads it
        per `/sync/*` request so a toggle takes effect without a restart.
        """
        return self.db_path.parent / "sync_state.json"

    @property
    def sync_history_path(self) -> Path:
        """Where per-peer sync breadcrumbs (last time + counts) are stored.

        Kept apart from `sync_state_path` so a frequent history write (auto-sync
        every tick) never race-clobbers a concurrent `enable`/`disable`. Used
        only to render `memex sync status` without a network round-trip.
        """
        return self.db_path.parent / "sync_history.json"

    @field_validator("ollama_host")
    @classmethod
    def _warn_non_local_ollama_host(cls, value: str) -> str:
        """Warn (do not fail) when OLLAMA_HOST is malformed or non-loopback.

        Only relevant when MEMEX_EMBED_BACKEND=ollama, but validated
        unconditionally so the warning surfaces at startup. A non-local host
        means every indexed chunk of chat text is sent off-box; that should be
        a conscious choice, not a silent default.
        """
        parsed = urlparse(value)
        logger = logging.getLogger("memex.config")
        if parsed.scheme not in ("http", "https"):
            logger.warning(
                "OLLAMA_HOST %r has no http(s) scheme; the Ollama client may reject it.",
                value,
            )
            return value
        host = (parsed.hostname or "").lower()
        if host and host not in _LOOPBACK_HOSTS:
            logger.warning(
                "OLLAMA_HOST points to a non-local host (%s): all chat text will be "
                "sent there, in clear text over http unless https is used.",
                host,
            )
        return value


def get_settings() -> Settings:
    """Settings factory.

    Use in tests to inject overrides without touching the global singleton.
    """
    return Settings()


settings = get_settings()
