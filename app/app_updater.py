"""Safe GitHub release detection and one-click installer updates for ClipEngine.

Installed builds can update themselves without asking users to download an EXE.
Portable builds remain portable and are never silently replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request

RELEASE_API = "https://api.github.com/repos/rickbesada-netizen/ClipEngine-App2/releases/latest"
VERSION_PATTERN = re.compile(r"^v1\.1\.(\d+)$")
USER_AGENT = "ClipEngine-Windows-Updater"
CACHE_SECONDS = 1800


def installed_executable(executable=None, localappdata=None):
    exe = Path(executable or sys.executable).resolve()
    location = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA")
    if not location or exe.name.lower() != "clipengine.exe":
        return False
    return exe.parent == (Path(location).resolve() / "Programs" / "ClipEngine")


def parse_release(release, installed_build):
    """Only accept the repository's installer, with a higher build number."""
    m = VERSION_PATTERN.fullmatch(str(release.get("tag_name", "")))
    if not m:
        return None
    newer = int(m.group(1))
    if newer <= int(installed_build):
        return None
    for asset in release.get("assets", []):
        url = str(asset.get("browser_download_url", ""))
        required = ("/rickbesada-netizen/ClipEngine-App2/releases/download/"
                    + release["tag_name"] + "/ClipEngineSetup.exe")
        if (asset.get("name") == "ClipEngineSetup.exe"
                and url.startswith("https://github.com")
                and url.endswith(required)
                and asset.get("state") == "uploaded"
                and int(asset.get("size", 0)) > 1_000_000):
            digest = str(asset.get("digest", ""))
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                return None
            return {"build": newer, "url": url, "sha256": digest[7:].lower(),
                    "size": int(asset["size"]), "version": release["tag_name"]}
    return None


def latest_release(build):
    request = urllib.request.Request(RELEASE_API, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.load(response)
    return parse_release(data, build)


class UpdateService:
    def __init__(self, build, executable=None, localappdata=None):
        self.build = int(build) if str(build).isdigit() else -1
        self.executable = executable or sys.executable
        self.localappdata = localappdata
        self._lock = threading.RLock()
        self._release = None
        self._status = {"phase": "idle", "progress": 0, "error": ""}
        self._last_checked_at = 0
        self._worker = None

    def is_installed(self):
        return installed_executable(self.executable, self.localappdata)

    def check(self, force=False):
        import time
        if not self.is_installed() or self.build < 0:
            return {"installed": False, "available": False, "build": self.build}
        with self._lock:
            if not force and time.monotonic() - self._last_checked_at < CACHE_SECONDS:
                candidate = self._release
                return {"installed": True, "available": candidate is not None,
                        "build": self.build, "latest": candidate["build"] if candidate else self.build}
        candidate = latest_release(self.build)
        with self._lock:
            self._release = candidate
            self._last_checked_at = time.monotonic()
        return {"installed": True, "available": candidate is not None,
                "build": self.build, "latest": candidate["build"] if candidate else self.build}

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self):
        if not self.is_installed():
            return {"ok": False, "error": "Automatic installation requires ClipEngineSetup.exe"}
        with self._lock:
            if self._worker and self._worker.is_alive():
                return {"ok": False, "error": "Update already running"}
            candidate = self._release
            if not candidate:
                return {"ok": False, "error": "No newer release has been verified"}
            self._status = {"phase": "downloading", "progress": 0, "error": ""}
            self._worker = threading.Thread(target=self._download_install,
                                             args=(dict(candidate),),
                                             name="clipengine-updater", daemon=True)
            self._worker.start()
        return {"ok": True, "message": "Downloading verified update"}

    def _set_status(self, **updates):
        with self._lock:
            self._status.update(updates)

    def _download_install(self, candidate):
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="ClipEngineSetup_", suffix=".exe")
            os.close(fd)
            hasher = hashlib.sha256()
            total = 0
            req = urllib.request.Request(candidate["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as response, open(path, "wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > candidate["size"]:
                        raise RuntimeError("Update exceeds published file size")
                    hasher.update(block)
                    output.write(block)
                    self._set_status(progress=min(100, int(total * 100 / candidate["size"])))
            if total != candidate["size"] or hasher.hexdigest() != candidate["sha256"]:
                raise RuntimeError("Downloaded setup file failed integrity verification")
            with open(path, "rb") as f:
                if f.read(2) != b"MZ":
                    raise RuntimeError("Downloaded installer is not a Windows executable")
            self._set_status(phase="installing", progress=100)
            subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES",
                              "/CLOSEAPPLICATIONS", "/NORESTART"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            # Inno Setup replaces the app only after ClipEngine exits.
            threading.Timer(2.0, lambda: os._exit(0)).start()
        except Exception as error:
            self._set_status(phase="error", error=f"{type(error).__name__}: {error}")
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
