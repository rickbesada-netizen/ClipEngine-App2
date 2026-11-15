"""Offline release-check tests. These do not download or install anything."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app_updater


def release(build, *, digest="sha256:" + "a" * 64):
    tag = f"v1.1.{build}"
    return {
        "tag_name": tag,
        "assets": [{
            "name": "ClipEngineSetup.exe",
            "browser_download_url": (
                "https://github.com/rickbesada-netizen/ClipEngine-App2/releases/download/"
                + tag + "/ClipEngineSetup.exe"),
            "size": 150_000_000, "digest": digest, "state": "uploaded",
        }]
    }


class UpdateTests(unittest.TestCase):
    def test_newer_release(self):
        self.assertEqual(app_updater.parse_release(release(51), 50)["build"], 51)

    def test_current_and_old_releases_are_not_updates(self):
        self.assertIsNone(app_updater.parse_release(release(50), 50))
        self.assertIsNone(app_updater.parse_release(release(49), 50))

    def test_rejects_bad_digest_and_url(self):
        self.assertIsNone(app_updater.parse_release(release(51, digest=""), 50))
        bad = release(51)
        bad["assets"][0]["browser_download_url"] = "https://example.org/ClipEngineSetup.exe"
        self.assertIsNone(app_updater.parse_release(bad, 50))

    def test_rejects_unrelated_software_release(self):
        bad = release(51)
        bad["tag_name"] = "v9.9.999"
        self.assertIsNone(app_updater.parse_release(bad, 50))

    def test_installed_mode_distinguishes_portable(self):
        root = Path("C:/Users/Example/AppData/Local")
        self.assertTrue(app_updater.installed_executable(
            root / "Programs/ClipEngine/ClipEngine.exe", root))
        self.assertFalse(app_updater.installed_executable(
            Path("C:/Users/Example/Downloads/ClipEngine.exe"), root))


if __name__ == "__main__":
    unittest.main()
