"""LAN listening host: terminals POST each event here the moment it happens
(the fast path; the poll in `Bridge.tick` is the catch-up)."""

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("schoolos_bridge.listener")

MAX_BODY = 2 * 1024 * 1024


def make_server(bridge, host: str, port: int) -> ThreadingHTTPServer:
    by_ip = {d.ip: d.serial for d in bridge.config.devices}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY))
            serial = by_ip.get(self.client_address[0])
            if serial is not None and serial in bridge.drivers:
                try:
                    events = bridge.drivers[serial].parse_push(body, self.headers.get("Content-Type", ""))
                    if events:
                        bridge.collect(serial, events, advance=False)
                except Exception:
                    log.exception("could not parse push from %s", self.client_address[0])
            else:
                log.warning("push from unknown address %s ignored", self.client_address[0])
            # Always 200: a terminal that gets errors retries and backs off.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)
