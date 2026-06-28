"""`memex sync` command group: sync conversations between paired devices.

The whole feature is OFF by default (`memex sync enable` turns it on). Once on,
devices pair once (`memex sync pair`), then:
- `pull`: one-directional, take the peer's version (peer authoritative).
- `push`: one-directional, send the local version (local authoritative).
- `reconcile`: two-way, last-writer-wins by `updated_at`, leaves both equal.

`enable`/`disable` flip the master gate; `status` shows the current state, the
local embedding identity, and each peer's last sync. The peer must be running
`memex serve` reachable from this device (e.g. bound to its Tailscale address);
the `/sync/*` endpoints are token-gated and 404 while sync is disabled. Auto-sync
on `serve` startup + a sparse interval is opt-in (`MEMEX_SYNC_AUTO`, and only
while enabled). Even enabled, the feature exposes nothing unless the user
deliberately binds `memex serve` beyond loopback.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from memex.config import settings
from memex.core.embeddings import EmbedderError, get_default_embedder
from memex.core.storage.db import connect_and_init
from memex.sync import client, peers
from memex.sync import state as sync_state
from memex.sync.peers import Peer

sync_app = typer.Typer(
    help="Sync conversations between your devices.",
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


@sync_app.command("enable")
def enable() -> None:
    """Turn multi-device sync on (it is OFF by default)."""
    sync_state.set_enabled(True)
    console.print("[green]Sync enabled.[/green]")
    console.print(
        "  Pair a device with `memex sync pair`, then `memex sync reconcile`.\n"
        "  The /sync endpoints stay loopback-only until you also run "
        "`memex serve --host <addr>` and add that host to MEMEX_INGEST_ALLOWED_HOSTS."
    )


@sync_app.command("disable")
def disable() -> None:
    """Turn multi-device sync off (the /sync endpoints 404, data commands refuse)."""
    sync_state.set_enabled(False)
    console.print(
        "Sync [bold]disabled[/bold]. Paired peers are kept; re-enable with `memex sync enable`."
    )


def _detect_tailscale_ip() -> str | None:
    """Best-effort Tailscale IPv4 via the tailscale CLI. None if unavailable.

    Works the same on macOS / Linux / Windows (the CLI is `tailscale` /
    `tailscale.exe` on PATH), so `sync serve` needs no per-OS address dance.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _merge_hosts(existing: str, addr: str) -> str:
    """Add `addr` to a comma-separated Host allow-list, de-duplicated."""
    hosts = [h.strip() for h in existing.split(",") if h.strip()]
    if addr not in hosts:
        hosts.append(addr)
    return ",".join(hosts)


@sync_app.command("serve")
def serve(
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Address the other device dials (e.g. your Tailscale IP). "
            "Auto-detected from Tailscale if omitted.",
        ),
    ] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Port to serve on.")] = 5777,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = None,
) -> None:
    """Serve the sync endpoints reachable from a paired device, set up in one step.

    Unlike plain `memex serve`, this resolves a reachable address, binds to it AND
    adds it to the Host allow-list (the usual two-step footgun: `--host` only binds,
    the allow-list is separate), enables sync if needed, and prints the exact
    `memex sync pair` line to run on the other device. It binds to that address
    only (not all interfaces), so it runs alongside your always-on loopback capture
    server without a port clash. Keep it up while the other device reconciles.
    """
    import uvicorn

    from memex.transports import http_ingest

    addr = host or _detect_tailscale_ip()
    if not addr:
        console.print(
            "[red]Could not detect a reachable address.[/red] Pass [bold]--host <ip>[/bold] "
            "(e.g. your Tailscale IP from `tailscale ip -4`)."
        )
        raise typer.Exit(code=2)

    # Master gate: the /sync endpoints 404 until enabled. Running this command is an
    # explicit sync intent, so flip it on (and say so) rather than fail closed.
    if not sync_state.is_enabled():
        sync_state.set_enabled(True)
        console.print("[green]Sync enabled.[/green]")

    # Auto-allow-list the dial address, then rebuild the app so the TrustedHost
    # middleware picks it up (plain `serve` only binds; this removes the footgun).
    settings.ingest_allowed_hosts = _merge_hosts(settings.ingest_allowed_hosts, addr)
    app = http_ingest.build_app()
    token = http_ingest.load_or_create_ingest_token()
    http_ingest._token = token
    if db_path is not None:
        http_ingest._conn = connect_and_init(db_path, check_same_thread=False)

    name = socket.gethostname() or "this-device"
    url = f"http://{addr}:{port}"
    console.print(
        f"\n[bold]Memex sync serve[/bold] reachable at [cyan]{url}[/cyan] "
        "(token required; bound to that address only)."
    )
    console.print("On the [bold]other device[/bold], run:")
    # Echo the full pair line (token included) only to an interactive terminal, so a
    # redirected/daemon log never captures the token; otherwise point to `memex token`.
    if sys.stdout is not None and sys.stdout.isatty():
        console.print(
            f"  [bold cyan]memex sync pair --name {name} --url {url} --token {token}[/bold cyan]"
        )
        console.print(f"  [bold cyan]memex sync reconcile --peer {name}[/bold cyan]")
    else:
        console.print(
            f"  memex sync pair --name {name} --url {url}\n"
            "  (it will prompt for the token; get it with `memex token` here)"
        )
    console.print("[dim]Ctrl+C to stop.[/dim]\n")
    uvicorn.run(app, host=addr, port=port, log_level="info")


