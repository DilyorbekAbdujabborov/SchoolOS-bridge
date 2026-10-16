import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from schoolos_bridge.setup import discovery, store, tomlwrite
from schoolos_bridge.setup.auth import issue_link
from schoolos_bridge.setup.server import make_server, register_devices


class TomlWriteTests(unittest.TestCase):
    def test_round_trips_through_tomllib(self):
        import tomllib

        data = {
            "cloud_url": "https://tochna.uz",
            "token": "tok",
            "devices": [
                {
                    "serial": "SER-1",
                    "vendor": "hikvision",
                    "ip": "192.168.1.64",
                    "username": "admin",
                    "password": 'p"w\\x',
                    "direction": "BOTH",
                    "location_label": "Asosiy kirish",
                }
            ],
        }
        parsed = tomllib.loads(tomlwrite.dumps(data))
        self.assertEqual(parsed["cloud_url"], "https://tochna.uz")
        self.assertEqual(parsed["devices"][0]["password"], 'p"w\\x')
        self.assertEqual(parsed["devices"][0]["location_label"], "Asosiy kirish")

    def test_escapes_control_characters(self):
        self.assertEqual(tomlwrite._string("a\nb\tc"), '"a\\nb\\tc"')

    def test_omits_none_values(self):
        text = tomlwrite.dumps({"cloud_url": "https://c", "token": None})
        self.assertIn("cloud_url", text)
        self.assertNotIn("token", text)

    def test_value_rejects_none(self):
        with self.assertRaises(ValueError):
            tomlwrite._value(None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_read_with_owner_only_permissions(self):
        store.write_config(
            self.path,
            {"cloud_url": "https://c", "token": "t", "devices": [{"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "p", "direction": "IN"}]},
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        data = store.read_config(self.path)
        self.assertEqual(data["devices"][0]["serial"], "S")

    def test_secrets_never_reach_the_file_in_clear(self):
        store.write_config(
            self.path,
            {"cloud_url": "https://c", "token": "cloud-token-xyz", "devices": [{"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "terminal-pass", "direction": "IN"}]},
        )
        text = self.path.read_text()
        self.assertNotIn("cloud-token-xyz", text)
        self.assertNotIn("terminal-pass", text)
        self.assertIn("enc:v1:", text)
        key = self.path.parent / "secret.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        data = store.read_config(self.path)
        self.assertEqual(data["token"], "cloud-token-xyz")
        self.assertEqual(data["devices"][0]["password"], "terminal-pass")

    def test_migrate_seals_a_clear_text_config(self):
        self.path.write_text(
            'cloud_url = "https://c"\ntoken = "old-token"\n\n[[devices]]\nserial = "S"\nvendor = "hikvision"\n'
            'ip = "1.2.3.4"\nusername = "admin"\npassword = "old-pass"\ndirection = "IN"\n'
        )
        self.assertTrue(store.migrate(self.path))
        self.assertNotIn("old-pass", self.path.read_text())
        self.assertFalse(store.migrate(self.path))
        self.assertEqual(store.read_config(self.path)["devices"][0]["password"], "old-pass")

    def test_foreign_key_is_reported_not_crashed(self):
        from schoolos_bridge.vault import VaultError

        store.write_config(self.path, {"cloud_url": "https://c", "token": "t"})
        (self.path.parent / "secret.key").write_bytes(b"x" * 44)
        with self.assertRaises(VaultError):
            store.read_config(self.path)

    def test_read_missing_is_empty(self):
        self.assertEqual(store.read_config(self.path), {})

    def test_write_falls_back_to_in_place_when_replace_is_denied(self):
        store.write_config(self.path, {"cloud_url": "https://old", "token": "t"})
        with mock.patch("schoolos_bridge.setup.store.os.replace", side_effect=PermissionError("read-only dir")):
            store.write_config(self.path, {"cloud_url": "https://new", "token": "t"})
        self.assertEqual(store.read_config(self.path)["cloud_url"], "https://new")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_public_config_hides_secrets(self):
        store.write_config(
            self.path,
            {"cloud_url": "https://c", "token": "t", "devices": [{"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "secret", "direction": "IN"}]},
        )
        public = store.public_config(self.path)
        self.assertTrue(public["token_set"])
        self.assertNotIn("token", public)
        device = public["devices"][0]
        self.assertNotIn("password", device)
        self.assertTrue(device["password_set"])
        self.assertNotIn("secret", json.dumps(public))

    def test_merge_save_keeps_existing_token_and_password(self):
        existing = {
            "cloud_url": "https://old",
            "token": "keep-me",
            "devices": [{"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "keep-pw", "direction": "IN"}],
        }
        data = store.merge_save(
            existing,
            {
                "cloud_url": "https://new",
                "token": "",
                "devices": [{"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "", "direction": "OUT"}],
            },
        )
        self.assertEqual(data["token"], "keep-me")
        self.assertEqual(data["devices"][0]["password"], "keep-pw")
        self.assertEqual(data["devices"][0]["direction"], "OUT")

    def test_clean_device_drops_unknown_keys(self):
        self.assertEqual(
            store.clean_device({"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "p", "direction": "IN", "junk": 1}),
            {"serial": "S", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "p", "direction": "IN"},
        )

    def test_merge_save_omits_empty_state_file(self):
        data = store.merge_save({}, {"cloud_url": "https://c", "token": "t", "devices": []})
        self.assertNotIn("state_file", data)

    def test_saved_config_loads_with_the_bridge_loader(self):
        from schoolos_bridge.config import load_config

        data = store.merge_save(
            {},
            {
                "cloud_url": "https://tochna.uz",
                "token": "tok",
                "devices": [
                    {"serial": "SER-1", "vendor": "hikvision", "ip": "192.168.1.64:8000", "username": "admin", "password": "p", "direction": "BOTH", "location_label": "Kirish"}
                ],
            },
        )
        store.write_config(self.path, data)
        loaded = load_config(self.path)
        self.assertEqual(loaded.cloud_url, "https://tochna.uz")
        self.assertEqual(loaded.devices[0].serial, "SER-1")
        self.assertEqual(loaded.devices[0].location_label, "Kirish")
        self.assertEqual(loaded.devices[0].ip, "192.168.1.64:8000")


class HostParsingTests(unittest.TestCase):
    def test_cidr(self):
        hosts = discovery._hosts("192.168.1.0/30")
        self.assertEqual(hosts, ["192.168.1.1", "192.168.1.2"])

    def test_single_ip(self):
        self.assertEqual(discovery._hosts("10.0.0.7"), ["10.0.0.7"])

    def test_range_with_offset(self):
        self.assertEqual(discovery._hosts("10.0.0.10-12"), ["10.0.0.10", "10.0.0.11", "10.0.0.12"])

    def test_range_with_end_address(self):
        self.assertEqual(discovery._hosts("10.0.0.10-10.0.0.11"), ["10.0.0.10", "10.0.0.11"])

    def test_garbage_is_empty(self):
        self.assertEqual(discovery._hosts("not-an-ip"), [])

    def test_oversized_network_is_refused(self):
        with self.assertRaises(discovery.ScanError):
            discovery._hosts("10.0.0.0/8")

    def test_oversized_range_is_refused(self):
        with self.assertRaises(discovery.ScanError):
            discovery._hosts("10.0.0.1-10.0.5.1")


def _resp(status=200, text="", headers=None):
    return mock.Mock(status_code=status, text=text, headers=headers or {})


class DetectTests(unittest.TestCase):
    def test_401_digest_challenge_is_a_terminal(self):
        http = mock.Mock()
        http.get.return_value = _resp(401, headers={"WWW-Authenticate": 'Digest realm="DS-1234"'})
        self.assertIsNotNone(discovery.detect("192.168.1.64", http=http))

    def test_200_with_device_info_is_a_terminal(self):
        http = mock.Mock()
        http.get.return_value = _resp(200, text="<DeviceInfo><serialNumber>X</serialNumber></DeviceInfo>")
        self.assertIsNotNone(discovery.detect("192.168.1.64", http=http))

    def test_404_is_not_a_terminal(self):
        http = mock.Mock()
        http.get.return_value = _resp(404)
        self.assertIsNone(discovery.detect("192.168.1.64", http=http))

    def test_connection_errors_are_skipped(self):
        import requests

        http = mock.Mock()
        http.get.side_effect = requests.ConnectionError("nope")
        self.assertIsNone(discovery.detect("192.168.1.64", http=http))


class ScanTests(unittest.TestCase):
    def test_scan_collects_only_terminals(self):
        import requests

        term = _resp(401, headers={"WWW-Authenticate": "Digest realm=x"})
        miss = _resp(404)

        def get(url, **kwargs):
            if "192.168.1.5" in url and ":80" not in url:
                return term
            return miss

        http = mock.Mock()
        http.get.side_effect = get
        found = discovery.scan("192.168.1.0/29", http=http)
        self.assertEqual([row["ip"] for row in found], ["192.168.1.5"])
        self.assertEqual(found[0]["port"], 80)


class ProbeTests(unittest.TestCase):
    def _http(self, device_info, clock):
        http = mock.Mock()

        def request(method, url, **kwargs):
            if url.endswith("/ISAPI/System/deviceInfo"):
                return _resp(200, text=device_info)
            return _resp(200, text=clock)

        http.request.side_effect = request
        return http

    def test_probe_reads_identity(self):
        info = "<DeviceInfo><serialNumber>SER-9</serialNumber><model>DS-K1</model><firmwareVersion>V4.38</firmwareVersion></DeviceInfo>"
        clock = "<Time><timeZone>CST-5:00:00</timeZone><localTime>2026-10-14T10:00:00+05:00</localTime></Time>"
        result = discovery.probe("10.0.0.5", "admin", "pw", http=self._http(info, clock))
        self.assertTrue(result["ok"])
        self.assertEqual(result["serial"], "SER-9")
        self.assertEqual(result["model"], "DS-K1")

    def test_probe_maps_401_to_login_required(self):
        http = mock.Mock()
        http.request.return_value = _resp(401, text="")
        result = discovery.probe("10.0.0.5", "admin", "bad", http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "login_required")

    def test_probe_includes_port_in_url(self):
        http = self._http("<DeviceInfo><serialNumber>S</serialNumber></DeviceInfo>", "<Time><timeZone></timeZone><localTime></localTime></Time>")
        discovery.probe("10.0.0.5", "admin", "pw", port=8000, http=http)
        url = http.request.call_args_list[0].args[1]
        self.assertIn("http://10.0.0.5:8000", url)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.server = make_server(str(self.path), "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop())
        self.token = self.sign_in()

    def sign_in(self):
        status, body = self.call("POST", "/api/session", {"link": issue_link(self.path)}, token=None)
        self.assertEqual(status, 200, body)
        return json.loads(body)["token"]

    def _stop(self):
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            self.tmp.cleanup()

        return cleanup

    def call(self, method, path, body=None, token=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        if token == "":
            token = self.token
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def test_index_is_served(self):
        status, body = self.call("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"SchoolOS", body)

    def test_asset_is_served(self):
        status, body = self.call("GET", "/assets/app.js", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"function", body)

    def test_path_traversal_is_refused(self):
        status, _ = self.call("GET", "/assets/../server.py", token=None)
        self.assertEqual(status, 404)

    def test_api_requires_a_session(self):
        status, _ = self.call("GET", "/api/state", token=None)
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/state", token="not.a.jwt")
        self.assertEqual(status, 401)

    def test_link_works_once(self):
        link = issue_link(self.path)
        status, _ = self.call("POST", "/api/session", {"link": link}, token=None)
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/api/session", {"link": link}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "bad_link")

    def test_forged_link_is_refused(self):
        other = Path(self.tmp.name) / "elsewhere" / "config.toml"
        status, _ = self.call("POST", "/api/session", {"link": issue_link(other)}, token=None)
        self.assertEqual(status, 401)

    def test_logout_ends_the_session(self):
        status, _ = self.call("POST", "/api/logout", {})
        self.assertEqual(status, 200)
        status, _ = self.call("GET", "/api/state")
        self.assertEqual(status, 401)

    def test_device_delete_removes_from_config(self):
        store.write_config(self.path, {"cloud_url": "https://c", "token": "t", "devices": [
            {"serial": "A", "vendor": "hikvision", "ip": "1.1.1.1", "username": "admin", "password": "p", "direction": "IN"},
            {"serial": "B", "vendor": "hikvision", "ip": "1.1.1.2", "username": "admin", "password": "p", "direction": "IN"},
        ]})
        status, body = self.call("POST", "/api/device-delete", {"serial": "A"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["deleted"], 1)
        self.assertEqual([d["serial"] for d in store.read_config(self.path)["devices"]], ["B"])

    def test_responses_carry_hardening_headers(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = response.headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_bad_tokens_are_rate_limited(self):
        from schoolos_bridge.setup import server as server_module

        for _ in range(server_module.AUTH_MAX_FAILURES):
            status, _ = self.call("GET", "/api/state", token="bad")
            self.assertEqual(status, 401)
        # The budget is spent: even a *valid* session is refused until it clears.
        status, body = self.call("GET", "/api/state")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"], "too_many_requests")

    def test_unknown_endpoint_is_not_enumerable_without_a_session(self):
        # An unauthenticated peer must not learn which paths exist (401 for all,
        # never 404 for some).
        status, _ = self.call("POST", "/api/does-not-exist", {}, token=None)
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/api/does-not-exist", {})
        self.assertEqual(status, 404)

    def test_state_returns_public_config(self):
        status, body = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["config_path"], str(self.path))
        self.assertIn("default_subnet", data)
        self.assertIn("service", data)
        self.assertIn(data["platform"], ("windows", "macos", "linux"))

    def test_scan_uses_discovery(self):
        with mock.patch.object(discovery, "scan", return_value=[{"ip": "192.168.1.64", "port": 80, "server": "", "status": 401}]) as scan:
            status, body = self.call("POST", "/api/scan", {"target": "192.168.1.0/24"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["found"][0]["ip"], "192.168.1.64")
        self.assertFalse(data["found"][0]["configured"])
        scan.assert_called_once()

    def test_scan_too_large_is_400(self):
        with mock.patch.object(discovery, "scan", side_effect=discovery.ScanError("juda katta")):
            status, body = self.call("POST", "/api/scan", {"target": "10.0.0.0/8"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "scan_too_large")

    def test_probe_uses_discovery(self):
        with mock.patch.object(discovery, "probe", return_value={"ok": True, "serial": "S"}) as probe:
            status, body = self.call("POST", "/api/probe", {"ip": "192.168.1.64", "password": "pw"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(probe.call_args.args[:3], ("192.168.1.64", "admin", "pw"))

    def test_cloud_test_reports_ok(self):
        with mock.patch("schoolos_bridge.setup.server.CloudClient") as client:
            client.return_value.heartbeat.return_value = None
            status, body = self.call("POST", "/api/cloud-test", {"cloud_url": "https://c", "token": "t"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_cloud_test_rejects_bad_token(self):
        from schoolos_bridge.cloud import CloudError

        with mock.patch("schoolos_bridge.setup.server.CloudClient") as client:
            client.return_value.heartbeat.side_effect = CloudError("POST heartbeat/: HTTP 401")
            status, body = self.call("POST", "/api/cloud-test", {"cloud_url": "https://c", "token": "bad"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_token")

    def test_save_requires_token(self):
        status, body = self.call("POST", "/api/save", {"cloud_url": "https://c", "devices": []})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "validation")

    def test_save_writes_config(self):
        payload = {
            "cloud_url": "https://c",
            "token": "tok",
            "devices": [
                {"serial": "SER-1", "vendor": "hikvision", "ip": "192.168.1.64", "username": "admin", "password": "pw", "direction": "BOTH", "location_label": "Kirish"}
            ],
        }
        with mock.patch("schoolos_bridge.setup.server.CloudClient") as client:
            client.return_value.heartbeat.return_value = None
            status, body = self.call("POST", "/api/save", payload)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["saved"])
        # Saving also registers the terminal with the cloud (auto-provision).
        self.assertTrue(data["registration"]["ok"])
        registered = client.return_value.heartbeat.call_args.args[0]
        self.assertEqual(registered[0]["serial"], "SER-1")
        saved = store.read_config(self.path)
        self.assertEqual(saved["token"], "tok")
        self.assertEqual(saved["devices"][0]["location_label"], "Kirish")

    def test_save_survives_a_cloud_that_is_down(self):
        from schoolos_bridge.cloud import CloudError

        payload = {
            "cloud_url": "https://c",
            "token": "tok",
            "devices": [{"serial": "SER-1", "vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "pw", "direction": "IN"}],
        }
        with mock.patch("schoolos_bridge.setup.server.CloudClient") as client:
            client.return_value.heartbeat.side_effect = CloudError("unreachable")
            status, body = self.call("POST", "/api/save", payload)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["saved"])  # the config is written regardless
        self.assertFalse(data["registration"]["ok"])
        self.assertEqual(store.read_config(self.path)["token"], "tok")

    def test_register_publishes_saved_devices(self):
        store.write_config(
            self.path,
            {
                "cloud_url": "https://c",
                "token": "tok",
                "devices": [{"serial": "SER-9", "vendor": "hikvision", "ip": "10.0.0.9", "username": "admin", "password": "pw", "direction": "IN"}],
            },
        )
        with mock.patch("schoolos_bridge.setup.server.CloudClient") as client:
            client.return_value.heartbeat.return_value = None
            status, body = self.call("POST", "/api/register", {})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(client.return_value.heartbeat.call_args.args[0][0]["serial"], "SER-9")

    def test_register_without_config_reports_error(self):
        status, body = self.call("POST", "/api/register", {})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "not_configured")

    def test_save_rejects_device_without_serial(self):
        payload = {"cloud_url": "https://c", "token": "tok", "devices": [{"vendor": "hikvision", "ip": "1.2.3.4", "username": "admin", "password": "pw", "direction": "IN"}]}
        status, body = self.call("POST", "/api/save", payload)
        self.assertEqual(status, 400)

    def test_check_returns_results(self):
        from types import SimpleNamespace

        store.write_config(self.path, {"cloud_url": "https://c", "token": "t", "devices": []})
        fake = [
            SimpleNamespace(status="PASS", name="Cloud", detail="ok"),
            SimpleNamespace(status="FAIL", name="Config", detail="missing"),
        ]
        with mock.patch("schoolos_bridge.setup.server.run_checks", return_value=fake):
            status, body = self.call("POST", "/api/check", {})
        self.assertEqual(status, 200)
        results = json.loads(body)["results"]
        self.assertEqual([r["status"] for r in results], ["PASS", "FAIL"])
        self.assertEqual(results[1]["name"], "Config")


class RegisterDevicesTests(unittest.TestCase):
    def test_publishes_identity_without_secrets(self):
        client = mock.Mock()
        result = register_devices(
            "https://c",
            "tok",
            [
                {
                    "serial": "S",
                    "vendor": "hikvision",
                    "model": "DS-K1",
                    "ip": "10.0.0.5:8000",
                    "username": "admin",
                    "password": "secret",
                    "direction": "OUT",
                    "location_label": "Chiqish",
                }
            ],
            client=client,
        )
        self.assertTrue(result["ok"])
        sent = client.heartbeat.call_args.args[0]
        self.assertEqual(sent[0]["model"], "DS-K1")
        self.assertEqual(sent[0]["direction"], "OUT")
        self.assertNotIn("password", sent[0])
        self.assertNotIn("username", sent[0])

    def test_missing_token_is_not_configured(self):
        result = register_devices("https://c", "", [{"serial": "S"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_configured")

    def test_bad_token_is_reported(self):
        from schoolos_bridge.cloud import CloudError

        client = mock.Mock()
        client.heartbeat.side_effect = CloudError("POST heartbeat/: HTTP 401")
        result = register_devices("https://c", "bad", [{"serial": "S"}], client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_token")


if __name__ == "__main__":
    unittest.main()
