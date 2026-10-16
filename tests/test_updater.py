import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from schoolos_bridge import updater

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()


def release(version="0.2.0", platform="linux", sha="ab" * 32, url="https://h/b"):
    sig = base64.b64encode(KEY.sign(updater.signed_message(version, platform, sha))).decode()
    return {"version": version, "sha256": sha, "signature": sig, "url": url}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(updater, "PUBLIC_KEY", PUB)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("schoolos_bridge.updater.verify_release.__defaults__", (PUB,))
        patch.start()
        self.addCleanup(patch.stop)

    def test_valid_newer_release_accepted(self):
        self.assertEqual(updater.newer_release(release(), "linux", "0.1.0")["version"], "0.2.0")

    def test_same_or_older_ignored(self):
        self.assertIsNone(updater.newer_release(release("0.1.0"), "linux", "0.1.0"))
        self.assertIsNone(updater.newer_release(release("0.1.0"), "linux", "0.2.0"))

    def test_none_ignored(self):
        self.assertIsNone(updater.newer_release(None, "linux", "0.1.0"))

    def test_tampered_fields_rejected(self):
        for change in ({"sha256": "cd" * 32}, {"version": "9.9.9"}):
            with self.assertRaises(updater.UpdateError):
                updater.newer_release({**release(), **change}, "linux", "0.1.0")

    def test_other_platform_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.newer_release(release(platform="windows"), "linux", "0.1.0")

    def test_missing_signature_rejected(self):
        bad = release()
        del bad["signature"]
        with self.assertRaises(updater.UpdateError):
            updater.newer_release(bad, "linux", "0.1.0")

    def test_http_url_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.newer_release(release(url="http://h/b"), "linux", "0.1.0")

    def test_wrong_key_rejected(self):
        other = Ed25519PrivateKey.generate()
        forged = release()
        forged["signature"] = base64.b64encode(
            other.sign(updater.signed_message("0.2.0", "linux", forged["sha256"]))
        ).decode()
        with self.assertRaises(updater.UpdateError):
            updater.newer_release(forged, "linux", "0.1.0")


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.data


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get(self, url, **kw):
        return FakeResponse(self.data)


class DownloadInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_download_checks_sha256(self):
        dest = self.dir / "b.new"
        data = b"binary"
        updater.download("https://h/b", dest, hashlib.sha256(data).hexdigest(), FakeSession(data))
        self.assertEqual(dest.read_bytes(), data)
        with self.assertRaises(updater.UpdateError):
            updater.download("https://h/b", dest, "00" * 32, FakeSession(data))
        self.assertFalse(dest.exists())

    def _script(self, name, ok=True, version="0.2.0"):
        path = self.dir / name
        path.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "--version" ]; then echo "schoolos-bridge {version}"; exit 0; fi\n'
            f"exit {0 if ok else 1}\n"
        )
        return path

    def test_install_swaps_and_keeps_backup(self):
        current = self._script("bridge", version="0.1.0")
        new = self._script("bridge.new")
        updater.install(new, current, "0.2.0")
        self.assertIn("0.2.0", current.read_text())
        self.assertIn("0.1.0", (self.dir / "bridge.bak").read_text())
        self.assertFalse(new.exists())

    def test_failed_self_test_keeps_current(self):
        current = self._script("bridge", version="0.1.0")
        new = self._script("bridge.new", ok=False)
        with self.assertRaises(updater.UpdateError):
            updater.install(new, current, "0.2.0")
        self.assertIn("0.1.0", current.read_text())
        self.assertFalse(new.exists())

    def test_wrong_reported_version_rejected(self):
        current = self._script("bridge", version="0.1.0")
        new = self._script("bridge.new", version="0.1.5")
        with self.assertRaises(updater.UpdateError):
            updater.install(new, current, "0.2.0")
        self.assertIn("0.1.0", current.read_text())


class MaybeUpdateTests(unittest.TestCase):
    def test_not_frozen_never_updates(self):
        cloud = mock.Mock()
        self.assertFalse(updater.maybe_update(cloud))
        cloud.fetch_update.assert_not_called()

    def test_cloud_errors_are_swallowed(self):
        cloud = mock.Mock()
        cloud.fetch_update.side_effect = RuntimeError("down")
        with mock.patch.object(updater, "is_frozen", return_value=True):
            self.assertFalse(updater.maybe_update(cloud))


class ManifestTests(unittest.TestCase):
    def test_manifest_roundtrips_through_updater(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
        import release_manifest

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "schoolos-bridge-linux"
            binary.write_bytes(b"exe")
            manifest = release_manifest.build_manifest(KEY, "0.2.0", "https://h/dl/", {"linux": binary})
        entry = manifest["platforms"]["linux"]
        self.assertEqual(entry["url"], "https://h/dl/schoolos-bridge-linux")
        updater.verify_release({"version": "0.2.0", **entry}, "linux", PUB)
