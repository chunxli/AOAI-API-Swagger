#Requires -Version 5.1
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "setup.ps1")

function Assert-Setup {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-SetupThrows {
    param([scriptblock]$Action, [string]$Pattern)
    $failure = $null
    try { & $Action } catch { $failure = $_ }
    Assert-Setup ($null -ne $failure) "Expected an explicit failure."
    Assert-Setup ($failure.Exception.Message -match $Pattern) "Unexpected failure: $failure"
}

function Test-SetupCase {
    param([string]$Name, [scriptblock]$Action)
    & $Action
    Write-Host "[PASS] $Name"
}

Test-SetupCase "Multiple executable matches return only the first path" {
    function Get-Command {
        param($Name, $CommandType, $ErrorAction)
        Assert-Setup ($CommandType -eq "Application") "Only application commands should be discovered."
        return @(
            [pscustomobject]@{ Source = "C:\Windows\py.exe" }
            [pscustomobject]@{ Source = "C:\Example\WindowsApps\py.exe" }
        )
    }
    $command = Find-SetupCommand @("py.exe")
    Assert-Setup ($command -is [string]) "Command discovery must return one path, not an array."
    Assert-Setup ($command -eq "C:\Windows\py.exe") "Executable search order was not preserved."
}

Test-SetupCase "Command discovery tries alternate names when the first is missing" {
    function Get-Command {
        param($Name, $CommandType, $ErrorAction)
        if ($Name -eq "az.exe") { return [pscustomobject]@{ Source = "C:\Example\az.exe" } }
    }
    Assert-Setup ((Find-SetupCommand @("az.cmd", "az.exe")) -eq "C:\Example\az.exe") "Alternate executable was not found."
    Assert-Setup ($null -eq (Find-SetupCommand @("missing.exe"))) "Missing commands must not be reported as installed."
}

Test-SetupCase "Python detection invokes one launcher when multiple are installed" {
    function Get-Command {
        param($Name, $CommandType, $ErrorAction)
        if ($Name -eq "py.exe") {
            return @(
                [pscustomobject]@{ Source = "Invoke-MockSetupLauncher" }
                [pscustomobject]@{ Source = "unexpected-second-launcher.exe" }
            )
        }
    }
    function Invoke-MockSetupLauncher {
        Assert-Setup ($args[0] -eq "-3" -and $args[1] -eq "-c") "Incorrect Python launcher arguments."
        $global:LASTEXITCODE = 0
        return "Invoke-MockSetupPython"
    }
    function Invoke-MockSetupPython {
        Assert-Setup ($args[0] -eq "-c" -and $args[1].Contains("sys.version_info >= (3, 10)")) "Python version was not checked."
        $global:LASTEXITCODE = 0
        return "Invoke-MockSetupPython"
    }
    Assert-Setup ((Find-SetupPython) -eq "Invoke-MockSetupPython") "Python launcher discovery failed."
}

Test-SetupCase "Native command failures are not ignored" {
    $shell = (Get-Process -Id $PID).Path
    Assert-SetupThrows {
        Invoke-SetupCommand $shell @("-NoProfile", "-Command", "exit 7") "Test command"
    } "exit 7"
}

Test-SetupCase "Missing prerequisite without winget fails explicitly" {
    function Find-SetupCommand { return $null }
    Assert-SetupThrows { Get-SetupTools -Install } "winget"
    Assert-SetupThrows { Get-SetupTools } "Missing Git"
}

Test-SetupCase "Missing tool is installed with the exact winget package" {
    $script:installed = $false
    function Find-SetupCommand {
        param([string[]]$Names)
        if ($Names[0] -eq "git.exe" -and -not $script:installed) { return $null }
        return "mock-$($Names[0])"
    }
    function Find-SetupPython { return "mock-python" }
    function Update-SetupPath {}
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        Assert-Setup ($Command -eq "mock-winget.exe") "Unexpected installer."
        Assert-Setup ($Arguments -contains "Git.Git") "Missing exact package ID."
        Assert-Setup ($Arguments -contains "--accept-source-agreements") "Missing source agreement flag."
        $script:installed = $true
    }
    $tools = Get-SetupTools -Install
    Assert-Setup ($script:installed -and $tools.Count -eq 4) "Tool provisioning incomplete."
}

