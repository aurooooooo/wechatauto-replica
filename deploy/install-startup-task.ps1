[CmdletBinding()]
param(
    [switch]$NoAi,
    [switch]$StartNow,
    [switch]$AllowDirty,
    [string]$TaskName = "WeChatAuto Listener"
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $DeployDir
$StartupScript = Join-Path $DeployDir "start-on-logon.ps1"
$PowerShell = Join-Path $PSHOME "powershell.exe"
$UserId = "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME

if (-not (Test-Path -LiteralPath $StartupScript)) {
    throw "Startup script was not found: $StartupScript"
}

$StartupArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $StartupScript
if ($NoAi) {
    $StartupArguments += " -NoAi"
}
if ($AllowDirty) {
    $StartupArguments += " -AllowDirty"
}

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument $StartupArguments `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Logon task registered: $TaskName"
Write-Host "Startup script: $StartupScript"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
