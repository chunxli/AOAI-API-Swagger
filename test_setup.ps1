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

Test-SetupCase "Native command failures include optional recovery guidance" {
    $shell = (Get-Process -Id $PID).Path
    Assert-SetupThrows {
        Invoke-SetupCommand $shell @("-NoProfile", "-Command", "exit 7") "Test command" "Use -NpmRegistryPrefix for approved registry mapping."
    } "exit 7.*[\s\S]*NpmRegistryPrefix"
}

Test-SetupCase "Registry prefix accepts only HTTPS URLs without credentials, queries or fragments" {
    Assert-Setup ((Resolve-SetupNpmRegistryPrefix "") -eq "") "Missing mapping must remain disabled."
    Assert-Setup ((Resolve-SetupNpmRegistryPrefix "https://packages.example.test/feed/npm/registry/") -eq "https://packages.example.test/feed/npm/registry") "Registry prefix was not normalized."
    foreach ($prefix in @(
        "http://packages.example.test/feed", "packages.example.test", " ",
        "https://user:secret@packages.example.test/feed",
        "https://packages.example.test/feed?token=secret",
        "https://packages.example.test/feed#fragment"
    )) {
        Assert-SetupThrows { Resolve-SetupNpmRegistryPrefix $prefix } "absolute HTTPS"
    }
}

Test-SetupCase "Invalid registry prefix fails before any tool installation or repository changes" {
    function Get-SetupTools { throw "Must not install tools" }
    function Initialize-SetupRepository { throw "Must not update code" }
    Assert-SetupThrows {
        Invoke-WebAppSetup -Directory $PSScriptRoot -NpmRegistryPrefix "http://packages.example.test/feed"
    } "absolute HTTPS"
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
    Invoke-WebAppSetup -Directory $PSScriptRoot -PreflightOnly -NpmRegistryPrefix "https://packages.example.test/feed"
}

Test-SetupCase "Full setup executes repository, dependencies, login, and start in order" {
    $script:stages = @()
    function Get-SetupTools {
        param([switch]$Install)
        Assert-Setup ([bool]$Install) "Normal setup must provision missing tools."
        return @{}
    }
    function Initialize-SetupRepository { $script:stages += "clone" }
    function Install-SetupDependencies {
        param($Python, $Npm, $Directory, $NpmRegistryPrefix)
        Assert-Setup ($NpmRegistryPrefix -eq "https://packages.example.test/feed") "Mapping was not passed to dependency installation."
        $script:stages += "install"
    }
    function Connect-SetupAzure { $script:stages += "login" }
    function Start-SetupApplication { $script:stages += "start" }
    Invoke-WebAppSetup -Directory $PSScriptRoot -NpmRegistryPrefix "https://packages.example.test/feed/"
    Assert-Setup (($script:stages -join ",") -eq "clone,install,login,start") "Incorrect setup order."
}

Test-SetupCase "Failed repository update prevents dependency changes, sign-in and restart" {
    function Get-SetupTools { return @{} }
    function Initialize-SetupRepository { throw "Updating repository failed" }
    function Install-SetupDependencies { throw "Must not install dependencies" }
    function Connect-SetupAzure { throw "Must not sign in" }
    function Start-SetupApplication { throw "Must not restart service" }
    Assert-SetupThrows { Invoke-WebAppSetup -Directory $PSScriptRoot } "Updating repository failed"
}