Test-SetupCase "CheckOnly performs no clone, install, sign-in or startup" {
    function Get-SetupTools {
        param([switch]$Install)
        Assert-Setup (-not $Install) "CheckOnly must not install tools."
        return @{}
    }
    function Initialize-SetupRepository { throw "Must not clone" }
    function Install-SetupDependencies { throw "Must not install dependencies" }
    function Connect-SetupAzure { throw "Must not sign in" }
    function Start-SetupApplication { throw "Must not start service" }
    Invoke-WebAppSetup -Directory $PSScriptRoot -PreflightOnly
}

Test-SetupCase "Full setup executes repository, dependencies, login, and start in order" {
    $script:stages = @()
    function Get-SetupTools {
        param([switch]$Install)
        Assert-Setup ([bool]$Install) "Normal setup must provision missing tools."
        return @{}
    }
    function Initialize-SetupRepository { $script:stages += "clone" }
    function Install-SetupDependencies { $script:stages += "install" }
    function Connect-SetupAzure { $script:stages += "login" }
    function Start-SetupApplication { $script:stages += "start" }
    Invoke-WebAppSetup -Directory $PSScriptRoot
    Assert-Setup (($script:stages -join ",") -eq "clone,install,login,start") "Incorrect setup order."
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ("aoai-setup-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    Test-SetupCase "Fresh setup clones into the chosen directory" {
        $directory = Join-Path $fixture "fresh"
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            Assert-Setup ($Arguments[0] -eq "clone") "Expected clone."
            Assert-Setup ($Arguments[2] -eq "https://github.com/chunxli/AOAI-API-Swagger.git") "Unexpected repository."
            New-Item -ItemType Directory -Path $directory | Out-Null
            foreach ($name in @("server.py", "start.ps1", "service.ps1", "requirements-lock.txt", "package.json")) {
                New-Item -ItemType File -Path (Join-Path $directory $name) | Out-Null
            }
        }
        Initialize-SetupRepository "mock-git" $directory
        Assert-Setup (Test-Path -LiteralPath (Join-Path $directory "server.py")) "Clone not validated."
    }

    Test-SetupCase "Existing checkout is reused without pull or destructive changes" {
        $directory = Join-Path $fixture "fresh"
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            if ($Arguments -contains "rev-parse") { return $directory }
            if ($Arguments -contains "get-url") { return "https://github.com/chunxli/AOAI-API-Swagger.git" }
            throw "Unexpected Git operation."
        }
        Initialize-SetupRepository "mock-git" $directory
    }

    Test-SetupCase "Existing directory with another origin is rejected" {
        $directory = Join-Path $fixture "fresh"
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            if ($Arguments -contains "rev-parse") { return $directory }
            return "https://example.com/another.git"
        }
        Assert-SetupThrows { Initialize-SetupRepository "mock-git" $directory } "different origin"
        Assert-Setup (Test-Path -LiteralPath (Join-Path $directory "server.py")) "Existing files removed."
    }

    Test-SetupCase "Incomplete virtual environment is not deleted or silently replaced" {
        $directory = Join-Path $fixture "broken"
        New-Item -ItemType Directory -Path (Join-Path $directory ".venv") | Out-Null
        function Invoke-SetupCommand { throw "Must not install into broken environment." }
        Assert-SetupThrows { Install-SetupDependencies "mock-python" "mock-npm" $directory } "incomplete"
        Assert-Setup (Test-Path -LiteralPath (Join-Path $directory ".venv")) "Existing .venv was removed."
    }

    Test-SetupCase "Dependency installer uses pinned Python requirements and configured npm without scripts" {
        $directory = Join-Path $fixture "dependencies"
        New-Item -ItemType Directory -Path (Join-Path $directory ".venv\Scripts") -Force | Out-Null
        New-Item -ItemType File -Path (Join-Path $directory ".venv\Scripts\python.exe") | Out-Null
        $script:installCalls = @()
        $location = $PWD.Path
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            $script:installCalls += ,@($Arguments)
        }
        Install-SetupDependencies "mock-python" "mock-npm" $directory
        Assert-Setup ($script:installCalls.Count -eq 3) "Incorrect dependency stages."
        Assert-Setup ($script:installCalls[1] -contains (Join-Path $directory "requirements-lock.txt")) "Missing lockfile."
        Assert-Setup ($script:installCalls[2] -contains "--ignore-scripts") "npm lifecycle scripts must stay disabled."
        Assert-Setup (@($script:installCalls[2] | Where-Object { $_ -like "--registry*" }).Count -eq 0) "Setup must honor the configured npm registry."
        Assert-Setup (@($script:installCalls[2] | Where-Object { $_ -like "--*ssl*" }).Count -eq 0) "Setup must not override npm TLS verification."
        Assert-Setup ($PWD.Path -eq $location) "Working directory was not restored."
    }

    Test-SetupCase "Startup honors the configured port and checks health before opening the browser" {
        $directory = Join-Path $fixture "startup"
        New-Item -ItemType Directory -Path $directory | Out-Null
        Set-Content -LiteralPath (Join-Path $directory "service.ps1") -Value 'param($Action) if ($Action -ne "restart") { throw "Expected restart" }' -Encoding ASCII
        $script:openedUrl = $null
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            Assert-Setup ($Arguments -contains "--print-port") "Port must use the shared settings loader."
            return "9000"
        }
        function Invoke-RestMethod {
            param($Uri, $TimeoutSec)
            Assert-Setup ($Uri -eq "http://127.0.0.1:9000/api/status") "Wrong health endpoint."
            return @{ session_token = "offline-session"; schema_version = "3.2.0" }
        }
        function Start-Process {
            param($FilePath)
            $script:openedUrl = $FilePath
        }
        Start-SetupApplication $directory
        Assert-Setup ($script:openedUrl -eq "http://127.0.0.1:9000") "Browser URL does not match configured port."
    }

    Test-SetupCase "Unexpected health response prevents success and browser launch" {
        $directory = Join-Path $fixture "startup"
        function Invoke-SetupCommand { return "9000" }
        function Invoke-RestMethod { return @{ other_application = $true } }
        function Start-Process { throw "Must not open browser before successful health check." }
        Assert-SetupThrows { Start-SetupApplication $directory } "expected WebApp health"
    }
} finally {
    Remove-Item -LiteralPath $fixture -Recurse -Force
}

