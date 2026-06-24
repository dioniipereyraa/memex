"""`memex sync` command group: sync conversations between paired devices.

Experimental. Devices pair once (`memex sync pair`), then:
- `pull`: one-directional, take the peer's version (peer authoritative).
- `push`: one-directional, send the local version (local authoritative).
- `reconcile`: two-way, last-writer-wins by `updated_at`, leaves both equal.

The peer must be running `memex serve` reachable from this device (e.g. bound to
its Tailscale address); the `/sync/*` endpoints are token-gated. Auto-sync on
`serve` startup + a sparse interval is opt-in (`MEMEX_SYNC_AUTO`); Phase 3 will
add `enable`/`disable`/`status`. The feature exposes nothing unless the user
pairs a peer AND deliberately binds `memex serve` beyond loopback.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex.core.embeddings import EmbedderError, get_default_embedder
from memex.core.storage.db import connect_and_init
from memex.sync import client, peers
from memex.sync.peers import Peer

sync_app = typer.Typer(
    help="Sync conversations between your devices (experimental).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@sync_app.command("pair")
def pair(
    name: Annotated[str, typer.Option("--name", help="A short label for the peer device.")],
    url: Annotated[
        str,
        typer.Option("--url", help="Peer base URL, e.g. http://100.x.y.z:5777 (Tailscale/LAN)."),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="The peer's access token (`memex token` on that device). Prompted if omitted.",
        ),
    ] = None,
) -> None:
    """Register a peer device to pull from (stored user-only, 0600)."""
    if not token:
        token = typer.prompt("Peer access token", hide_input=True)
    try:
        peer = Peer(name=name, url=url, token=token)
    except ValueError as e:
        console.print(f"[red]Invalid peer:[/red] {e}")
        raise typer.Exit(code=2) from e

    # Verify connectivity + token now so the user finds out at pairing time, not
    # at the first pull. Save the peer regardless (it may legitimately be
    # offline at pairing time).
    verified = False
    try:
        manifest = client.fetch_manifest(peer)
        verified = True
        n = len(manifest.get("conversations") or [])
        console.print(
            f"[green]Reached {name}[/green]: {n} conversations, "
            f"model {manifest.get('embed_model')} dim {manifest.get('embed_dim')}."
        )
    except urllib.error.HTTPError as e:
        console.print(
            f"[yellow]Saved, but the peer rejected the token[/yellow] (HTTP {e.code}). "
            "Check the token with `memex token` on the peer."
        )
    except (urllib.error.URLError, OSError) as e:
        console.print(
            f"[yellow]Saved, but could not reach the peer now[/yellow] ({e}). "
            "Make sure `memex serve` is running and reachable on the peer."
        )

    peers.add_peer(peer)
    console.print(f"Paired [bold]{name}[/bold] -> {peer.url}{' (verified)' if verified else ''}.")


@sync_app.command("peers")
def list_peers() -> None:
    """List paired devices (tokens are never printed)."""
    known = peers.load_peers()
    if not known:
        console.print("[yellow]No peers paired. Use `memex sync pair`.[/yellow]")
        return
    table = Table(title="Sync peers")
    table.add_column("Name", style="bold cyan")
    table.add_column("URL")
    for peer in known:
        table.add_row(peer.name, peer.url)
    console.print(table)


@sync_app.command("unpair")
def unpair(
    name: Annotated[str, typer.Argument(help="Name of the peer to remove.")],
) -> None:
    """Remove a paired device."""
    if peers.remove_peer(name):
        console.print(f"Removed peer [bold]{name}[/bold].")
    else:
        console.print(f"[yellow]No peer named {name!r}.[/yellow]")


def _resolve_targets(peer_name: str | None) -> list[Peer]:
    targets = peers.load_peers()
    if peer_name is not None:
        targets = [p for p in targets if p.name == peer_name]
        if not targets:
            console.print(f"[red]No peer named {peer_name!r}.[/red] See `memex sync peers`.")
            raise typer.Exit(code=2)
    if not targets:
        console.print("[yellow]No peers paired. Use `memex sync pair`.[/yellow]")
        raise typer.Exit(code=2)
    return targets


def _local_identity() -> tuple[str, int]:
    try:
        embedder = get_default_embedder()
    except EmbedderError as e:
        console.print(f"[red]Embedder error:[/red] {e}")
        raise typer.Exit(code=2) from e
    return embedder.model_name, embedder.dim


def _mismatch_line(peer_model: object, peer_dim: object, local_model: str, local_dim: int) -> None:
    console.print(
        f"  [red]Refused:[/red] embedding mismatch (peer {peer_model}/{peer_dim}, "
        f"local {local_model}/{local_dim}). Both devices must use the same embedding model."
    )


@sync_app.command("pull")
def pull(
    peer_name: Annotated[
        str | None,
        typer.Option("--peer", help="Pull from this peer only (default: all paired peers)."),
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Pull new or changed conversations from a paired device into this one (peer wins)."""
    targets = _resolve_targets(peer_name)
    local_model, local_dim = _local_identity()
    conn = connect_and_init(db_path)
    try:
        for peer in targets:
            console.print(f"\n[bold]Pulling from {peer.name}[/bold] ({peer.url})...")
            try:
                summary = client.pull(conn, peer, local_model=local_model, local_dim=local_dim)
            except (urllib.error.URLError, OSError) as e:
                console.print(f"  [yellow]Skipped:[/yellow] could not reach {peer.name} ({e}).")
                continue
            except ValueError as e:
                console.print(f"  [red]Bad response from {peer.name}:[/red] {e}")
                continue
            if summary.refused_mismatch:
                _mismatch_line(summary.peer_model, summary.peer_dim, local_model, local_dim)
                continue
            console.print(
                f"  Peer has {summary.manifest_total}, new/changed {summary.to_fetch}, "
                f"[green]inserted {summary.inserted}[/green]"
                + (f", [red]failed {summary.failed}[/red]" if summary.failed else "")
                + "."
            )
    finally:
        conn.close()