Test-SetupCase "Background service hides its console and redirects unbuffered logs to files" {
    . (Join-Path $PSScriptRoot "service.ps1")
    $script:backgroundStarted = $false
    function Assert-Prerequisites {}
    function Get-ManagedProcess { return $null }
    function Get-ConfiguredPort { return 9000 }
    function Get-NetTCPConnection { return $null }
    function New-Item {}
    function Set-Content {
        param($LiteralPath, $Value, $Encoding)
        Assert-Setup ($LiteralPath -eq $pidFile -and $Value -eq 123) "Incorrect managed PID recording."
    }
    function Start-Process {
        param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
        Assert-Setup ($FilePath -eq $python) "Unexpected service interpreter."
        Assert-Setup ($ArgumentList -eq ('-u "{0}" --background' -f $server)) "Service must use unbuffered background mode."
        Assert-Setup ($WindowStyle -eq "Hidden") "Service launch must hide the console."
        Assert-Setup ($WorkingDirectory -eq $PSScriptRoot) "Incorrect service working directory."
        Assert-Setup ($RedirectStandardOutput -eq $stdoutLog -and $RedirectStandardError -eq $stderrLog) "Service logs must go to files."
        $script:backgroundStarted = $true
        return [pscustomobject]@{ Id = 123; HasExited = $false }
    }
    function Wait-UntilHealthy {
        param($Port, $Process)
        Assert-Setup ($Port -eq 9000 -and $Process.Id -eq 123) "Service health check was bypassed."
        return $true
    }
    function Stop-Process { throw "Must not stop any real process in the launch test." }
    Start-ServiceProcess
    Assert-Setup $script:backgroundStarted "Background service was not launched."
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

    Test-SetupCase "Clean existing checkout updates main with a fast-forward-only pull" {
        $directory = Join-Path $fixture "fresh"
        $script:pullCount = 0
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            if ($Arguments -contains "rev-parse") { return $directory }
            if ($Arguments -contains "get-url") { return "https://github.com/chunxli/AOAI-API-Swagger.git" }
            if ($Arguments -contains "branch") { return "main" }
            if ($Arguments -contains "status") {
                Assert-Setup ($Arguments -contains "--porcelain") "Must check local changes."
                return
            }
            if ($Arguments -contains "pull") {
                Assert-Setup (($Arguments -join "|") -eq "-C|$directory|pull|--ff-only|--no-rebase|origin|main") "Unsafe or unexpected update command."
                $script:pullCount++
                return
            }
            throw "Unexpected Git operation."
        }
        Initialize-SetupRepository "mock-git" $directory
        Assert-Setup ($script:pullCount -eq 1) "Existing code was not updated."
    }

    Test-SetupCase "Local tracked, staged or untracked changes stop automatic updates" {
        $directory = Join-Path $fixture "fresh"
        foreach ($change in @(" M server.py", "M  server.py", "?? local-note.txt")) {
            $script:workingTreeChange = $change
            function Invoke-SetupCommand {
                param($Command, $Arguments, $Description)
                if ($Arguments -contains "rev-parse") { return $directory }
                if ($Arguments -contains "get-url") { return "https://github.com/chunxli/AOAI-API-Swagger.git" }
                if ($Arguments -contains "branch") { return "main" }
                if ($Arguments -contains "status") { return $script:workingTreeChange }
                throw "Must not pull or modify a dirty checkout."
            }
            Assert-SetupThrows { Initialize-SetupRepository "mock-git" $directory } "local changes"
        }
    }

    Test-SetupCase "Non-main branches and detached HEAD are not switched or updated" {
        $directory = Join-Path $fixture "fresh"
        foreach ($branch in @("feature/local-work", "")) {
            $script:currentBranch = $branch
            function Invoke-SetupCommand {
                param($Command, $Arguments, $Description)
                if ($Arguments -contains "rev-parse") { return $directory }
                if ($Arguments -contains "get-url") { return "https://github.com/chunxli/AOAI-API-Swagger.git" }
                if ($Arguments -contains "branch") { return $script:currentBranch }
                throw "Must not update another branch."
            }
            Assert-SetupThrows { Initialize-SetupRepository "mock-git" $directory } "main branch"
        }
    }

    Test-SetupCase "Pull failures are surfaced without reset, checkout or stash" {
        $directory = Join-Path $fixture "fresh"
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            if ($Arguments -contains "rev-parse") { return $directory }
            if ($Arguments -contains "get-url") { return "https://github.com/chunxli/AOAI-API-Swagger.git" }
            if ($Arguments -contains "branch") { return "main" }
            if ($Arguments -contains "status") { return }
            if ($Arguments -contains "pull") { throw "Updating repository failed (exit 1)" }
            throw "Unexpected destructive Git operation."
        }
        Assert-SetupThrows { Initialize-SetupRepository "mock-git" $directory } "Updating repository failed"
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

    Test-SetupCase "Real Git clone and update preserve ignored data and reject dirty or divergent checkouts" {
        $git = Find-SetupCommand @("git.exe")
        Assert-Setup ([bool]$git) "Git is required for the offline repository tests."
        $invokeNative = ${function:Invoke-SetupCommand}
        $remote = Join-Path $fixture "origin.git"
        $seed = Join-Path $fixture "seed"
        $directory = Join-Path $fixture "installed app"
        $remoteUri = ([uri]($remote + "\")).AbsoluteUri
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description)
            if ($Arguments -contains "clone" -or $Arguments -contains "pull") {
                $Arguments = @(
                    "-c", "protocol.https.allow=never",
                    "-c", "url.$remoteUri.insteadOf=https://github.com/chunxli/AOAI-API-Swagger.git"
                ) + $Arguments
            }
            & $invokeNative $Command $Arguments $Description
        }
        function Save-SetupFixtureCommit {
            param($Directory, $Message)
            Invoke-SetupCommand $git @("-C", $Directory, "add", "--all") "Staging fixture" | Out-Host
            Invoke-SetupCommand $git @(
                "-C", $Directory, "-c", "user.name=Setup Test", "-c", "user.email=setup@example.test",
                "-c", "commit.gpgsign=false", "commit", "-m", $Message,
                "-m", "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
            ) "Committing fixture" | Out-Host
        }
        Invoke-SetupCommand $git @("init", "--bare", "--initial-branch=main", $remote) "Creating offline remote" | Out-Host
        Invoke-SetupCommand $git @("init", "--initial-branch=main", $seed) "Creating fixture source" | Out-Host
        foreach ($name in @("server.py", "start.ps1", "service.ps1", "requirements-lock.txt", "package.json")) {
            Set-Content -LiteralPath (Join-Path $seed $name) -Value "initial" -Encoding ASCII
        }
        Set-Content -LiteralPath (Join-Path $seed ".gitignore") -Value @(
            "config.json", ".runtime/", ".venv/", "node_modules/"
        ) -Encoding ASCII
        Save-SetupFixtureCommit $seed "Initial fixture"
        Invoke-SetupCommand $git @("-C", $seed, "remote", "add", "origin", $remote) "Connecting offline remote" | Out-Host
        Invoke-SetupCommand $git @("-C", $seed, "push", "origin", "main") "Publishing fixture" | Out-Host
        Initialize-SetupRepository $git $directory
        $configPath = Join-Path $directory "config.json"
        Set-Content -LiteralPath $configPath -Value '{"port":9000}' -Encoding ASCII
        $configHash = (Get-FileHash -LiteralPath $configPath).Hash
        foreach ($name in @(".runtime", ".venv", "node_modules")) {
            $localDirectory = Join-Path $directory $name
            New-Item -ItemType Directory -Path $localDirectory | Out-Null
            Set-Content -LiteralPath (Join-Path $localDirectory "preserved.txt") -Value "keep" -Encoding ASCII
        }
        Set-Content -LiteralPath (Join-Path $seed "server.py") -Value "updated" -Encoding ASCII
        Save-SetupFixtureCommit $seed "Update fixture"
        Invoke-SetupCommand $git @("-C", $seed, "push", "origin", "main") "Publishing update" | Out-Host
        Initialize-SetupRepository $git $directory
        $expected = Invoke-SetupCommand $git @("-C", $seed, "rev-parse", "HEAD") "Reading remote fixture revision"
        $updated = Invoke-SetupCommand $git @("-C", $directory, "rev-parse", "HEAD") "Reading installed revision"
        Assert-Setup ($updated -eq $expected) "Existing code did not reach the new commit."
        Initialize-SetupRepository $git $directory
        Assert-Setup ((Get-FileHash -LiteralPath $configPath).Hash -eq $configHash) "Local configuration was changed."
        foreach ($name in @(".runtime", ".venv", "node_modules")) {
            Assert-Setup (Test-Path -LiteralPath (Join-Path $directory "$name\preserved.txt")) "Ignored installation data was removed."
        }
        $sourcePath = Join-Path $directory "server.py"
        $cleanSource = Get-Content -LiteralPath $sourcePath -Raw
        Add-Content -LiteralPath $sourcePath -Value "local edit" -Encoding ASCII
        Assert-SetupThrows { Initialize-SetupRepository $git $directory } "local changes"
        Assert-Setup ((Get-Content -LiteralPath $sourcePath -Raw).Contains("local edit")) "Local edits were lost."
        Set-Content -LiteralPath $sourcePath -Value $cleanSource -NoNewline -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $directory "service.ps1") -Value "local commit" -Encoding ASCII
        Save-SetupFixtureCommit $directory "Local fixture work"
        $before = Invoke-SetupCommand $git @("-C", $directory, "rev-parse", "HEAD") "Reading local fixture revision"
        Set-Content -LiteralPath (Join-Path $seed "service.ps1") -Value "remote commit" -Encoding ASCII
        Save-SetupFixtureCommit $seed "Divergent fixture update"
        Invoke-SetupCommand $git @("-C", $seed, "push", "origin", "main") "Publishing divergent update" | Out-Host
        Assert-SetupThrows { Initialize-SetupRepository $git $directory } "Updating repository failed"
        $after = Invoke-SetupCommand $git @("-C", $directory, "rev-parse", "HEAD") "Checking preserved revision"
        Assert-Setup ($before -eq $after) "Divergent local commits were changed."
        Assert-Setup (-not (Test-Path -LiteralPath (Join-Path $directory ".git\MERGE_HEAD"))) "Update left a merge in progress."
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
            param($Command, $Arguments, $Description, $FailureHint)
            $script:installCalls += ,@($Arguments)
            if ($Command -eq "mock-npm") {
                Assert-Setup ($FailureHint -match "EALLOWREMOTE" -and $FailureHint -match "NpmRegistryPrefix") "npm failures need specific recovery guidance."
            }
        }
        Install-SetupDependencies "mock-python" "mock-npm" $directory
        Assert-Setup ($script:installCalls.Count -eq 3) "Incorrect dependency stages."
        Assert-Setup ($script:installCalls[1] -contains (Join-Path $directory "requirements-lock.txt")) "Missing lockfile."
        Assert-Setup ($script:installCalls[2] -contains "--ignore-scripts") "npm lifecycle scripts must stay disabled."
        Assert-Setup (@($script:installCalls[2] | Where-Object { $_ -like "--registry*" }).Count -eq 0) "Setup must honor the configured npm registry."
        Assert-Setup (@($script:installCalls[2] | Where-Object { $_ -like "--*ssl*" }).Count -eq 0) "Setup must not override npm TLS verification."
        Assert-Setup (@($script:installCalls[2] | Where-Object { $_ -like "--allow-*" -or $_ -like "--replace-registry-host*" }).Count -eq 0) "Default setup must not change download permissions or registry mappings."
        Assert-Setup ($PWD.Path -eq $location) "Working directory was not restored."
    }

    Test-SetupCase "Explicit mapping affects only this npm install and never changes npm configuration" {
        $directory = Join-Path $fixture "dependencies"
        $script:mappedNpmCalls = 0
        $location = $PWD.Path
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description, $FailureHint)
            if ($Command -ne "mock-npm") { return }
            Assert-Setup ($Arguments[0] -eq "install") "Setup must not write npm configuration."
            Assert-Setup ($Arguments -contains "--replace-registry-host=https://packages.example.test/feed/npm/registry") "Missing exact source prefix mapping."
            Assert-Setup (@($Arguments | Where-Object { $_ -like "--allow-*" -or $_ -like "--*ssl*" -or $_ -like "--registry*" }).Count -eq 0) "Mapping must not override permissions, TLS or the configured registry."
            $script:mappedNpmCalls++
        }
        Install-SetupDependencies "mock-python" "mock-npm" $directory -NpmRegistryPrefix "https://packages.example.test/feed/npm/registry/"
        Assert-Setup ($script:mappedNpmCalls -eq 1) "Expected exactly one mapped install."
        Assert-Setup (-not (Test-Path -LiteralPath (Join-Path $directory ".npmrc"))) "Setup must not create persistent registry configuration."
        Assert-Setup ($PWD.Path -eq $location) "Working directory was not restored."
    }

    Test-SetupCase "Mapped npm failures are not retried with weaker settings" {
        $directory = Join-Path $fixture "dependencies"
        $location = $PWD.Path
        $script:failedNpmCalls = 0
        function Invoke-SetupCommand {
            param($Command, $Arguments, $Description, $FailureHint)
            if ($Command -eq "mock-npm") {
                $script:failedNpmCalls++
                throw "Installing Swagger UI assets failed (exit 1). $FailureHint"
            }
        }
        Assert-SetupThrows {
            Install-SetupDependencies "mock-python" "mock-npm" $directory -NpmRegistryPrefix "https://packages.example.test/feed"
        } "exit 1.*[\s\S]*EALLOWREMOTE"
        Assert-Setup ($script:failedNpmCalls -eq 1) "Setup must not silently retry or change registries."
        Assert-Setup ($PWD.Path -eq $location) "Failed install did not restore the working directory."
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
