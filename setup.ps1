#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallDirectory,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

function Find-SetupCommand {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($command) { return $command.Source }
    }
}

function Find-SetupPython {
    $ErrorActionPreference = "Continue"
    $candidates = @(
        Find-SetupCommand @("python.exe")
        Find-SetupCommand @("python3.exe")
    )
    $launcher = Find-SetupCommand @("py.exe")
    if ($launcher) {
        $result = & $launcher -3 -c "import sys; print(sys.executable)" 2>&1
        if ($LASTEXITCODE -eq 0) { $candidates += [string]($result | Select-Object -Last 1) }
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not $candidate -or $candidate -like "*\WindowsApps\python*.exe") { continue }
        $result = & $candidate -c "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>&1
        if ($LASTEXITCODE -eq 0) { return [string]($result | Select-Object -Last 1) }
    }
}

function Invoke-SetupCommand {
    param([string]$Command, [string[]]$Arguments, [string]$Description)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit $LASTEXITCODE). Resolve the error above and rerun setup.ps1."
    }
}

function Update-SetupPath {
    $paths = @(
        [Environment]::GetEnvironmentVariable("Path", "Machine")
        [Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path
    )
    $env:Path = ($paths | Where-Object { $_ }) -join ";"
}

function Get-SetupTools {
    param([switch]$Install)
    $definitions = @(
        @{ Name = "Git"; Id = "Git.Git"; Probe = { Find-SetupCommand @("git.exe") } }
        @{ Name = "Python 3.10+"; Id = "Python.Python.3.12"; Probe = { Find-SetupPython } }
        @{ Name = "Node.js and npm"; Id = "OpenJS.NodeJS.LTS"; Probe = {
            if ((Find-SetupCommand @("node.exe")) -and (Find-SetupCommand @("npm.cmd"))) {
                Find-SetupCommand @("npm.cmd")
            }
        } }
        @{ Name = "Azure CLI"; Id = "Microsoft.AzureCLI"; Probe = { Find-SetupCommand @("az.cmd", "az.exe") } }
    )
    $tools = @{}
    foreach ($definition in $definitions) {
        $executable = & $definition.Probe
        if (-not $executable) {
            if (-not $Install) {
                throw "Missing $($definition.Name). Run setup.ps1 without -CheckOnly to install prerequisites."
            }
            $winget = Find-SetupCommand @("winget.exe")
            if (-not $winget) {
                throw "Missing $($definition.Name) and winget. Install/update Microsoft App Installer from Microsoft Store, or install the prerequisites manually, then rerun."
            }
            Write-Host "Installing $($definition.Name) with winget. Windows may request administrator consent."
            Invoke-SetupCommand $winget @(
                "install", "--id", $definition.Id, "--exact", "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
            ) "Installing $($definition.Name)" | Out-Host
            Update-SetupPath
            $executable = & $definition.Probe
            if (-not $executable) {
                throw "$($definition.Name) is still unavailable. Reopen PowerShell (or restart Windows if requested by the installer), then rerun setup.ps1."
            }
        }
        $tools[$definition.Id] = $executable
        Write-Host "[OK] $($definition.Name)"
    }
    return $tools
}

function Initialize-SetupRepository {
    param([string]$Git, [string]$Directory)
    $repository = "https://github.com/chunxli/AOAI-API-Swagger.git"
    if (-not (Test-Path -LiteralPath $Directory) -or
        @(Get-ChildItem -LiteralPath $Directory -Force).Count -eq 0) {
        Invoke-SetupCommand $Git @("clone", "--", $repository, $Directory) "Cloning repository" | Out-Host
    } else {
        $root = Invoke-SetupCommand $Git @("-C", $Directory, "rev-parse", "--show-toplevel") "Checking existing repository"
        if ([IO.Path]::GetFullPath([string]$root).TrimEnd("\", "/") -ne $Directory.TrimEnd("\", "/")) {
            throw "Install directory is not the repository root. Choose an empty directory with -InstallDirectory."
        }
        $origin = Invoke-SetupCommand $Git @("-C", $Directory, "remote", "get-url", "origin") "Checking repository origin"
        if ([string]$origin -notin @($repository, "https://github.com/chunxli/AOAI-API-Swagger", "git@github.com:chunxli/AOAI-API-Swagger.git")) {
            throw "Existing directory has a different origin. Choose an empty -InstallDirectory; no existing files were overwritten."
        }
        Write-Host "Reusing existing checkout; no pull, reset, or checkout will be performed."
    }
    foreach ($required in @("server.py", "start.ps1", "service.ps1", "requirements-lock.txt", "package.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Directory $required) -PathType Leaf)) {
            throw "Repository is missing $required. Verify the checkout before rerunning."
        }
    }
}

function Test-SetupAzureSession {
    param([string]$Az)
    $ErrorActionPreference = "Continue"
    & $Az account get-access-token --resource https://management.azure.com/ --output none --only-show-errors 2>$null
    return $LASTEXITCODE -eq 0
}

function Connect-SetupAzure {
    param([string]$Az)
    $accountsJson = Invoke-SetupCommand $Az @("account", "list", "--output", "json", "--only-show-errors") "Reading Azure accounts"
    $accounts = @($accountsJson | ConvertFrom-Json | Where-Object { $_.state -eq "Enabled" })
    $needsLogin = $accounts.Count -eq 0
    if (-not $needsLogin) {
        $needsLogin = -not (Test-SetupAzureSession $Az)
    }
    if ($needsLogin) {
        Write-Host "Azure sign-in required. Complete the browser sign-in and subscription selection when prompted."
        Invoke-SetupCommand $Az @("login", "--output", "none", "--only-show-errors") "Azure login" | Out-Host
    } else {
        Write-Host "[OK] Reusing valid Azure CLI sign-in."
    }
    Invoke-SetupCommand $Az @(
        "account", "get-access-token", "--resource", "https://management.azure.com/",
        "--output", "none", "--only-show-errors"
    ) "Verifying Azure sign-in" | Out-Host
    $accountsJson = Invoke-SetupCommand $Az @("account", "list", "--output", "json", "--only-show-errors") "Reading Azure subscriptions"
    if (@($accountsJson | ConvertFrom-Json | Where-Object { $_.state -eq "Enabled" }).Count -eq 0) {
        throw "No enabled Azure subscriptions. Check your tenant, subscription access, and az login before retrying."
    }
}

function Install-SetupDependencies {
    param([string]$Python, [string]$Npm, [string]$Directory)
    $venv = Join-Path $Directory ".venv"
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venv)) {
        Invoke-SetupCommand $Python @("-m", "venv", $venv) "Creating virtual environment" | Out-Host
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Existing .venv is incomplete. Repair it or choose a fresh install directory; setup will not delete it."
    }
    Invoke-SetupCommand $venvPython @(
        "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
    ) "Checking virtual environment Python version" | Out-Host
    Invoke-SetupCommand $venvPython @(
        "-m", "pip", "install", "-r", (Join-Path $Directory "requirements-lock.txt")
    ) "Installing Python dependencies" | Out-Host
    Push-Location $Directory
    try {
        Invoke-SetupCommand $Npm @(
            "install", "--ignore-scripts", "--no-audit", "--no-fund"
        ) "Installing Swagger UI assets" | Out-Host
    } finally {
        Pop-Location
    }
}