Test-SetupCase "Valid cached Azure login is reused without printing tokens" {
    function Test-SetupAzureSession { return $true }
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        Assert-Setup ($Arguments[0] -ne "login") "Unexpected login prompt."
        if ($Arguments -contains "get-access-token") {
            Assert-Setup ($Arguments -contains "none") "Token output must be suppressed."
        } else {
            return '[{"state":"Enabled"}]'
        }
    }
    Connect-SetupAzure "mock-az"
}

Test-SetupCase "No accounts triggers browser login, then verifies subscription access" {
    $script:loggedIn = $false
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        if ($Arguments[0] -eq "login") { $script:loggedIn = $true; return }
        if ($Arguments -contains "list") {
            if ($script:loggedIn) { return '[{"state":"Enabled"}]' }
            return "[]"
        }
    }
    Connect-SetupAzure "mock-az"
    Assert-Setup $script:loggedIn "Login was not requested."
}

Test-SetupCase "Expired session triggers reauthentication" {
    $script:loggedIn = $false
    function Test-SetupAzureSession { return $false }
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        if ($Arguments[0] -eq "login") { $script:loggedIn = $true; return }
        if ($Arguments -contains "list") { return '[{"state":"Enabled"}]' }
    }
    Connect-SetupAzure "mock-az"
    Assert-Setup $script:loggedIn "Expired session was not reauthenticated."
}

Test-SetupCase "Failed or cancelled Azure login stops setup" {
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        if ($Arguments[0] -eq "login") { throw "Azure login failed (exit 1)" }
        return "[]"
    }
    Assert-SetupThrows { Connect-SetupAzure "mock-az" } "Azure login failed"
}

Test-SetupCase "Successful authentication with no enabled subscriptions fails explicitly" {
    function Invoke-SetupCommand {
        param($Command, $Arguments, $Description)
        if ($Arguments -contains "list") { return "[]" }
    }
    Assert-SetupThrows { Connect-SetupAzure "mock-az" } "No enabled Azure subscriptions"
}

Write-Host "All setup tests passed. No software was installed, no repository was cloned remotely, and no Azure login or service changes were made."
