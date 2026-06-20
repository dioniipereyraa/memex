"""Memex CLI.

Commands:
- `memex ingest <zip>`: ingest an official Claude.ai export.
- `memex ingest-claude-code`: index local Claude Code / terminal sessions.
- `memex search "<query>"`: semantic search over the local DB.
- `memex stats`: show what is indexed.
- `memex serve`: run the local HTTP server for live capture.
- `memex serve-remote`: run the remote MCP server (HTTP + OAuth) for claude.ai.
- `memex reindex-fts`: rebuild the FTS5 index from `chunks`.
- `memex repos`: manage code repos that boost search results.
- `memex tag` / `memex untag`: manual chat-to-repo association.
- `memex session-context`: SessionStart hook helper for Claude Code.

Invoke as `uv run memex ...`, or `memex ...` if the .venv is active.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex.config import settings
from memex.core.embeddings import EmbedderError, get_default_embedder
from memex.core.embeddings.lazy import LazyEmbedder
from memex.core.ingest.pipeline import ingest_claude_code_sessions, ingest_export
from memex.core.repos import find_repo_root, match_text, parse_repo, resolve_repo_key
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.proctitle import set_process_title

app = typer.Typer(
    help="Memex: index your Claude.ai chats for semantic + lexical retrieval.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def ingest(
    zip_path: Annotated[
        Path,
        typer.Argument(help="Path to the official Claude.ai export zip."),
    ],
    chunk_size: Annotated[
        int,
        typer.Option("--chunk-size", help="Chunk size in tokens."),
    ] = 0,
    chunk_overlap: Annotated[
        int,
        typer.Option("--chunk-overlap", help="Overlap between chunks in tokens."),
    ] = -1,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database. Default: MEMEX_DB_PATH."),
    ] = None,
) -> None:
    """Ingest an official Claude.ai export into the local DB."""
    if not zip_path.exists():
        console.print(f"[red]Export not found: {zip_path}[/red]")
        raise typer.Exit(code=1)

    cs = chunk_size if chunk_size > 0 else settings.chunk_size
    co = chunk_overlap if chunk_overlap >= 0 else settings.chunk_overlap

    console.print(f"[bold]Ingesting[/bold] [cyan]{zip_path}[/cyan]")
    console.print(f"  chunk_size={cs} tokens, chunk_overlap={co} tokens")
    console.print(f"  DB: {db_path or settings.db_path}")

    conn = connect_and_init(db_path)
    try:
        embedder = get_default_embedder()
        console.print(f"  embedder: {settings.embed_backend} ({embedder.model_name})\n")
        with console.status("[yellow]Processing...[/yellow]"):
            summary = ingest_export(
                conn,
                zip_path,
                embedder,
                chunk_size=cs,
                chunk_overlap=co,
            )
    except EmbedderError as e:
        console.print(f"[red]Embedder error:[/red] {e}")
        raise typer.Exit(code=2) from e
    finally:
        conn.close()

    table = Table(title="Ingest complete", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Projects", str(summary.projects))
    table.add_row("Conversations", str(summary.conversations))
    table.add_row("Messages", str(summary.messages))
    table.add_row("Chunks", str(summary.chunks))
    table.add_row("Empty messages skipped", str(summary.skipped_empty_messages))
    table.add_row("Errors", str(len(summary.errors)))
    console.print(table)

    if summary.errors:
        console.print("\n[yellow]Errors during ingest:[/yellow]")
        for err in summary.errors[:10]:
            console.print(f"  [dim]- {err}[/dim]")
        if len(summary.errors) > 10:
            console.print(f"  [dim]...and {len(summary.errors) - 10} more[/dim]")


def _acquire_ingest_lock(db_path: Path) -> object | None:
    """Take a non-blocking, process-lifetime lock for Claude Code ingest.

    Returns an open file handle (keep it referenced for the lock to hold) or
    None if another ingest already holds it. Best-effort on platforms without
    `fcntl` (Windows): returns a handle without locking.
    """
    lock_path = db_path.parent / "ingest.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
    except OSError:
        return object()  # cannot create the lock file; do not block ingest
    try:
        import fcntl
    except ImportError:
        return handle  # no flock on this platform; best-effort
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


@app.command("ingest-claude-code")
def ingest_claude_code(
    path: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Root dir of session logs (default ~/.claude/projects), or a "
            "single .jsonl file to ingest just that session.",
        ),
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database. Default: MEMEX_DB_PATH."),
    ] = None,
) -> None:
    """Index local Claude Code / terminal sessions into the same store.

    Scans `~/.claude/projects/**/*.jsonl` (one file per session) and ingests
    them with source `claude_code`, so a single search covers claude.ai chats
    and Claude Code work alike. Incremental: unchanged sessions are skipped,
    so re-running is cheap. Sessions are auto-associated with the registered
    repo of their working directory (`memex repos add` to register one).

    Pass `--path <file.jsonl>` to ingest a single session (used by the
    SessionEnd hook). The embedding model is loaded lazily, so a scan that
    finds nothing new costs almost nothing.
    """
    set_process_title("Memex ingest")
    root = path if path is not None else Path.home() / ".claude" / "projects"

    # Single-ingest lock: the manual command, the 15-min schedule, and the
    # SessionEnd hook can otherwise overlap and each load the embedding model,
    # doubling/tripling RAM and CPU. A non-blocking flock means a second ingest
    # simply skips (the schedule re-runs in 15 min). The lock is released
    # automatically when the process exits, even on crash.
    lock_handle = _acquire_ingest_lock(db_path or settings.db_path)
    if lock_handle is None:
        console.print("[yellow]Another Memex ingest is already running; skipping.[/yellow]")
        raise typer.Exit(code=0)

    console.print(f"[bold]Ingesting Claude Code sessions[/bold] from [cyan]{root}[/cyan]")
    console.print(f"  DB: {db_path or settings.db_path}")

    conn = connect_and_init(db_path)
    try:
        # Lazy: the fastembed model only loads if there is something new to
        # embed, so the periodic backstop scan stays cheap when nothing changed.
        embedder = LazyEmbedder(get_default_embedder)
        console.print(f"  embedder: {settings.embed_backend} (loaded on demand)\n")
        with console.status("[yellow]Scanning sessions...[/yellow]"):
            summary = ingest_claude_code_sessions(conn, embedder, root)
    except EmbedderError as e:
        console.print(f"[red]Embedder error:[/red] {e}")
        raise typer.Exit(code=2) from e
    finally:
        conn.close()

    table = Table(title="Claude Code ingest complete", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Sessions ingested", str(summary.conversations))
    table.add_row("Unchanged (skipped)", str(summary.skipped_unchanged_conversations))
    table.add_row("Messages", str(summary.messages))
    table.add_row("Chunks", str(summary.chunks))
    table.add_row("Empty messages skipped", str(summary.skipped_empty_messages))
    table.add_row("Errors", str(len(summary.errors)))
    console.print(table)

    if summary.errors:
        console.print("\n[yellow]Errors during ingest:[/yellow]")
        for err in summary.errors[:10]:
            console.print(f"  [dim]- {err}[/dim]")
        if len(summary.errors) > 10:
            console.print(f"  [dim]...and {len(summary.errors) - 10} more[/dim]")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Text to search for.")],
    limit: Annotated[
        int,
        typer.Option("-n", "--limit", help="How many results to return."),
    ] = 5,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Search strategy: hybrid (default), semantic, lexical.",
        ),
    ] = "hybrid",
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Search the indexed corpus (hybrid by default)."""
    conn = connect_and_init(db_path)
    try:
        embedder = get_default_embedder()
        if mode == "lexical":
            hits = repo.text_search(conn, query, limit=limit)
        elif mode == "semantic":
            query_vec = embedder.embed_one(query)
            hits = repo.vector_search(conn, query_vec, limit=limit)
        elif mode == "hybrid":
            query_vec = embedder.embed_one(query)
            hits = repo.hybrid_search(conn, query, query_vec, limit=limit)
        else:
            console.print(f"[red]Invalid mode:[/red] {mode!r}. Valid: hybrid, semantic, lexical.")
            raise typer.Exit(code=2)
    except EmbedderError as e:
        console.print(f"[red]Embedder error:[/red] {e}")
        raise typer.Exit(code=2) from e
    finally:
        conn.close()

    if not hits:
        console.print("[yellow]No results. Is the DB empty? Try `memex stats`.[/yellow]")
        return

    console.print(f"\n[bold]Query:[/bold] [italic]{query}[/italic]  [dim](mode={mode})[/dim]")
    console.print(f"[dim]{len(hits)} results (lower distance = more relevant)[/dim]\n")
    for i, hit in enumerate(hits, 1):
        console.print(
            f"[bold cyan]#{i}[/bold cyan] [bold]{hit.conversation.title or '(no title)'}[/bold]"
        )
        console.print(
            f"  [dim]uuid={hit.conversation.uuid[:8]}...  source={hit.conversation.source.value}  "
            f"dist={hit.distance:.4f}[/dim]"
        )
        if hit.conversation.summary:
            sm = hit.conversation.summary
            console.print(f"  [italic dim]{sm[:180]}{'...' if len(sm) > 180 else ''}[/italic dim]")
        console.print(f"  {hit.snippet}\n")


