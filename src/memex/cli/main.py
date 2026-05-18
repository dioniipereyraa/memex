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
from memex.core.embeddings.ollama import OllamaEmbedder
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
    console.print(f"  embedder: Ollama @ {settings.ollama_host} ({settings.embed_model})")
    console.print(f"  DB: {db_path or settings.db_path}\n")

    conn = connect_and_init(db_path)
    try:
        embedder = OllamaEmbedder()
        with console.status("[yellow]Procesando…[/yellow]"):
            summary = ingest_export(
                conn,
                zip_path,
                embedder,
                chunk_size=cs,
                chunk_overlap=co,
            )
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
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path a la base SQLite."),
    ] = None,
) -> None:
    """Búsqueda semántica sobre la base indexada."""
    conn = connect_and_init(db_path)
    try:
        embedder = OllamaEmbedder()
        query_vec = embedder.embed_one(query)
        hits = repo.vector_search(conn, query_vec, limit=limit)
    finally:
        conn.close()

    if not hits:
        console.print("[yellow]Sin resultados. ¿La base está vacía? Probá `memex stats`.[/yellow]")
        return

    console.print(f"\n[bold]Query:[/bold] [italic]{query}[/italic]")
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


def _scalar(conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


if __name__ == "__main__":
    app()
