[CmdletBinding()]
param(
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$service = Join-Path $PSScriptRoot "service.ps1"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not $Foreground) {
    & $service start
    return
}

& $python (Join-Path $PSScriptRoot "server.py")
exit $LASTEXITCODE
