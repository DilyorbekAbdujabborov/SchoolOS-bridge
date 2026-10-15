"""python -m schoolos_bridge.setup --config ./config.toml --port 8098

Open the printed URL from a browser on the same network. The wizard never shows
the config file's secrets back to the browser and refuses `/api/*` calls without
the printed PIN (unless `--no-auth` is given, for a trusted local machine).

On an installed bridge this is the `schoolos-bridge-setup` executable (or, from
source, `deploy/schoolos-bridge-setup`); `--self-test` just checks the build is
intact and exits.
"""

import argparse
import logging
import secrets
import sys

from .. import __version__
from ..config import default_config_path
from ..logutil import setup_logging
from . import discovery
from .server import STATIC_DIR, make_server

log = logging.getLogger("schoolos_bridge.setup")

REQUIRED_ASSETS = ("index.html", "app.js", "style.css")


def _banner(host, port, pin, config_path, auth):
    if host in ("0.0.0.0", "::", ""):
        shown_host = discovery.primary_ip()
    else:
        shown_host = host
    lines = [
        "",
        "  SchoolOS bridge — mahalliy sozlash ustasi",
        "  " + "─" * 52,
        f"  Manzil:  http://{shown_host}:{port}",
        f"  Sozlama: {config_path}",
    ]
    if auth:
        lines.append(f"  PIN:     {pin}    (brauzerda so'raladi)")
    else:
        lines.append("  PIN:     o'chirilgan (--no-auth)")
    lines += [
        "",
        "  Skanerlash uchun shu kompyuter terminal bilan bir tarmoqda bo'lsin.",
        "  To'xtatish uchun Ctrl+C.",
        "",
    ]
    print("\n".join(lines), flush=True)


def _self_test() -> int:
    """Prove the wizard's web assets made it into the build (a frozen app has
    them inside its own bundle, not next to the package on disk)."""
    missing = [name for name in REQUIRED_ASSETS if not (STATIC_DIR / name).is_file()]
    if missing:
        print(f"self-test FAIL: yetishmayotgan fayllar: {', '.join(missing)} ({STATIC_DIR})", file=sys.stderr)
        return 1
    print(f"self-test OK: {STATIC_DIR}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="schoolos_bridge.setup", description="Bridge setup wizard")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--pin", default="", help="fixed PIN (default: random, printed)")
    parser.add_argument("--no-auth", action="store_true", help="disable the PIN (trusted host only)")
    parser.add_argument(
        "--scan-timeout", type=float, default=discovery.DEFAULT_SWEEP_TIMEOUT,
        help="seconds to wait for each host while sweeping",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="check that the bundled web assets are present, then exit",
    )
    parser.add_argument("--version", action="version", version=f"schoolos-bridge-setup {__version__}")
    args = parser.parse_args()

    if args.self_test:
        raise SystemExit(_self_test())

    setup_logging()
    pin = "" if args.no_auth else (args.pin or f"{secrets.randbelow(1_000_000):06d}")

    server = make_server(args.config, args.host, args.port, pin=pin, scan_timeout=args.scan_timeout)
    _banner(args.host, args.port, pin, args.config, bool(pin))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  To'xtatildi.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
