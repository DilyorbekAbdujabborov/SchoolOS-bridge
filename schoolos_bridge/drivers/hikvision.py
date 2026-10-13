"""Hikvision access-control terminals over ISAPI (HTTP Digest).

Verified on a DS-K1T343MFWX, firmware V4.38.0:
- person events are `major 5 / minor 75`; door relay noise (21/22) is ignored;
- `AcsEvent` search pages hold at most 30 rows and say `MORE` while more remain;
- one digest session reused for repeated POSTs gets a persistent 401, so every
  request carries a fresh `HTTPDigestAuth`;
- the unit shipped in `CST-8:00:00` (Shanghai), which skews every event by 3h —
  `ensure_time()` sets the school zone.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default as email_policy
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth

from .base import DeviceDriver, DeviceProbe, DriverError, Person, RawEvent, TerminalPerson

SCHOOL_TZ = ZoneInfo("Asia/Tashkent")
#: POSIX zone string for UTC+5 (the sign is inverted in POSIX notation).
SCHOOL_POSIX_TZ = "CST-5:00:00"
MAX_CLOCK_DRIFT = timedelta(seconds=60)
PAGE_SIZE = 30
PERSON_MAJOR, PERSON_MINOR = 5, 75
NAME_MAX = 32
#: `CST-5:00:00` → sign, hours, minutes, seconds (POSIX: the sign is inverted).
POSIX_TZ = re.compile(r"^[A-Z]{3}([+-])(\d{1,2}):(\d{2}):(\d{2})$")
#: Only regular users are ours; visitors / blocklist entries are left alone.
REGULAR_USER_TYPE = "normal"
#: Snapshot endpoints tried in order — main stream first, then the sub stream.
SNAPSHOT_PATHS = ("/ISAPI/Streaming/channels/101/picture", "/ISAPI/Streaming/channels/1/picture")
#: JPEG magic (SOI): a snapshot endpoint that returns XML/HTML is not a photo.
JPEG_SOI = b"\xff\xd8"


def _posix_offset(posix: str) -> timezone:
    match = POSIX_TZ.match(posix or "")
    if not match:
        raise DriverError(f"bad POSIX time zone {posix!r}")
    sign, hours, minutes, seconds = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds))
    return timezone(-delta if sign == "+" else delta)


def _tag(text: str, name: str) -> str | None:
    match = re.search(rf"<{name}>([^<]+)</{name}>", text)
    return match.group(1) if match else None


class HikvisionDriver(DeviceDriver):
    name_max = NAME_MAX

    def __init__(self, cfg, http=requests, timeout: float = 15):
        self.cfg = cfg
        self.serial = cfg.serial
        self.http = http
        self.timeout = timeout

    # -- transport -----------------------------------------------------------
    def _call(self, method: str, path: str, **kwargs):
        try:
            return self.http.request(
                method,
                f"http://{self.cfg.ip}{path}",
                auth=HTTPDigestAuth(self.cfg.username, self.cfg.password),
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise DriverError(f"{method} {path}: {exc}") from exc

    def _json(self, method: str, path: str, **kwargs) -> dict:
        response = self._call(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise DriverError(f"{method} {path}: HTTP {response.status_code}") from exc
        if "statusCode" in body and body["statusCode"] != 1:
            raise DriverError(body.get("subStatusCode") or body.get("statusString") or "error")
        if response.status_code >= 400:
            raise DriverError(f"{method} {path}: HTTP {response.status_code}")
        return body

    # -- identity ----------------------------------------------------------------
    def _text(self, path: str) -> str:
        response = self._call("GET", path)
        if response.status_code >= 400:
            raise DriverError(f"GET {path}: HTTP {response.status_code}")
        return response.text

    def probe(self) -> DeviceProbe:
        info = self._text("/ISAPI/System/deviceInfo")
        clock = self._text("/ISAPI/System/time")
        return DeviceProbe(
            serial=_tag(info, "serialNumber") or "",
            model=_tag(info, "model") or "",
            firmware=_tag(info, "firmwareVersion") or "",
            time_zone=_tag(clock, "timeZone") or "",
            local_time=_tag(clock, "localTime") or "",
        )

    # -- clock -----------------------------------------------------------------
    def ensure_time(self) -> None:
        text = self._call("GET", "/ISAPI/System/time").text
        zone = re.search(r"<timeZone>([^<]+)</timeZone>", text)
        local = re.search(r"<localTime>([^<]+)</localTime>", text)
        now = datetime.now(SCHOOL_TZ).replace(microsecond=0)
        if zone and local and zone.group(1) == SCHOOL_POSIX_TZ:
            try:
                if abs(datetime.fromisoformat(local.group(1)) - now) <= MAX_CLOCK_DRIFT:
                    return
            except ValueError:
                pass
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<timeMode>manual</timeMode><localTime>{now.isoformat()}</localTime>"
            f"<timeZone>{SCHOOL_POSIX_TZ}</timeZone></Time>"
        )
        response = self._call(
            "PUT", "/ISAPI/System/time", data=xml, headers={"Content-Type": "application/xml"}
        )
        if "<statusCode>1</statusCode>" not in response.text:
            raise DriverError(f"time not set: {response.text[:200]}")

    def set_timezone(self, posix: str) -> bool:
        """Set the zone only, keeping the terminal's time mode. In manual mode the
        clock is set too (a manual clock is otherwise left in the old zone)."""
        zone = _posix_offset(posix)
        text = self._call("GET", "/ISAPI/System/time").text
        mode = _tag(text, "timeMode") or "manual"
        now = datetime.now(zone).replace(microsecond=0)
        if _tag(text, "timeZone") == posix:
            if mode != "manual":
                return False
            try:
                if abs(datetime.fromisoformat(_tag(text, "localTime") or "") - now) <= MAX_CLOCK_DRIFT:
                    return False
            except ValueError:
                pass
        local = f"<localTime>{now.isoformat()}</localTime>" if mode == "manual" else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<timeMode>{mode}</timeMode>{local}<timeZone>{posix}</timeZone></Time>"
        )
        response = self._call(
            "PUT", "/ISAPI/System/time", data=xml, headers={"Content-Type": "application/xml"}
        )
        if "<statusCode>1</statusCode>" not in response.text:
            raise DriverError(f"time zone not set: {response.text[:200]}")
        return True

    # -- camera ----------------------------------------------------------------
    def capture(self) -> bytes:
        """A JPEG snapshot from the terminal's camera. Tries each known snapshot
        endpoint until one returns real JPEG bytes."""
        for path in SNAPSHOT_PATHS:
            response = self._call("GET", path)
            if response.status_code < 400 and response.content.startswith(JPEG_SOI):
                return response.content
        raise DriverError("capture: terminal did not return a snapshot")

    # -- events ----------------------------------------------------------------
    def _event(self, osid, time, cursor, verify_mode, raw) -> RawEvent:
        return RawEvent(
            device_serial=self.serial,
            osid=str(osid),
            event_time=time,
            direction=self.cfg.direction if self.cfg.direction != "BOTH" else "UNKNOWN",
            verify_mode=verify_mode or "",
            cursor=int(cursor),
            raw=raw,
        )

    def fetch_events(self, after_cursor: int | None, since: str | None = None) -> list[RawEvent]:
        cond = {
            "searchID": "schoolos",
            "maxResults": PAGE_SIZE,
            "major": PERSON_MAJOR,
            "minor": PERSON_MINOR,
            "timeReverseOrder": False,
        }
        # Always bound the search: without a time window the terminal pages
        # through its entire history (100k+ rows) on every poll.
        now = datetime.now(SCHOOL_TZ)
        start = (
            datetime.fromisoformat(since)
            if since
            else now.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        cond["startTime"] = start.replace(microsecond=0).isoformat()
        cond["endTime"] = (now + timedelta(days=1)).replace(microsecond=0).isoformat()
        events, position = [], 0
        while True:
            body = self._json(
                "POST",
                "/ISAPI/AccessControl/AcsEvent?format=json",
                json={"AcsEventCond": {**cond, "searchResultPosition": position}},
            )["AcsEvent"]
            rows = body.get("InfoList", [])
            for row in rows:
                if (
                    row.get("major") == PERSON_MAJOR
                    and row.get("minor") == PERSON_MINOR
                    and row.get("employeeNoString")
                    and (after_cursor is None or row["serialNo"] > after_cursor)
                ):
                    events.append(
                        self._event(row["employeeNoString"], row["time"], row["serialNo"],
                                    row.get("currentVerifyMode"), {"serialNo": row["serialNo"]})
                    )
            position += len(rows)
            if body.get("responseStatusStrg") != "MORE" or not rows:
                break
        return sorted(events, key=lambda e: e.cursor)

    def parse_push(self, body: bytes, content_type: str) -> list[RawEvent]:
        payloads = []
        if content_type.startswith("multipart/"):
            message = message_from_bytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body, policy=email_policy
            )
            for part in message.iter_parts():
                if part.get_content_type() == "application/json":
                    payloads.append(part.get_payload(decode=True))
        else:
            payloads.append(body)
        events = []
        for raw in payloads:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            ace = data.get("AccessControllerEvent") or {}
            if (
                data.get("eventType") == "AccessControllerEvent"
                and ace.get("majorEventType") == PERSON_MAJOR
                and ace.get("subEventType") == PERSON_MINOR
                and ace.get("employeeNoString")
            ):
                events.append(
                    self._event(ace["employeeNoString"], data["dateTime"], ace.get("serialNo", 0),
                                ace.get("currentVerifyMode"), {"serialNo": ace.get("serialNo")})
                )
        return events

    # -- people ----------------------------------------------------------------
    def enroll(self, person: Person) -> None:
        now = datetime.now(SCHOOL_TZ)
        user = {
            "employeeNo": person.osid,
            "name": person.name[:NAME_MAX],
            "userType": "normal",
            "Valid": {
                "enable": True,
                "beginTime": now.strftime("%Y-%m-%dT00:00:00"),
                "endTime": now.replace(year=now.year + 10).strftime("%Y-%m-%dT23:59:59"),
                "timeType": "local",
            },
            "doorRight": "1",
            "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
        }
        try:
            self._json("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json", json={"UserInfo": user})
        except DriverError as exc:
            if str(exc) != "employeeNoAlreadyExist":
                raise
            self._json("PUT", "/ISAPI/AccessControl/UserInfo/Modify?format=json", json={"UserInfo": user})
        if person.photo:
            meta = json.dumps({"faceLibType": "blackFD", "FDID": "1", "FPID": person.osid})
            self._json(
                "PUT",
                "/ISAPI/Intelligent/FDLib/FDSetUp?format=json",
                files={
                    "FaceDataRecord": (None, meta, "application/json"),
                    "img": ("face.jpg", person.photo, "image/jpeg"),
                },
            )
        elif person.remove_face:
            try:
                self._json(
                    "PUT",
                    "/ISAPI/Intelligent/FDLib/FDSearch/Delete?format=json&FDID=1&faceLibType=blackFD",
                    json={"FPID": [{"value": person.osid}]},
                )
            except DriverError as exc:
                # No face stored is the state we want, not a failure.
                if "notexist" not in str(exc).lower():
                    raise

    def list_persons(self) -> list[TerminalPerson]:
        persons, position = [], 0
        while True:
            body = self._json(
                "POST",
                "/ISAPI/AccessControl/UserInfo/Search?format=json",
                json={"UserInfoSearchCond": {
                    "searchID": "schoolos", "searchResultPosition": position, "maxResults": PAGE_SIZE,
                }},
            ).get("UserInfoSearch", {})
            rows = body.get("UserInfo") or []
            for row in rows:
                if not row.get("employeeNo"):
                    continue
                persons.append(TerminalPerson(
                    osid=str(row["employeeNo"]),
                    name=row.get("name", ""),
                    has_face=int(row.get("numOfFace") or 0) > 0,
                    is_admin=(
                        bool(row.get("localUIRight"))
                        or str(row.get("userLevel", "")).lower() == "administrator"
                        or row.get("userType", REGULAR_USER_TYPE) != REGULAR_USER_TYPE
                    ),
                ))
            position += len(rows)
            if body.get("responseStatusStrg") != "MORE" or not rows:
                break
        return persons

    def delete(self, osid: str) -> None:
        self._json(
            "PUT",
            "/ISAPI/AccessControl/UserInfo/Delete?format=json",
            json={"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": osid}]}},
        )
