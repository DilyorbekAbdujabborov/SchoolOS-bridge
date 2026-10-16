<#
  Scripted install of the *built* bridge (CI / many PCs). By hand you do not
  need this: double-click dist\schoolos-bridge.exe and it installs itself.

    python installer\build.py
    .\deploy\install-windows.ps1              # from an elevated PowerShell in bridge\
    .\deploy\install-windows.ps1 -Uninstall

  Both just call the exe's own `install` / `uninstall` (scheduled task as SYSTEM,
  data folder locked to Administrators + SYSTEM).
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$exe = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "dist") "schoolos-bridge.exe"
if (-not (Test-Path $exe)) {
    throw "Topilmadi: $exe — avval quring: python installer\build.py"
}
if ($Uninstall) {
    & $exe uninstall
} else {
    & $exe install
}
exit $LASTEXITCODE
