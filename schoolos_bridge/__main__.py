"""python -m schoolos_bridge --config /etc/schoolos-bridge.toml"""

import argparse
import logging
import threading
import time

from .cloud import CloudClient
from .config import load_config
from .drivers import DriverError, build_driver
from .listener import make_server
from .service import Bridge
from .state import State

log = logging.getLogger("schoolos_bridge")


def main() -> None:
    parser = argparse.ArgumentParser(prog="schoolos_bridge")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
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
