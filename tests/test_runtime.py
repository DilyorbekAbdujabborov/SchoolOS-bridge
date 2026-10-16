import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from schoolos_bridge import runtime
from schoolos_bridge.runtime import Runner
from schoolos_bridge.setup import auth, store


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"
        self.runner = Runner(self.path, listen=False)

    def _write(self, devices=()):
        store.write_config(self.path, {
            "cloud_url": "https://c.example", "token": "tok-123456",
            "state_file": str(Path(self.tmp.name) / "state.json"), "devices": list(devices),
        })
        # A second write within the same mtime tick must still look changed.
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    def test_waits_without_a_config(self):
        self.runner.reload_if_changed()
        status = self.runner.status()
        self.assertFalse(status["running"])
        self.assertIn("Sozlama", status["error"])

    def test_starts_when_the_config_appears_and_reloads_on_change(self):
        self.runner.reload_if_changed()
        self._write()
        self.assertTrue(self.runner.reload_if_changed())
        self.assertTrue(self.runner.status()["running"])
        first = self.runner.bridge
        self.assertFalse(self.runner.reload_if_changed())
        self._write()
        self.assertTrue(self.runner.reload_if_changed())
        self.assertIsNot(self.runner.bridge, first)

    def test_a_clear_text_config_is_sealed_on_start(self):
        self.path.write_text('cloud_url = "https://c.example"\ntoken = "plain-token"\n')
        self.runner.reload_if_changed()
        self.assertTrue(self.runner.status()["running"])
        self.assertNotIn("plain-token", self.path.read_text())
        self.assertEqual(self.runner.bridge.config.token, "plain-token")

    def test_broken_config_is_reported(self):
        self.path.write_text("not = [toml")
        self.runner.reload_if_changed()
        self.assertFalse(self.runner.status()["running"])
        self.assertIn("Sozlama xatosi", self.runner.status()["error"])


class JwtTests(unittest.TestCase):
    KEY = b"k" * 32

    def test_round_trip(self):
        token = auth.encode({"typ": "session", "exp": 9_999_999_999}, self.KEY)
        self.assertEqual(auth.decode(token, self.KEY, "session")["typ"], "session")

    def test_tampered_wrong_key_wrong_type_expired(self):
        token = auth.encode({"typ": "session", "exp": 9_999_999_999}, self.KEY)
        head, body, sig = token.split(".")
        cases = [
            f"{head}.{body}x.{sig}",
            auth.encode({"typ": "session", "exp": 9_999_999_999}, b"other" * 8),
            auth.encode({"typ": "link", "exp": 9_999_999_999}, self.KEY),
            auth.encode({"typ": "session", "exp": 1}, self.KEY),
            "eyJhbGciOiJub25lIn0." + body + ".",  # alg=none
        ]
        for bad in cases:
            with self.assertRaises(auth.AuthError):
                auth.decode(bad, self.KEY, "session")

    def test_session_dies_with_the_process(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            token, _ = auth.Sessions(config).exchange(auth.issue_link(config))
            with self.assertRaises(auth.AuthError):
                auth.Sessions(config).check(token)  # a restarted bridge


class PushHostSyncTests(unittest.TestCase):
    def _runner_and_bridge(self, listen_host="0.0.0.0"):
        from types import SimpleNamespace

        driver = mock.Mock()
        driver.ensure_push_host.return_value = True
        device = SimpleNamespace(serial="S1", ip="10.0.0.5")
        config = SimpleNamespace(devices=[device], listen_host=listen_host, listen_port=8099)
        bridge = SimpleNamespace(config=config, drivers={"S1": driver})
        return runtime.Runner("/nonexistent.toml"), bridge, driver

    def test_points_terminal_at_lan_ip_and_throttles(self):
        runner, bridge, driver = self._runner_and_bridge()
        with mock.patch.object(runtime, "lan_ip_towards", return_value="10.0.0.9"):
            runner._sync_push_hosts(bridge)
            runner._sync_push_hosts(bridge)  # within PUSH_HOST_EVERY: skipped
        driver.ensure_push_host.assert_called_once_with("10.0.0.9", 8099)

    def test_explicit_listen_host_wins_and_unknown_ip_skipped(self):
        runner, bridge, driver = self._runner_and_bridge("192.168.1.7")
        runner._sync_push_hosts(bridge)
        driver.ensure_push_host.assert_called_once_with("192.168.1.7", 8099)
        runner, bridge, driver = self._runner_and_bridge()
        with mock.patch.object(runtime, "lan_ip_towards", return_value=None):
            runner._sync_push_hosts(bridge)
        driver.ensure_push_host.assert_not_called()