@app.command()
def stats(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Show statistics of what is indexed."""
    conn = connect_and_init(db_path)
    try:
        n_projects = _scalar(conn, "SELECT COUNT(*) FROM projects")
        n_convs = _scalar(conn, "SELECT COUNT(*) FROM conversations")
        n_msgs = _scalar(conn, "SELECT COUNT(*) FROM messages")
        n_chunks = repo.count_chunks(conn)
        by_source = conn.execute(
            "SELECT source, COUNT(*) AS n FROM conversations GROUP BY source"
        ).fetchall()
    finally:
        conn.close()

    table = Table(title="Database stats", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Projects", str(n_projects))
    table.add_row("Conversations", str(n_convs))
    for row in by_source:
        table.add_row(f"  [dim]{row['source']}[/dim]", str(row["n"]))
    table.add_row("Messages", str(n_msgs))
    table.add_row("Chunks", str(n_chunks))
    console.print(table)


def _redirect_streams_if_headless() -> None:
    """If running without a console (Windows `pythonw`), reopen std streams.

    Under `pythonw.exe` (the interpreter the Windows logon Scheduled Task uses,
    to avoid a console window) `sys.stdout`/`sys.stderr` are `None`. Any
    `console.print(...)`, `isatty()` check, or uvicorn log write then raises and
    the server dies before it binds. Reopen the streams to a `serve.log` in the
    data dir so output goes somewhere valid (and Windows finally gets logs).
    No-op when a console is present.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = Path(settings.db_path).parent / "serve.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Interface to listen on (127.0.0.1 recommended)."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="HTTP server port."),
    ] = 5777,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database (default: settings.db_path)."),
    ] = None,
) -> None:
    """Start the local HTTP server for live capture from the Chrome ext.

    The server listens for POSTs to `/ingest/conversation` that the Chrome
    extension sends every time you open a new chat on claude.ai. Keep it
    running in a terminal (or as an OS service via `install-autostart.ps1`
    on Windows).

    Sharing the SQLite DB with the MCP server is safe: both use WAL mode.
    """
    # pythonw (the Windows logon task) has no console: reopen std streams to a
    # log file first, or the console output below crashes the server on None.
    _redirect_streams_if_headless()
    set_process_title("Memex capture")
    import uvicorn

    from memex.core.storage.db import connect_and_init
    from memex.transports import http_ingest

    # If `--db` was passed, inject the connection before uvicorn starts.
    if db_path is not None:
        http_ingest._conn = connect_and_init(db_path, check_same_thread=False)

    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]Warning:[/yellow] binding to [bold]{host}[/bold] exposes live "
            "capture beyond loopback. The access token is required, but treat the "
            "port as reachable by other machines on your network."
        )

    # Per-install access token. Required on /ingest; paste it into the Chrome
    # extension popup once to pair. Stored user-only next to the DB.
    token = http_ingest.load_or_create_ingest_token()
    http_ingest._token = token

    console.print(f"[bold]Memex serve[/bold] listening on [cyan]http://{host}:{port}[/cyan]")
    console.print("Connect the Memex Chrome ext and start using claude.ai.")
    # Only echo the token to an interactive terminal. Under a daemon (launchd)
    # stdout is redirected to a file that may be world-readable, so printing the
    # token there would leak it; direct non-interactive users to `memex token`.
    if sys.stdout is not None and sys.stdout.isatty():
        console.print("Pair the extension with this access token (paste it in the popup):")
        console.print(f"  [bold cyan]{token}[/bold cyan]")
    else:
        console.print("Run `memex token` in a terminal to get the pairing token.")
    console.print("[dim]Ctrl+C to stop.[/dim]\n")
    uvicorn.run(http_ingest.app, host=host, port=port, log_level="info")