@sync_app.command("push")
def push(
    peer_name: Annotated[
        str | None,
        typer.Option("--peer", help="Push to this peer only (default: all paired peers)."),
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Push local conversations the peer is missing or has differently (local wins)."""
    targets = _resolve_targets(peer_name)
    local_model, local_dim = _local_identity()
    conn = connect_and_init(db_path)
    try:
        for peer in targets:
            console.print(f"\n[bold]Pushing to {peer.name}[/bold] ({peer.url})...")
            try:
                summary = client.push(conn, peer, local_model=local_model, local_dim=local_dim)
            except (urllib.error.URLError, OSError) as e:
                console.print(f"  [yellow]Skipped:[/yellow] could not reach {peer.name} ({e}).")
                continue
            except ValueError as e:
                console.print(f"  [red]Bad response from {peer.name}:[/red] {e}")
                continue
            if summary.refused_mismatch:
                _mismatch_line(summary.peer_model, summary.peer_dim, local_model, local_dim)
                continue
            console.print(
                f"  new/changed {summary.to_push}, [green]pushed {summary.pushed}[/green]"
                + (f", [red]failed {summary.failed}[/red]" if summary.failed else "")
                + "."
            )
    finally:
        conn.close()


@sync_app.command("reconcile")
def reconcile(
    peer_name: Annotated[
        str | None,
        typer.Option("--peer", help="Reconcile with this peer only (default: all paired peers)."),
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Two-way sync with a paired device, leaving both equal (last writer wins)."""
    targets = _resolve_targets(peer_name)
    local_model, local_dim = _local_identity()
    conn = connect_and_init(db_path)
    try:
        for peer in targets:
            console.print(f"\n[bold]Reconciling with {peer.name}[/bold] ({peer.url})...")
            try:
                summary = client.reconcile(conn, peer, local_model=local_model, local_dim=local_dim)
            except (urllib.error.URLError, OSError) as e:
                console.print(f"  [yellow]Skipped:[/yellow] could not reach {peer.name} ({e}).")
                continue
            except ValueError as e:
                console.print(f"  [red]Bad response from {peer.name}:[/red] {e}")
                continue
            if summary.refused_mismatch:
                _mismatch_line(local_model, local_dim, local_model, local_dim)
                continue
            console.print(
                f"  [green]pulled {summary.pulled}[/green], "
                f"[green]pushed {summary.pushed}[/green]"
                + (f", [red]failed {summary.failed}[/red]" if summary.failed else "")
                + "."
            )
    finally:
        conn.close()
