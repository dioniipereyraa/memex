#!/usr/bin/env bash
# Wrapper to run the remote MCP server (`memex serve-remote`, the claude.ai
# connector) as a long-lived daemon under launchd/systemd. Runs from the repo
# root so the `.env` (MEMEX_REMOTE_* + GitHub OAuth creds) and the relative DB
# path resolve. Pair it with a persistent tunnel (Tailscale Funnel) on the
# same port. Idle cost is low (no embedding model until a search runs).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || exit 1

cd "$REPO"
exec "$UV" run memex serve-remote
