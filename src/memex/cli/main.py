"""CLI de Memex.

Comandos:
- `memex ingest <zip>`: ingesta un export oficial de Claude.ai.
- `memex search "<query>"`: búsqueda semántica sobre la base local.
- `memex stats`: estadísticas de qué hay indexado.

Se invoca como `uv run memex ...` o `memex ...` si el .venv está activado.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex.config import settings
from memex.core.embeddings import EmbedderError, get_default_embedder
from memex.core.ingest.pipeline import ingest_export
from memex.core.repos import find_repo_root, match_text, parse_repo, resolve_repo_key
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init

app = typer.Typer(
    help="Memex: indexa tus chats de Claude.ai para retrieval semántico.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def ingest(
    zip_path: Annotated[
        Path,
        typer.Argument(help="Path al export zip oficial de Claude.ai."),
    ],
    chunk_size: Annotated[
        int,
        typer.Option("--chunk-size", help="Tamaño de chunk en tokens."),
    ] = 0,
    chunk_overlap: Annotated[
        int,
        typer.Option("--chunk-overlap", help="Overlap entre chunks en tokens."),
    ] = -1,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite. Default: MEMEX_DB_PATH."),
    ] = None,
) -> None:
    """Ingesta un export oficial de Claude.ai a la base local."""
    if not zip_path.exists():
        console.print(f"[red]Export no encontrado: {zip_path}[/red]")
        raise typer.Exit(code=1)

    cs = chunk_size if chunk_size > 0 else settings.chunk_size
    co = chunk_overlap if chunk_overlap >= 0 else settings.chunk_overlap

    console.print(f"[bold]Ingestando[/bold] [cyan]{zip_path}[/cyan]")
    console.print(f"  chunk_size={cs} tokens, chunk_overlap={co} tokens")
    console.print(f"  DB: {db_path or settings.db_path}")

    conn = connect_and_init(db_path)
    try:
        embedder = get_default_embedder()
        console.print(f"  embedder: {settings.embed_backend} ({embedder.model_name})\n")
        with console.status("[yellow]Procesando…[/yellow]"):
            summary = ingest_export(
                conn,
                zip_path,
                embedder,
                chunk_size=cs,
                chunk_overlap=co,
            )
    except EmbedderError as e:
        console.print(f"[red]Error de embeddings:[/red] {e}")
        raise typer.Exit(code=2) from e
    finally:
        conn.close()

    table = Table(title="Ingest completo", show_header=False)
    table.add_column("Métrica", style="bold")
    table.add_column("Cantidad", justify="right")
    table.add_row("Projects", str(summary.projects))
    table.add_row("Conversaciones", str(summary.conversations))
    table.add_row("Mensajes", str(summary.messages))
    table.add_row("Chunks", str(summary.chunks))
    table.add_row("Mensajes vacíos saltados", str(summary.skipped_empty_messages))
    table.add_row("Errores", str(len(summary.errors)))
    console.print(table)

    if summary.errors:
        console.print("\n[yellow]Errores durante el ingest:[/yellow]")
        for err in summary.errors[:10]:
            console.print(f"  [dim]- {err}[/dim]")
        if len(summary.errors) > 10:
            console.print(f"  [dim]…y {len(summary.errors) - 10} más[/dim]")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Texto a buscar.")],
    limit: Annotated[
        int,
        typer.Option("-n", "--limit", help="Cantidad de resultados a devolver."),
    ] = 5,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Estrategia de búsqueda: hybrid (default), semantic, lexical.",
        ),
    ] = "hybrid",
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite."),
    ] = None,
) -> None:
    """Búsqueda sobre la base indexada (híbrida por default)."""
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
            console.print(
                f"[red]Mode inválido:[/red] {mode!r}. Válidos: hybrid, semantic, lexical."
            )
            raise typer.Exit(code=2)
    except EmbedderError as e:
        console.print(f"[red]Error de embeddings:[/red] {e}")
        raise typer.Exit(code=2) from e
    finally:
        conn.close()

    if not hits:
        console.print("[yellow]Sin resultados. ¿La base está vacía? Probá `memex stats`.[/yellow]")
        return

    console.print(f"\n[bold]Query:[/bold] [italic]{query}[/italic]  [dim](mode={mode})[/dim]")
    console.print(f"[dim]{len(hits)} resultados (más bajo = más relevante)[/dim]\n")
    for i, hit in enumerate(hits, 1):
        console.print(
            f"[bold cyan]#{i}[/bold cyan] [bold]{hit.conversation.title or '(sin título)'}[/bold]"
        )
        console.print(
            f"  [dim]uuid={hit.conversation.uuid[:8]}…  source={hit.conversation.source.value}  "
            f"dist={hit.distance:.4f}[/dim]"
        )
        if hit.conversation.summary:
            sm = hit.conversation.summary
            console.print(f"  [italic dim]{sm[:180]}{'…' if len(sm) > 180 else ''}[/italic dim]")
        console.print(f"  {hit.snippet}\n")


@app.command()
def stats(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite."),
    ] = None,
) -> None:
    """Muestra estadísticas de qué hay indexado."""
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

    table = Table(title="Estadísticas de la base", show_header=False)
    table.add_column("Métrica", style="bold")
    table.add_column("Cantidad", justify="right")
    table.add_row("Projects", str(n_projects))
    table.add_row("Conversaciones", str(n_convs))
    for row in by_source:
        table.add_row(f"  [dim]{row['source']}[/dim]", str(row["n"]))
    table.add_row("Mensajes", str(n_msgs))
    table.add_row("Chunks", str(n_chunks))
    console.print(table)


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Interfaz donde escuchar (127.0.0.1 recomendado)."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Puerto del HTTP server."),
    ] = 5777,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite (default: settings.db_path)."),
    ] = None,
) -> None:
    """Arranca el HTTP server local para captura en vivo desde la Chrome ext.

    El server escucha POSTs de `/ingest/conversation` que la Chrome ext envía
    cada vez que abrís un chat nuevo en claude.ai. Lo mantenés corriendo en
    una terminal (o como servicio del SO).

    Compartir la SQLite con el MCP server es seguro: ambos usan WAL mode.
    """
    import uvicorn

    from memex.core.storage.db import connect_and_init
    from memex.transports import http_ingest

    # Si el usuario pasó --db, inyectamos la conn antes de arrancar uvicorn.
    if db_path is not None:
        http_ingest._conn = connect_and_init(db_path, check_same_thread=False)

    console.print(f"[bold]Memex serve[/bold] escuchando en [cyan]http://{host}:{port}[/cyan]")
    console.print("Conectá la Chrome ext de Memex y empezá a usar claude.ai.")
    console.print("[dim]Ctrl+C para parar.[/dim]\n")
    uvicorn.run(http_ingest.app, host=host, port=port, log_level="info")


@app.command("reindex-fts")
def reindex_fts(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite."),
    ] = None,
) -> None:
    """Repuebla el índice FTS5 desde la tabla `chunks`.

    Útil para bases existentes que se crearon antes de tener `fts_chunks`,
    o si el índice se desincronizó por alguna razón. No re-llama a Ollama
    (no toca embeddings); solo copia el texto al índice lexical.
    """
    conn = connect_and_init(db_path)
    try:
        with console.status("[yellow]Re-indexando FTS…[/yellow]"):
            n = repo.rebuild_fts_index(conn)
        console.print(f"[green]FTS reconstruido:[/green] {n} chunks indexados.")
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
        with console.status(f"[yellow]Scanning {len(chats)} chats…[/yellow]"):
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
        assert info is not None, "resolve_repo_key returned a missing key"

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


if __name__ == "__main__":
    app()
