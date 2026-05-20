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


if __name__ == "__main__":
    app()
