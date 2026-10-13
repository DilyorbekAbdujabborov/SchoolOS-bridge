"""Durable bridge state: per-device event cursor + events not yet accepted by
the cloud. Written atomically so a crash never leaves half a file."""

import json
import os
from pathlib import Path


class State:
    def __init__(self, path):
        self.path = Path(path)
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._cursors: dict[str, int] = data.get("cursors", {})
        self._since: dict[str, str] = data.get("since", {})
        self.outbox: list[dict] = data.get("outbox", [])
        #: serial → osid → face_version last uploaded (or adopted) on that terminal.
        self._faces: dict[str, dict[str, str]] = data.get("faces", {})

    def cursor(self, serial: str) -> int | None:
        return self._cursors.get(serial)

    def since(self, serial: str) -> str | None:
        return self._since.get(serial)

    def set_cursor(self, serial: str, value: int, since: str | None = None) -> None:
        self._cursors[serial] = value
        if since:
            self._since[serial] = since

    def face(self, serial: str, osid: str) -> str | None:
        return self._faces.get(serial, {}).get(osid)

    def set_face(self, serial: str, osid: str, version: str) -> None:
        self._faces.setdefault(serial, {})[osid] = version

    def drop_face(self, serial: str, osid: str) -> None:
        self._faces.get(serial, {}).pop(osid, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "cursors": self._cursors, "since": self._since, "outbox": self.outbox, "faces": self._faces,
        }))
        os.replace(tmp, self.path)
