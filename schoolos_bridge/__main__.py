"""python -m schoolos_bridge --config /etc/schoolos-bridge/config.toml"""

import argparse
import logging
import threading
import time

from . import __version__
from .cloud import CloudClient
from .config import default_config_path, load_config
from .drivers import DriverError, build_driver
from .listener import make_server
from .logutil import setup_logging
from .service import Bridge
from .state import State

log = logging.getLogger("schoolos_bridge")


def main() -> None:
    parser = argparse.ArgumentParser(prog="schoolos_bridge")
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="config file (default: %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument(
        "--log-file",
        help="also append logs to this file (rotated at 2 MB) — a Windows "
        "scheduled task has no journal to read",
    )
    parser.add_argument("--version", action="version", version=f"schoolos-bridge {__version__}")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        # A one-line message beats a traceback on a machine nobody can ssh into.
        parser.error(f"{args.config}: {exc}")
    setup_logging(secrets=config.secrets(), log_file=args.log_file)

    drivers = {d.serial: build_driver(d) for d in config.devices}
    for serial, driver in drivers.items():
        try:
            driver.ensure_time()
        except DriverError as exc:
            log.warning("could not check time on %s: %s", serial, exc)
    bridge = Bridge(config, CloudClient(config.cloud_url, config.token), drivers, State(config.state_file))

    if args.once:
        bridge.tick()
        return
    server = make_server(bridge, config.listen_host, config.listen_port)
    threading.Thread(target=server.serve_forever, daemon=True, name="listener").start()
    log.info("bridge up: %d device(s), listening on %s:%d", len(drivers), config.listen_host, config.listen_port)
    while True:
        try:
            bridge.tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
