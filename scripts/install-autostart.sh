#!/usr/bin/env bash
# install-autostart.sh: register the Memex live-capture server as a systemd
# user unit so it starts on login and stays up across reboots. Linux only.
#
# Subcommands (mirror the Windows .ps1):
#   install   write the unit, enable + start it now
#   uninstall stop + disable + remove the unit
#   status    show systemctl status for the unit
#
# This is the Linux counterpart of scripts/install-autostart.ps1. For macOS,
# install the launchd agents via the README section "Running always-on
# (macOS)" using scripts/com.memex.*.plist.template.

set -euo pipefail

UNIT_NAME="memex-serve.service"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$USER_UNIT_DIR/$UNIT_NAME"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/memex"
LOG_FILE="$LOG_DIR/serve.log"

# The script lives in <repo>/scripts/, so the repo root is its parent.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

usage() {
    cat >&2 <<EOF
Usage: $0 {install|uninstall|status}

Manages the Memex live-capture server as a systemd user unit on Linux.

  install     Write the unit file, enable it, and start it now.
              Survives logout via 'loginctl enable-linger \$USER' if the
              user wants the server up without an interactive session.
  uninstall   Stop, disable, and remove the unit (keeps the log file).
  status      Print 'systemctl --user status' for the Memex unit.

Repo root resolved to: $REPO_ROOT
Unit will be written to: $UNIT_PATH
Log file (managed by the unit): $LOG_FILE
EOF
}

require_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "Error: systemctl not found. This script needs systemd (Linux)." >&2
        echo "If you are on macOS, use launchd; that is tracked separately." >&2
        exit 1
    fi
    if [ ! -d /run/systemd/system ] && [ -z "${MEMEX_AUTOSTART_FORCE:-}" ]; then
        echo "Error: systemd does not look active (no /run/systemd/system)." >&2
        echo "Set MEMEX_AUTOSTART_FORCE=1 to override (eg. inside containers)." >&2
        exit 1
    fi
}

resolve_uv() {
    # Resolve uv lazily: if it is on PATH at install time, embed its absolute
    # path so the unit does not depend on PATH at boot. If not on PATH, the
    # unit will use the literal name 'uv' and rely on the shell's PATH lookup
    # (works for most setups where uv is in ~/.local/bin or similar).
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    else
        echo "uv"
    fi
}

cmd_install() {
    require_systemd

    local uv_bin
    uv_bin="$(resolve_uv)"

    mkdir -p "$USER_UNIT_DIR"
    mkdir -p "$LOG_DIR"

    cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=Memex live-capture HTTP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=$uv_bin run memex serve
Restart=on-failure
RestartSec=5
# Tee stdout + stderr into a log file in \$XDG_STATE_HOME. systemd journal
# also keeps them under 'systemctl --user status memex-serve'.
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
UNIT

    systemctl --user daemon-reload
    # enable + restart (not `enable --now`): --now does not restart a running
    # unit, and the serve reads the persisted sync flags only at startup.
    systemctl --user enable "$UNIT_NAME"
    systemctl --user restart "$UNIT_NAME"

    echo "Installed $UNIT_NAME"
    echo "  unit:  $UNIT_PATH"
    echo "  log:   $LOG_FILE"
    echo "  uv:    $uv_bin"
    echo ""
    echo "To make the server run even when you are logged out:"
    echo "  loginctl enable-linger \$USER"
    echo ""
    echo "Tail the log: tail -f $LOG_FILE"
    echo "Status:       systemctl --user status $UNIT_NAME"
}

cmd_uninstall() {
    require_systemd
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    if [ -f "$UNIT_PATH" ]; then
        rm -f "$UNIT_PATH"
        systemctl --user daemon-reload
        echo "Removed $UNIT_PATH (log file kept at $LOG_FILE)."
    else
        echo "Nothing to remove: $UNIT_PATH does not exist."
    fi
}

cmd_status() {
    require_systemd
    if [ ! -f "$UNIT_PATH" ]; then
        echo "$UNIT_NAME is not installed."
        echo "Run '$0 install' to register it."
        return 0
    fi
    systemctl --user status "$UNIT_NAME" --no-pager || true
}

case "${1:-status}" in
    install)   cmd_install   ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status    ;;
    -h|--help) usage          ;;
    *)
        usage
        exit 2
        ;;
esac
