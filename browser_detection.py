"""Locate an existing Microsoft Edge or Google Chrome on Windows.

Use official, normally installed browser binaries. No installation, elevation,
or browser download is required when one of these browsers is already present.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def _candidate_paths() -> list[tuple[str, Path]]:
    env = os.environ
    paths: list[tuple[str, Path]] = []

    def add(channel: str, root: str, relative: str) -> None:
        base = env.get(root, "")
        if base:
            paths.append((channel, Path(base) / relative))

    for key in ("PROGRAMFILES(X86)", "ProgramFiles", "LOCALAPPDATA"):
        add("msedge", key, r"Microsoft\Edge\Application\msedge.exe")
    for key in ("ProgramFiles", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        add("chrome", key, r"Google\Chrome\Application\chrome.exe")
    return paths


def find_preferred_browser() -> str | None:
    """Return Playwright channel for a locally installed browser, if found."""
    if sys.platform != "win32":
        return None
    for channel, path in _candidate_paths():
        if path.is_file():
            return channel

    # App Paths can locate nonstandard official installs.
    try:
        import winreg
        for channel, exe in (("msedge", "msedge.exe"), ("chrome", "chrome.exe")):
            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _kind = winreg.QueryValueEx(key, "")
                        if isinstance(value, str) and Path(value.strip('"')).is_file():
                            return channel
                except (OSError, ValueError):
                    continue
    except ImportError:
        pass

    for channel, exe in (("msedge", "msedge.exe"), ("chrome", "chrome.exe")):
        if shutil.which(exe):
            return channel
    return None


if __name__ == "__main__":
    browser = find_preferred_browser()
    if browser:
        print(f"Browser Windows ditemukan: {browser}. Tidak perlu mengunduh Chromium.")
        raise SystemExit(0)
    print("Google Chrome atau Microsoft Edge belum ditemukan di komputer ini.")
    raise SystemExit(1)
