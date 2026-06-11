# install.ps1: one-command setup of Memex on Windows (PowerShell).
#
# Run it from the root of a cloned repo:
#   git clone https://github.com/dioniipereyraa/memex
#   cd memex
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#
# What it does (nothing hidden, nothing invasive):
#   1. Installs uv (the Python package manager) if not already present,
#      using Astral's official installer.
#   2. Runs `uv sync`, which downloads the pinned Python (3.13) and all
#      dependencies into a local .venv. No system Python needed.
#   3. Runs `memex doctor` to verify the install.
#
# It does NOT install git, Ollama, or anything else system-wide. Embeddings
# work out of the box via fastembed; Ollama is optional (see README).

$ErrorActionPreference = "Stop"

# Resolve repo root from this script's location.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $RepoRoot

if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    Write-Error "pyproject.toml not found in $RepoRoot. Run this from inside a cloned Memex repository."
    exit 1
}

Write-Host "==> Memex install (Windows)"
Write-Host "    Repo: $RepoRoot"

# 1) Ensure uv is available.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "==> uv already installed: $(uv --version)"
} else {
    Write-Host "==> Installing uv (Astral official installer)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    # The installer adds uv to PATH for new shells; make it usable now too.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "uv was installed but is not on PATH. Open a new terminal and re-run."
        exit 1
    }
    Write-Host "==> uv installed: $(uv --version)"
}

# 2) Sync: installs the pinned Python and all dependencies into .venv.
Write-Host "==> Installing dependencies (uv sync)..."
uv sync

# 3) Verify.
Write-Host "==> Verifying install (memex doctor)..."
uv run memex doctor

Write-Host ""
Write-Host "==> Done. Memex is installed."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Ingest your Claude.ai export:"
Write-Host "       uv run memex ingest C:\path\to\your-export.zip"
Write-Host "  2. Index your local Claude Code sessions:"
Write-Host "       uv run memex ingest-claude-code"
Write-Host "  3. Wire it into Claude Code (user scope, all sessions):"
Write-Host "       claude mcp add --scope user memex -- uv run --directory `"$RepoRoot`" memex-mcp"
Write-Host ""
Write-Host "See README.md for live capture, the claude.ai connector, and more."
