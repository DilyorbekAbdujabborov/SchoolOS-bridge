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
- heartbeat with each terminal's LAN IP every minute.

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
sudoedit /etc/schoolos-bridge.toml     # token, terminal IP + password
sudo cp deploy/schoolos-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now schoolos-bridge
journalctl -u schoolos-bridge -f
```

The bridge token comes from the director's **Terminallar** page
(`/app/director/terminals`): "Bridge qo'shish" creates a bridge and shows its
token once — copy it into `token` in the config. Lost it? "Yangi token" issues
a new one (the old token stops working). Then add each terminal there with
"Terminal qo'shish" (bridge, serial, IP, location, direction); a new active
terminal gets every member of the school automatically, and "Sinxronlash"
re-pushes on demand.

## Terminal setup (Hikvision)

- Time: the bridge sets the zone to UTC+5 on start (`CST-5:00:00`).
- Real-time events: Configuration → Network → Advanced → HTTP Listening →
  bridge IP, port `8099` (or `listen_port`), URL `/hik`, protocol HTTP.
- Faces: enrolled automatically from the user's SchoolOS avatar.

## Development

```bash
python -m unittest            # from bridge/
python -m schoolos_bridge --config config.toml --once
```
