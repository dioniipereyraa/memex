"""Small network helpers shared by the CLI commands that bind a sync-reachable
server (`memex sync serve` and `memex serve --sync`).

Kept in a leaf module (no imports from the rest of the CLI) so both the `sync`
subcommand group and the top-level `serve` command can use them without a
circular import.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Hosts always kept in the allow-list so loopback (the Chrome extension's local
# capture) keeps working even when a sync address is added.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

# The macOS Tailscale GUI app ships its CLI INSIDE the app bundle and does not put
# it on PATH (unlike the Homebrew/standalone packages, which `shutil.which` finds),
# so probe the bundle path explicitly or detection silently fails on a stock Mac.
_TAILSCALE_FALLBACK_PATHS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",)


def _tailscale_candidates() -> list[str]:
    """Executables to try for the tailscale CLI, most-standard first."""
    candidates: list[str] = []
    on_path = shutil.which("tailscale")
    if on_path:
        candidates.append(on_path)
    candidates.extend(p for p in _TAILSCALE_FALLBACK_PATHS if Path(p).exists())
    # Bare name as a last resort (covers a PATH that `shutil.which` could not read).
    candidates.append("tailscale")
    return candidates


def detect_tailscale_ip() -> str | None:
    """Best-effort Tailscale IPv4 via the tailscale CLI. None if unavailable.

    Works the same on macOS / Linux / Windows (the CLI is `tailscale` /
    `tailscale.exe`). Resolves the absolute path with `shutil.which` first
    (defense in depth: do not rely on a bare-name PATH lookup for a subprocess),
    then the macOS app-bundle path (the GUI app does not export the CLI to PATH),
    then the bare name.
    """
    for exe in _tailscale_candidates():
        try:
            result = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if candidate:
                return candidate
    return None


def merge_hosts(existing: str, *addrs: str) -> str:
    """Add `addrs` to a comma-separated Host allow-list, de-duplicated, order kept.

    Loopback is always preserved (it is already in the default list), so adding a
    Tailscale address never drops the extension's local capture access.
    """
    hosts = [h.strip() for h in existing.split(",") if h.strip()]
    for addr in addrs:
        if addr and addr not in hosts:
            hosts.append(addr)
    return ",".join(hosts)
