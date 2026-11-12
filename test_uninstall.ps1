#Requires -Version 5.1
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "uninstall.ps1")

function Assert-Uninstall {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-UninstallThrows {
    param([scriptblock]$Action, [string]$Pattern)
    $failure = $null
    try { & $Action } catch { $failure = $_ }
    Assert-Uninstall ($null -ne $failure) "Expected an explicit failure."
    Assert-Uninstall ($failure.Exception.Message -match $Pattern) "Unexpected failure: $failure"
}

function Test-UninstallCase {
    param([string]$Name, [scriptblock]$Action)
    & $Action
    Write-Host "[PASS] $Name"
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ("aoai-uninstall-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    $directory = Join-Path $fixture "installed app"
    New-Item -ItemType Directory -Path (Join-Path $directory ".venv\Scripts") -Force | Out-Null
    foreach ($name in @("server.py", "service.ps1", "start.ps1", "startup_settings.py", ".venv\Scripts\python.exe")) {
        New-Item -ItemType File -Path (Join-Path $directory $name) | Out-Null
    }
    Set-Content -LiteralPath (Join-Path $directory "package.json") -Value '{"name":"azure-openai-local-swagger"}' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $directory "config.json") -Value '{"port":9000}' -Encoding ASCII

    Test-UninstallCase "Default directory resolution matches setup" {
        Assert-Uninstall ((Get-UninstallDirectory $directory) -eq $directory) "Explicit install path was not honored."
        Assert-Uninstall ((Get-UninstallDirectory) -eq $PSScriptRoot) "In-repository uninstall must use this checkout."
    }
    Test-UninstallCase "Default uninstall stops and disables only the chosen installation, preserving files" {
        $script:steps = @()
        $before = (Get-FileHash -LiteralPath (Join-Path $directory "config.json")).Hash
        function Disable-UninstallStartup { param($Directory) $script:steps += "disable:$Directory" }
        function Stop-UninstallService { param($Directory) $script:steps += "stop:$Directory" }
        Invoke-WebAppUninstall -Directory $directory
        Assert-Uninstall (($script:steps -join "|") -eq "disable:$directory|stop:$directory") "Incorrect uninstall order or target."
        Assert-Uninstall ((Get-FileHash -LiteralPath (Join-Path $directory "config.json")).Hash -eq $before) "Local configuration changed."
        Assert-Uninstall (Test-Path -LiteralPath (Join-Path $directory ".venv\Scripts\python.exe")) "Virtual environment was removed."
        Invoke-WebAppUninstall -Directory $directory
        Assert-Uninstall ($script:steps.Count -eq 4) "Repeated uninstall was not supported."
    }
    Test-UninstallCase "WhatIf never changes startup, service or files" {
        function Disable-UninstallStartup { throw "Must not modify startup" }
        function Stop-UninstallService { throw "Must not stop services" }
        Invoke-WebAppUninstall -Directory $directory -WhatIf
    }
    Test-UninstallCase "Invalid installation fails before any changes" {
        function Disable-UninstallStartup { throw "Must not modify startup" }
        function Stop-UninstallService { throw "Must not stop services" }
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory $fixture } "missing"
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory (Join-Path $fixture "absent") } "does not exist"
    }
    Test-UninstallCase "Custom and development directories cannot be recursively deleted" {
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory $directory -DeleteFiles } "only supports the default"
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory $PSScriptRoot -DeleteFiles } "only supports the default"
    }
    Test-UninstallCase "Deletion rejects links and running from inside the installation" {
        function Get-UninstallDirectory { return $directory }
        function Get-Item {
            return [pscustomobject]@{ Attributes = [IO.FileAttributes]::Directory }
        }
        function Get-ChildItem {
            return [pscustomobject]@{ Attributes = [IO.FileAttributes]::ReparsePoint }
        }
        Assert-UninstallThrows { Assert-UninstallDirectory $directory -DeleteFiles } "junction or symbolic link"
        Push-Location $directory
        try {
            Assert-UninstallThrows { Assert-UninstallDirectory $directory -DeleteFiles } "Leave the installation directory"
        } finally {
            Pop-Location
        }
    }
    Test-UninstallCase "RemoveFiles WhatIf does not delete or stop anything" {
        function Assert-UninstallDirectory {}
        function Disable-UninstallStartup { throw "Must not modify startup" }
        function Stop-UninstallService { throw "Must not stop services" }
        function Remove-Item { throw "Must not delete files" }
        Invoke-WebAppUninstall -Directory $directory -DeleteFiles -WhatIf
    }
    Test-UninstallCase "Unexpected package identity is rejected" {
        function Get-Content { return '{"name":"another-app"}' }
        Assert-UninstallThrows { Assert-UninstallDirectory $directory } "identity mismatch"
    }
    Test-UninstallCase "Startup failure stops uninstall before service changes or deletion" {
        function Disable-UninstallStartup { throw "startup denied" }
        function Stop-UninstallService { throw "Must not stop after startup failure" }
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory $directory } "startup denied"
        Assert-Uninstall (Test-Path -LiteralPath $directory) "Installation was deleted."
    }
    Test-UninstallCase "Service failure is explicit and preserves files" {
        function Disable-UninstallStartup {}
        function Stop-UninstallService { throw "service stop failed" }
        Assert-UninstallThrows { Invoke-WebAppUninstall -Directory $directory } "service stop failed"
        Assert-Uninstall (Test-Path -LiteralPath $directory) "Installation was deleted."
    }
    Test-UninstallCase "Live app process prevents success even when the PID file is absent" {
        Set-Content -LiteralPath (Join-Path $directory "service.ps1") -Value 'param($Action) exit 0' -Encoding ASCII
        function Get-CimInstance {
            return [pscustomobject]@{
                Name = "python.exe"
                ProcessId = 123
                CommandLine = '"python.exe" "' + (Join-Path $directory "server.py") + '" --background'
            }
        }
        Assert-UninstallThrows { Stop-UninstallService $directory } "still running"
    }
    Test-UninstallCase "Other applications are not treated as this service" {
        function Get-CimInstance {
            return [pscustomobject]@{ Name = "python.exe"; ProcessId = 456; CommandLine = '"python.exe" "C:\other\server.py"' }
        }
        Stop-UninstallService $directory
    }
} finally {
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
Write-Host "All uninstall tests passed. No real service, startup registration or installation was changed."
