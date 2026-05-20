#Requires -Version 5.1
<#
.SYNOPSIS
Instala, desinstala o consulta el autostart del servidor Memex Live Capture vía Windows Task Scheduler.

.DESCRIPTION
Registra una Scheduled Task "MemexServe" que arranca `uv run memex serve` al log on del usuario actual.
No requiere permisos de administrador (la task corre en el contexto del usuario).

El working directory de la task se fija al repo donde vive este script al momento del install. Si movés el repo, hay que re-instalar.

Esta es una solución Windows-only de etapa temprana. La versión cross-platform formal (Linux systemd, macOS launchd) está planificada como `memex install-service` en Fase 5 del ROADMAP.

.PARAMETER Install
Registra la task y la dispara inmediatamente (no espera al próximo logon). Si la task ya existe, la sobreescribe.

.PARAMETER Uninstall
Borra la task. Mantiene el archivo de log en %LOCALAPPDATA%\Memex\serve.log.

.PARAMETER Status
Muestra si la task está registrada, su estado, último resultado y próximo trigger.

.EXAMPLE
.\scripts\install-autostart.ps1 -Install

.EXAMPLE
.\scripts\install-autostart.ps1 -Status

.EXAMPLE
.\scripts\install-autostart.ps1 -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = "Status")]
param(
    [Parameter(ParameterSetName = "Install")]
    [switch]$Install,

    [Parameter(ParameterSetName = "Uninstall")]
    [switch]$Uninstall,

    [Parameter(ParameterSetName = "Status")]
    [switch]$Status
)

$ErrorActionPreference = "Stop"

# Forzar UTF-8 en la consola para que tildes y eñes salgan bien en cualquier codepage.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$TaskName = "MemexServe"
$RepoRoot = (Get-Item $PSScriptRoot).Parent.FullName
$WrapperScript = Join-Path $PSScriptRoot "_run-server.ps1"
$LogDir = Join-Path $env:LOCALAPPDATA "Memex"
$LogFile = Join-Path $LogDir "serve.log"

function Get-MemexTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Install-MemexTask {
    if (-not (Test-Path $WrapperScript)) {
        throw "Wrapper no encontrado: $WrapperScript"
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv no encontrado en PATH. Instalá uv desde https://docs.astral.sh/uv/ antes de continuar."
    }

    Write-Host "Instalando Memex autostart..."
    Write-Host "  Repo:    $RepoRoot"
    Write-Host "  Wrapper: $WrapperScript"
    Write-Host "  uv:      $($uv.Source)"
    Write-Host "  Log:     $LogFile"

    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

    if (Get-MemexTask) {
        Write-Host "  Task ya existe; sobreescribiendo..."
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $argument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperScript`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    # LogonType S4U: corre como el usuario pero sin sesión interactiva (sin
    # ventana, independiente del shell que dispara la task). Cerrar VS Code u
    # otros shells no mata la task. No requiere password ni admin; sí requiere
    # el privilege "Log on as a batch job" (típicamente concedido a usuarios
    # estándar en Windows 10/11).
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType S4U `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Memex Live Capture: arranca el HTTP server local al log on del usuario." | Out-Null

    Write-Host "  Task '$TaskName' registrada."

    Start-ScheduledTask -TaskName $TaskName
    Write-Host "  Task disparada (server arrancando en background)."
    Write-Host ""
    Write-Host "Tail del log con:"
    Write-Host "  Get-Content '$LogFile' -Wait -Tail 20"
}

function Uninstall-MemexTask {
    if (-not (Get-MemexTask)) {
        Write-Host "Task '$TaskName' no está registrada. Nada que hacer."
        return
    }

    Write-Host "Removiendo Memex autostart..."
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  Task '$TaskName' removida."
    Write-Host ""
    Write-Host "El log file sigue en $LogFile. Borralo a mano si querés."
}

function Show-Status {
    $task = Get-MemexTask
    if (-not $task) {
        Write-Host "Task '$TaskName' no está registrada."
        Write-Host "Para registrarla:"
        Write-Host "  .\scripts\install-autostart.ps1 -Install"
        return
    }

    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    Write-Host "Task '$TaskName':"
    Write-Host "  Estado:           $($task.State)"
    Write-Host "  Último run:       $($info.LastRunTime)"
    Write-Host "  Último resultado: $($info.LastTaskResult)  (0 = OK)"
    Write-Host "  Próximo run:      $($info.NextRunTime)"
    Write-Host ""
    Write-Host "Log file: $LogFile"
    if (Test-Path $LogFile) {
        $size = (Get-Item $LogFile).Length
        Write-Host "  (tamaño: $size bytes)"
    }
    else {
        Write-Host "  (todavía no existe)"
    }
}

try {
    if ($Install) {
        Install-MemexTask
    }
    elseif ($Uninstall) {
        Uninstall-MemexTask
    }
    else {
        Show-Status
    }
}
catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    exit 1
}
