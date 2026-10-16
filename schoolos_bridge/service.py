"""The bridge loop: terminal events → cloud, cloud commands → terminal.

Events go through a durable outbox: they are saved before the cloud sees them
and dropped only after the cloud accepted the batch, so neither a network blip
nor a restart loses or re-sends them. The poll cursor advances only past
events that came from polling — a listening-host push can arrive out of order,
and the poll is what guarantees nothing in between is skipped (the cloud
dedups whatever arrives twice).
"""

import json
import logging
import re
import threading
import time

from .cloud import CloudError, CloudRejected, explain
from .drivers import DriverError, Person
from .logutil import LOG_BUFFER

log = logging.getLogger("schoolos_bridge")

BATCH_SIZE = 500
HEARTBEAT_EVERY = 60
LOG_SHIP_EVERY = 30
#: Outbox ceiling (≈ a few days of a big school). Past it the bridge stops
#: pulling from the terminals instead of dropping anything: the terminal keeps
#: its own history and the cursor resumes from the last event the cloud took.
MAX_OUTBOX = 50_000
#: A reconcile only ever deletes people that look like ours: a 10-digit OSID.
#: Anything else (a guard enrolled by hand, the terminal admin) stays.
OSID = re.compile(r"^\d{10}$")
#: Cloud's ack `result` limit.
MAX_RESULT = 2000
MAX_ERRORS = 5
#: One unknown-face shot per terminal per this window — a person lingering at the
#: door must not flood the review queue with near-identical shots.
UNMATCHED_INTERVAL = 60.0
#: Below this similarity the terminal's closest library face is not reported as
#: the cause; a stranger stays a stranger.
FACE_MATCH_MIN = 60.0


