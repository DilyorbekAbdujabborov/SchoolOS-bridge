<#
  Install the *built* bridge so it starts with Windows.

    # build first, then from an elevated PowerShell in the bridge\ directory:
    python installer\build.py
    .\deploy\install-windows.ps1

  Registers a Scheduled Task that runs as SYSTEM at boot and restarts the
  service if it exits. A plain console program cannot be a real Windows SCM
  service, so this is the no-extra-download way to keep it running; if you
  prefer a classic service, wrap the exe with WinSW or NSSM instead.

    .\deploy\install-windows.ps1 -Uninstall    # remove the task
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$TaskName = "SchoolOS Bridge"
)

$ErrorActionPreference = "Stop"
$root = Join-Path $env:ProgramData "SchoolOS Bridge"
$bin = Join-Path $root "bin"
$logs = Join-Path $root "logs"
$service = Join-Path $bin "schoolos-bridge.exe"
$setup = Join-Path $bin "schoolos-bridge-setup.exe"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator sifatida ishga tushiring / run as Administrator."
}

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "O'chirildi / removed: $TaskName"
    return
}

$dist = Join-Path (Split-Path -Parent $PSScriptRoot) "dist"
foreach ($exe in @("schoolos-bridge.exe", "schoolos-bridge-setup.exe")) {
    if (-not (Test-Path (Join-Path $dist $exe))) {
        throw "Topilmadi: $dist\$exe — avval quring: python installer\build.py"
    }
}

New-Item -ItemType Directory -Force -Path $bin, $logs | Out-Null
Copy-Item -Force (Join-Path $dist "schoolos-bridge.exe") $bin
Copy-Item -Force (Join-Path $dist "schoolos-bridge-setup.exe") $bin
if (-not (Test-Path (Join-Path $root "config.toml"))) {
    Copy-Item (Join-Path (Split-Path -Parent $PSScriptRoot) "config.example.toml") (Join-Path $root "config.toml")
    Write-Host "Sozlamani ustasiz to'ldiring: $setup"
}

$arguments = "--config `"$root\config.toml`" --log-file `"$root\logs\schoolos-bridge.log`""
$action = New-ScheduledTaskAction -Execute $service -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "O'rnatildi / installed: $TaskName"
Write-Host "Log: $root\logs\schoolos-bridge.log"
Write-Host "To'xtatish / stop: Stop-ScheduledTask -TaskName '$TaskName'"
