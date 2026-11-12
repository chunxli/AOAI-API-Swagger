#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallDirectory,
    [switch]$RemoveFiles
)

$ErrorActionPreference = "Stop"

function Get-UninstallDirectory {
    param([string]$Directory)
    if (-not $Directory) {
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "server.py") -PathType Leaf) {
            $Directory = $PSScriptRoot
        } else {
            $Directory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "AzureOpenAILab"
        }
    }
    return [IO.Path]::GetFullPath($Directory).TrimEnd("\")
}

function Assert-UninstallDirectory {
    param([string]$Directory, [switch]$DeleteFiles)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        throw "Installation directory does not exist: $Directory. Specify the original path with -InstallDirectory."
    }
    foreach ($name in @("server.py", "service.ps1", "start.ps1", "startup_settings.py", "package.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Directory $name) -PathType Leaf)) {
            throw "Not a complete Azure OpenAI Lab installation (missing $name): $Directory. No changes were made."
        }
    }
    $package = Get-Content -LiteralPath (Join-Path $Directory "package.json") -Raw | ConvertFrom-Json
    if ($package.name -ne "azure-openai-local-swagger") {
        throw "Installation identity mismatch. No changes were made."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Directory ".venv\Scripts\python.exe") -PathType Leaf)) {
        throw "Installation Python is missing. Restore .venv before uninstalling; no changes were made."
    }
    if ($DeleteFiles) {
        $default = Get-UninstallDirectory (Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "AzureOpenAILab")
        if ($Directory -ne $default -or $Directory -eq $PSScriptRoot.TrimEnd("\")) {
            throw "-RemoveFiles only supports the default LocalAppData installation using a separately downloaded uninstall.ps1. Custom/development directories are never deleted automatically."
        }
        $location = [IO.Path]::GetFullPath($PWD.Path).TrimEnd("\")
        if ($location -eq $Directory -or $location.StartsWith($Directory + "\", [StringComparison]::OrdinalIgnoreCase)) {
            throw "Leave the installation directory before using -RemoveFiles (for example, Set-Location `$env:USERPROFILE)."
        }
        $item = Get-Item -LiteralPath $Directory -Force
        $links = @($item) + @(Get-ChildItem -LiteralPath $Directory -Recurse -Force)
        if ($links | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) {
            throw "Installation contains a junction or symbolic link. Refusing automatic deletion."
        }
    }
}

function Disable-UninstallStartup {
    param([string]$Directory)
    $python = Join-Path $Directory ".venv\Scripts\python.exe"
    & $python -c "import sys; from pathlib import Path; root = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(root)); from startup_settings import StartupManager; state = StartupManager(root).write(False); assert state['enabled'] is False; print('Windows logon autostart disabled.')" $Directory
    if ($LASTEXITCODE -ne 0) { throw "Unable to disable Windows autostart. Files were not deleted." }
}

function Stop-UninstallService {
    param([string]$Directory)
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Directory "service.ps1") stop
    if ($LASTEXITCODE -ne 0) { throw "Unable to stop the managed service. Files were not deleted." }
    $server = Join-Path $Directory "server.py"
    $escapedServer = [regex]::Escape($server)
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.CommandLine -match ('(?i)(?:^|[\s"])' + $escapedServer + '(?:[\s"]|$)')
    })
    if ($running.Count) {
        throw "An application process is still running (PID $($running.ProcessId -join ', ')). Stop it explicitly before uninstalling; files were not deleted."
    }
}

function Invoke-WebAppUninstall {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Directory, [switch]$DeleteFiles)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "This uninstaller supports Windows only."
    }
    $Directory = Get-UninstallDirectory $Directory
    Assert-UninstallDirectory $Directory -DeleteFiles:$DeleteFiles
    if (-not $PSCmdlet.ShouldProcess($Directory, "Disable Windows logon autostart and stop Azure OpenAI Lab")) {
        return
    }
    if ($DeleteFiles -and -not $PSCmdlet.ShouldContinue(
        "Permanently delete $Directory including config, request history, logs, virtual environment and all local files?",
        "Delete installation and local data"
    )) {
        Write-Host "Uninstall cancelled. No changes were made."
        return
    }
    Disable-UninstallStartup $Directory
    Stop-UninstallService $Directory
    if ($DeleteFiles) {
        # Assert again immediately before deletion; never follow reparse points.
        Assert-UninstallDirectory $Directory -DeleteFiles
        Remove-Item -LiteralPath $Directory -Recurse -Force
        if (Test-Path -LiteralPath $Directory) { throw "Installation removal was incomplete: $Directory" }
        Write-Host "Uninstalled and deleted: $Directory"
    } else {
        Write-Host "Service stopped and autostart disabled. Files and data preserved: $Directory"
        Write-Host "Run setup.ps1 again to update and restart this installation."
    }
    Write-Host "Azure resources, Azure CLI sign-in, shared tools and browser storage were not changed."
}

if ($MyInvocation.InvocationName -ne ".") {
    try {
        Invoke-WebAppUninstall -Directory $InstallDirectory -DeleteFiles:$RemoveFiles
    } catch {
        Write-Error "Uninstall failed: $($_.Exception.Message)" -ErrorAction Continue
        exit 1
    }
}
