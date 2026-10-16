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
to be reachable from the internet. Terminal admin passwords and the bridge token
live only in the bridge config, **sealed** (see [Secrets](#secrets)).

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
# keys (secret.key, ui.key) and state live here
sudo install -d -m 700 -o schoolos-bridge /etc/schoolos-bridge
sudo cp deploy/schoolos-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now schoolos-bridge
journalctl -u schoolos-bridge -f
```

Then sign in to the **setup page** (below) and fill the config there:
`sudo -u schoolos-bridge /opt/schoolos-bridge/venv/bin/python -m schoolos_bridge open --config /etc/schoolos-bridge.toml --print`.

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

## One program

`schoolos-bridge` is a single program: the relay loop, the terminal listener and
the setup page run in one process. The setup page writes the config and the
running bridge picks it up within seconds — there is no restart step.

```text
schoolos-bridge                 # double-click: install (Windows) or run, then open the setup page
schoolos-bridge run             # what a service runs: relay + listener + setup page
schoolos-bridge open [--print]  # sign in to the running bridge (opens the browser, or prints the link)
schoolos-bridge check           # PASS/WARN/FAIL self-checks
schoolos-bridge install | uninstall    # Windows only (the double-click does install for you)
schoolos-bridge --self-test | --version
```

Flags: `--config`, `--log-file`, `--ui-host` (default `127.0.0.1`), `--ui-port`
(default `8098`), `--no-ui`, `--no-browser`, `--once`.

### Windows: one file, double-click

Download `schoolos-bridge.exe` and double-click it. It asks for administrator
rights once (UAC), copies itself to `%ProgramData%\SchoolOS Bridge\bin`, locks
that folder to Administrators + SYSTEM, registers the **SchoolOS Bridge**
scheduled task (runs as SYSTEM at boot, restarts if it exits), starts it, and
opens the setup page in the browser. Double-clicking again later just signs you
in to the running bridge. `schoolos-bridge.exe uninstall` removes the task (config
and logs stay). Logs: `%ProgramData%\SchoolOS Bridge\logs\schoolos-bridge.log`.

## Setup page

A small web page served **by the bridge itself** — separate from the SchoolOS
web app. Sign-in works like Jupyter: no password or PIN to type.

- `schoolos-bridge open` (or the double-click) mints a **one-time link**
  (`http://127.0.0.1:8098/#t=…`): a JWT signed with the bridge's `ui.key`, valid
  for **2 minutes** and **once**. Making one needs read access to `ui.key`, i.e.
  administrator / the bridge's user — that is the security boundary.
- The page trades it for a **session token** kept only in that browser tab
  (`sessionStorage`), valid 8 hours. Sessions are signed with a key that lives
  only in the bridge's memory, so a restart signs everyone out. "Chiqish" ends it.
- The page listens on `127.0.0.1` only. A headless box can use
  `--ui-host 0.0.0.0` and `schoolos-bridge open --print` to get a link to open
  from another machine on the LAN. Wrong links are throttled.

On the page:

1. **Bulut** — cloud URL and bridge token; "Aloqani tekshirish" proves the token
   with an empty heartbeat.
2. **Terminallar** — "Skanerlash" sweeps the local `/24` (or a CIDR, an IP, a
   range like `192.168.1.10-40`); "IP qo'shish" adds one by hand. Type the admin
   password and "Tekshirish" reads serial and model through the bridge's driver.
   "O'chirish" (click twice) removes a saved terminal from the config at once.
3. **Saqlash** — writes the sealed config, registers the terminals with the cloud,
   and shows the self-checks. The running bridge reloads by itself.

The header shows whether the relay is running and with how many terminals.

## Secrets

The bridge token and terminal passwords are stored in `config.toml` as
`enc:v1:…` (Fernet). The key is `secret.key` next to the config (on Linux, under
the default directory when the config sits directly in `/etc`):

- **Windows:** the key is wrapped with DPAPI (machine scope) and the folder is
  readable by Administrators + SYSTEM only.
- **Linux / macOS:** the key file is `0600`, owned by the bridge's user.

A config with clear-text secrets (an older bridge, or a hand edit) is sealed
automatically the next time the bridge loads it. To change a secret by hand,
write it in clear text — it gets sealed on the next load. Secrets never appear
in logs. The browser never gets saved passwords or the token back.

## Building

The program packages as one self-contained executable, so a school machine does
not need Python installed.

```bash
cd bridge
python -m venv .build-venv && . .build-venv/bin/activate   # Windows: .build-venv\Scripts\activate
pip install -r requirements-build.txt
python installer/build.py            # writes dist/schoolos-bridge(.exe)
```

PyInstaller cannot cross-compile: build on the OS you ship to. CI does all three
(`.github/workflows/bridge-build.yml`) and uploads one artifact per OS.

Without `--config` each platform reads its own default: `/etc/schoolos-bridge/`
(Linux), `~/Library/Application Support/SchoolOS Bridge/` (macOS),
`%ProgramData%\SchoolOS Bridge` (Windows). `SCHOOLOS_BRIDGE_CONFIG` overrides it.

### Services elsewhere

- **Linux (systemd):** the block above (source install), or point the unit at
  the built binary (`ExecStart=/opt/schoolos-bridge/schoolos-bridge run --config ...`).
- **macOS (launchd):** `sudo deploy/install-macos.sh` copies the executable to
  `/usr/local/bin`, seeds the config in `/usr/local/etc/schoolos-bridge`, and
  loads `uz.schoolos.bridge`. Logs: `/usr/local/var/log/schoolos-bridge.log`.

## Terminal setup (Hikvision)

- Time: the bridge sets the zone to UTC+5 on start (`CST-5:00:00`).
- Real-time events: Configuration → Network → Advanced → HTTP Listening →
  bridge IP, port `8099` (or `listen_port`), URL `/hik`, protocol HTTP.
- Faces: enrolled automatically from the user's SchoolOS avatar.

## Development

```bash
python -m unittest            # from bridge/
python -m schoolos_bridge run --config config.toml               # bridge + setup page
python -m schoolos_bridge open --config config.toml --print      # sign-in link
python -m schoolos_bridge --self-test                            # assets + vault intact?

python installer/build.py     # build the standalone executable into dist/
```
