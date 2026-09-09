$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $DeployDir
$EnvFile = Join-Path $DeployDir ".env"

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Missing deploy\.env. Run deploy\start.ps1 first."
}

# Windows PowerShell 默认按系统 ANSI 读文件；UTF-8 中文注释会吞掉下一行。
foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    $Trimmed = $Line.Trim()
    if (-not $Trimmed -or $Trimmed.StartsWith("#")) {
        continue
    }
    $Pair = $Trimmed.Split("=", 2)
    if ($Pair.Count -eq 2) {
        Set-Item -LiteralPath ("Env:{0}" -f $Pair[0]) -Value $Pair[1]
    }
}

Push-Location $ProjectDir
try {
    python -u -m wechatauto.listen_messages --all @args
}
finally {
    Pop-Location
}