"""A tiny TOML writer for the bridge config.

The standard library reads TOML (`tomllib`) but has no writer, and the bridge
keeps its dependency list to `requests` only, so this serializes the one shape we
need: a flat table of scalars followed by `[[devices]]` tables. Values are
validated by `config.load_config` when the file is read back.
"""

#: Keys written, in this order, at the top level.
TOP_KEYS = ("cloud_url", "token", "poll_seconds", "state_file", "listen_host", "listen_port")
#: Keys written, in this order, in each `[[devices]]` table.
DEVICE_KEYS = ("serial", "vendor", "model", "ip", "username", "password", "direction", "location_label")

_SIMPLE_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _string(value: str) -> str:
    """A TOML basic string. Astral characters are written raw (the file is
    UTF-8); control characters become `\\uXXXX`, which TOML allows."""
    parts = ['"']
    for char in value:
        if char in _SIMPLE_ESCAPES:
            parts.append(_SIMPLE_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            parts.append(f"\\u{ord(char):04X}")
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        raise ValueError("cannot serialize None to TOML")
    return _string(str(value))


def dumps(data: dict) -> str:
    """Serialize `data` (a dict shaped like the bridge config) to TOML text."""
    lines: list[str] = []
    for key in TOP_KEYS:
        if data.get(key) is not None:
            lines.append(f"{key} = {_value(data[key])}")
    for device in data.get("devices") or []:
        lines.append("")
        lines.append("[[devices]]")
        for key in DEVICE_KEYS:
            if device.get(key) is not None:
                lines.append(f"{key} = {_value(device[key])}")
    return "\n".join(lines) + "\n"
