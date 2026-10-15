import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from schoolos_bridge.cloud import CloudError, CloudRejected
from schoolos_bridge.config import Config, DeviceConfig
from schoolos_bridge.drivers import DeviceDriver, DriverError, FaceMatch, RawEvent, TerminalPerson
from schoolos_bridge.listener import make_server
from schoolos_bridge.service import Bridge
from schoolos_bridge.state import State

SERIAL = "SER-1"


def _event(cursor, osid="5893949988", minute=0):
    return RawEvent(SERIAL, osid, f"2026-10-10T08:{minute:02d}:00+05:00", "IN", "face", cursor, {"serialNo": cursor})


class FakeDriver(DeviceDriver):
    serial = SERIAL

    def __init__(self):
        self.events, self.enrolled, self.deleted, self.calls = [], [], [], []
        self.fail = None
        self.persons = []
        self.zones = []
        self.fail_osids = set()
        self.face_matches = []

    def ensure_time(self):
        pass

    def fetch_events(self, after_cursor, since=None):
        self.calls.append((after_cursor, since))
        return [e for e in self.events if after_cursor is None or e.cursor > after_cursor]

    def parse_push(self, body, content_type):
        return [_event(json.loads(body)["cursor"])]

    def enroll(self, person):
        if self.fail or person.osid in self.fail_osids:
            raise DriverError(self.fail or "deviceBusy")
        self.enrolled.append(person)

    def list_persons(self):
        if self.fail:
            raise DriverError(self.fail)
        return list(self.persons)

    def set_timezone(self, posix):
        if self.fail:
            raise DriverError(self.fail)
        self.zones.append(posix)
        return True

    def delete(self, osid):
        self.deleted.append(osid)

    def capture(self):
        if self.fail:
            raise DriverError(self.fail)
        return b"\xff\xd8photo"

    def search_face(self, photo, max_results=5):
        if self.fail:
            raise DriverError(self.fail)
        return list(self.face_matches[:max_results])


