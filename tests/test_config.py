import tempfile
import unittest
from pathlib import Path

from schoolos_bridge.config import load_config, normalize_cloud_url

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class ConfigTests(unittest.TestCase):
    def test_loads_example(self):
        cfg = load_config(EXAMPLE)
        self.assertEqual(cfg.cloud_url, "https://tochna.uz")
        self.assertEqual(cfg.poll_seconds, 2)
        self.assertEqual(cfg.listen_port, 8099)
        self.assertEqual(len(cfg.devices), 1)
        device = cfg.devices[0]
        self.assertEqual((device.vendor, device.ip, device.direction), ("hikvision", "192.168.43.65", "IN"))

    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_rejects_missing_token(self):
        with self.assertRaises(ValueError):
            load_config(self._write('cloud_url = "https://x"\n'))

    def test_rejects_unknown_vendor(self):
        path = self._write(
            'cloud_url = "https://x"\ntoken = "t"\n[[devices]]\nserial = "s"\nvendor = "acme"\n'
            'ip = "1.2.3.4"\nusername = "a"\npassword = "p"\n'
        )
        with self.assertRaises(ValueError):
            load_config(path)


class CloudUrlTests(unittest.TestCase):
    def test_normalizes_what_technicians_paste(self):
        cases = {
            "": "https://tochna.uz",
            None: "https://tochna.uz",
            "tochna.uz": "https://tochna.uz",
            "http://tochna.uz/": "https://tochna.uz",
            "https://tochna.uz/api": "https://tochna.uz",
            "https://tochna.uz/api/": "https://tochna.uz",
            "https://tochna.uz/api/devices/": "https://tochna.uz",
            " HTTPS://tochna.uz/app?x=1 ": "https://tochna.uz/app",
            "https://example.uz/schoolos/api": "https://example.uz/schoolos",
            "http://localhost:8000/api": "http://localhost:8000",
            "http://192.168.1.10:8000": "http://192.168.1.10:8000",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_cloud_url(raw), expected)

    def test_missing_cloud_url_defaults(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write('token = "t"\n')
        f.close()
        self.addCleanup(Path(f.name).unlink)
        self.assertEqual(load_config(f.name).cloud_url, "https://tochna.uz")

    def test_client_never_doubles_api(self):
        from schoolos_bridge.cloud import CloudClient

        class Session:
            def request(self, method, url, **kwargs):
                self.url = url
                return type("R", (), {"ok": True, "status_code": 200, "json": lambda s: {}})()

        session = Session()
        CloudClient("https://tochna.uz/api", "t", session=session).heartbeat([])
        self.assertEqual(session.url, "https://tochna.uz/api/devices/heartbeat/")
