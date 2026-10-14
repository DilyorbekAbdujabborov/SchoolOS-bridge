# SchoolOS bridge

One small service per school. It sits on the school LAN next to the
access-control terminals and is the only thing that talks to them:

- terminal events (face recognized) → SchoolOS cloud `POST /api/devices/events/`
  — pushed by the terminal to the bridge's listening host in real time, with an
  `AcsEvent` poll as catch-up; kept in a durable outbox until the cloud accepts them;
- cloud commands (`ENROLL` / `DELETE`, plus the nightly `RECONCILE` roster diff and
  `SET_TIME` zone fix) → terminal, then acked back. A reconcile deletes only
  10-digit OSID users that are not terminal admins, and never on an empty roster;
- `CAPTURE`: the terminal snaps a fresh photo of the person standing in front of
  it and the bridge uploads it to the cloud (stored as their avatar, then enrolled);
- heartbeat with each terminal's identity (serial, model, IP, location,
  direction) every minute — this is also how the cloud learns about a terminal
  configured on the bridge, and registers it.

Traffic to the cloud is outbound HTTPS only; nothing on the school network has
to be reachable from the internet. Terminal admin passwords live only in the
bridge config file.

Drivers: `hikvision` (ISAPI, tested on DS-K1T343MFWX fw V4.38.0). A new kind of
terminal is a new `DeviceDriver` in `schoolos_bridge/drivers/` — the cloud
does not change.

## Install (Debian/Ubuntu mini-PC)

```bash
sudo useradd --system --home /opt/schoolos-bridge schoolos-bridge
sudo mkdir -p /opt/schoolos-bridge && sudo cp -r schoolos_bridge requirements.txt /opt/schoolos-bridge/
sudo python3 -m venv /opt/schoolos-bridge/venv
sudo /opt/schoolos-bridge/venv/bin/pip install -r /opt/schoolos-bridge/requirements.txt
sudo cp config.example.toml /etc/schoolos-bridge.toml
sudo chown schoolos-bridge: /etc/schoolos-bridge.toml && sudo chmod 600 /etc/schoolos-bridge.toml
sudo cp deploy/schoolos-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now schoolos-bridge
journalctl -u schoolos-bridge -f
```

Fill the config with the **local setup wizard** below (it finds the terminals on
the LAN and writes the token, IPs and passwords for you), or edit
`/etc/schoolos-bridge.toml` by hand.

The bridge token comes from the director's **Terminallar** page
(`/app/director/terminals`): "Bridge qo'shish" creates a bridge and shows its
token once — copy it into `token` in the config. Lost it? "Yangi token" issues
a new one (the old token stops working).

You do **not** have to add each terminal on that page by hand: a bridge reports
the serial, model, IP, location and direction of every terminal it drives, and
the cloud registers any it does not know yet. A newly registered active terminal
gets every member of the school automatically, and "Sinxronlash" re-pushes on
demand. (The page's "Terminal qo'shish" still exists for adding one manually or
editing a bridge/serial by hand.)

## Local setup wizard

A small web page served **by the bridge itself** on the school LAN — separate
from the SchoolOS web app. A technician opens it, scans the network, types each
terminal's admin password, and the bridge writes its config. Terminal passwords
never leave the machine, and the browser never sees the saved ones back.

```bash
# from the bridge/ directory (or /opt/schoolos-bridge on an installed bridge)
sudo ./deploy/schoolos-bridge-setup
```

It prints a URL and a one-time **PIN**; open the URL from any device on the same
network and type the PIN. The wizard:

1. **Bulut** — paste the cloud URL and bridge token, "Aloqani tekshirish" proves
   the token with an empty heartbeat.
2. **Terminallar** — "Tarmoqni skanerlash" sweeps the local `/24` (or type a
   CIDR, a single IP, or a range like `192.168.1.10-40`). Hosts that answer
   ISAPI are listed; type the admin password and "Tekshirish" reads the serial,
   model, firmware and clock through the same driver the bridge uses. Set the
   location label and direction per terminal.
3. **Saqlash** — writes `config.toml` (chmod 600), registers the terminals with
   the cloud (so they exist and start syncing even before the service runs), and
   runs the same PASS/WARN/FAIL checks as `schoolos-bridge check`.

Flags: `--port` (default 8098), `--host` (default `0.0.0.0`), `--pin` (fix the
PIN), `--no-auth` (trusted machine only), `--scan-timeout`. Restart the service
afterwards: `sudo systemctl restart schoolos-bridge`.

## Terminal setup (Hikvision)

- Time: the bridge sets the zone to UTC+5 on start (`CST-5:00:00`).
- Real-time events: Configuration → Network → Advanced → HTTP Listening →
  bridge IP, port `8099` (or `listen_port`), URL `/hik`, protocol HTTP.
- Faces: enrolled automatically from the user's SchoolOS avatar.

## Development

```bash
python -m unittest            # from bridge/
python -m schoolos_bridge --config config.toml --once
python -m schoolos_bridge.setup --config config.toml --port 8098   # setup wizard
```