class FakeCloud:
    def __init__(self):
        self.pushed, self.acks, self.heartbeats, self.commands = [], [], [], []
        self.down = False
        self.photos = {}
        self.uploads = []
        self.poison = set()
        self.unmatched_osids = set()
        self.unmatched_uploads = []
        self.upload_fail = False

    def push_events(self, events):
        if self.down:
            raise CloudError("down")
        if any(e["raw"]["serialNo"] in self.poison for e in events):
            raise CloudRejected("HTTP 400")
        self.pushed.append(events)
        unmatched = [f"1:2026-10-10T08:00:00+05:00:{e['osid']}" for e in events if e["osid"] in self.unmatched_osids]
        return {"accepted": [e["osid"] for e in events], "unmatched": unmatched}

    def heartbeat(self, devices):
        self.heartbeats.append(devices)

    def fetch_commands(self):
        if self.down:
            raise CloudError("down")
        commands, self.commands = self.commands, []
        return commands

    def ack(self, command_id, status, result=""):
        self.acks.append((command_id, status, result))

    def fetch_photo(self, command_id, osid=None):
        return self.photos.get((command_id, osid) if osid else command_id)

    def upload_capture(self, command_id, photo):
        self.uploads.append((command_id, photo))

    def upload_unmatched(self, device_serial, osid, event_time, photo, candidate_osid=None, similarity=None):
        if self.upload_fail:
            raise CloudError("down")
        self.unmatched_uploads.append((device_serial, osid, event_time, photo, candidate_osid, similarity))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "state.json"
        self.config = Config(
            cloud_url="https://x", token="t", state_file=self.state_path,
            devices=[DeviceConfig(SERIAL, "hikvision", "10.0.0.5", "admin", "pw", "IN")],
        )
        self.driver, self.cloud = FakeDriver(), FakeCloud()
        self.bridge = self._bridge()

    def _bridge(self):
        return Bridge(self.config, self.cloud, {SERIAL: self.driver}, State(self.state_path))

    def test_events_pushed_and_cursor_advanced(self):
        self.driver.events = [_event(10, minute=1), _event(11, minute=2)]
        self.bridge.tick()
        self.assertEqual([e["raw"]["serialNo"] for e in self.cloud.pushed[0]], [10, 11])
        self.assertEqual(self.bridge.state.cursor(SERIAL), 11)
        self.assertEqual(self.bridge.state.since(SERIAL), "2026-10-10T08:02:00+05:00")
        self.bridge.tick()
        self.assertEqual(self.driver.calls[-1], (11, "2026-10-10T08:02:00+05:00"))
        self.assertEqual(len(self.cloud.pushed), 1)

    def test_cloud_down_keeps_outbox_and_cursor(self):
        self.driver.events = [_event(10)]
        self.cloud.down = True
        self.bridge.tick()
        self.bridge.tick()
        self.assertIsNone(self.bridge.state.cursor(SERIAL))
        self.assertEqual(len(self.bridge.state.outbox), 1)  # not duplicated by the second poll
        self.cloud.down = False
        self.bridge.tick()
        self.assertEqual(len(self.cloud.pushed), 1)
        self.assertEqual(self.bridge.state.cursor(SERIAL), 10)

    def test_rejected_event_does_not_block_the_rest(self):
        self.driver.events = [_event(10), _event(11), _event(12)]
        self.cloud.poison = {11}
        self.bridge.tick()
        pushed = [e["raw"]["serialNo"] for batch in self.cloud.pushed for e in batch]
        self.assertEqual(pushed, [10, 12])
        self.assertEqual(self.bridge.state.outbox, [])
        self.assertEqual(self.bridge.state.cursor(SERIAL), 12)

    def test_full_outbox_stops_polling_without_losing_events(self):
        from schoolos_bridge import service

        self.cloud.down = True
        self.driver.events = [_event(n) for n in range(10, 16)]
        original = service.MAX_OUTBOX
        service.MAX_OUTBOX = 4
        self.addCleanup(setattr, service, "MAX_OUTBOX", original)
        self.bridge.tick()
        self.assertEqual([e["cursor"] for e in self.bridge.state.outbox], [10, 11, 12, 13])
        self.bridge.collect(SERIAL, [_event(99)], advance=False)  # push while full: poll recovers it
        self.assertEqual(len(self.bridge.state.outbox), 4)
        self.cloud.down = False
        self.bridge.tick()  # flush 10-13, cursor 13
        self.bridge.tick()  # poll resumes after the cursor: 14, 15
        pushed = [e["raw"]["serialNo"] for batch in self.cloud.pushed for e in batch]
        self.assertEqual(pushed, [10, 11, 12, 13, 14, 15])
        self.assertEqual(self.bridge.state.cursor(SERIAL), 15)

    def test_restart_does_not_resend(self):
        self.driver.events = [_event(10)]
        self.bridge.tick()
        self._bridge().tick()
        self.assertEqual(len(self.cloud.pushed), 1)

    def test_outbox_survives_restart(self):
        self.driver.events = [_event(10)]
        self.cloud.down = True
        self.bridge.tick()
        self.cloud.down = False
        self.driver.events = []
        self._bridge().tick()
        self.assertEqual(len(self.cloud.pushed), 1)

    def test_pushed_event_does_not_advance_cursor(self):
        self.bridge.collect(SERIAL, [_event(50)], advance=False)
        self.bridge.tick()
        self.assertEqual(len(self.cloud.pushed), 1)
        self.assertIsNone(self.bridge.state.cursor(SERIAL))

    def test_enroll_command_downloads_photo_and_acks_done(self):
        self.cloud.commands = [{"id": 7, "type": "ENROLL", "device_serial": SERIAL,
                                "payload": {"osid": "5893949988", "name": "E2E Test", "has_photo": True}}]
        self.cloud.photos[7] = b"jpg"
        self.bridge.tick()
        self.assertEqual(self.driver.enrolled[0].photo, b"jpg")
        self.assertEqual(self.driver.enrolled[0].name, "E2E Test")
        self.assertEqual(self.cloud.acks, [(7, "DONE", "")])

    def test_enroll_command_passes_remove_face(self):
        self.cloud.commands = [{"id": 11, "type": "ENROLL", "device_serial": SERIAL,
                                "payload": {"osid": "1", "name": "x", "has_photo": False, "remove_face": True}}]
        self.bridge.tick()
        self.assertTrue(self.driver.enrolled[0].remove_face)
        self.assertIsNone(self.driver.enrolled[0].photo)

    def test_delete_command(self):
        self.cloud.commands = [{"id": 8, "type": "DELETE", "device_serial": SERIAL, "payload": {"osid": "1"}}]
        self.bridge.tick()
        self.assertEqual(self.driver.deleted, ["1"])
        self.assertEqual(self.cloud.acks, [(8, "DONE", "")])

    def test_capture_command_uploads_photo_and_acks_done(self):
        self.cloud.commands = [{"id": 12, "type": "CAPTURE", "device_serial": SERIAL,
                                "payload": {"osid": "5893949988", "name": "E2E Test"}}]
        self.bridge.tick()
        self.assertEqual(self.cloud.uploads, [(12, b"\xff\xd8photo")])
        self.assertEqual(self.cloud.acks, [(12, "DONE", "7 bytes")])

    def test_capture_failure_acks_failed(self):
        self.driver.fail = "cameraBusy"
        self.cloud.commands = [{"id": 13, "type": "CAPTURE", "device_serial": SERIAL,
                                "payload": {"osid": "5893949988", "name": "E2E Test"}}]
        self.bridge.tick()
        self.assertEqual(self.cloud.uploads, [])
        self.assertEqual(self.cloud.acks, [(13, "FAILED", "cameraBusy")])

    def test_driver_error_acks_failed(self):
        self.driver.fail = "deviceBusy"
        self.cloud.commands = [{"id": 9, "type": "ENROLL", "device_serial": SERIAL,
                                "payload": {"osid": "1", "name": "x", "has_photo": False}}]
        self.bridge.tick()
        self.assertEqual(self.cloud.acks, [(9, "FAILED", "deviceBusy")])

    def test_unknown_device_acks_failed(self):
        self.cloud.commands = [{"id": 10, "type": "DELETE", "device_serial": "NOPE", "payload": {"osid": "1"}}]
        self.bridge.tick()
        self.assertEqual(self.cloud.acks, [(10, "FAILED", "unknown device")])

    def test_heartbeat_reports_device_ips(self):
        self.bridge.tick()
        self.assertEqual(
            self.cloud.heartbeats,
            [
                [
                    {
                        "serial": SERIAL,
                        "ip": "10.0.0.5",
                        "vendor": "hikvision",
                        "model": "",
                        "direction": "IN",
                        "location_label": "",
                    }
                ]
            ],
        )
        self.bridge.tick()
        self.assertEqual(len(self.cloud.heartbeats), 1)  # throttled to once a minute


