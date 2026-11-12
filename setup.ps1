#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallDirectory,
    [switch]$CheckOnly,
    [string]$NpmRegistryPrefix
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
    param([string]$Command, [string[]]$Arguments, [string]$Description, [string]$FailureHint)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        $message = "$Description failed (exit $LASTEXITCODE). Resolve the error above and rerun setup.ps1."
        if ($FailureHint) { $message += "`n$FailureHint" }
        throw $message
    }
}

function Resolve-SetupNpmRegistryPrefix {
    param([string]$Prefix)
    if ($Prefix -eq "") { return "" }
    $uri = $null
    if (-not [Uri]::TryCreate($Prefix, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or -not $uri.Host -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "NpmRegistryPrefix must be an absolute HTTPS URL prefix without credentials, query parameters, or fragments."
    }
    return $uri.AbsoluteUri.TrimEnd("/")
}

function Invoke-SetupNpmInstall {
    param([string]$Npm, [string[]]$Arguments)
    $command = Get-Command $Npm -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $global:LASTEXITCODE = $null
    & $command.Source @Arguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        $lines.Add($line)
        Write-Host $line
    }
    if ($null -eq $global:LASTEXITCODE) { throw "Unable to obtain npm installation exit status." }
    return @{ ExitCode = $global:LASTEXITCODE; Output = ($lines -join "`n") }
}

