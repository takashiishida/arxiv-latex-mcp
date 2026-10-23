"""Release metadata and desktop archives must match what users install."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

from scripts.build_bundle import build_bundle

if sys.version_info >= (3, 11):
    from scripts.check_release import check_release

ROOT = Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    def test_archive_is_reproducible_and_contains_only_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            first = build_bundle(Path(directory) / "first.mcpb")
            second = build_bundle(Path(directory) / "second.mcpb")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                expected = {
                    "manifest.json", "requirements.txt", "README.md", "LICENSE",
                    "LICENSE-APACHE", "THIRD_PARTY_NOTICES", "server/main.py",
                } | {p.relative_to(ROOT).as_posix() for p in (ROOT / "arxiv_latex_mcp").glob("*.py")}
                self.assertEqual(set(archive.namelist()), expected)
                self.assertEqual(archive.read("requirements.txt"), (ROOT / "requirements.txt").read_bytes())
                manifest = json.loads(archive.read("manifest.json"))
                self.assertIn(manifest["server"]["entry_point"], archive.namelist())
                self.assertNotIn("PYTHONPATH", manifest["server"]["mcp_config"].get("env", {}))


@unittest.skipIf(sys.version_info < (3, 11), "Release tooling uses Python 3.11+; runtime supports 3.10")
class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("pyproject.toml", "uv.lock", "manifest.json", "requirements.txt", "arxiv_latex_mcp/__init__.py"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        shutil.copytree(ROOT / "docs" / "releases", self.root / "docs" / "releases")

    def test_current_release_tag_matches(self):
        version = json.loads((self.root / "manifest.json").read_text())["version"]
        self.assertEqual(check_release(self.root, f"v{version}"), version)

    def test_wrong_tag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Release tag"):
            check_release(self.root, "v99.0.0")

    def test_manifest_version_drift_is_rejected(self):
        target = self.root / "manifest.json"
        manifest = json.loads(target.read_text())
        manifest["version"] = "99.0.0"
        target.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "versions disagree"):
            check_release(self.root)

    def test_dependency_drift_is_rejected(self):
        (self.root / "requirements.txt").write_text("mcp>=1.6.0\n")
        with self.assertRaisesRegex(ValueError, "dependencies disagree"):
            check_release(self.root)


if __name__ == "__main__":
    unittest.main()
