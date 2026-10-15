"""The PyInstaller packaging must stay in step with the source it bundles.

If an entry point, the wizard's web assets, or a build requirement is renamed,
the build silently ships a broken binary — these tests fail first.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import schoolos_bridge
from schoolos_bridge import config

BRIDGE = Path(__file__).resolve().parent.parent
INSTALLER = BRIDGE / "installer"
STATIC = BRIDGE / "schoolos_bridge" / "setup" / "static"


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=BRIDGE,
        capture_output=True,
        text=True,
        check=False,
    )


class PackagingTests(unittest.TestCase):
    def test_spec_references_both_entry_points(self):
        spec = (INSTALLER / "schoolos-bridge.spec").read_text()
        self.assertIn("installer/entry_service.py", spec)
        self.assertIn("installer/entry_setup.py", spec)

    def test_spec_bundles_the_wizard_web_assets(self):
        spec = (INSTALLER / "schoolos-bridge.spec").read_text()
        self.assertIn("setup/static", spec)
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((STATIC / name).is_file(), name)

    def test_build_requirements_pull_in_pyinstaller(self):
        text = (BRIDGE / "requirements-build.txt").read_text().lower()
        self.assertIn("pyinstaller", text)
        self.assertIn("-r requirements.txt", text)

    def test_service_file_per_platform_exists(self):
        deploy = BRIDGE / "deploy"
        for name in (
            "schoolos-bridge.service",  # Linux (systemd)
            "uz.schoolos.bridge.plist",  # macOS (launchd)
            "install-macos.sh",
            "install-windows.ps1",  # Windows (scheduled task)
        ):
            self.assertTrue((deploy / name).is_file(), name)


class CommandLineTests(unittest.TestCase):
    def test_service_reports_its_version(self):
        done = _run("schoolos_bridge", "--version")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(schoolos_bridge.__version__, done.stdout)

    def test_service_help_lists_config_and_log_file(self):
        done = _run("schoolos_bridge", "--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--config", done.stdout)
        self.assertIn("--log-file", done.stdout)

    def test_setup_self_test_finds_the_web_assets(self):
        done = _run("schoolos_bridge.setup", "--self-test")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("self-test OK", done.stdout)


class DefaultPathTests(unittest.TestCase):
    """Without a config override the path must be writable per platform:
    /etc on Linux, ~/Library on macOS, ProgramData on Windows."""

    def _path(self, platform, legacy=None):
        patches = [mock.patch.dict(os.environ, {config.ENV_CONFIG: ""})]
        patches.append(mock.patch.object(config.sys, "platform", platform))
        if legacy is not None:
            patches.append(mock.patch.object(config, "LEGACY_POSIX_CONFIG", legacy))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return config.default_config_path()

    def test_linux_uses_etc(self):
        path = self._path("linux", legacy=Path("/nonexistent/schoolos-bridge.toml"))
        self.assertEqual(str(path), "/etc/schoolos-bridge/config.toml")

    def test_macos_uses_application_support(self):
        path = self._path("darwin")
        self.assertTrue(str(path).replace("\\", "/").endswith("SchoolOS Bridge/config.toml"), path)

    def test_windows_uses_programdata(self):
        with mock.patch.dict(os.environ, {"ProgramData": r"C:\ProgramData"}):
            path = self._path("win32")
        self.assertTrue(str(path).replace("\\", "/").endswith("SchoolOS Bridge/config.toml"), path)


if __name__ == "__main__":
    unittest.main()