function Find-SetupNpmRegistryPrefix {
    param([string]$Npm, [string]$FailureOutput)
    $failure = [regex]::Match($FailureOutput, 'Refusing to fetch "(?<name>(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)@(?<url>https://[^"\s]+)"')
    if (-not $failure.Success) {
        throw "Cannot automatically map this EALLOWREMOTE failure: a standard registry tarball URL could not be identified. See README: npm proxy compatibility."
    }
    $name = $failure.Groups["name"].Value
    $url = [uri](Resolve-SetupNpmRegistryPrefix $failure.Groups["url"].Value)
    $packageBase = ($name -split "/")[-1]
    $archive = [regex]::Match($url.AbsolutePath,
        "/" + [regex]::Escape("$name/-/$packageBase-") + '(?<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)\.tgz$')
    if (-not $archive.Success) {
        throw "Cannot automatically map a non-standard registry tarball name or version. See README: npm proxy compatibility."
    }
    $version = $archive.Groups["version"].Value
    $mapping = Invoke-SetupCommand $Npm @("config", "get", "replace-registry-host") "Reading npm registry mapping"
    if ([string]$mapping -cne "npmjs") {
        throw "Automatic recovery will not override an existing npm registry mapping. Check the configuration or supply -NpmRegistryPrefix explicitly."
    }
    $registryText = Invoke-SetupCommand $Npm @("config", "get", "registry") "Reading npm registry"
    $registryPrefix = Resolve-SetupNpmRegistryPrefix ([string]$registryText).Trim()
    if (-not $registryPrefix) { throw "Cannot automatically map packages without a configured HTTPS registry." }
    $registry = [uri]$registryPrefix
    if ($name.StartsWith("@")) {
        $scope = ($name -split "/")[0]
        $scopedRegistry = Invoke-SetupCommand $Npm @("config", "get", "${scope}:registry") "Reading scoped npm registry"
        if ($scopedRegistry -and [string]$scopedRegistry -notin @("undefined", "null")) {
            $scopedPrefix = Resolve-SetupNpmRegistryPrefix ([string]$scopedRegistry).Trim()
            if ($scopedPrefix -cne $registryPrefix) {
                throw "Automatic recovery cannot remap a different scoped registry to the default registry. Check your approved registry configuration."
            }
        }
    }
    $json = Invoke-SetupCommand $Npm @(
        "view", "${name}@${version}", "name", "version", "dist", "--json", "--ignore-scripts"
    ) "Verifying registry package metadata"
    $parsed = ($json -join "`n") | ConvertFrom-Json
    $records = @($parsed)
    if ($records.Count -ne 1) {
        throw "Registry metadata must identify exactly one package version; no retry was performed."
    }
    $metadata = $records[0]
    if ($metadata -isnot [pscustomobject] -or $metadata.name -cne $name -or $metadata.version -cne $version -or
        -not $metadata.dist.tarball) {
        throw "Registry metadata does not confirm the failed package identity and exact version; no retry was performed."
    }
    $advertised = [uri](Resolve-SetupNpmRegistryPrefix ([string]$metadata.dist.tarball))
    if ($advertised.AbsoluteUri -cne $url.AbsoluteUri) {
        throw "Registry metadata does not confirm the failed tarball URL; no retry was performed."
    }
    if (-not $metadata.dist.integrity -and [string]$metadata.dist.shasum -notmatch '^[a-fA-F0-9]{40}$') {
        throw "Registry metadata has no package checksum; no automatic retry was performed."
    }
    if ($url.GetLeftPart([UriPartial]::Authority) -eq $registry.GetLeftPart([UriPartial]::Authority) -and
        $url.AbsolutePath.StartsWith($registry.AbsolutePath.TrimEnd("/") + "/", [StringComparison]::Ordinal)) {
        throw "The failed URL already belongs to the configured registry. Automatic prefix mapping does not apply."
    }
    return $url.GetLeftPart([UriPartial]::Authority) + $url.AbsolutePath.Substring(0, $archive.Index)
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
        $branch = Invoke-SetupCommand $Git @("-C", $Directory, "branch", "--show-current") "Checking current branch"
        if ([string]$branch -ne "main") {
            throw "Automatic updates require the main branch. Resolve the current branch or detached HEAD manually; setup will not switch branches."
        }
        $changes = Invoke-SetupCommand $Git @("-C", $Directory, "status", "--porcelain", "--untracked-files=normal") "Checking local changes"
        if ($changes) {
            throw "Existing checkout has local changes. Commit, back up, or move them before retrying; setup will not reset, stash, or overwrite them."
        }
        Write-Host "Existing installation detected. Updating main from origin (fast-forward only)."
        Invoke-SetupCommand $Git @(
            "-C", $Directory, "pull", "--ff-only", "--no-rebase", "origin", "main"
        ) "Updating repository" | Out-Host
        Write-Host "[OK] Application code is up to date."
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
    param([string]$Python, [string]$Npm, [string]$Directory, [string]$NpmRegistryPrefix)
    $prefix = Resolve-SetupNpmRegistryPrefix $NpmRegistryPrefix
    $npmArguments = @("install", "--ignore-scripts", "--no-audit", "--no-fund")
    if ($prefix) {
        $npmArguments += "--replace-registry-host=$prefix"
        Write-Host "Mapping the supplied tarball registry prefix to npm's configured registry for this install only."
        Write-Host "Download permissions, TLS verification, and persistent npm configuration are unchanged."
    }
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
        $hint = "If npm reports EALLOWREMOTE, compare npm --version, npm config get registry, and npm config get allow-remote."
        $hint += " For an approved proxy that serves the same package tarballs, rerun with -NpmRegistryPrefix '<original HTTPS tarball registry prefix>'; see README: npm proxy compatibility."
        $hint += " Setup does not relax download permissions or TLS verification."
        $result = Invoke-SetupNpmInstall $Npm $npmArguments
        if ($result.ExitCode -ne 0 -and -not $prefix -and
            $result.Output -match '(?m)^\s*npm\s+(?:error|ERR!)\s+code\s+EALLOWREMOTE\s*$') {
            Write-Host "npm reported EALLOWREMOTE. Verifying package metadata before one automatic registry-mapping retry."
            $autoPrefix = Find-SetupNpmRegistryPrefix $Npm $result.Output
            Write-Host "Verified package identity, version, checksum availability and source URL. Retrying through npm's configured registry."
            Write-Host "Download permissions, TLS verification, and persistent npm configuration are unchanged."
            $result = Invoke-SetupNpmInstall $Npm ($npmArguments + @("--replace-registry-host=$autoPrefix"))
        }
        if ($result.ExitCode -ne 0) {
            throw "Installing Swagger UI assets failed (exit $($result.ExitCode)). No further retries were attempted.`n$hint"
        }
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
    param([string]$Directory, [switch]$PreflightOnly, [string]$NpmRegistryPrefix)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "This installer supports Windows only."
    }
    $NpmRegistryPrefix = Resolve-SetupNpmRegistryPrefix $NpmRegistryPrefix
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
        Write-Host "Preflight passed. No clone, update, installation, login, startup, or settings changes were performed."
        return
    }
    Write-Host "[1/4] Cloning or updating repository (private repositories require GitHub access)."
    Initialize-SetupRepository $tools["Git.Git"] $Directory
    Write-Host "[2/4] Installing project dependencies."
    Install-SetupDependencies $tools["Python.Python.3.12"] $tools["OpenJS.NodeJS.LTS"] $Directory -NpmRegistryPrefix $NpmRegistryPrefix
    Write-Host "[3/4] Checking Azure sign-in."
    Connect-SetupAzure $tools["Microsoft.AzureCLI"]
    Write-Host "[4/4] Starting the WebApp."
    Start-SetupApplication $Directory
}

if ($MyInvocation.InvocationName -ne ".") {
    try {
        Invoke-WebAppSetup -Directory $InstallDirectory -PreflightOnly:$CheckOnly -NpmRegistryPrefix $NpmRegistryPrefix
    } catch {
        Write-Error "Setup failed: $($_.Exception.Message)" -ErrorAction Continue
        exit 1
    }
}