class Bridge:
    def __init__(self, config, cloud, drivers, state, clock=time.monotonic):
        self.config = config
        self.cloud = cloud
        self.drivers = drivers
        self.state = state
        self.clock = clock
        self.lock = threading.Lock()
        self._last_heartbeat = None
        self._last_log_ship = None
        #: serial → last monotonic time an unknown-face shot was taken.
        self._last_unmatched: dict[str, float] = {}

    # -- events ----------------------------------------------------------------
    def collect(self, serial: str, events, advance: bool) -> None:
        with self.lock:
            index = {(e["serial"], e["cursor"]): e for e in self.state.outbox}
            for event in events:
                existing = index.get((serial, event.cursor))
                if existing is not None:
                    existing["advance"] = existing["advance"] or advance
                    # A push carries the picture a poll of the same event lacks.
                    payload = event.to_cloud()
                    if "photo" in payload and "photo" not in existing["event"]:
                        existing["event"]["photo"] = payload["photo"]
                    continue
                if len(self.state.outbox) >= MAX_OUTBOX:
                    # Not lost: polled events are re-fetched past the cursor,
                    # and pushes never advance it.
                    log.warning("outbox full (%d), deferring events from %s", MAX_OUTBOX, serial)
                    break
                entry = {"serial": serial, "cursor": event.cursor, "since": event.event_time,
                         "advance": advance, "event": event.to_cloud()}
                self.state.outbox.append(entry)
                index[(serial, event.cursor)] = entry
            self.state.save()

    def _poll(self) -> None:
        if len(self.state.outbox) >= MAX_OUTBOX:
            return
        for serial, driver in self.drivers.items():
            try:
                events = driver.fetch_events(self.state.cursor(serial), self.state.since(serial))
            except DriverError as exc:
                log.warning("poll %s failed: %s", serial, exc)
                continue
            if events:
                self.collect(serial, events, advance=True)

    def _flush(self) -> None:
        while True:
            with self.lock:
                batch = self.state.outbox[:BATCH_SIZE]
            if not batch:
                return
            try:
                unmatched = self._push(batch)
            except CloudError as exc:
                log.warning("push failed, %d events kept: %s", len(batch), exc)
                return
            with self.lock:
                sent = {id(entry) for entry in batch}
                self.state.outbox = [e for e in self.state.outbox if id(e) not in sent]
                for entry in batch:
                    serial = entry["serial"]
                    current = self.state.cursor(serial)
                    if entry["advance"] and (current is None or entry["cursor"] > current):
                        self.state.set_cursor(serial, entry["cursor"], entry["since"])
                self.state.save()
            self._record_unmatched(batch, unmatched)

    def _push(self, batch) -> list[str]:
        """Send a batch; when the cloud rejects its content, send one by one and
        drop only the events it refuses, so one bad event can't block a school's
        attendance. Transient errors propagate (batch stays in the outbox).
        Returns the cloud's unmatched dedup keys for this batch."""
        try:
            result = self.cloud.push_events([entry["event"] for entry in batch])
        except CloudRejected:
            if len(batch) == 1:
                log.error("cloud rejected event, dropped: %s", batch[0]["event"])
                return []
            unmatched: list[str] = []
            for entry in batch:
                unmatched += self._push([entry])
            return unmatched
        if result.get("failed"):
            log.warning("cloud failed %d events: %s", len(result["failed"]), result["failed"][:5])
        return list(result.get("unmatched") or [])

    def _record_unmatched(self, batch, unmatched: list[str]) -> None:
        """The cloud told us these events resolved to nobody: capture the shot
        and (best effort) the terminal's closest library face, then report it."""
        if not unmatched:
            return
        by_osid = {entry["event"]["osid"]: entry for entry in batch}
        for key in unmatched:
            osid = key.rsplit(":", 1)[-1]
            entry = by_osid.get(osid)
            if entry is None:
                continue
            self._snapshot_unknown(entry["serial"], entry["event"])

    def _snapshot_unknown(self, serial: str, event: dict) -> None:
        driver = self.drivers.get(serial)
        if driver is None:
            return
        now = self.clock()
        last = self._last_unmatched.get(serial)
        if last is not None and now - last < UNMATCHED_INTERVAL:
            return
        self._last_unmatched[serial] = now
        try:
            photo = driver.capture()
        except DriverError as exc:
            log.warning("unknown-face snapshot %s failed: %s", serial, exc)
            return
        candidate_osid, similarity = None, None
        try:
            for match in driver.search_face(photo):
                if (
                    match.osid
                    and match.osid != event["osid"]
                    and (match.similarity is None or match.similarity >= FACE_MATCH_MIN)
                ):
                    candidate_osid, similarity = match.osid, match.similarity
                    break
        except DriverError as exc:
            log.warning("unknown-face search %s failed: %s", serial, exc)
        try:
            self.cloud.upload_unmatched(
                serial, event["osid"], event["event_time"], photo, candidate_osid, similarity
            )
        except CloudError as exc:
            log.warning("unknown-face upload %s failed: %s", serial, exc)

    # -- commands --------------------------------------------------------------
    def _run_command(self, command: dict) -> None:
        driver = self.drivers.get(command["device_serial"])
        if driver is None:
            self.cloud.ack(command["id"], "FAILED", "unknown device")
            return
        payload = command["payload"]
        result = ""
        try:
            if command["type"] == "ENROLL":
                photo = self.cloud.fetch_photo(command["id"]) if payload.get("has_photo") else None
                self._enroll(driver, payload, photo)
            elif command["type"] == "CAPTURE":
                photo = driver.capture()
                self.cloud.upload_capture(command["id"], photo)
                result = f"{len(photo)} bytes"
            elif command["type"] == "DELETE":
                driver.delete(payload["osid"])
                self.state.drop_face(driver.serial, payload["osid"])
                self.state.save()
            elif command["type"] == "RECONCILE":
                result = json.dumps(self._reconcile(command["id"], driver, payload.get("roster") or []))
            elif command["type"] == "SET_TIME":
                result = "changed" if driver.set_timezone(payload["posix"]) else "unchanged"
            else:
                raise DriverError(f"unknown command type {command['type']}")
        except (DriverError, CloudError) as exc:
            log.warning("command %s failed: %s", command["id"], exc)
            self.cloud.ack(command["id"], "FAILED", str(exc)[:MAX_RESULT])
            return
        self.cloud.ack(command["id"], "DONE", result[:MAX_RESULT])

    def _enroll(self, driver, item: dict, photo: bytes | None) -> None:
        driver.enroll(
            Person(
                osid=item["osid"],
                name=item.get("name", ""),
                photo=photo,
                remove_face=bool(item.get("remove_face")),
            )
        )
        if photo and item.get("face_version"):
            self.state.set_face(driver.serial, item["osid"], item["face_version"])
        elif item.get("remove_face"):
            self.state.drop_face(driver.serial, item["osid"])
        self.state.save()

    def _reconcile(self, command_id: int, driver, roster: list[dict]) -> dict:
        """Converge the terminal to `roster`: enroll who is missing or outdated,
        delete OSID-numbered strays. Per-person failures are counted, not fatal;
        only failing to list the terminal fails the command."""
        on_terminal = {p.osid: p for p in driver.list_persons()}
        counts = {"enrolled": 0, "deleted": 0, "unchanged": 0, "kept": 0, "failed": 0}
        errors: list[str] = []
        serial = driver.serial
        expected = set()
        for item in roster:
            osid = item["osid"]
            expected.add(osid)
            present = on_terminal.get(osid)
            name = item.get("name", "")
            if driver.name_max:
                name = name[: driver.name_max]
            version = item.get("face_version")
            known = self.state.face(serial, osid)
            needs_face = bool(item.get("has_photo")) and (
                present is None or not present.has_face or (known is not None and known != version)
            )
            if present is not None and present.name == name and not needs_face:
                if item.get("has_photo") and known is None and version:
                    # A face is there and we never recorded one: take it as current
                    # rather than re-uploading every photo on the first reconcile.
                    self.state.set_face(serial, osid, version)
                counts["unchanged"] += 1
                continue
            try:
                photo = self.cloud.fetch_photo(command_id, osid=osid) if needs_face else None
                self._enroll(driver, {**item, "remove_face": False}, photo)
                counts["enrolled"] += 1
            except (DriverError, CloudError) as exc:
                counts["failed"] += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{osid}: {exc}"[:200])
        for osid, person in on_terminal.items():
            if osid in expected:
                continue
            # An empty roster is a cloud mistake, never "delete everyone".
            if not roster or person.is_admin or not OSID.match(osid):
                counts["kept"] += 1
                continue
            try:
                driver.delete(osid)
                self.state.drop_face(serial, osid)
                counts["deleted"] += 1
            except DriverError as exc:
                counts["failed"] += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{osid}: {exc}"[:200])
        self.state.save()
        return {**counts, "errors": errors}

    def _commands(self) -> None:
        try:
            commands = self.cloud.fetch_commands()
        except CloudError as exc:
            log.warning("fetch commands failed: %s", exc)
            return
        for command in commands:
            try:
                self._run_command(command)
            except CloudError as exc:
                # Ack lost: the cloud re-issues the command after its claim timeout.
                log.warning("ack for command %s failed: %s", command["id"], exc)

    # -- heartbeat -------------------------------------------------------------
    def _heartbeat(self) -> None:
        now = self.clock()
        if self._last_heartbeat is not None and now - self._last_heartbeat < HEARTBEAT_EVERY:
            return
        try:
            self.cloud.heartbeat([self._device_report(d) for d in self.config.devices])
            self._last_heartbeat = now
        except CloudError as exc:
            log.warning("heartbeat failed: %s — %s", exc, explain(exc, getattr(self.cloud, "base_url", ""))[1])

    def _ship_logs(self) -> None:
        """Upload pending WARNING+ lines so a director can read them in the cloud.
        A failed upload puts them back; the buffer is bounded, so it can't grow."""
        now = self.clock()
        if self._last_log_ship is not None and now - self._last_log_ship < LOG_SHIP_EVERY:
            return
        lines = LOG_BUFFER.take()
        if not lines:
            return
        self._last_log_ship = now
        try:
            self.cloud.push_logs(lines)
        except CloudError:
            LOG_BUFFER.restore(lines)

    @staticmethod
    def _device_report(device) -> dict:
        """What the cloud needs to register a terminal the director never added:
        the identity, where it is and what it is."""
        return {
            "serial": device.serial,
            "ip": device.ip or None,
            "vendor": device.vendor,
            "model": device.model,
            "direction": device.direction,
            "location_label": device.location_label,
        }

    def tick(self) -> None:
        self._poll()
        self._flush()
        self._commands()
        self._heartbeat()
        self._ship_logs()
