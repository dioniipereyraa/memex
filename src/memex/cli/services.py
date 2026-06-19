"""OS-service (autostart) management for Memex.

macOS launchd implementation plus shared helpers used by both
`memex install-service` and `memex setup`. Windows (Scheduled Task) and Linux
(systemd user unit) are handled by the shell/PowerShell scripts in `scripts/`
(see `cli.main._run_install_service`).

Repo-anchored (Phase A): the generated launchd agents run `serve-daemon.sh`
from the cloned repo and log into `<repo>/data`. A PyPI/uvx install has no
repo, so autostart there is Phase B (self-contained services + a user data
dir); `source_repo_root()` returns None in that case so callers can degrade
gracefully.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

# launchd labels for the agents Memex can run. `serve` (live capture) and
# `ingest-claude-code` (the 15-minute backstop to the SessionEnd hook) are the
# default pair. The remote connector is opt-in: without the .env OAuth/tunnel
# config it crash-loops, so it is never installed unless explicitly requested.
SERVE = "serve"
INGEST = "ingest-claude-code"
REMOTE = "serve-remote"

AGENT_LABELS: dict[str, str] = {
    SERVE: "com.memex.serve",
    INGEST: "com.memex.ingest-claude-code",
    REMOTE: "com.memex.serve-remote",
}

DEFAULT_SERVICES: tuple[str, ...] = (SERVE, INGEST)

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def source_repo_root() -> Path | None:
    """Absolute path to the cloned Memex repo, or None on a wheel install.

    `services.py` lives at `src/memex/cli/services.py`, so the repo root is
    four parents up. A real checkout has both `pyproject.toml` and `scripts/`
    (the launchd templates + daemon wrappers); a PyPI/uvx install ships
    neither, so we return None and the caller falls back (Phase B).
    """
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "scripts").is_dir():
        return root
    return None


def render_agent(repo_root: Path, service: str) -> tuple[str, str]:
    """Return `(label, plist_xml)` for one launchd agent.

    Reads `scripts/com.memex.<service>.plist.template` and substitutes the
    `__REPO__` placeholder with the absolute repo path. Raises `ValueError`
    for an unknown service and `FileNotFoundError` if the template is missing.
    """
    if service not in AGENT_LABELS:
        raise ValueError(f"unknown service {service!r}")
    template = repo_root / "scripts" / f"com.memex.{service}.plist.template"
    if not template.is_file():
        raise FileNotFoundError(f"missing launchd template: {template}")
    xml = template.read_text(encoding="utf-8").replace("__REPO__", str(repo_root))
    return AGENT_LABELS[service], xml


def _plist_dest(label: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def macos_install(repo_root: Path, services: Iterable[str]) -> list[str]:
    """Write and (re)load the launchd agents for `services`.

    Reloads cleanly if an agent was already loaded (a best-effort `unload`
    before `load`), so re-running is idempotent. Returns one human-readable
    status line per agent.
    """
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    # The plists write logs into <repo>/data; make sure it exists so launchd
    # does not fail to open StandardOutPath on a fresh clone.
    (repo_root / "data").mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for service in services:
        label, xml = render_agent(repo_root, service)
        dest = _plist_dest(label)
        # Unload first so a changed plist is picked up; ignore "not loaded".
        subprocess.run(
            ["launchctl", "unload", str(dest)], check=False, capture_output=True
        )
        dest.write_text(xml, encoding="utf-8")
        result = subprocess.run(
            ["launchctl", "load", str(dest)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            lines.append(f"loaded {label}")
        else:
            detail = (result.stderr or "").strip() or f"exit {result.returncode}"
            lines.append(f"FAILED {label}: {detail}")
    return lines


def macos_uninstall(services: Iterable[str]) -> list[str]:
    """Unload and remove the launchd agents for `services` (keeps logs)."""
    lines: list[str] = []
    for service in services:
        label = AGENT_LABELS[service]
        dest = _plist_dest(label)
        if dest.exists():
            subprocess.run(
                ["launchctl", "unload", str(dest)], check=False, capture_output=True
            )
            dest.unlink()
            lines.append(f"removed {label}")
        else:
            lines.append(f"{label} not installed")
    return lines


def macos_status(services: Iterable[str]) -> list[str]:
    """Report whether each agent is installed and currently loaded."""
    lines: list[str] = []
    for service in services:
        label = AGENT_LABELS[service]
        dest = _plist_dest(label)
        if not dest.exists():
            lines.append(f"{label}: not installed")
            continue
        result = subprocess.run(
            ["launchctl", "list", label], check=False, capture_output=True, text=True
        )
        state = "loaded" if result.returncode == 0 else "installed, not loaded"
        lines.append(f"{label}: {state}")
    return lines