def _roster(osid, name="Ali Valiyev", face=None):
    return {"osid": osid, "name": name, "has_photo": face is not None, "face_version": face}


class SyncCommandTests(BridgeTests.__base__):
    setUp = BridgeTests.setUp
    _bridge = BridgeTests._bridge

    def _reconcile(self, roster, command_id=20):
        self.cloud.commands = [{"id": command_id, "type": "RECONCILE", "device_serial": SERIAL,
                                "payload": {"roster": roster}}]
        self.bridge.tick()
        command, status, result = self.cloud.acks[-1]
        self.assertEqual(command, command_id)
        return status, json.loads(result) if result.startswith("{") else result

    def test_reconcile_enrolls_missing_and_deletes_strays(self):
        self.driver.persons = [
            TerminalPerson("1000000001", "Ali Valiyev", has_face=True, is_admin=False),
            TerminalPerson("1000000009", "Left school", has_face=True, is_admin=False),
            TerminalPerson("1000000008", "Terminal admin", has_face=False, is_admin=True),
            TerminalPerson("guard", "Manual entry", has_face=False, is_admin=False),
        ]
        self.cloud.photos[(20, "1000000002")] = b"jpg"
        status, result = self._reconcile([_roster("1000000001", face="v1"), _roster("1000000002", "Vali", face="v1")])
        self.assertEqual(status, "DONE")
        self.assertEqual([(p.osid, p.photo) for p in self.driver.enrolled], [("1000000002", b"jpg")])
        self.assertEqual(self.driver.deleted, ["1000000009"])
        self.assertEqual(
            result, {"enrolled": 1, "deleted": 1, "unchanged": 1, "kept": 2, "failed": 0, "errors": []}
        )
        # Faces already there are adopted as current; the new one is recorded.
        self.assertEqual(self.bridge.state.face(SERIAL, "1000000001"), "v1")
        self.assertEqual(self.bridge.state.face(SERIAL, "1000000002"), "v1")

    def test_reconcile_reuploads_outdated_face_and_renamed_person(self):
        self.driver.persons = [
            TerminalPerson("1000000001", "Ali Valiyev", has_face=True, is_admin=False),
            TerminalPerson("1000000002", "Old name", has_face=False, is_admin=False),
        ]
        self.bridge.state.set_face(SERIAL, "1000000001", "v1")
        self.cloud.photos[(20, "1000000001")] = b"new"
        status, result = self._reconcile([_roster("1000000001", face="v2"), _roster("1000000002", "New name")])
        self.assertEqual(sorted(p.osid for p in self.driver.enrolled), ["1000000001", "1000000002"])
        self.assertEqual(self.bridge.state.face(SERIAL, "1000000001"), "v2")
        self.assertEqual(result["enrolled"], 2)

    def test_reconcile_with_missing_face_on_terminal_uploads(self):
        self.driver.persons = [TerminalPerson("1000000001", "Ali Valiyev", has_face=False, is_admin=False)]
        self.cloud.photos[(20, "1000000001")] = b"jpg"
        self._reconcile([_roster("1000000001", face="v1")])
        self.assertEqual(self.driver.enrolled[0].photo, b"jpg")

    def test_reconcile_empty_roster_never_deletes(self):
        self.driver.persons = [TerminalPerson("1000000009", "x", has_face=False, is_admin=False)]
        status, result = self._reconcile([])
        self.assertEqual(self.driver.deleted, [])
        self.assertEqual(result["kept"], 1)

    def test_reconcile_person_failure_is_counted_not_fatal(self):
        self.driver.fail_osids = {"1000000002"}
        status, result = self._reconcile([_roster("1000000002"), _roster("1000000003")])
        self.assertEqual(status, "DONE")
        self.assertEqual((result["enrolled"], result["failed"]), (1, 1))
        self.assertIn("1000000002", result["errors"][0])

    def test_reconcile_listing_failure_acks_failed(self):
        self.driver.fail = "deviceBusy"
        status, result = self._reconcile([_roster("1000000001")])
        self.assertEqual((status, result), ("FAILED", "deviceBusy"))

    def test_set_time_command(self):
        self.cloud.commands = [{"id": 21, "type": "SET_TIME", "device_serial": SERIAL,
                                "payload": {"timezone": "Asia/Tashkent", "posix": "CST-5:00:00"}}]
        self.bridge.tick()
        self.assertEqual(self.driver.zones, ["CST-5:00:00"])
        self.assertEqual(self.cloud.acks, [(21, "DONE", "changed")])

    def test_enroll_and_delete_track_face_versions(self):
        self.cloud.commands = [{"id": 7, "type": "ENROLL", "device_serial": SERIAL,
                                "payload": {"osid": "1000000001", "name": "A", "has_photo": True,
                                            "face_version": "v3"}}]
        self.cloud.photos[7] = b"jpg"
        self.bridge.tick()
        self.assertEqual(self.bridge.state.face(SERIAL, "1000000001"), "v3")
        self.cloud.commands = [{"id": 8, "type": "DELETE", "device_serial": SERIAL,
                                "payload": {"osid": "1000000001"}}]
        self.bridge.tick()
        self.assertIsNone(self.bridge.state.face(SERIAL, "1000000001"))


