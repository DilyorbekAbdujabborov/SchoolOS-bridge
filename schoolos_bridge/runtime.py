"""One process does everything: the relay loop, the terminal listener and the
setup page.

The setup page writes the config; the loop notices the file changed and
rebuilds the drivers, the cloud client and the listener in place — no restart
step for the technician. Until a usable config exists the loop just waits (the
setup page is up regardless), so a fresh install starts cleanly too.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import updater
from .cloud import CloudClient
from .config import load_config
from .drivers import DriverError, build_driver
from .listener import make_server
from .logutil import set_secrets
from .service import Bridge
from .setup import store
from .state import State
from .vault import VaultError

log = logging.getLogger("schoolos_bridge")

#: How often the loop looks at the config file while nothing is configured.
IDLE_SECONDS = 2.0


class Runner:
    def __init__(self, config_path, listen=True):
        self.config_path = Path(config_path)
        self.listen = listen
        self.bridge: Bridge | None = None
        self.error: str | None = None
        self.started_at: str | None = None
        self._listener = None
        self._signature = object()  # never equal: the first check always loads
        self._lock = threading.Lock()
        self._last_update_check = None

    # -- config ----------------------------------------------------------------
    def _file_signature(self):
        try:
            stat = self.config_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def reload_if_changed(self) -> bool:
        signature = self._file_signature()
        if signature == self._signature:
            return False
        self._signature = signature
        self._reload()
        return True

    def _reload(self) -> None:
        self._stop()
        if self._signature is None:
            self._set_error("Sozlama hali yo'q — sozlash sahifasida to'ldiring.")
            return
        try:
            # Seal any clear-text secret left by an older build or a hand edit.
            if store.migrate(self.config_path):
                log.info("config: clear-text secrets sealed")
                self._signature = self._file_signature()
            config = load_config(self.config_path)
        except (OSError, ValueError, VaultError) as exc:
            self._set_error(f"Sozlama xatosi: {exc}")
            return
        set_secrets(config.secrets())
        drivers = {d.serial: build_driver(d) for d in config.devices}
        for serial, driver in drivers.items():
            try:
                driver.ensure_time()
            except DriverError as exc:
                log.warning("could not check time on %s: %s", serial, exc)
        bridge = Bridge(config, CloudClient(config.cloud_url, config.token), drivers, State(config.state_file))
        if self.listen:
            try:
                server = make_server(bridge, config.listen_host, config.listen_port)
            except OSError as exc:
                self._set_error(f"{config.listen_host}:{config.listen_port} porti band: {exc}")
                return
            threading.Thread(target=server.serve_forever, daemon=True, name="listener").start()
            self._listener = server
        with self._lock:
            self.bridge = bridge
            self.error = None
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.info(
            "bridge up: %d device(s), listening on %s:%d",
            len(drivers), config.listen_host, config.listen_port,
        )

    def _set_error(self, message: str) -> None:
        with self._lock:
            if message != self.error:
                log.warning("%s", message)
            self.error = message

    def _stop(self) -> None:
        with self._lock:
            self.bridge = None
        if self._listener is not None:
            self._listener.shutdown()
            self._listener.server_close()
            self._listener = None

    # -- loop ------------------------------------------------------------------
    def tick(self) -> None:
        self.reload_if_changed()
        bridge = self.bridge
        if bridge is not None:
            bridge.tick()
            self._maybe_update(bridge)

    def _maybe_update(self, bridge) -> None:
        now = time.monotonic()
        if self._last_update_check is not None and now - self._last_update_check < updater.CHECK_EVERY:
            return
        self._last_update_check = now
        if updater.maybe_update(bridge.cloud):
            self._stop()
            raise SystemExit(updater.RESTART_EXIT_CODE)

    def forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            bridge = self.bridge
            time.sleep(bridge.config.poll_seconds if bridge else IDLE_SECONDS)

    def status(self) -> dict:
        """What the setup page shows at the top: is the relay running?"""
        with self._lock:
            bridge, error, started = self.bridge, self.error, self.started_at
        if bridge is None:
            return {"running": False, "error": error, "devices": 0, "outbox": 0, "since": None}
        return {
            "running": True,
            "error": None,
            "devices": len(bridge.config.devices),
            "outbox": len(bridge.state.outbox),
            "since": started,
        }