@app.command("serve-remote")
def serve_remote(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Interface to listen on. Keep 127.0.0.1: the tunnel does the public part.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option("--port", "-p", help="Port (default: MEMEX_REMOTE_PORT, 8377)."),
    ] = None,
) -> None:
    """Start the remote MCP server (Streamable HTTP + OAuth) for claude.ai.

    Serves the same 4 tools as `memex-mcp`, but over HTTP at `/mcp`,
    protected by a GitHub OAuth allow-list. Expose it with a tunnel that
    publishes MEMEX_REMOTE_BASE_URL and proxies here (e.g. Tailscale
    Funnel), then add that URL as a custom connector in claude.ai.

    Requires in `.env`: MEMEX_REMOTE_BASE_URL, MEMEX_GITHUB_CLIENT_ID,
    MEMEX_GITHUB_CLIENT_SECRET, MEMEX_REMOTE_ALLOWED_GITHUB_LOGINS.
    """
    set_process_title("Memex connector")
    import uvicorn

    from memex.transports.http import RemoteConfigError, build_remote_app

    try:
        remote_app = build_remote_app()
    except RemoteConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None

    resolved_port = port if port is not None else settings.remote_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]Warning:[/yellow] binding to [bold]{host}[/bold] exposes the MCP "
            "server beyond loopback. OAuth still protects it, but the intended setup "
            "is loopback + tunnel."
        )

    console.print(
        f"[bold]Memex remote MCP[/bold] listening on [cyan]http://{host}:{resolved_port}/mcp[/cyan]"
    )
    console.print(f"Public URL (via tunnel): [cyan]{settings.remote_base_url}/mcp[/cyan]")
    console.print(
        f"Allowed GitHub users: [bold cyan]{settings.remote_allowed_github_logins}[/bold cyan]"
    )
    console.print("[dim]Ctrl+C to stop.[/dim]\n")
    uvicorn.run(remote_app, host=host, port=resolved_port, log_level="info")


@app.command("token")
def show_token() -> None:
    """Print the live-capture access token (paste it into the Chrome extension).

    The token authenticates the extension to the local `memex serve` endpoint.
    It is generated on first use and stored user-only next to the database
    (see `MEMEX_DB_PATH`). Re-run this any time you need to re-pair.
    """
    from memex.transports import http_ingest

    console.print(http_ingest.load_or_create_ingest_token())


