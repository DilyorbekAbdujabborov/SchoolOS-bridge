import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from schoolos_bridge.config import DeviceConfig
from schoolos_bridge.drivers import DriverError, Person, TerminalPerson
from schoolos_bridge.drivers.hikvision import HikvisionDriver

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE = json.loads((FIXTURES / "acs_event_page.json").read_text())
CFG = DeviceConfig(serial="SER-1", vendor="hikvision", ip="10.0.0.5", username="admin", password="pw", direction="IN")
OK = {"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"}


def _resp(json_body=None, text="", status=200):
    r = mock.Mock(status_code=status, text=text or json.dumps(json_body or {}))
    r.json.return_value = json_body
    return r


def _page(events, more):
    page = copy.deepcopy(PAGE)
    page["AcsEvent"]["InfoList"] = events
    page["AcsEvent"]["numOfMatches"] = len(events)
    page["AcsEvent"]["responseStatusStrg"] = "MORE" if more else "OK"
    return page


class HikvisionDriverTests(unittest.TestCase):
    def setUp(self):
        self.http = mock.Mock()
        self.driver = HikvisionDriver(CFG, http=self.http)
        self.events = PAGE["AcsEvent"]["InfoList"]

    def calls(self):
        return [(c.args[0], c.args[1].split("10.0.0.5")[1]) for c in self.http.request.call_args_list]

    def test_fetch_events_filters_and_sorts_ascending(self):
        noise = {"major": 5, "minor": 75, "time": "2026-10-10T08:00:00+05:00", "serialNo": 5}
        self.http.request.return_value = _resp(_page([self.events[2], noise, self.events[0], self.events[1]], False))
        events = self.driver.fetch_events(after_cursor=128292)
        self.assertEqual([e.cursor for e in events], [128295, 128298])
        first = events[0]
        self.assertEqual(
            (first.device_serial, first.osid, first.event_time, first.direction, first.verify_mode),
            ("SER-1", "1000000001", "2026-10-10T08:55:15+05:00", "IN", "faceOrFpOrCardOrPw"),
        )
        body = self.http.request.call_args.kwargs["json"]["AcsEventCond"]
        self.assertEqual((body["major"], body["minor"], body["timeReverseOrder"]), (5, 75, False))

    def test_fetch_events_searches_from_since(self):
        self.http.request.return_value = _resp(_page([], False))
        self.driver.fetch_events(after_cursor=128298, since="2026-10-10T08:56:49+05:00")
        body = self.http.request.call_args.kwargs["json"]["AcsEventCond"]
        self.assertEqual(body["startTime"], "2026-10-10T08:56:49+05:00")
        self.assertIn("endTime", body)

    def test_fetch_events_without_since_starts_today(self):
        self.http.request.return_value = _resp(_page([], False))
        self.driver.fetch_events(after_cursor=None)
        start = self.http.request.call_args.kwargs["json"]["AcsEventCond"]["startTime"]
        self.assertTrue(start.endswith("T00:00:00+05:00"), start)

    def test_fetch_events_pages_until_exhausted(self):
        self.http.request.side_effect = [
            _resp(_page(self.events[:2], True)),
            _resp(_page(self.events[2:], False)),
        ]
        events = self.driver.fetch_events(after_cursor=None)
        self.assertEqual(len(events), 3)
        positions = [c.kwargs["json"]["AcsEventCond"]["searchResultPosition"] for c in self.http.request.call_args_list]
        self.assertEqual(positions, [0, 2])

    def test_fresh_auth_per_request(self):
        self.http.request.side_effect = [_resp(_page(self.events[:1], True)), _resp(_page([], False))]
        self.driver.fetch_events(after_cursor=None)
        auths = [c.kwargs["auth"] for c in self.http.request.call_args_list]
        self.assertIsNot(auths[0], auths[1])

    def test_enroll_existing_person_modifies(self):
        exists = {"statusCode": 6, "statusString": "Invalid Content", "subStatusCode": "employeeNoAlreadyExist"}
        self.http.request.side_effect = [_resp(exists, status=400), _resp(OK), _resp(OK)]
        self.driver.enroll(Person(osid="5893949988", name="E2E Test", photo=b"jpg"))
        self.assertEqual(
            self.calls(),
            [
                ("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json"),
                ("PUT", "/ISAPI/AccessControl/UserInfo/Modify?format=json"),
                ("PUT", "/ISAPI/Intelligent/FDLib/FDSetUp?format=json"),
            ],
        )
        user = self.http.request.call_args_list[0].kwargs["json"]["UserInfo"]
        self.assertEqual((user["employeeNo"], user["name"], user["userType"]), ("5893949988", "E2E Test", "normal"))
        face_meta = json.loads(self.http.request.call_args_list[2].kwargs["files"]["FaceDataRecord"][1])
        self.assertEqual(face_meta, {"faceLibType": "blackFD", "FDID": "1", "FPID": "5893949988"})

    def test_enroll_without_photo_skips_face(self):
        self.http.request.return_value = _resp(OK)
        self.driver.enroll(Person(osid="5893949988", name="x" * 40))
        self.assertEqual(self.calls(), [("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json")])
        self.assertEqual(len(self.http.request.call_args.kwargs["json"]["UserInfo"]["name"]), 32)

    def test_enroll_remove_face_deletes_face(self):
        self.http.request.return_value = _resp(OK)
        self.driver.enroll(Person(osid="5893949988", name="E2E Test", remove_face=True))
        self.assertEqual(
            self.calls(),
            [
                ("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json"),
                ("PUT", "/ISAPI/Intelligent/FDLib/FDSearch/Delete?format=json&FDID=1&faceLibType=blackFD"),
            ],
        )
        self.assertEqual(self.http.request.call_args.kwargs["json"], {"FPID": [{"value": "5893949988"}]})

    def test_remove_face_when_none_exists_is_fine(self):
        missing = {"statusCode": 4, "statusString": "Invalid Operation", "subStatusCode": "notExist"}
        self.http.request.side_effect = [_resp(OK), _resp(missing, status=400)]
        self.driver.enroll(Person(osid="1", name="x", remove_face=True))  # no raise

    def test_delete_payload(self):
        self.http.request.return_value = _resp(OK)
        self.driver.delete("5893949988")
        self.assertEqual(self.calls(), [("PUT", "/ISAPI/AccessControl/UserInfo/Delete?format=json")])
        self.assertEqual(
            self.http.request.call_args.kwargs["json"],
            {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": "5893949988"}]}},
        )

    def test_error_status_raises(self):
        self.http.request.return_value = _resp(
            {"statusCode": 4, "statusString": "Invalid Operation", "subStatusCode": "deviceBusy"}, status=400
        )
        with self.assertRaisesRegex(DriverError, "deviceBusy"):
            self.driver.delete("1")

    def test_ensure_time_fixes_wrong_zone(self):
        wrong = (
            '<?xml version="1.0"?><Time><timeMode>manual</timeMode>'
            "<localTime>2026-10-10T08:42:24+08:00</localTime><timeZone>CST-8:00:00</timeZone></Time>"
        )
        self.http.request.side_effect = [_resp(text=wrong), _resp(text="<ResponseStatus><statusCode>1</statusCode></ResponseStatus>")]
        self.driver.ensure_time()
        method, path = self.calls()[1]
        self.assertEqual((method, path), ("PUT", "/ISAPI/System/time"))
        self.assertIn("<timeZone>CST-5:00:00</timeZone>", self.http.request.call_args.kwargs["data"])

    def test_ensure_time_leaves_correct_clock(self):
        from datetime import datetime

        now = datetime.now().astimezone().replace(microsecond=0).isoformat()
        right = f"<Time><localTime>{now}</localTime><timeZone>CST-5:00:00</timeZone></Time>"
        self.http.request.return_value = _resp(text=right)
        self.driver.ensure_time()
        self.assertEqual([m for m, _ in self.calls()], ["GET"])

    def test_parse_push_access_controller_event(self):
        body = json.dumps({
            "ipAddress": "10.0.0.5", "dateTime": "2026-10-10T09:10:00+05:00",
            "eventType": "AccessControllerEvent",
            "AccessControllerEvent": {"majorEventType": 5, "subEventType": 75, "employeeNoString": "5893949988",
                                      "serialNo": 128400, "currentVerifyMode": "faceOrFpOrCardOrPw"},
        }).encode()
        boundary = "MIME_boundary"
        multipart = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"AccessControllerEvent\"\r\n"
            f"Content-Type: application/json\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
        events = self.driver.parse_push(multipart, f"multipart/form-data; boundary={boundary}")
        self.assertEqual([(e.osid, e.cursor, e.event_time) for e in events],
                         [("5893949988", 128400, "2026-10-10T09:10:00+05:00")])

    def test_parse_push_ignores_heartbeats_and_noise(self):
        heartbeat = json.dumps({"eventType": "heartBeat", "dateTime": "2026-10-10T09:10:00+05:00"}).encode()
        self.assertEqual(self.driver.parse_push(heartbeat, "application/json"), [])
        door = json.dumps({"eventType": "AccessControllerEvent", "dateTime": "x",
                           "AccessControllerEvent": {"majorEventType": 5, "subEventType": 21}}).encode()
        self.assertEqual(self.driver.parse_push(door, "application/json"), [])


def _users_page(users, more, total=None):
    return {"UserInfoSearch": {
        "searchID": "schoolos", "responseStatusStrg": "MORE" if more else "OK",
        "numOfMatches": len(users), "totalMatches": total or len(users), "UserInfo": users,
    }}


class HikvisionPersonsAndTimeTests(unittest.TestCase):
    def setUp(self):
        self.http = mock.Mock()
        self.driver = HikvisionDriver(CFG, http=self.http)

    def calls(self):
        return [(c.args[0], c.args[1].split("10.0.0.5")[1]) for c in self.http.request.call_args_list]

    def test_list_persons_pages_by_30(self):
        first = [{"employeeNo": f"10000000{i:02d}", "name": f"P{i}", "userType": "normal", "numOfFace": 1}
                 for i in range(30)]
        second = [
            {"employeeNo": "admin1", "name": "Admin", "userType": "normal", "localUIRight": True},
            {"employeeNo": "1000000099", "name": "No face", "userType": "normal", "numOfFace": 0},
        ]
        self.http.request.side_effect = [_resp(_users_page(first, True, 32)), _resp(_users_page(second, False, 32))]
        persons = self.driver.list_persons()
        self.assertEqual(len(persons), 32)
        self.assertEqual(self.calls()[0], ("POST", "/ISAPI/AccessControl/UserInfo/Search?format=json"))
        conds = [c.kwargs["json"]["UserInfoSearchCond"] for c in self.http.request.call_args_list]
        self.assertEqual([(c["searchResultPosition"], c["maxResults"]) for c in conds], [(0, 30), (30, 30)])
        self.assertEqual(persons[0], TerminalPerson("1000000000", "P0", has_face=True, is_admin=False))
        self.assertTrue(persons[30].is_admin)
        self.assertFalse(persons[31].has_face)

    def test_list_persons_empty_terminal(self):
        self.http.request.return_value = _resp({"UserInfoSearch": {"responseStatusStrg": "NO MATCH", "numOfMatches": 0}})
        self.assertEqual(self.driver.list_persons(), [])

    def test_list_persons_admin_by_user_level(self):
        users = [{"employeeNo": "1000000001", "name": "Boss", "userType": "normal", "userLevel": "Administrator"},
                 {"employeeNo": "1000000002", "name": "Guest", "userType": "visitor"}]
        self.http.request.return_value = _resp(_users_page(users, False))
        self.assertEqual([p.is_admin for p in self.driver.list_persons()], [True, True])

    def test_set_timezone_keeps_ntp_mode(self):
        current = ("<Time><timeMode>NTP</timeMode><localTime>2026-10-10T08:42:24+08:00</localTime>"
                   "<timeZone>CST-8:00:00</timeZone></Time>")
        self.http.request.side_effect = [_resp(text=current), _resp(text="<statusCode>1</statusCode>")]
        self.assertTrue(self.driver.set_timezone("CST-5:00:00"))
        xml = self.http.request.call_args.kwargs["data"]
        self.assertEqual(self.calls()[1], ("PUT", "/ISAPI/System/time"))
        self.assertIn("<timeMode>NTP</timeMode>", xml)
        self.assertIn("<timeZone>CST-5:00:00</timeZone>", xml)
        self.assertNotIn("<localTime>", xml)

    def test_set_timezone_manual_mode_also_sets_clock(self):
        current = ("<Time><timeMode>manual</timeMode><localTime>2026-10-10T08:42:24+08:00</localTime>"
                   "<timeZone>CST-8:00:00</timeZone></Time>")
        self.http.request.side_effect = [_resp(text=current), _resp(text="<statusCode>1</statusCode>")]
        self.driver.set_timezone("CST-5:00:00")
        xml = self.http.request.call_args.kwargs["data"]
        self.assertIn("<timeMode>manual</timeMode>", xml)
        self.assertRegex(xml, r"<localTime>[0-9T:-]+\+05:00</localTime>")

    def test_set_timezone_noop_when_already_right(self):
        from datetime import datetime, timezone as tz, timedelta

        now = datetime.now(tz(timedelta(hours=5))).replace(microsecond=0).isoformat()
        current = f"<Time><timeMode>manual</timeMode><localTime>{now}</localTime><timeZone>CST-5:00:00</timeZone></Time>"
        self.http.request.return_value = _resp(text=current)
        self.assertFalse(self.driver.set_timezone("CST-5:00:00"))
        self.assertEqual([m for m, _ in self.calls()], ["GET"])

    def test_set_timezone_refused_raises(self):
        current = "<Time><timeMode>NTP</timeMode><timeZone>CST-8:00:00</timeZone></Time>"
        self.http.request.side_effect = [_resp(text=current), _resp(text="<statusCode>4</statusCode>")]
        with self.assertRaises(DriverError):
            self.driver.set_timezone("CST-5:00:00")

    def test_set_timezone_rejects_bad_posix(self):
        with self.assertRaises(DriverError):
            self.driver.set_timezone("Asia/Tashkent")
