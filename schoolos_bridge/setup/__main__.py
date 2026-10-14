"""Start the bridge's local setup wizard.

    python -m schoolos_bridge.setup                 # config at the install path
    python -m schoolos_bridge.setup --config ./config.toml --port 8098

Open the printed URL from a browser on the same network. The wizard never shows
the config file's secrets back to the browser and refuses `/api/*` calls without
the printed PIN (unless `--no-auth` is given, for a trusted local machine).
"""

import argparse
import logging
import secrets

from ..config import default_config_path
from . import discovery
from .server import make_server

log = logging.getLogger("schoolos_bridge.setup")


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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
