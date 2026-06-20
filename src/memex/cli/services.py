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

import contextlib
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from memex.config import settings

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


# ---------- wheel/PyPI install (no repo) ----------
#
# A wheel install ships only `src/memex` (no `scripts/`, no plist templates),
# so the repo-anchored agents above do not apply. These generate self-contained
# service definitions that run the installed CLI via
# `sys.executable -m memex.cli.main ...`, which depends on neither the repo nor a
# console script on PATH at boot. The DB/exports live in the per-user data dir
# (config._default_data_dir on a wheel install); the resolved `MEMEX_DB_PATH` is
# pinned into the service so it matches what the user's CLI sees even if they set
# it in their shell. Persistent autostart wants a `pip`/`pipx` install (a stable
# `sys.executable`); transient `uvx` runs are not a good autostart target.

# CLI subcommand each agent runs.
_SERVICE_CLI_ARGS: dict[str, tuple[str, ...]] = {
    SERVE: ("serve",),
    INGEST: ("ingest-claude-code",),
    REMOTE: ("serve-remote",),
}

LINUX_UNIT_NAME = "memex-serve.service"


def module_command(*cli_args: str) -> list[str]:
    """Invoke the installed Memex CLI without depending on PATH.

    Uses the running interpreter and the package module (`memex.cli.main` has a
    `__main__` guard), so the absolute `sys.executable` makes the service work at
    boot for a pip/pipx install regardless of shell PATH.
    """
    return [sys.executable, "-m", "memex.cli.main", *cli_args]


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def data_dir() -> Path:
    """Directory holding the DB; service logs go alongside it."""
    return settings.db_path.parent


def render_wheel_plist(service: str, log_dir: Path | None = None) -> str:
    """Generate a launchd plist for a wheel install (no `__REPO__` template)."""
    if service not in AGENT_LABELS:
        raise ValueError(f"unknown service {service!r}")
    label = AGENT_LABELS[service]
    logs = log_dir if log_dir is not None else data_dir()
    log = logs / f"{label}.log"
    args = module_command(*_SERVICE_CLI_ARGS[service])
    program = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in args)
    # The ingest backstop runs on an interval; serve / serve-remote stay alive.
    cadence = (
        "    <key>StartInterval</key>\n    <integer>900</integer>"
        if service == INGEST
        else "    <key>KeepAlive</key>\n    <true/>"
    )
    db_path = _xml_escape(str(settings.db_path))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MEMEX_DB_PATH</key>
        <string>{db_path}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(logs))}</string>
    <key>RunAtLoad</key>
    <true/>
{cadence}
    <key>ProcessType</key>
    <string>Background</string>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(log))}</string>
</dict>
</plist>
"""


def macos_install_wheel(services: Iterable[str]) -> list[str]:
    """Write and (re)load self-contained launchd agents for a wheel install."""
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    logs = data_dir()
    logs.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for service in services:
        label = AGENT_LABELS[service]
        dest = _plist_dest(label)
        subprocess.run(
            ["launchctl", "unload", str(dest)], check=False, capture_output=True
        )
        dest.write_text(render_wheel_plist(service, logs), encoding="utf-8")
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


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def render_systemd_unit(log_dir: Path | None = None) -> str:
    """Generate a systemd user unit running the installed `memex serve`."""
    logs = log_dir if log_dir is not None else data_dir()
    log = logs / "serve.log"
    exec_start = " ".join(shlex.quote(a) for a in module_command("serve"))
    db_path = settings.db_path
    return f"""[Unit]
Description=Memex live-capture HTTP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="MEMEX_DB_PATH={db_path}"
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


def linux_install_wheel(action: str) -> list[str]:
    """install/uninstall/status the systemd user unit for a wheel install."""
    unit_dir = _systemd_user_dir()
    unit_path = unit_dir / LINUX_UNIT_NAME

    if action == "uninstall":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", LINUX_UNIT_NAME],
            check=False,
            capture_output=True,
        )
        if unit_path.exists():
            unit_path.unlink()
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
            )
            return [f"removed {LINUX_UNIT_NAME}"]
        return [f"{LINUX_UNIT_NAME} not installed"]

    if action == "status":
        if not unit_path.exists():
            return [f"{LINUX_UNIT_NAME}: not installed"]
        result = subprocess.run(
            ["systemctl", "--user", "is-active", LINUX_UNIT_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
        return [f"{LINUX_UNIT_NAME}: {result.stdout.strip() or 'unknown'}"]

    # install
    logs = data_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(render_systemd_unit(logs), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", LINUX_UNIT_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [f"installed {LINUX_UNIT_NAME}", f"log: {logs / 'serve.log'}"]
    if result.returncode != 0:
        lines.append(f"systemctl enable failed: {(result.stderr or '').strip()}")
    return lines


WINDOWS_TASK_NAME = "MemexServe"


def _windows_python_for_service() -> str:
    """Path to the windowless interpreter (pythonw.exe) next to sys.executable.

    pythonw runs the server with no console window. Falls back to the regular
    interpreter if pythonw is not present (a non-standard install layout).
    """
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else Path(sys.executable))


def render_windows_task_xml() -> str:
    """Task Scheduler XML for a wheel install: run `memex serve` at logon.

    Registered with `schtasks /Create /XML`, which takes Command and Arguments
    as separate elements and so avoids the `/TR` quoting pitfalls. Runs
    `pythonw -m memex.cli.main serve` (no console window, no repo, no PATH
    dependency). The DB resolves to the per-user data dir from the installed
    package, so the task and the user's CLI share it without pinning an env var.
    Restarts up to 3 times on failure; no execution time limit (a long server).
    """
    command = _xml_escape(_windows_python_for_service())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Memex live-capture server</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>-m memex.cli.main serve</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def windows_install_wheel(action: str) -> list[str]:
    """install/uninstall/status the logon Scheduled Task for a wheel install.

    No admin needed (the task runs in the user's context). Output is not
    captured: pythonw has no console, so for live logs run `memex serve` by hand.
    """
    if action == "uninstall":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [f"removed task {WINDOWS_TASK_NAME}"]
        return [f"{WINDOWS_TASK_NAME} not installed"]

    if action == "status":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
        return [f"{WINDOWS_TASK_NAME}: {'registered' if result.returncode == 0 else 'not installed'}"]

    # install: write the task XML to a temp file (UTF-16, as Task Scheduler
    # expects) and register it, replacing any existing task with /F.
    xml_path = ""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", encoding="utf-16", delete=False
    ) as handle:
        handle.write(render_windows_task_xml())
        xml_path = handle.name
    try:
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", xml_path, "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(xml_path)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return [f"FAILED to create task {WINDOWS_TASK_NAME}: {detail}"]
    # The trigger fires at logon; start it once now so it is up immediately.
    subprocess.run(
        ["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME], check=False, capture_output=True
    )
    return [f"installed task {WINDOWS_TASK_NAME}", "runs: pythonw -m memex.cli.main serve"]
