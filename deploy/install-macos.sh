#!/bin/bash
# Install the *built* bridge as a macOS LaunchDaemon. Run with sudo from bridge/:
#
#   python installer/build.py          # produces dist/schoolos-bridge
#   sudo deploy/install-macos.sh
#
# It copies the executable to /usr/local/bin, the config to
# /usr/local/etc/schoolos-bridge, and loads the LaunchDaemon.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

BIN=/usr/local/bin
ETC=/usr/local/etc/schoolos-bridge
LOG=/usr/local/var/log
LABEL=uz.schoolos.bridge
PLIST="/Library/LaunchDaemons/${LABEL}.plist"

if [ "$(id -u)" -ne 0 ]; then
    echo "sudo bilan ishga tushiring / run with sudo" >&2
    exit 1
fi

SERVICE="$ROOT/dist/schoolos-bridge"
if [ ! -x "$SERVICE" ]; then
    echo "Topilmadi: $SERVICE — avval quring: python installer/build.py" >&2
    exit 1
fi

install -d "$ETC" "$LOG" "$BIN"
install -m 0755 "$SERVICE" "$BIN/schoolos-bridge"
rm -f "$BIN/schoolos-bridge-setup"  # older two-program builds
install -m 0644 "$HERE/uz.schoolos.bridge.plist" "$PLIST"

if [ ! -f "$ETC/config.toml" ]; then
    install -m 0600 "$ROOT/config.example.toml" "$ETC/config.toml"
    echo "Sozlash sahifasi: sudo $BIN/schoolos-bridge open --config $ETC/config.toml"
fi

# Reload if an older copy is already installed (ignore "not found" on first run).
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
launchctl enable "system/${LABEL}"

echo "O'rnatildi / installed: ${LABEL}"
echo "Log: $LOG/schoolos-bridge.log"
echo "Holat / status: sudo launchctl print system/${LABEL}"
