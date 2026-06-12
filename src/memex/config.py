"""Memex configuration.

Reads from environment variables and optionally from a `.env` file.
Default values are safe for local development.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    """Unified project settings.

    Env var names follow `.env.example`. Some use the `MEMEX_` prefix to
    avoid collisions, others (like OLLAMA_HOST) are the dependency's
    standard names and are respected as-is.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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

    db_path: Path = Field(default=Path("./data/memex.db"), alias="MEMEX_DB_PATH")
    exports_dir: Path = Field(default=Path("./data/exports"), alias="MEMEX_EXPORTS_DIR")

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
