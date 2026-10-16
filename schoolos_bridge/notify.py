"""Best-effort desktop notification. Never raises, never blocks the relay.

Only meaningful from a process in the user's desktop session (the double-click
launcher / installer). The SYSTEM service on Windows has no desktop to show on.
"""

import subprocess
import sys

TITLE = "SchoolOS"


def _command(title: str, message: str) -> list[str] | None:
    if sys.platform == "win32":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
            f"$n.ShowBalloonTip(8000,'{title}','{message}',[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 9;$n.Dispose()"
        )
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
    if sys.platform == "darwin":
        return ["osascript", "-e", f'display notification "{message}" with title "{title}"']
    return ["notify-send", title, message]


def send(message: str, title: str = TITLE) -> None:
    command = _command(title.replace("'", "").replace('"', ""), message.replace("'", "").replace('"', ""))
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
    except (OSError, ValueError):
        pass