class UnmatchedTests(BridgeTests.__base__):
    setUp = BridgeTests.setUp
    _bridge = BridgeTests._bridge

    def _unmatched_tick(self, cursor=10, osid="5893949988"):
        self.driver.events = [_event(cursor, osid=osid)]
        self.cloud.unmatched_osids = {osid}
        self.bridge.tick()

    def test_unknown_event_snapshots_and_reports_candidate(self):
        self.driver.face_matches = [FaceMatch("1000000001", 88.0)]
        self._unmatched_tick()
        self.assertEqual(
            self.cloud.unmatched_uploads,
            [(SERIAL, "5893949988", "2026-10-10T08:00:00+05:00", b"\xff\xd8photo", "1000000001", 88.0)],
        )

    def test_unknown_without_match_reports_no_candidate(self):
        self._unmatched_tick()
        upload = self.cloud.unmatched_uploads[0]
        self.assertIsNone(upload[4])
        self.assertIsNone(upload[5])

    def test_low_similarity_match_is_ignored(self):
        self.driver.face_matches = [FaceMatch("1000000001", 30.0)]
        self._unmatched_tick()
        self.assertIsNone(self.cloud.unmatched_uploads[0][4])

    def test_similarity_missing_is_accepted(self):
        self.driver.face_matches = [FaceMatch("1000000001", None)]
        self._unmatched_tick()
        self.assertEqual(self.cloud.unmatched_uploads[0][4], "1000000001")

    def test_match_equal_to_stranger_osid_is_ignored(self):
        self.driver.face_matches = [FaceMatch("5893949988", 99.0)]
        self._unmatched_tick()
        self.assertIsNone(self.cloud.unmatched_uploads[0][4])

    def test_known_event_is_not_snapshotted(self):
        self.driver.events = [_event(10, osid="1000000001")]
        self.bridge.tick()
        self.assertEqual(self.cloud.unmatched_uploads, [])

    def test_snapshots_are_throttled_per_device(self):
        self.driver.face_matches = []
        self._unmatched_tick(cursor=10)
        self._unmatched_tick(cursor=11)
        self.assertEqual(len(self.cloud.unmatched_uploads), 1)

    def test_snapshot_failure_is_swallowed(self):
        self.driver.fail = "cameraBusy"
        self._unmatched_tick()
        self.assertEqual(self.cloud.unmatched_uploads, [])

    def test_upload_failure_is_swallowed(self):
        self.cloud.upload_fail = True
        self._unmatched_tick()
        self.assertEqual(self.cloud.unmatched_uploads, [])


