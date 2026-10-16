"""The single ``schoolos-bridge`` program.

    schoolos-bridge                 # double-click: install (Windows) or run, open the setup page
    schoolos-bridge run             # relay + terminal listener + setup page (what services run)
    schoolos-bridge open [--print]  # sign in to the running bridge's setup page
    schoolos-bridge check           # print the self-checks
    schoolos-bridge install | uninstall    # Windows: scheduled task as SYSTEM
    schoolos-bridge --self-test | --version

The setup page is on http://127.0.0.1:8098 by default; ``--ui-host 0.0.0.0``
opens it to the LAN (a headless Linux box, say). Sign-in is a one-time link —
see ``setup.auth``.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from . import __version__, notify
from .config import default_config_path
from .logutil import setup_logging

log = logging.getLogger("schoolos_bridge")

STARTED = "SchoolOS ko'prig'i ishladi"
UI_HOST = "127.0.0.1"
UI_PORT = 8098
REQUIRED_ASSETS = ("index.html", "app.js", "style.css")
WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schoolos-bridge", description="SchoolOS bridge")
    parser.add_argument("command", nargs="?", default="",
                        choices=["", "run", "open", "check", "install", "uninstall"])
    parser.add_argument("--config", default=str(default_config_path()),
                        help="config file (default: %(default)s)")
    parser.add_argument("--log-file", help="also append logs to this file (rotated at 2 MB)")
    parser.add_argument("--ui-host", default=UI_HOST, help="setup page address (default: %(default)s)")
    parser.add_argument("--ui-port", type=int, default=UI_PORT)
    parser.add_argument("--no-ui", action="store_true", help="run without the setup page")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--print", action="store_true", dest="print_link",
                        help="open: print the sign-in link instead of opening a browser")
    parser.add_argument("--once", action="store_true", help="run a single relay tick and exit")
    parser.add_argument("--self-test", action="store_true", help="check the build is intact, then exit")
    parser.add_argument("--version", action="version", version=f"schoolos-bridge {__version__}")
    return parser


# -- helpers -------------------------------------------------------------------
def _shown_host(host: str) -> str:
    if host in ("0.0.0.0", "::", ""):
        from .setup import discovery

        return discovery.primary_ip()
    return host


def _ui_up(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((target, port), 0.5):
            return True
    except OSError:
        return False


def _sign_in_url(args) -> str:
    from .setup.auth import issue_link

    return f"http://{_shown_host(args.ui_host)}:{args.ui_port}/#t={issue_link(args.config)}"


def _say(*lines: str) -> None:
    print("\n".join(("", *lines, "")), flush=True)


def _pause() -> None:
    """Keep a double-clicked console window open long enough to be read."""
    if WINDOWS and FROZEN and sys.stdin and sys.stdin.isatty():
        try:
            input("  Yopish uchun Enter bosing...")
        except EOFError:
            pass


def _open_page(args, wait: float = 0) -> int:
    """Mint a sign-in link for the running bridge and open (or print) it."""
    deadline = time.monotonic() + wait
    while not _ui_up(args.ui_host, args.ui_port):
        if time.monotonic() >= deadline:
            _say(f"  Bridge sozlash sahifasi ishlamayapti ({args.ui_host}:{args.ui_port}).")
            return 1
        time.sleep(0.5)
    try:
        url = _sign_in_url(args)
    except Exception as exc:  # noqa: BLE001 - shown to a person, not logged
        if WINDOWS and not _is_admin():
            from . import winsvc

            if winsvc.elevate("open", "--config", args.config):
                return 0
        _say(f"  Kirish havolasini yaratib bo'lmadi: {exc}",
             "  Administrator (Linux: bridge foydalanuvchisi yoki root) sifatida qayta urinib ko'ring.")
        return 1
    if args.print_link or args.no_browser:
        _say("  Sozlash sahifasi (havola 2 daqiqa amal qiladi, bir marta):", f"  {url}")
    else:
        webbrowser.open(url)
        _say("  Sozlash sahifasi brauzerda ochildi.")
    return 0


def _is_admin() -> bool:
    from . import winsvc

    return winsvc.is_admin()


def _self_test() -> int:
    """Prove the setup page's assets, the drivers and the vault made it into the
    build (a frozen app carries them inside its own bundle)."""
    from .setup.server import STATIC_DIR
    from .vault import self_test as vault_self_test

    missing = [name for name in REQUIRED_ASSETS if not (STATIC_DIR / name).is_file()]
    if missing:
        print(f"self-test FAIL: yetishmayotgan fayllar: {', '.join(missing)} ({STATIC_DIR})", file=sys.stderr)
        return 1
    try:
        from .drivers import hikvision  # noqa: F401

        sealed_with = vault_self_test()
    except Exception as exc:  # noqa: BLE001 - a broken frozen build must fail here, not at runtime
        print(f"self-test FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"self-test OK: {STATIC_DIR} · {sealed_with}")
    return 0


# -- commands ------------------------------------------------------------------
def _run(args, open_browser: bool = False) -> int:
    from .runtime import Runner

    setup_logging(log_file=args.log_file)
    runner = Runner(args.config)
    if args.once:
        runner.tick()
        return 0

    if not args.no_ui:
        from .setup.server import make_server

        try:
            server = make_server(args.config, args.ui_host, args.ui_port, status=runner.status)
        except OSError as exc:
            log.error("setup page: %s:%d band (%s)", args.ui_host, args.ui_port, exc)
        else:
            threading.Thread(target=server.serve_forever, daemon=True, name="setup-ui").start()
            log.info("setup page on http://%s:%d", _shown_host(args.ui_host), args.ui_port)
            if open_browser:
                notify.send(STARTED)
            if open_browser and not args.no_browser:
                threading.Thread(target=_open_page, args=(args, 10), daemon=True).start()
    log.info("%s (v%s)", STARTED, __version__)
    try:
        runner.forever()
    except KeyboardInterrupt:
        _say("  To'xtatildi.")
    return 0


def _double_click(args) -> int:
    """No command: the friendly path for a person, not a service manager."""
    if _ui_up(args.ui_host, args.ui_port):
        # Already running (the installed task, or another window): just sign in.
        code = _open_page(args)
        if code == 0:
            notify.send(STARTED)
        _pause()
        return code
    if WINDOWS and FROZEN:
        from . import winsvc

        if winsvc.task_exists():
            # Installed but still starting (e.g. just after boot): wait for it.
            code = _open_page(args, wait=30)
            _pause()
            return code
        else:
            _say("  SchoolOS bridge o'rnatilmoqda — administrator ruxsatini bering...")
            if winsvc.is_admin():
                return _install(args)
            if winsvc.elevate("install"):
                return 0
            _say("  Ruxsat berilmadi. Bridge hozir shu oynada ishlaydi (oyna yopilsa to'xtaydi).")
    return _run(args, open_browser=True)


def _install(args) -> int:
    from . import winsvc

    if not WINDOWS:
        _say("  'install' faqat Windows uchun. Linux: deploy/schoolos-bridge.service, macOS: deploy/install-macos.sh")
        return 2
    if not winsvc.is_admin():
        return 0 if winsvc.elevate("install") else 1
    try:
        target = winsvc.install()
    except Exception as exc:  # noqa: BLE001 - shown to a person
        _say(f"  O'rnatib bo'lmadi: {exc}")
        _pause()
        return 1
    args.config = str(winsvc.home() / "config.toml")
    _say("  O'rnatildi: Windows bilan birga avtomatik ishga tushadi.", f"  Dastur: {target}")
    code = _open_page(args, wait=30)
    if code == 0:
        notify.send(STARTED)
    _pause()
    return code


def _uninstall(args) -> int:
    from . import winsvc

    if not WINDOWS:
        return 2
    if not winsvc.is_admin():
        return 0 if winsvc.elevate("uninstall") else 1
    winsvc.uninstall()
    _say("  O'chirildi: avtomatik ishga tushish to'xtatildi. Sozlama va loglar saqlanib qoldi.")
    _pause()
    return 0


def _check(args) -> int:
    from .checks import render, run_checks

    results = run_checks(args.config)
    print(render(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0


def main(argv=None) -> None:
    args = _parser().parse_args(argv)
    if args.self_test:
        raise SystemExit(_self_test())
    if args.command == "" and not (sys.stdin and sys.stdin.isatty()):
        # Started by a service manager with no command (an older unit file):
        # never install or pop a browser there, just run.
        args.command = "run"
    commands = {
        "": _double_click,
        "run": _run,
        "open": lambda a: _open_page(a, wait=5),
        "check": _check,
        "install": _install,
        "uninstall": _uninstall,
    }
    raise SystemExit(commands[args.command](args))


if __name__ == "__main__":
    main()
