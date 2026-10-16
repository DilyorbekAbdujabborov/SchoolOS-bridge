"""The device port. The bridge loop only ever talks to `DeviceDriver`; each
vendor is one adapter, so a new terminal type is a new file here — never a
cloud change."""

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


#: Larger pictures are not sent — the cloud would drop them anyway.
MAX_PICTURE_BYTES = 400 * 1024


class DriverError(Exception):
    """The terminal refused or failed an operation."""


@dataclass(frozen=True)
class Person:
    osid: str
    name: str
    photo: bytes | None = None
    #: The user's avatar was removed: drop the face stored on the terminal.
    remove_face: bool = False


@dataclass(frozen=True)
class TerminalPerson:
    """Someone stored on a terminal, as `list_persons` reports them."""

    osid: str
    name: str
    has_face: bool = False
    #: The terminal's own admin (or a non-regular user type): never deleted by a reconcile.
    is_admin: bool = False


@dataclass(frozen=True)
class FaceMatch:
    """One hit from a 1:N face search on a terminal's own face library."""

    osid: str
    similarity: float | None = None


@dataclass(frozen=True)
class DeviceProbe:
    """What `schoolos-bridge check` reads from a terminal (no side effects)."""

    serial: str = ""
    model: str = ""
    firmware: str = ""
    #: POSIX zone, e.g. `CST-5:00:00` (UTC+5; POSIX inverts the sign).
    time_zone: str = ""
    local_time: str = ""


@dataclass(frozen=True)
class RawEvent:
    device_serial: str
    osid: str
    event_time: str  # ISO 8601 with the terminal's offset
    direction: str
    verify_mode: str
    cursor: int  # monotonically increasing per device
    raw: dict = field(default_factory=dict)
    #: JPEG the terminal attached to the event (push only), if any.
    picture: bytes | None = None

    def to_cloud(self) -> dict:
        data = {
            "device_serial": self.device_serial,
            "osid": self.osid,
            "event_time": self.event_time,
            "direction": self.direction,
            "verify_mode": self.verify_mode,
            "raw": self.raw,
        }
        if self.picture and len(self.picture) <= MAX_PICTURE_BYTES:
            data["photo"] = base64.b64encode(self.picture).decode("ascii")
        return data


class DeviceDriver(ABC):
    serial: str
    #: Longest person name the terminal stores (None: no limit). A reconcile
    #: compares names after this truncation.
    name_max: int | None = None

    def probe(self) -> DeviceProbe:
        """Read the terminal's identity and clock, changing nothing. Raises
        `DriverError` when it is unreachable or refuses the credentials."""
        raise DriverError("probe not supported by this driver")

    @abstractmethod
    def ensure_time(self) -> None:
        """Make the terminal's clock and zone match the school's."""

    @abstractmethod
    def fetch_events(self, after_cursor: int | None, since: str | None = None) -> list[RawEvent]:
        """Person events newer than `after_cursor`, ascending by cursor.

        `since` is the `event_time` of the cursor's event: drivers search from
        there instead of the terminal's whole history. Both None → today."""

    @abstractmethod
    def parse_push(self, body: bytes, content_type: str) -> list[RawEvent]:
        """Person events from one listening-host push from the terminal."""

    @abstractmethod
    def enroll(self, person: Person) -> None:
        """Create or update the person; upload `photo` when given, otherwise
        drop the stored face only when `remove_face` is set."""

    @abstractmethod
    def delete(self, osid: str) -> None:
        """Remove the person (and their face) from the terminal."""

    @abstractmethod
    def list_persons(self) -> list[TerminalPerson]:
        """Everyone stored on the terminal."""

    @abstractmethod
    def set_timezone(self, posix: str) -> bool:
        """Set the terminal's zone to `posix` (e.g. `CST-5:00:00`), keeping its
        time mode. True when something changed."""

    def ensure_push_host(self, host: str, port: int, url: str = "/hik") -> bool:
        """Make the terminal push its events to `host:port`+`url`. True when the
        terminal's setting changed. Vendors without a push host leave it a no-op."""
        return False

    def capture(self) -> bytes:
        """A fresh JPEG photo from the terminal's camera, for a CAPTURE command.
        Raises `DriverError` when the terminal cannot take one. Vendors without
        a camera leave this unsupported."""
        raise DriverError("capture not supported by this driver")

    def search_face(self, photo: bytes, max_results: int = 5) -> list[FaceMatch]:
        """The closest faces to `photo` in the terminal's own face library, best
        first — used to guess whose bad enrollment an unknown shot belongs to.
        Raises `DriverError`; vendors without face search leave this unsupported."""
        raise DriverError("face search not supported by this driver")
