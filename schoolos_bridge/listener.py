"""LAN listening host: terminals POST each event here the moment it happens
(the fast path; the poll in `Bridge.tick` is the catch-up)."""

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("schoolos_bridge.listener")

MAX_BODY = 2 * 1024 * 1024
#: A terminal that opens a connection and stalls must not pin a worker thread.
SOCKET_TIMEOUT = 30


def make_server(bridge, host: str, port: int) -> ThreadingHTTPServer:
    by_ip = {d.ip: d.serial for d in bridge.config.devices}

    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT

        def _read_body(self) -> bytes:
            """Read the declared body, bounded on both sides.

            A hostile or broken client can send a negative ``Content-Length``
            (``rfile.read(-1)`` would block until it hangs up) or claim a body
            larger than we are willing to buffer (the remainder would then be
            parsed as the next request on a keep-alive connection). Clamp to
            ``[0, MAX_BODY]`` and hang up when we truncate."""
            raw = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw)
            except ValueError:
                return b""
            if length <= 0:
                return b""
            if length > MAX_BODY:
                self.close_connection = True
                length = MAX_BODY
            return self.rfile.read(length)

        def do_POST(self):  # noqa: N802 — http.server API
            body = self._read_body()
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