@app.command("reindex-fts")
def reindex_fts(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Rebuild the FTS5 index from the `chunks` table.

    Useful for DBs created before `fts_chunks` existed, or if the index
    drifted out of sync. Does not re-call the embedder (chunks are
    untouched); only copies text into the lexical index.
    """
    conn = connect_and_init(db_path)
    try:
        with console.status("[yellow]Rebuilding FTS index...[/yellow]"):
            n = repo.rebuild_fts_index(conn)
        console.print(f"[green]FTS rebuilt:[/green] {n} chunks indexed.")
    finally:
        conn.close()


def _scalar(conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


# ---------- repos sub-app ----------

repos_app = typer.Typer(
    help="Register code repos so Memex can boost search results that touch them.",
    no_args_is_help=True,
)
app.add_typer(repos_app, name="repos")


@repos_app.command("add")
def repos_add(
    path: Annotated[
        Path,
        typer.Argument(
            help="Path to the repo directory. Reads .git/config and pyproject.toml/package.json/Cargo.toml."
        ),
    ],
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Register a repo so Memex can match chats against it.

    Memex reads the repo's git remote (if any) and manifest name to build
    a stable key. Re-running `add` on the same repo refreshes its fields
    without losing existing associations.
    """
    try:
        info = parse_repo(path)
    except (FileNotFoundError, NotADirectoryError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e

    conn = connect_and_init(db_path)
    try:
        existed = repo.get_repo(conn, info.key) is not None
        repo.insert_repo(conn, info)
        conn.commit()
    finally:
        conn.close()

    verb = "Updated" if existed else "Registered"
    console.print(f"[green]{verb}[/green] [bold]{info.name}[/bold]  [dim]({info.key})[/dim]")
    if info.remote_url:
        console.print(f"  remote: [cyan]{info.remote_url}[/cyan]")
    if info.path:
        console.print(f"  path:   [dim]{info.path}[/dim]")
    if info.manifest_name and info.manifest_name != info.name:
        console.print(f"  manifest name: [dim]{info.manifest_name}[/dim]")
    console.print(
        "\n[dim]Tip:[/dim] run [bold]memex repos scan[/bold] to associate this repo "
        "with chats already in the DB."
    )


@repos_app.command("list")
def repos_list(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """List registered repos."""
    conn = connect_and_init(db_path)
    try:
        repos_in_db = repo.list_repos(conn)
    finally:
        conn.close()

    if not repos_in_db:
        console.print(
            "[yellow]No repos registered yet.[/yellow] Use "
            "[bold]memex repos add <path>[/bold] to register one."
        )
        return

    table = Table(title=f"Registered repos ({len(repos_in_db)})")
    table.add_column("Key", style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Remote", overflow="fold")
    table.add_column("Path", style="dim", overflow="fold")
    for info in repos_in_db:
        table.add_row(
            info.key,
            info.name,
            info.remote_url or "[dim]-[/dim]",
            info.path or "[dim]-[/dim]",
        )
    console.print(table)


@repos_app.command("remove")
def repos_remove(
    key: Annotated[str, typer.Argument(help="Canonical repo key (see `memex repos list`).")],
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Remove a registered repo. Cascades and removes its chat associations."""
    conn = connect_and_init(db_path)
    try:
        removed = repo.delete_repo(conn, key)
        conn.commit()
    finally:
        conn.close()

    if removed:
        console.print(f"[green]Removed[/green] [bold]{key}[/bold]")
    else:
        console.print(f"[yellow]No repo with key[/yellow] [bold]{key}[/bold]")
        raise typer.Exit(code=1)


@repos_app.command("scan")
def repos_scan(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Re-scan every conversation against every registered repo.

    The auto-scan runs at ingest time; this command is for re-running it
    over an existing corpus (after registering new repos, or after
    tweaking the matcher). Manual tags are preserved.
    """
    conn = connect_and_init(db_path)
    try:
        repos_in_db = repo.list_repos(conn)
        if not repos_in_db:
            console.print(
                "[yellow]No repos registered.[/yellow] Use "
                "[bold]memex repos add <path>[/bold] first."
            )
            return

        chats = conn.execute("SELECT uuid, title FROM conversations").fetchall()
        if not chats:
            console.print("[yellow]No conversations in DB yet.[/yellow]")
            return

        new_or_refreshed = 0
        scanned = 0
        with console.status(f"[yellow]Scanning {len(chats)} chats...[/yellow]"):
            for chat in chats:
                text = repo.get_conversation_text(conn, chat["uuid"])
                if not text:
                    continue
                scanned += 1
                matches = match_text(text, repos_in_db)
                for m in matches:
                    repo.associate_chat_repo(
                        conn,
                        chat["uuid"],
                        m.repo_key,
                        source="auto",
                        confidence=m.confidence,
                    )
                    new_or_refreshed += 1
        conn.commit()
    finally:
        conn.close()

    console.print(
        f"[green]Scanned[/green] {scanned} chats. "
        f"[green]Applied[/green] {new_or_refreshed} associations "
        f"across {len(repos_in_db)} repo(s)."
    )


# ---------- chat-to-repo manual tagging ----------


@app.command("tag")
def tag(
    chat_uuid: Annotated[str, typer.Argument(help="Conversation UUID.")],
    repo_key: Annotated[str, typer.Argument(help="Repo key (see `memex repos list`).")],
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Manually associate a chat with a repo.

    A manual tag is sticky: subsequent auto-scans will not overwrite it.
    Use `memex untag` to remove.
    """
    conn = connect_and_init(db_path)
    try:
        # Validate both ids exist for a clearer error message.
        if repo.get_conversation(conn, chat_uuid) is None:
            console.print(f"[red]Conversation not found:[/red] {chat_uuid}")
            raise typer.Exit(code=1)
        if repo.get_repo(conn, repo_key) is None:
            console.print(
                f"[red]Repo not found:[/red] {repo_key}. "
                "Run [bold]memex repos list[/bold] to see registered repos."
            )
            raise typer.Exit(code=1)

        repo.associate_chat_repo(conn, chat_uuid, repo_key, source="manual")
        conn.commit()
    finally:
        conn.close()

    console.print(
        f"[green]Tagged[/green] {chat_uuid[:8]}...  ->  [bold]{repo_key}[/bold]  (manual)"
    )


@app.command("untag")
def untag(
    chat_uuid: Annotated[str, typer.Argument(help="Conversation UUID.")],
    repo_key: Annotated[str, typer.Argument(help="Repo key.")],
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Remove a chat-to-repo association (manual or auto)."""
    conn = connect_and_init(db_path)
    try:
        removed = repo.dissociate_chat_repo(conn, chat_uuid, repo_key)
        conn.commit()
    finally:
        conn.close()

    if removed:
        console.print(f"[green]Untagged[/green] {chat_uuid[:8]}...  -x->  [bold]{repo_key}[/bold]")
    else:
        console.print(
            f"[yellow]No association found between[/yellow] {chat_uuid[:8]}...  and  {repo_key}"
        )
        raise typer.Exit(code=1)


# ---------- SessionStart hook helper ----------


@app.command("session-context")
def session_context(
    repo_arg: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Repo path/URL/key. If omitted, auto-detects from cwd by walking up to find .git.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Max chats to include (default 5)."),
    ] = 5,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Print a context blob for Claude Code's `SessionStart` hook.

    Designed to be wired into `.claude/settings.json` so Claude Code injects
    Memex context at the start of every session. Detects the active repo
    from cwd (walks up looking for `.git`), looks up associated chats, and
    prints a short Markdown blob to stdout. If no repo is detected, no repo
    is registered, or no associations exist, prints nothing (the hook
    becomes a no-op, no context noise).

    Settings.json snippet (example):

    ```json
    {
      "hooks": {
        "SessionStart": [{
          "command": "uv run memex session-context"
        }]
      }
    }
    ```

    Output is plain text on stdout. All status/error chatter goes to stderr
    so it does not contaminate the injected context.
    """
    import sys

    def _stderr(msg: str) -> None:
        print(msg, file=sys.stderr)

    # Resolve repo: explicit arg first, then auto-detect from cwd.
    if repo_arg is None:
        root = find_repo_root(Path.cwd())
        if root is None:
            _stderr("[memex] No .git found walking up from cwd. Skipping context.")
            return
        repo_arg = str(root)

    conn = connect_and_init(db_path)
    try:
        key = resolve_repo_key(conn, repo_arg)
        if key is None:
            _stderr(
                f"[memex] Repo {repo_arg!r} is not registered. "
                "Run `memex repos add <path>` to register it."
            )
            return

        info = repo.get_repo(conn, key)
        if info is None:
            # Race: resolve_repo_key found it, then it got removed before
            # get_repo. Treat as not-registered.
            _stderr(f"[memex] Repo with key {key!r} disappeared mid-lookup.")
            return

        # Manual associations first, then auto by confidence descending.
        rows = repo.list_conversations_for_repo(conn, key)
        if not rows:
            _stderr(
                f"[memex] No chats associated to {info.name!r} yet. "
                "Run `memex repos scan` to associate existing chats."
            )
            return

        rows.sort(
            key=lambda r: (
                0 if r[1] == "manual" else 1,
                -(r[2] if r[2] is not None else 0.0),
            )
        )
        rows = rows[: max(1, limit)]

        # Hydrate each row with the conversation metadata.
        items: list[tuple[str, str, str | None, str, float | None]] = []
        for uuid, source_kind, conf in rows:
            conv = repo.get_conversation(conn, uuid)
            if conv is None:
                continue
            items.append((uuid, conv.title, conv.summary, source_kind, conf))
    finally:
        conn.close()

    if not items:
        return

    # Markdown blob on stdout. Claude Code injects this verbatim into the
    # session context, so we keep it terse and clearly Memex-attributed.
    print(f"## Memex: recent chats related to `{info.name}`\n")
    print(
        f"Repo: `{info.key}`. {len(items)} chat(s) associated. "
        "Use `search_chats(query, repo=...)` for deeper retrieval.\n"
    )
    for uuid, title, summary, source_kind, conf in items:
        marker = "[manual]" if source_kind == "manual" else f"[auto {conf:.2f}]"
        print(f"- **{title}** {marker}")
        print(f"  uuid: `{uuid}`")
        if summary:
            short = summary if len(summary) <= 280 else summary[:277] + "..."
            print(f"  summary: {short}")
        print()


# ---------- cross-platform autostart ----------


@app.command("install-service")
def install_service(
    action: Annotated[
        str,
        typer.Argument(
            help="Action: install, uninstall, or status.",
        ),
    ] = "install",
    remote: Annotated[
        bool,
        typer.Option(
            "--remote",
            help="(macOS) also manage the claude.ai remote connector agent. It "
            "needs the MEMEX_REMOTE_* config in .env, or it will crash-loop.",
        ),
    ] = False,
) -> None:
    """Register Memex live-capture as an OS service so it starts on login.

    Cross-platform dispatcher that calls the right installer for the host OS:
    Windows (Scheduled Task via PowerShell), Linux (systemd user unit), and
    macOS (launchd agents). On macOS it manages the live-capture server plus
    the 15-minute Claude Code ingest backstop; pass --remote to also manage
    the claude.ai connector.

    Examples:
      memex install-service           # default: install
      memex install-service status    # show current state
      memex install-service uninstall # remove the service
    """
    if action not in ("install", "uninstall", "status"):
        console.print(f"[red]Invalid action:[/red] {action!r}. Use install, uninstall, or status.")
        raise typer.Exit(code=2)
    raise typer.Exit(code=_run_install_service(action, remote=remote))


def _run_install_service(action: str, remote: bool = False) -> int:
    """Dispatch an install-service action to the host OS; return an exit code.

    Shared by the `install-service` command and `setup`. From a cloned repo it
    uses the proven repo paths (Windows/Linux shell scripts, macOS plist
    templates). From a wheel/PyPI install (no `scripts/`), `cli.services`
    generates self-contained definitions that run the installed CLI directly: a
    launchd plist (macOS), a systemd user unit (Linux), or a logon Scheduled
    Task (Windows). `remote` (the claude.ai connector agent) is macOS-only.
    """
    import contextlib
    import platform
    import shutil
    import subprocess
    import sys as _sys

    from memex.cli import services

    repo_root = services.source_repo_root()
    scripts_dir = (repo_root / "scripts") if repo_root else None
    system = platform.system()

    if system == "Darwin":
        svc_list = list(services.DEFAULT_SERVICES)
        if remote:
            svc_list.append(services.REMOTE)
        try:
            if action == "install":
                lines = (
                    services.macos_install(repo_root, svc_list)
                    if repo_root
                    else services.macos_install_wheel(svc_list)
                )
            elif action == "uninstall":
                lines = services.macos_uninstall(svc_list)
            else:
                lines = services.macos_status(svc_list)
        except (OSError, ValueError) as e:
            console.print(f"[red]Service {action} failed:[/red] {e}")
            return 1
        labels = ", ".join(services.AGENT_LABELS[s] for s in svc_list)
        console.print(f"[bold]launchd {action}[/bold] ({labels}):")
        for line in lines:
            console.print(f"  {line}")
        if action == "install":
            logs = (repo_root / "data") if repo_root else services.data_dir()
            console.print(
                "\nThe agents start with your Mac and restart if they crash. "
                f"Logs in [cyan]{logs}[/cyan]."
            )
        return 0

    if system == "Linux":
        if remote:
            console.print(
                "[yellow]Note:[/yellow] --remote is macOS-only; the systemd unit "
                "manages the live-capture server only."
            )
        if repo_root is None:
            try:
                lines = services.linux_install_wheel(action)
            except OSError as e:
                console.print(f"[red]Service {action} failed:[/red] {e}")
                return 1
            console.print(f"[bold]systemd --user {action}[/bold]:")
            for line in lines:
                console.print(f"  {line}")
            return 0
        sh = scripts_dir / "install-autostart.sh"
        if not sh.is_file():
            console.print(f"[red]Missing installer script:[/red] {sh}")
            return 1
        # Make sure the script is executable (matters on fresh clones). Best
        # effort: if chmod fails, bash still runs it via the explicit invocation.
        with contextlib.suppress(OSError):
            sh.chmod(sh.stat().st_mode | 0o111)
        bash = shutil.which("bash") or "/bin/bash"
        result = subprocess.run([bash, str(sh), action], check=False)
        return result.returncode

    if system == "Windows":
        if remote:
            console.print(
                "[yellow]Note:[/yellow] --remote is macOS-only; the Windows task "
                "manages the live-capture server only."
            )
        if repo_root is None:
            try:
                lines = services.windows_install_wheel(action)
            except OSError as e:
                console.print(f"[red]Service {action} failed:[/red] {e}")
                return 1
            console.print(f"[bold]Scheduled Task {action}[/bold] ({services.WINDOWS_TASK_NAME}):")
            for line in lines:
                console.print(f"  {line}")
            return 1 if any(line.startswith("FAILED") for line in lines) else 0
        ps1 = scripts_dir / "install-autostart.ps1"
        if not ps1.is_file():
            console.print(f"[red]Missing installer script:[/red] {ps1}")
            return 1
        flag = {"install": "-Install", "uninstall": "-Uninstall", "status": "-Status"}[action]
        powershell = shutil.which("powershell") or "powershell.exe"
        result = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), flag],
            check=False,
        )
        return result.returncode

    console.print(
        f"[red]Unsupported platform:[/red] {system!r} ({_sys.platform}). "
        "Memex install-service supports Windows, Linux, and macOS."
    )
    return 2


# ---------- one-command setup ----------

# Chrome Web Store listing for the live-capture extension. Surfaced by `setup`
# as the one manual step left (a browser extension cannot be installed from a
# terminal).
_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/"
    "memex-live-capture/bncngnabecfilefblppkolhdnaelibnb"
)


def _setup_mcp() -> tuple[str, str, str]:
    """Register the stdio MCP server with Claude Code (user scope).

    Idempotent: if a `memex` server is already registered, it is left as is.
    If the `claude` CLI is not on PATH, prints the manual command and reports a
    WARN (nothing else in setup depends on it). Returns a (step, status,
    detail) row for the summary table.
    """
    import shutil
    import subprocess

    from memex.cli import services

    root = services.source_repo_root()
    if root is not None:
        # Cloned repo: run through uv so the pinned .venv is used.
        invocation = ["uv", "run", "--directory", str(root), "memex-mcp"]
    else:
        # Wheel install (PyPI/uvx): the console script is on PATH.
        invocation = ["memex-mcp"]

    claude = shutil.which("claude")
    if claude is None:
        console.print(
            "[yellow]`claude` CLI not found.[/yellow] Register the MCP server manually:"
        )
        console.print(f"  claude mcp add --scope user memex -- {' '.join(invocation)}")
        return ("MCP server", "WARN", "claude CLI not found; manual command printed")

    listed = subprocess.run(
        [claude, "mcp", "list"], check=False, capture_output=True, text=True
    )
    if listed.returncode == 0 and "memex" in listed.stdout:
        return ("MCP server", "OK", "already registered")

    added = subprocess.run(
        [claude, "mcp", "add", "--scope", "user", "memex", "--", *invocation],
        check=False,
        capture_output=True,
        text=True,
    )
    if added.returncode == 0:
        return ("MCP server", "OK", "registered (user scope)")
    detail = (added.stderr or added.stdout or "claude mcp add failed").strip()
    return ("MCP server", "WARN", detail[:80])


def _setup_ingest() -> tuple[str, str, str]:
    """Index local Claude Code sessions so search has content immediately.

    Incremental and lazy (unchanged sessions skipped, the model loads only if
    there is something new), so re-running setup is cheap. Never aborts setup:
    any failure is reported as a WARN row.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return ("Claude Code index", "WARN", f"{root} not found; nothing to index yet")
    try:
        conn = connect_and_init(None)
        try:
            embedder = LazyEmbedder(get_default_embedder)
            with console.status(
                "[yellow]Indexing local Claude Code sessions "
                "(first run downloads the embedding model)...[/yellow]"
            ):
                summary = ingest_claude_code_sessions(conn, embedder, root)
        finally:
            conn.close()
    except Exception as e:
        # Setup must never crash on ingest; report it as a warning instead.
        return ("Claude Code index", "WARN", f"{type(e).__name__}: {e}")
    detail = (
        f"{summary.conversations} new, "
        f"{summary.skipped_unchanged_conversations} unchanged"
    )
    return ("Claude Code index", "OK", detail)


@app.command("setup")
def setup(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Accept defaults and skip the confirmation prompt."),
    ] = False,
    mcp: Annotated[
        bool,
        typer.Option("--mcp/--no-mcp", help="Register the MCP server with Claude Code."),
    ] = True,
    autostart: Annotated[
        bool,
        typer.Option(
            "--autostart/--no-autostart",
            help="Install the always-on live-capture service.",
        ),
    ] = True,
    ingest: Annotated[
        bool,
        typer.Option(
            "--ingest/--no-ingest", help="Index your local Claude Code sessions now."
        ),
    ] = True,
    remote: Annotated[
        bool,
        typer.Option(
            "--remote",
            help="Also install the claude.ai remote connector agent (needs .env config).",
        ),
    ] = False,
) -> None:
    """Wire Memex into Claude Code end to end, in one command.

    Registers the MCP server, installs the always-on live-capture service,
    indexes your local Claude Code sessions, and prints the pairing token plus
    the Chrome extension steps. Every step is idempotent, so this is safe to
    re-run. Each step degrades to a warning instead of aborting the rest, so a
    missing `claude` CLI or a non-repo install still gets you as far as it can.
    """
    from memex.transports import http_ingest

    console.print("[bold]Memex setup[/bold]\n")
    planned = []
    if mcp:
        planned.append("register the MCP server with Claude Code")
    if autostart:
        planned.append("install the always-on live-capture service")
    if ingest:
        planned.append("index your local Claude Code sessions")
    planned.append("show the Chrome extension pairing token")
    console.print("This will:")
    for item in planned:
        console.print(f"  - {item}")
    console.print()
    if not yes and not typer.confirm("Proceed?", default=True):
        raise typer.Exit(code=0)

    results: list[tuple[str, str, str]] = []

    # 1. Embedder health (cheap: the model itself loads lazily on first embed).
    try:
        embedder = get_default_embedder()
        results.append(("Embedder", "OK", f"{settings.embed_backend} ({embedder.model_name})"))
    except Exception as e:
        # Report, never crash setup.
        results.append(("Embedder", "WARN", f"{type(e).__name__}: {e}"))

    # 2. MCP wiring.
    if mcp:
        results.append(_setup_mcp())

    # 3. Autostart service.
    if autostart:
        code = _run_install_service("install", remote=remote)
        results.append(
            (
                "Autostart",
                "OK" if code == 0 else "WARN",
                "installed" if code == 0 else "see messages above",
            )
        )

    # 4. Index local Claude Code sessions.
    if ingest:
        results.append(_setup_ingest())

    # 5. Pairing token (created on first call if missing).
    token = http_ingest.load_or_create_ingest_token()

    table = Table(title="Memex setup", show_header=True, header_style="bold")
    table.add_column("Step", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    status_style = {
        "OK": "[green]OK[/green]",
        "WARN": "[yellow]WARN[/yellow]",
        "FAIL": "[red]FAIL[/red]",
    }
    for name, status, detail in results:
        table.add_row(name, status_style[status], detail)
    console.print(table)

    console.print(
        "\n[bold]Last step: the Chrome extension[/bold] (captures your claude.ai chats):"
    )
    console.print(f"  1. Install it: [cyan]{_EXTENSION_URL}[/cyan]")
    console.print("  2. Open the extension popup and paste this access token:")
    console.print(f"     [bold cyan]{token}[/bold cyan]")
    console.print(
        "\n[dim]Re-run `memex token` to see the token again, or `memex doctor` to "
        "check everything is healthy.[/dim]"
    )


# ---------- diagnostics ----------


@app.command("doctor")
def doctor(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Check Memex setup: DB, embedder, local server, summarizer, repos.

    Run this when something is not working as expected. Each check reports
    OK / WARN / FAIL with a one-line detail. Exit code is 0 unless at least
    one check is FAIL (then exit 1), so this is safe to use in scripts.
    """
    import sys as _sys

    checks: list[tuple[str, str, str]] = []  # (name, status, detail)
    has_fail = False

    # 1. Python version.
    py = _sys.version_info
    if (py.major, py.minor) >= (3, 12):
        checks.append(("Python", "OK", f"{py.major}.{py.minor}.{py.micro}"))
    else:
        checks.append(
            (
                "Python",
                "FAIL",
                f"{py.major}.{py.minor} (need 3.12+)",
            )
        )
        has_fail = True

    # 2. Database.
    target_db = db_path or settings.db_path
    if not Path(target_db).exists():
        checks.append(
            (
                "Database",
                "WARN",
                f"{target_db} does not exist yet. Run `memex ingest <export.zip>` to create.",
            )
        )
    else:
        try:
            conn = connect_and_init(db_path)
            try:
                version_row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'version'"
                ).fetchone()
                version = version_row[0] if version_row else "unknown"
                checks.append(("Database", "OK", f"{target_db} (schema v{version})"))
            finally:
                conn.close()
        except Exception as e:
            checks.append(("Database", "FAIL", f"could not open {target_db}: {e}"))
            has_fail = True

    # 3. Embedder.
    try:
        embedder = get_default_embedder()
        checks.append(("Embedder", "OK", f"{settings.embed_backend} ({embedder.model_name})"))
    except EmbedderError as e:
        checks.append(("Embedder", "FAIL", str(e)))
        has_fail = True
    except Exception as e:
        checks.append(("Embedder", "FAIL", f"{type(e).__name__}: {e}"))
        has_fail = True

    # 4. Local HTTP server (memex serve).
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:5777/health", timeout=1.5) as response:
            if response.status == 200:
                checks.append(("Live capture server", "OK", "http://127.0.0.1:5777"))
            else:
                checks.append(
                    (
                        "Live capture server",
                        "WARN",
                        f"unexpected status {response.status}",
                    )
                )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        checks.append(
            (
                "Live capture server",
                "WARN",
                f"not running. Start with `memex serve` for live capture. ({type(e).__name__})",
            )
        )

    # 5. Summarizer config (if enabled).
    if settings.summary_enabled:
        if not settings.anthropic_api_key:
            checks.append(
                (
                    "Summarizer",
                    "FAIL",
                    "MEMEX_SUMMARY_ENABLED=true but ANTHROPIC_API_KEY missing.",
                )
            )
            has_fail = True
        else:
            try:
                import anthropic  # noqa: F401

                checks.append(("Summarizer", "OK", f"enabled, model={settings.summary_model}"))
            except ImportError:
                checks.append(
                    (
                        "Summarizer",
                        "FAIL",
                        "`anthropic` SDK not installed. Run `uv sync --extra summaries`.",
                    )
                )
                has_fail = True
    else:
        checks.append(("Summarizer", "OK", "disabled (MEMEX_SUMMARY_ENABLED=false)"))

    # 6. Repos registered (info, never fails).
    if Path(target_db).exists():
        try:
            conn = connect_and_init(db_path)
            try:
                n_repos = _scalar(conn, "SELECT COUNT(*) FROM repos")
                n_convs = _scalar(conn, "SELECT COUNT(*) FROM conversations")
            finally:
                conn.close()
            if n_repos == 0:
                checks.append(
                    (
                        "Repos",
                        "WARN",
                        "0 registered. Run `memex repos add <path>` to enable repo boost.",
                    )
                )
            else:
                checks.append(("Repos", "OK", f"{n_repos} registered"))
            if n_convs == 0:
                checks.append(
                    (
                        "Corpus",
                        "WARN",
                        "0 conversations indexed. Run `memex ingest <export.zip>`.",
                    )
                )
            else:
                checks.append(("Corpus", "OK", f"{n_convs} conversations indexed"))
        except Exception:
            # DB exists but failed to read; the Database check above already
            # flagged it.
            pass

    # Render.
    table = Table(title="Memex doctor", show_header=True, header_style="bold")
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    status_style = {
        "OK": "[green]OK[/green]",
        "WARN": "[yellow]WARN[/yellow]",
        "FAIL": "[red]FAIL[/red]",
    }
    for name, status, detail in checks:
        table.add_row(name, status_style[status], detail)
    console.print(table)

    if has_fail:
        console.print(
            "\n[red]One or more checks failed.[/red] Fix the FAIL items above and re-run."
        )
        raise typer.Exit(code=1)
    n_warns = sum(1 for _, s, _ in checks if s == "WARN")
    if n_warns:
        console.print(
            f"\n[yellow]{n_warns} warning(s).[/yellow] Memex will work, but some "
            "features may be disabled until you address them."
        )
    else:
        console.print("\n[green]All checks passed.[/green]")


if __name__ == "__main__":
    app()
