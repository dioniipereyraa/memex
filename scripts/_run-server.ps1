# Wrapper invocado por la Scheduled Task `MemexServe`. No correr a mano.
#
# Si querés arrancar el server manualmente para debug, usá directamente:
#   uv run memex serve
# desde el repo root.
#
# Este wrapper se encarga de:
# - Setear el working directory al repo (padre del directorio de este script).
# - Resolver `uv` en el PATH del usuario (puede haber cambiado desde el install).
# - Crear el directorio de log si no existe.
# - Redirigir todos los streams (stdout, stderr, warning, verbose) al log file
#   con append, para que reinicios sucesivos no pisen logs anteriores.

$ErrorActionPreference = "Stop"

$RepoRoot = (Get-Item $PSScriptRoot).Parent.FullName
$LogDir = Join-Path $env:LOCALAPPDATA "Memex"
$LogFile = Join-Path $LogDir "serve.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Set-Location -LiteralPath $RepoRoot

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    "[$(Get-Date -Format o)] FATAL: uv no encontrado en PATH del usuario. La task no puede arrancar el server." |
        Out-File -FilePath $LogFile -Append -Encoding utf8
    exit 1
}

"[$(Get-Date -Format o)] Arrancando memex serve (repo=$RepoRoot, uv=$($uv.Source))" |
    Out-File -FilePath $LogFile -Append -Encoding utf8

# Usamos `2>&1 | Out-File -Encoding utf8` en lugar de `*>> $LogFile` porque
# el redirect `*>>` en PowerShell 5.1 escribe UTF-16 LE por default y mezcla
# encoding con las líneas de banner que ya escribimos en UTF-8.
& $uv.Source run memex serve 2>&1 |
    Out-File -FilePath $LogFile -Append -Encoding utf8
