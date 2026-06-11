#!/usr/bin/env bash
# install.sh: one-command setup of Memex on macOS / Linux.
#
# Run it from the root of a cloned repo:
#   git clone https://github.com/dioniipereyraa/memex
#   cd memex
#   ./scripts/install.sh
#
# What it does (nothing hidden, nothing invasive):
#   1. Installs uv (the Python package manager) if it is not already present,
#      using Astral's official installer.
#   2. Runs `uv sync`, which downloads the pinned Python (3.13) and all
#      dependencies into a local .venv. No system Python needed.
#   3. Runs `memex doctor` to verify the install.
#
# It does NOT install git, Ollama, or anything else system-wide. Embeddings
# work out of the box via fastembed (a model downloads itself on first use);
# Ollama is optional and only if you opt in (see README).

set -euo pipefail

# Resolve repo root from this script's location, so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f "pyproject.toml" ]; then
    echo "Error: pyproject.toml not found in $REPO_ROOT." >&2
    echo "Run this script from inside a cloned Memex repository." >&2
    exit 1
fi

echo "==> Memex install (macOS / Linux)"
echo "    Repo: $REPO_ROOT"

# 1) Ensure uv is available.
if command -v uv >/dev/null 2>&1; then
    echo "==> uv already installed: $(uv --version)"
else
    echo "==> Installing uv (Astral official installer)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer puts uv in ~/.local/bin (or $XDG_BIN_HOME). Make it usable
    # in this same shell session without requiring a re-login.
    export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo "Error: uv was installed but is not on PATH. Open a new terminal and re-run." >&2
        exit 1
    fi
    echo "==> uv installed: $(uv --version)"
fi

# 2) Sync: installs the pinned Python and all dependencies into .venv.
echo "==> Installing Python ${REPO_ROOT##*/} dependencies (uv sync)..."
uv sync

# 3) Verify.
echo "==> Verifying install (memex doctor)..."
uv run memex doctor || true

cat <<'DONE'

==> Done. Memex is installed.

Next steps:
  1. Ingest your Claude.ai export:
       uv run memex ingest /path/to/your-export.zip
  2. Index your local Claude Code sessions:
       uv run memex ingest-claude-code
  3. Wire it into Claude Code (user scope, all sessions):
       claude mcp add --scope user memex -- uv run --directory "$(pwd)" memex-mcp

See README.md for live capture, the claude.ai connector, and more.
DONE
