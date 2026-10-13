import tempfile
import unittest
from pathlib import Path

from schoolos_bridge.config import load_config

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
