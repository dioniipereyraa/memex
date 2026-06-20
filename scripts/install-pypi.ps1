#Requires -Version 5.1
<#
.SYNOPSIS
Memex one-command install from PyPI on Windows (no git clone needed).

.DESCRIPTION
Installs uv (if missing), installs the `memex-chats` tool, and runs `memex setup`
to wire it into Claude Code (MCP server, always-on capture service as a logon
Scheduled Task, local session indexing, and the extension pairing token).

Run it with:
  powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/dioniipereyraa/memex/main/scripts/install-pypi.ps1 | iex"

For development (editable, with the extension + service templates), clone the
repo and use scripts/install.ps1 instead.
#>

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "==> Memex install (PyPI)"

# 1) uv: a single installer that also manages the Python version. Skip if present.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (it also brings the right Python)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}
# Make uv and the tool's executables usable in this same session (no relog).
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv was installed but is not on PATH. Open a new PowerShell and re-run."
    exit 1
}

# 2) Install Memex as an isolated tool (its own venv, on PATH as `memex`).
Write-Host "==> Installing memex-chats..."
uv tool install --upgrade memex-chats
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

# 3) Wire it into Claude Code: MCP registration, autostart, ingest, token.
#    -y because setup runs non-interactively here; it still prints what it did.
Write-Host "==> Wiring it into Claude Code (memex setup)..."
memex setup -y

Write-Host ""
Write-Host "==> Memex is installed and set up."
Write-Host "One step left (it lives in the browser): install the Chrome extension and"
Write-Host "paste the access token shown above, then click 'Backfill claude.ai history'."
Write-Host "  https://github.com/dioniipereyraa/memex#live-capture-phase-2"