function Start-SetupApplication {
    param([string]$Directory)
    $python = Join-Path $Directory ".venv\Scripts\python.exe"
    $port = Invoke-SetupCommand $python @((Join-Path $Directory "server.py"), "--print-port") "Reading application settings"
    $url = "http://127.0.0.1:$([int]$port)"
    & (Join-Path $Directory "service.ps1") restart
    $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 10
    if (-not $status.session_token -or -not $status.schema_version) {
        throw "The process at $url did not return the expected WebApp health response."
    }
    Write-Host ""
    Write-Host "Setup complete: $url"
    Write-Host "Select your subscription, Endpoint, and deployment in the page."
    Write-Host "No model inference was sent and Windows autostart was not changed."
    Start-Process $url
}

function Invoke-WebAppSetup {
    param([string]$Directory, [switch]$PreflightOnly)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "This installer supports Windows only."
    }
    if (-not $Directory) {
        if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot "server.py")) -and
            (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".git"))) {
            $Directory = $PSScriptRoot
        } else {
            $Directory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "AzureOpenAILab"
        }
    }
    $Directory = [IO.Path]::GetFullPath($Directory)
    Write-Host "Install directory: $Directory"
    $tools = Get-SetupTools -Install:(-not $PreflightOnly)
    if ($PreflightOnly) {
        Write-Host "Preflight passed. No clone, installation, login, startup, or settings changes were performed."
        return
    }
    Write-Host "[1/4] Preparing repository (private repositories require GitHub access)."
    Initialize-SetupRepository $tools["Git.Git"] $Directory
    Write-Host "[2/4] Installing project dependencies."
    Install-SetupDependencies $tools["Python.Python.3.12"] $tools["OpenJS.NodeJS.LTS"] $Directory
    Write-Host "[3/4] Checking Azure sign-in."
    Connect-SetupAzure $tools["Microsoft.AzureCLI"]
    Write-Host "[4/4] Starting the WebApp."
    Start-SetupApplication $Directory
}

if ($MyInvocation.InvocationName -ne ".") {
    try {
        Invoke-WebAppSetup -Directory $InstallDirectory -PreflightOnly:$CheckOnly
    } catch {
        Write-Error "Setup failed: $($_.Exception.Message)" -ErrorAction Continue
        exit 1
    }
}