class ListenerTests(unittest.TestCase):
    def test_push_from_known_device_lands_in_outbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(cloud_url="https://x", token="t", state_file=Path(tmp.name) / "s.json",
                        devices=[DeviceConfig(SERIAL, "hikvision", "127.0.0.1", "a", "p", "IN")])
        bridge = Bridge(config, FakeCloud(), {SERIAL: FakeDriver()}, State(config.state_file))
        server = make_server(bridge, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/hik", data=json.dumps({"cursor": 77}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        self.assertEqual(urllib.request.urlopen(request, timeout=5).status, 200)
        self.assertEqual([e["event"]["raw"]["serialNo"] for e in bridge.state.outbox], [77])

    def _serve(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(
            cloud_url="https://c", token="t", state_file=Path(tmp.name) / "state.json",
            devices=[DeviceConfig(SERIAL, "hikvision", "127.0.0.1", "admin", "pw", "IN")],
        )
        bridge = Bridge(config, FakeCloud(), {SERIAL: FakeDriver()}, State(config.state_file))
        server = make_server(bridge, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return bridge, server.server_address[1]

    def test_negative_content_length_does_not_hang(self):
        # `rfile.read(-1)` would block until the client hangs up; a stray negative
        # header must be treated as an empty body instead.
        bridge, port = self._serve()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /push HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\nConnection: close\r\n\r\n")
        self.assertIn(b"200", sock.recv(4096))
        self.assertEqual(bridge.state.outbox, [])

    def test_oversized_body_is_bounded_and_connection_closed(self):
        import schoolos_bridge.listener as listener

        original = listener.MAX_BODY
        listener.MAX_BODY = 16
        self.addCleanup(setattr, listener, "MAX_BODY", original)
        bridge, port = self._serve()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(sock.close)
        # Claim 100 bytes, send exactly that: only MAX_BODY is consumed and the
        # connection is closed so the surplus cannot be read as a new request.
        sock.sendall(b"POST /push HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n" + b"x" * 100)
        self.assertIn(b"200", sock.recv(4096))
        self.assertEqual(sock.recv(4096), b"")
