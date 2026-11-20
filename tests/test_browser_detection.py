"""Platform browser selection; no live browser required for these tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_detection


class BrowserDetectionTests(unittest.TestCase):
    def test_non_windows_does_not_choose_windows_browser(self):
        with patch.object(browser_detection.sys, "platform", "linux"):
            self.assertIsNone(browser_detection.find_preferred_browser())

    def test_finds_installed_edge_without_playwright_download(self):
        with tempfile.TemporaryDirectory() as directory:
            edge = Path(directory) / "msedge.exe"
            edge.touch()
            with patch.object(browser_detection.sys, "platform", "win32"), \
                    patch.object(browser_detection, "_candidate_paths", return_value=[("msedge", edge)]):
                self.assertEqual(browser_detection.find_preferred_browser(), "msedge")

    def test_finds_installed_chrome_when_edge_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            edge = Path(directory) / "missing_edge.exe"
            chrome = Path(directory) / "chrome.exe"
            chrome.touch()
            with patch.object(browser_detection.sys, "platform", "win32"), \
                    patch.object(browser_detection, "_candidate_paths", return_value=[("msedge", edge), ("chrome", chrome)]):
                self.assertEqual(browser_detection.find_preferred_browser(), "chrome")


if __name__ == "__main__":
    unittest.main()
