[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "restart", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$server = Join-Path $PSScriptRoot "server.py"
$runtime = Join-Path $PSScriptRoot ".runtime"
$pidFile = Join-Path $runtime "server.pid"
$stdoutLog = Join-Path $runtime "server.stdout.log"
$stderrLog = Join-Path $runtime "server.stderr.log"

function Assert-Prerequisites {
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Missing virtual environment. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "node_modules\swagger-ui-dist\swagger-ui-bundle.js"))) {
        throw "Missing Swagger UI assets. Run npm install --ignore-scripts --no-audit --no-fund in swagger_site."
    }
}

function Get-ConfiguredPort {
    $port = & $python $server --print-port
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to load application settings. Check the error above and optional config.json."
    }
    return [int]$port
}

function Initialize-StartupSettings {
    & $python (Join-Path $PSScriptRoot "startup_settings.py") --initialize-default
    if ($LASTEXITCODE -ne 0) {
        $startScript = (Join-Path $PSScriptRoot "start.ps1").Replace("'", "''")
        throw "Unable to initialize Windows logon startup. The service remains running. To start manually later: powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$startScript'"
    }
}

function Get-ManagedProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    $storedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$storedPid)) {
        throw "Invalid PID file: $pidFile"
    }
    $details = Get-CimInstance Win32_Process -Filter "ProcessId = $storedPid" -ErrorAction SilentlyContinue
    if ($null -eq $details) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }
    if ($details.CommandLine -notlike "*$server*") {
        throw "PID $storedPid is not the managed Swagger server. Refusing to stop it; remove $pidFile after verifying the process."
    }
    return Get-Process -Id $storedPid -ErrorAction Stop
}

function Wait-UntilHealthy {
    param([int]$Port, [System.Diagnostics.Process]$Process)

    $healthUrl = "http://127.0.0.1:$Port/api/status"
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($Process.HasExited) {
            return $false
        }
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return $true
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    return $false
}

function Start-ServiceProcess {
    Assert-Prerequisites
    $existing = Get-ManagedProcess
    $port = Get-ConfiguredPort
    if ($existing) {
        if (-not (Wait-UntilHealthy -Port $port -Process $existing)) {
            throw "Existing Swagger server is not healthy. Check $stderrLog before changing startup settings."
        }
        Initialize-StartupSettings
        Write-Host "Swagger server is already running (PID $($existing.Id)): http://127.0.0.1:$port"
        return
    }
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $port is already in use by PID $($listener[0].OwningProcess). Stop that process explicitly or change config.json."
    }

    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList ('-u "{0}" --background' -f $server) `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

    if (-not (Wait-UntilHealthy -Port $port -Process $process)) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        throw "Swagger server failed to become ready. Check $stderrLog"
    }
    Initialize-StartupSettings
    Write-Host "Swagger server started in background (PID $($process.Id)): http://127.0.0.1:$port"
    Write-Host "Logs: $stdoutLog and $stderrLog"
}

function Stop-ServiceProcess {
    $process = Get-ManagedProcess
    if (-not $process) {
        Write-Host "Swagger server is not running."
        return
    }
    Stop-Process -Id $process.Id
    $remaining = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if ($remaining) {
        Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
        $remaining = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($remaining) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Swagger server stopped (PID $($process.Id))."
}

if ($MyInvocation.InvocationName -eq ".") { return }

switch ($Action) {
    "start" {
        Start-ServiceProcess
    }
    "restart" {
        Stop-ServiceProcess
        Start-ServiceProcess
    }
    "stop" {
        Stop-ServiceProcess
    }
    "status" {
        $process = Get-ManagedProcess
        $port = Get-ConfiguredPort
        if ($process) {
            Write-Host "Swagger server is running (PID $($process.Id)): http://127.0.0.1:$port"
        }
        else {
            Write-Host "Swagger server is not running."
        }
    }
}
