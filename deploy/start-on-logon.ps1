[CmdletBinding()]
param(
    [switch]$NoAi,
    [switch]$SkipPull,
    [switch]$AllowDirty,
    [int]$DockerTimeoutSeconds = 180,
    [int]$WeChatTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $DeployDir
$StartScript = Join-Path $DeployDir "start.ps1"
$ListenerScript = Join-Path $DeployDir "run-listener.ps1"
$LogDir = Join-Path $env:LOCALAPPDATA "wechatauto"
$StartupLog = Join-Path $LogDir "startup.log"
$ListenerLog = Join-Path $LogDir "listener.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $Line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $StartupLog -Value $Line -Encoding UTF8
    Write-Host $Line
}

function Wait-Docker {
    $Docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $Docker) {
        throw "docker was not found. Install and start Docker Desktop first."
    }

    & $Docker.Source info *> $null
    if ($LASTEXITCODE -ne 0) {
        $DockerDesktop = @(
            (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\Docker Desktop.exe")
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

        if ($DockerDesktop) {
            Write-Log "Starting Docker Desktop."
            Start-Process -FilePath $DockerDesktop | Out-Null
        }
    }

    for ($Elapsed = 0; $Elapsed -lt $DockerTimeoutSeconds; $Elapsed += 5) {
        & $Docker.Source info *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Log "Docker is ready."
            return
        }
        Start-Sleep -Seconds 5
    }
    throw "Docker Desktop was not ready within $DockerTimeoutSeconds seconds."
}

function Wait-WeChat {
    for ($Elapsed = 0; $Elapsed -lt $WeChatTimeoutSeconds; $Elapsed += 5) {
        if (Get-Process -Name "Weixin" -ErrorAction SilentlyContinue) {
            Write-Log "Weixin.exe detected."
            return
        }
        Start-Sleep -Seconds 5
    }
    throw "Weixin.exe was not found. Start and log in to WeChat first."
}

Push-Location $ProjectDir
try {
    if (-not $SkipPull) {
        $Dirty = @(git status --porcelain)
        if ($Dirty.Count -gt 0 -and -not $AllowDirty) {
            throw "Git has uncommitted changes. Commit them first, or explicitly use -AllowDirty."
        }

        $env:GIT_TERMINAL_PROMPT = "0"
        Write-Log "Pulling the latest code from GitHub."
        $PullOutput = @(git pull --ff-only 2>&1)
        $PullOutput | ForEach-Object { Write-Log ([string]$_) }
        if ($LASTEXITCODE -ne 0) {
            throw "git pull failed. Check network, repository permissions, and the current branch."
        }
    }

    $Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "The .venv Python was not found. Run python -m venv .venv and install dependencies first."
    }

    Write-Log "Synchronizing Python dependencies."
    & $Python -m pip install -e ".[storage,ai]" --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }

    Wait-Docker

    Write-Log "Starting PostgreSQL and MinIO."
    & $StartScript
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to start."
    }

    Wait-WeChat

    $ListenerArgs = @()
    if ($NoAi) {
        $ListenerArgs += "--no-ai"
    }
    Write-Log "Starting the WeChat listener. Log: $ListenerLog"
    & $ListenerScript @ListenerArgs 2>&1 | Tee-Object -FilePath $ListenerLog -Append
    $ListenerExitCode = $LASTEXITCODE
    if ($ListenerExitCode -ne 0) {
        throw "The listener exited with code $ListenerExitCode."
    }
}
catch {
    Write-Log "Startup failed: $($_.Exception.Message)"
    throw
}
finally {
    Pop-Location
}