@sync_app.command("status")
def status(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Ping each peer to verify reachability + token (makes network calls).",
        ),
    ] = False,
) -> None:
    """Show whether sync is on, the local embedding identity, and each peer."""
    enabled = sync_state.is_enabled()
    console.print(f"Sync: {'[green]enabled[/green]' if enabled else '[yellow]disabled[/yellow]'}")
    console.print(
        f"Auto-sync: {'on' if settings.sync_auto else 'off'} "
        f"(every {settings.sync_interval_seconds}s, only while enabled and `serve` is running)"
    )

    local_model: str | None = None
    local_dim: int | None = None
    try:
        embedder = get_default_embedder()
        local_model, local_dim = embedder.model_name, embedder.dim
        console.print(f"Local embedding: {local_model} / {local_dim}")
    except EmbedderError as e:
        console.print(f"[yellow]Local embedding unavailable:[/yellow] {e}")

    known = peers.load_peers()
    if not known:
        console.print("[yellow]No peers paired.[/yellow] Use `memex sync pair`.")
        return

    table = Table(title="Sync peers")
    table.add_column("Name", style="bold cyan")
    table.add_column("URL")
    table.add_column("Last sync")
    table.add_column("Pulled/Pushed")
    if check:
        table.add_column("Reachable")
    for peer in known:
        hist = sync_state.get_peer_history(peer.name)
        counts = f"{hist['pulled']}/{hist['pushed']}" if hist else "-"
        row = [peer.name, peer.url, _format_last_sync(hist), counts]
        if check:
            row.append(_peer_reachable(peer, local_model, local_dim))
        table.add_row(*row)
    console.print(table)


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


def _require_enabled() -> None:
    """Refuse a data command while sync is disabled (the master gate, off by default)."""
    if not sync_state.is_enabled():
        console.print(
            "[yellow]Sync is disabled.[/yellow] Run `memex sync enable` first "
            "(it is off by default)."
        )
        raise typer.Exit(code=2)


def _warn_oversized(oversized: int, peer_name: str) -> None:
    """Tell the user when a record was too large to fit in any single request."""
    if not oversized:
        return
    console.print(
        f"  [yellow]{oversized} conversation(s) too large to transfer[/yellow] "
        f"(one record exceeds the body cap). Raise MEMEX_INGEST_MAX_BODY_BYTES "
        f"on {peer_name} (or lower MEMEX_MAX_CHUNKS_PER_CONVERSATION)."
    )


def _format_last_sync(hist: dict[str, Any] | None) -> str:
    if not hist:
        return "never"
    raw = hist.get("last_sync_at")
    if not isinstance(raw, str):
        return "?"
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def _peer_reachable(peer: Peer, local_model: str | None, local_dim: int | None) -> str:
    """One-word reachability for `status --check`: pings the peer's manifest."""
    try:
        manifest = client.fetch_manifest(peer)
    except urllib.error.HTTPError as e:
        return f"[red]HTTP {e.code}[/red]"
    except (urllib.error.URLError, OSError):
        return "[red]offline[/red]"
    except ValueError:
        return "[red]bad response[/red]"
    peer_model = manifest.get("embed_model")
    peer_dim = manifest.get("embed_dim")
    if local_model is not None and (peer_model != local_model or peer_dim != local_dim):
        return f"[yellow]model {peer_model}/{peer_dim}[/yellow]"
    return "[green]ok[/green]"


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
    _require_enabled()
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
            sync_state.record_sync(
                peer.name, pulled=summary.inserted, pushed=0, failed=summary.failed
            )
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
    _require_enabled()
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
            sync_state.record_sync(
                peer.name, pulled=0, pushed=summary.pushed, failed=summary.failed
            )
            console.print(
                f"  new/changed {summary.to_push}, [green]pushed {summary.pushed}[/green]"
                + (f", [red]failed {summary.failed}[/red]" if summary.failed else "")
                + "."
            )
            _warn_oversized(summary.oversized, peer.name)
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
    _require_enabled()
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
            sync_state.record_sync(
                peer.name, pulled=summary.pulled, pushed=summary.pushed, failed=summary.failed
            )
            console.print(
                f"  [green]pulled {summary.pulled}[/green], "
                f"[green]pushed {summary.pushed}[/green]"
                + (f", [red]failed {summary.failed}[/red]" if summary.failed else "")
                + "."
            )
            if summary.forks:
                console.print(
                    f"  [yellow]{summary.forks} conflict(s)[/yellow] (same timestamp, "
                    f"different content) left untouched. Force a side with "
                    f"`memex sync pull`/`push --peer {peer.name}`."
                )
            _warn_oversized(summary.oversized, peer.name)
    finally:
        conn.close()
