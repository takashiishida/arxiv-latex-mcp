"""The desktop launcher must not reuse stale or partially installed packages."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "bundle_bootstrap", Path(__file__).resolve().parents[1] / "server" / "main.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("mcp>=1.30.0,<2.0\nPyJWT>=2.15.1\n")
        patcher = mock.patch.object(bootstrap.subprocess, "check_call", side_effect=self.install)
        self.installer = patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, command, **kwargs):
        target = Path(command[command.index("--target") + 1])
        (target / "installed.txt").write_text(self.requirements.read_text())

    def test_legacy_folder_and_global_mcp_do_not_skip_install(self):
        legacy = self.root / "server" / "lib"
        legacy.mkdir(parents=True)
        (legacy / "old.txt").write_text("vulnerable dependencies")
        with mock.patch.dict(sys.modules, {"mcp": mock.Mock()}):
            target = bootstrap.prepare_dependencies(self.root)
        self.installer.assert_called_once()
        self.assertNotEqual(target, legacy)
        self.assertTrue((target / "installed.txt").is_file())
        command = self.installer.call_args.args[0]
        self.assertIn("--only-binary=:all:", command)
        self.assertEqual(command[:4], [sys.executable, "-I", "-m", "pip"])
        self.assertIs(self.installer.call_args.kwargs["stdout"], sys.stderr)

    def test_complete_install_is_reused_without_network(self):
        first = bootstrap.prepare_dependencies(self.root)
        self.assertEqual(bootstrap.prepare_dependencies(self.root), first)
        self.installer.assert_called_once()

    def test_requirements_change_creates_new_install(self):
        old = bootstrap.prepare_dependencies(self.root)
        self.requirements.write_text("mcp>=1.30.0,<2.0\nPyJWT>=2.16.0\n")
        new = bootstrap.prepare_dependencies(self.root)
        self.assertNotEqual(old, new)
        self.assertEqual((new / "installed.txt").read_text(), self.requirements.read_text())

    def test_runtime_change_creates_new_install(self):
        old = bootstrap.prepare_dependencies(self.root)
        with mock.patch.object(bootstrap.platform, "machine", return_value="different-architecture"):
            self.assertNotEqual(bootstrap.prepare_dependencies(self.root), old)

    def test_failed_upgrade_does_not_fall_back_to_old_packages(self):
        old = bootstrap.prepare_dependencies(self.root)
        self.requirements.write_text("mcp>=1.31.0,<2.0\n")
        self.installer.side_effect = subprocess.CalledProcessError(1, "pip")
        with self.assertRaises(subprocess.CalledProcessError):
            bootstrap.prepare_dependencies(self.root)
        self.assertEqual(list(old.parent.iterdir()), [old])
        self.installer.side_effect = self.install
        self.assertNotEqual(bootstrap.prepare_dependencies(self.root), old)

    def test_concurrent_complete_install_is_accepted(self):
        def other_launch(command, **kwargs):
            self.installer.side_effect = self.install
            bootstrap.prepare_dependencies(self.root)
        self.installer.side_effect = other_launch
        target = bootstrap.prepare_dependencies(self.root)
        self.assertEqual(list(target.parent.iterdir()), [target])
        self.assertTrue((target / "installed.txt").exists())


if __name__ == "__main__":
    unittest.main()
