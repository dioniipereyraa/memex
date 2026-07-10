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
from collections.abc import Callable, Iterable
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
        subprocess.run(["launchctl", "unload", str(dest)], check=False, capture_output=True)
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
            subprocess.run(["launchctl", "unload", str(dest)], check=False, capture_output=True)
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
        subprocess.run(["launchctl", "unload", str(dest)], check=False, capture_output=True)
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


def _systemd_safe_path(p: Path, what: str) -> str:
    r"""Reject a path that would break or subvert the (line-oriented) unit file.

    A systemd unit has no escape for a newline, so a `\n` in a value starts a new
    directive (e.g. an injected `ExecStartPre=` that would run at boot); a `"`
    breaks the quoted `Environment=` value. Both are rejected rather than written
    into an exploitable or broken unit. Normal filesystem paths contain neither.
    """
    s = str(p)
    bad = next((c for c in ("\n", "\r", '"') if c in s), None)
    if bad is not None:
        raise ValueError(f"refusing to write a systemd unit: {what} contains {bad!r}: {s!r}")
    return s


def render_systemd_unit(log_dir: Path | None = None) -> str:
    """Generate a systemd user unit running the installed `memex serve`."""
    logs = log_dir if log_dir is not None else data_dir()
    log = _systemd_safe_path(logs / "serve.log", "log path")
    exec_start = " ".join(shlex.quote(a) for a in module_command("serve"))
    db_path = _systemd_safe_path(settings.db_path, "MEMEX_DB_PATH")
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
UMask=0077
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
        ["systemctl", "--user", "enable", LINUX_UNIT_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [f"installed {LINUX_UNIT_NAME}", f"log: {logs / 'serve.log'}"]
    if result.returncode != 0:
        lines.append(f"systemctl enable failed: {(result.stderr or '').strip()}")
    # `enable --now` does NOT restart an already-running unit, and the serve reads
    # the persisted sync flags only at startup, so a `setup --sync` after a plain
    # `setup` would leave the old loopback-bound process running until the next
    # boot. `restart` starts a stopped unit and bounces a running one.
    restarted = subprocess.run(
        ["systemctl", "--user", "restart", LINUX_UNIT_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if restarted.returncode != 0:
        lines.append(f"systemctl restart failed: {(restarted.stderr or '').strip()}")
    return lines


WINDOWS_TASK_NAME = "MemexServe"
WINDOWS_INGEST_TASK_NAME = "MemexIngest"


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


def render_windows_ingest_task_xml() -> str:
    """Task Scheduler XML for the Claude Code ingest backstop on a wheel install.

    The serve task is the live-capture server; this is the periodic counterpart of
    the macOS `StartInterval` ingest agent, missing on Windows until now: it runs
    `pythonw -m memex.cli.main ingest-claude-code` at logon and every 15 minutes so
    local Claude Code sessions get indexed continuously, not just once at setup.
    Short-lived, so it gets a 1-hour limit (not the server's infinite one) and
    `IgnoreNew` (the ingest also holds a non-blocking flock, so a slow run never
    stacks). pythonw has no console, so `ingest-claude-code` reopens its streams to
    `ingest.log` when headless.
    """
    command = _xml_escape(_windows_python_for_service())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Memex Claude Code ingest backstop (every 15 min)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT15M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2024-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
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
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>-m memex.cli.main ingest-claude-code</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# (task name, XML renderer, "runs:" summary) for each Windows Scheduled Task.
_WINDOWS_TASKS: tuple[tuple[str, Callable[[], str], str], ...] = (
    (WINDOWS_TASK_NAME, render_windows_task_xml, "pythonw -m memex.cli.main serve"),
    (
        WINDOWS_INGEST_TASK_NAME,
        render_windows_ingest_task_xml,
        "pythonw -m memex.cli.main ingest-claude-code (every 15 min)",
    ),
)


def _windows_register_task(
    task_name: str, render: Callable[[], str], *, restart: bool = False
) -> str | None:
    """Create one Scheduled Task from its XML. Returns an error string or None.

    With `restart=True` a running instance is stopped before the start: `/Run` is
    ignored for a running task (MultipleInstancesPolicy IgnoreNew) and the
    always-on serve reads the persisted sync flags only at startup, so re-running
    setup with new flags must bounce it or the old bind survives until reboot.
    """
    xml_path = ""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as handle:
        handle.write(render())
        xml_path = handle.name
    try:
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", xml_path, "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(xml_path)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    if restart:
        subprocess.run(["schtasks", "/End", "/TN", task_name], check=False, capture_output=True)
    # Start it once now so it is up immediately (the triggers also cover logon).
    subprocess.run(["schtasks", "/Run", "/TN", task_name], check=False, capture_output=True)
    return None


def windows_ensure_sync_firewall(port: int = 5777) -> tuple[bool, str]:
    """Best-effort: ensure an inbound TCP allow rule for the sync port on Windows.

    In sync-reachable mode the always-on serve binds `0.0.0.0:<port>`, but Windows
    Firewall silently drops inbound connections (a peer just times out) until a
    rule allows the port. This adds one, idempotently by rule name. Needs admin; if
    it cannot add it (setup not elevated), returns the exact one-time command so the
    user runs it themselves. Never raises: returns (ok, message).
    """
    rule = f"Memex sync {port}"
    manual = (
        f'run once in an admin PowerShell: New-NetFirewallRule -DisplayName "{rule}" '
        f"-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow"
    )
    try:
        present = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if present.returncode == 0:
            return True, f"firewall port {port} already open (rule '{rule}')"
        added = subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={rule}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if added.returncode == 0:
            return True, f"opened inbound TCP {port} (rule '{rule}')"
        return False, f"could not open TCP {port} (needs admin); {manual}"
    except (OSError, subprocess.SubprocessError):
        return False, f"could not run netsh; {manual}"


def windows_install_wheel(action: str) -> list[str]:
    """install/uninstall/status the Scheduled Tasks for a wheel install.

    Two tasks: `MemexServe` (the always-on live-capture server) and `MemexIngest`
    (the 15-minute Claude Code ingest backstop). No admin needed (they run in the
    user's context). Output is not captured: pythonw has no console, so for live
    logs run `memex serve` by hand or read `serve.log` / `ingest.log` in the data
    dir.
    """
    if action == "uninstall":
        lines: list[str] = []
        for task_name, _render, _runs in _WINDOWS_TASKS:
            result = subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            lines.append(
                f"removed task {task_name}"
                if result.returncode == 0
                else f"{task_name} not installed"
            )
        return lines

    if action == "status":
        lines = []
        for task_name, _render, _runs in _WINDOWS_TASKS:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", task_name],
                check=False,
                capture_output=True,
                text=True,
            )
            state = "registered" if result.returncode == 0 else "not installed"
            lines.append(f"{task_name}: {state}")
        return lines

    # install: write each task XML to a temp file (UTF-16, as Task Scheduler
    # expects) and register it, replacing any existing task with /F.
    lines = []
    for task_name, render, runs in _WINDOWS_TASKS:
        # Only the always-on serve is bounced (it must pick up new sync flags);
        # the periodic ingest is never killed mid-run, its next trigger suffices.
        error = _windows_register_task(task_name, render, restart=task_name == WINDOWS_TASK_NAME)
        if error is not None:
            lines.append(f"FAILED to create task {task_name}: {error}")
        else:
            lines.append(f"installed task {task_name}")
            lines.append(f"  runs: {runs}")
    return lines
