import unittest
from unittest import mock

import requests

from schoolos_bridge.cloud import CloudClient, CloudError, CloudRejected, explain


def _response(status, json=None, content=b""):
    r = mock.Mock(status_code=status, content=content)
    r.json.return_value = json
    r.ok = 200 <= status < 300
    return r


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.client = CloudClient("https://cloud.test/", "tok", session=self.session)

    def test_push_events_posts_list_with_bearer(self):
        self.session.request.return_value = _response(200, {"accepted": ["k"]})
        self.assertEqual(self.client.push_events([{"osid": "1"}]), {"accepted": ["k"]})
        method, url = self.session.request.call_args.args
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual((method, url), ("POST", "https://cloud.test/api/devices/events/"))
        self.assertEqual(kwargs["json"], [{"osid": "1"}])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

    def test_fetch_photo_404_is_none(self):
        self.session.request.return_value = _response(404)
        self.assertIsNone(self.client.fetch_photo(7))

    def test_fetch_photo_returns_bytes(self):
        self.session.request.return_value = _response(200, content=b"img")
        self.assertEqual(self.client.fetch_photo(7), b"img")
        self.assertEqual(
            self.session.request.call_args.args[1], "https://cloud.test/api/devices/commands/7/photo/"
        )

    def test_server_error_raises(self):
        self.session.request.return_value = _response(500)
        with self.assertRaises(CloudError):
            self.client.fetch_commands()

    def test_network_error_raises(self):
        self.session.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(CloudError):
            self.client.heartbeat([{"serial": "s", "ip": "1.2.3.4"}])

    def test_ack_posts_status(self):
        self.session.request.return_value = _response(200, {"id": 3, "status": "DONE"})
        self.client.ack(3, "DONE")
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"status": "DONE", "result": ""})

    def test_bad_request_is_rejected_not_transient(self):
        self.session.request.return_value = _response(400)
        with self.assertRaises(CloudRejected):
            self.client.push_events([{"osid": ""}])

    def test_unauthorized_is_transient(self):
        self.session.request.return_value = _response(401)
        with self.assertRaises(CloudError) as ctx:
            self.client.push_events([])
        self.assertNotIsInstance(ctx.exception, CloudRejected)


class CloudRosterPhotoTests(unittest.TestCase):
    def test_fetch_photo_for_roster_member_passes_osid(self):
        session = mock.Mock()
        session.request.return_value = _response(200, content=b"img")
        client = CloudClient("https://cloud.test/", "tok", session=session)
        self.assertEqual(client.fetch_photo(7, osid="1000000001"), b"img")
        self.assertEqual(session.request.call_args.kwargs["params"], {"osid": "1000000001"})

    def test_upload_unmatched_posts_multipart_with_metadata(self):
        session = mock.Mock()
        session.request.return_value = _response(201, {"id": 5, "status": "NEW"})
        client = CloudClient("https://cloud.test/", "tok", session=session)
        client.upload_unmatched(
            "SER-1", "5893949988", "2026-10-10T08:00:00+05:00", b"\xff\xd8face",
            candidate_osid="1000000001", similarity=88.5,
        )
        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        self.assertEqual((method, url), ("POST", "https://cloud.test/api/devices/unmatched/"))
        data = kwargs["data"]
        self.assertEqual(data["device_serial"], "SER-1")
        self.assertEqual(data["osid"], "5893949988")
        self.assertEqual(data["event_time"], "2026-10-10T08:00:00+05:00")
        self.assertEqual(data["candidate_osid"], "1000000001")
        self.assertEqual(data["similarity"], "88.5")
        self.assertEqual(kwargs["files"]["photo"][1], b"\xff\xd8face")

    def test_upload_unmatched_omits_candidate_when_unknown(self):
        session = mock.Mock()
        session.request.return_value = _response(201, {"id": 5, "status": "NEW"})
        client = CloudClient("https://cloud.test/", "tok", session=session)
        client.upload_unmatched("SER-1", "5893949988", "2026-10-10T08:00:00+05:00", b"jpg")
        data = session.request.call_args.kwargs["data"]
        self.assertNotIn("candidate_osid", data)
        self.assertNotIn("similarity", data)

    def test_upload_unmatched_rejected_raises(self):
        session = mock.Mock()
        session.request.return_value = _response(400)
        client = CloudClient("https://cloud.test/", "tok", session=session)
        with self.assertRaises(CloudRejected):
            client.upload_unmatched("SER-1", "1", "2026-10-10T08:00:00+05:00", b"jpg")


class CloudErrorHintTests(unittest.TestCase):
    def _client(self, status, headers=None):
        session = mock.Mock()
        response = _response(status)
        response.headers = headers or {}
        session.request.return_value = response
        return CloudClient("https://tochna.uz", "t", session=session), session

    def test_redirect_is_not_followed_and_is_explained(self):
        client, session = self._client(301, {"Location": "https://new.example/api/devices/heartbeat/"})
        with self.assertRaises(CloudError) as ctx:
            client.heartbeat([])
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        code, hint = explain(ctx.exception, client.base_url)
        self.assertEqual(code, "redirect")
        self.assertIn("https://new.example/api/devices/heartbeat/", hint)

    def test_wrong_path_is_explained(self):
        for status in (404, 405):
            client, _ = self._client(status)
            with self.assertRaises(CloudError) as ctx:
                client.heartbeat([])
            self.assertEqual(explain(ctx.exception)[0], "wrong_url")

    def test_token_and_server_errors(self):
        self.assertEqual(explain(CloudError("x", status=401))[0], "invalid_token")
        self.assertEqual(explain(CloudError("x", status=502))[0], "server")
        self.assertEqual(explain(CloudError("POST heartbeat/: HTTP 403"))[0], "invalid_token")

    def test_tls_and_offline(self):
        for cause, code in ((requests.exceptions.SSLError(), "tls"), (requests.exceptions.ConnectTimeout(), "timeout"),
                            (requests.exceptions.ConnectionError(), "unreachable")):
            try:
                raise CloudError("down") from cause
            except CloudError as exc:
                self.assertEqual(explain(exc)[0], code)
