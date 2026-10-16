"""Windows: make the single ``schoolos-bridge.exe`` install itself.

Double-clicking the downloaded exe (see ``__main__``) asks for administrator
rights once (UAC), then:

* copies itself to ``%ProgramData%\\SchoolOS Bridge\\bin``;
* locks that folder (config, keys, logs) to Administrators + SYSTEM only;
* registers a Scheduled Task that runs it as SYSTEM at boot and restarts it if
  it ever exits — a plain console program cannot be an SCM service, and this
  needs no extra download.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "SchoolOS Bridge"
EXE_NAME = "schoolos-bridge.exe"
#: Administrators, SYSTEM — by SID, so a localized Windows still matches.
_ADMINS, _SYSTEM = "*S-1-5-32-544", "*S-1-5-18"


def home() -> Path:
    from .config import _windows_home

    return _windows_home()


def installed_exe() -> Path:
    return home() / "bin" / EXE_NAME


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - not Windows / no shell32
        return False


def elevate(*args: str) -> bool:
    """Re-run this exe with `args` as administrator (shows the UAC prompt).
    Returns False when the user declined or elevation is impossible."""
    import ctypes

    params = subprocess.list2cmdline(list(args))
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return int(result) > 32


def _run(command: list[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, capture_output=True, text=True, creationflags=flags)


def task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def _powershell(script: str) -> None:
    result = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "PowerShell xatosi")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def install() -> Path:
    """Copy, lock down, register and start. Needs administrator rights."""
    root = home()
    bin_dir, logs = root / "bin", root / "logs"
    bin_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    # Owner-only data folder: config.toml, secret.key, ui.key, state.json, logs.
    acl = _run(["icacls", str(root), "/inheritance:r", "/grant:r",
                f"{_ADMINS}:(OI)(CI)F", f"{_SYSTEM}:(OI)(CI)F"])
    if acl.returncode != 0:
        raise RuntimeError(f"icacls: {(acl.stderr or acl.stdout).strip()}")

    target = installed_exe()
    if task_exists():
        _powershell(f"Stop-ScheduledTask -TaskName {_quote(TASK_NAME)} -ErrorAction SilentlyContinue")
    source = Path(sys.executable).resolve()
    if source != target.resolve():
        shutil.copy2(source, target)

    config = root / "config.toml"
    arguments = f'run --config "{config}" --log-file "{logs / "schoolos-bridge.log"}"'
    _powershell(
        "$action = New-ScheduledTaskAction -Execute " + _quote(str(target))
        + " -Argument " + _quote(arguments) + ";"
        "$trigger = New-ScheduledTaskTrigger -AtStartup;"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero);"
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest;"
        f"Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Action $action -Trigger $trigger"
        " -Settings $settings -Principal $principal -Force | Out-Null;"
        f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}"
    )
    return target


def uninstall() -> None:
    """Stop and remove the task. Config, keys and logs stay (re-install keeps them)."""
    _powershell(
        f"Stop-ScheduledTask -TaskName {_quote(TASK_NAME)} -ErrorAction SilentlyContinue;"
        f"Unregister-ScheduledTask -TaskName {_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue"
    )
