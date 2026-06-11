#!/usr/bin/env bash
# Claude Code SessionEnd hook for Memex.
#
# Claude Code runs this when a session ends, passing a JSON object on stdin
# that includes `transcript_path` (the session's .jsonl). We ingest just that
# one session into Memex, in the BACKGROUND at low priority, and return
# immediately so the hook never blocks or slows Claude Code.
#
# Wire it up in ~/.claude/settings.json (see README "Automatic sync"):
#   "hooks": { "SessionEnd": [ { "matcher": "*", "hooks": [
#     { "type": "command", "command": "/abs/path/to/scripts/session-end-hook.sh" }
#   ] } ] }
#
# It is intentionally defensive: any problem (no uv, no transcript, bad JSON)
# exits 0 silently. A memory tool must never break the editor it watches.

set -uo pipefail
umask 077  # any file we create (the log) is user-only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || exit 0

# Read the hook payload and pull out transcript_path with stdlib json.
input="$(cat)"
transcript="$(printf '%s' "$input" | /usr/bin/python3 -c \
    'import sys,json
try:
    print(json.load(sys.stdin).get("transcript_path",""))
except Exception:
    pass' 2>/dev/null || true)"

[ -n "$transcript" ] || exit 0
[ -f "$transcript" ] || exit 0

mkdir -p "$REPO/data"
LOG="$REPO/data/hook-ingest.log"

# Fire and forget: detached, low priority. The embedding model loads only if
# this session has new content to index (LazyEmbedder), so an already-indexed
# session costs almost nothing.
nohup nice -n 19 "$UV" run --directory "$REPO" \
    memex ingest-claude-code --path "$transcript" >>"$LOG" 2>&1 &

exit 0
