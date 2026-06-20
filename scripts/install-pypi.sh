#!/usr/bin/env bash
# Memex one-command install from PyPI (no git clone needed).
#
# Installs uv (if missing), installs the `memex-chats` tool, and runs
# `memex setup` to wire it into Claude Code (MCP server, always-on capture
# service, local session indexing, and the extension pairing token).
#
# Run it with:
#   curl -LsSf https://raw.githubusercontent.com/dioniipereyraa/memex/main/scripts/install-pypi.sh | sh
#
# For development (editable, with the extension + service templates), clone the
# repo and use scripts/install.sh instead.

set -euo pipefail

echo "==> Memex install (PyPI)"

# 1) uv: a single installer that also manages the Python version. Skip if present.
if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv (it also brings the right Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Make uv and the tool's executables usable in this same shell (no relog needed).
export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv was installed but is not on PATH. Open a new terminal and re-run." >&2
    exit 1
fi

# 2) Install Memex as an isolated tool (its own venv, on PATH as `memex`).
echo "==> Installing memex-chats..."
uv tool install --upgrade memex-chats

# 3) Wire it into Claude Code: MCP registration, autostart, ingest, token.
#    -y because this runs non-interactively (piped from curl); setup still
#    prints exactly what it did.
echo "==> Wiring it into Claude Code (memex setup)..."
memex setup -y

cat <<'DONE'

==> Memex is installed and set up.

One step left (it lives in the browser, so a terminal cannot do it): install the
Chrome extension and paste the access token shown above to start capturing your
claude.ai chats, then click "Backfill claude.ai history" to import your history.
  https://github.com/dioniipereyraa/memex#live-capture-phase-2
DONE
