"""Gives one video file a temporary public web address, so Instagram can fetch it.

Instagram-only accounts (Instagram API with Instagram Login) can't receive an uploaded file: Instagram downloads
the video from a public address (video_url) instead. This shares just that one file, for just as long as needed:

1. Cloudflare's free quick tunnel (cloudflared, downloaded once from Cloudflare's GitHub releases): a random
   https://<words>.trycloudflare.com address that leads to a tiny file server on this laptop, which serves
   only that file under a random path. Nothing is stored anywhere else, and the address dies when we close it.
2. If the tunnel can't start: Litterbox (catbox.moe), a free temporary file host whose links expire after 1 hour.
"""
from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline as P

TOOLS = P.DATA / "tools"
CF_EXE = TOOLS / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
CF_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
          + ("cloudflared-windows-amd64.exe" if os.name == "nt" else "cloudflared-linux-amd64"))
LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"
_NO_WINDOW = getattr(P, "_NO_WINDOW", 0)


class ShareError(Exception):
    pass


# ------------------------------------------------------------------ the one-file server
def _file_server(path: Path, token: str):
    size = path.stat().st_size
    want = f"/{token}/video.mp4"  # fixed name: real file names can have spaces and odd characters

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _head(self, code, start=0, end=size - 1):
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()

        def _range(self):
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if not m:
                return None
            a, b = m.groups()
            if a == "":
                start, end = max(0, size - int(b or 0)), size - 1
            else:
                start, end = int(a), min(size - 1, int(b) if b else size - 1)
            return (start, end) if start <= end else None

        def do_HEAD(self):
            if self.path.split("?")[0] != want:
                self.send_error(404); return
            r = self._range()
            self._head(206, *r) if r else self._head(200)

        def do_GET(self):
            if self.path.split("?")[0] != want:
                self.send_error(404); return
            r = self._range()
            start, end = r if r else (0, size - 1)
            self._head(206 if r else 200, start, end)
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, want


# ------------------------------------------------------------------ Cloudflare quick tunnel
def _ensure_cloudflared() -> Path:
    if CF_EXE.exists() and CF_EXE.stat().st_size > 1_000_000:
        return CF_EXE
    import requests
    TOOLS.mkdir(parents=True, exist_ok=True)
    tmp = CF_EXE.with_suffix(".part")
    with requests.get(CF_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise ShareError("Couldn't download Cloudflare's tunnel tool.")
    tmp.replace(CF_EXE)
    if os.name != "nt":
        CF_EXE.chmod(0o755)
    return CF_EXE


def _start_tunnel(port: int):
    exe = _ensure_cloudflared()
    proc = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            creationflags=_NO_WINDOW if os.name == "nt" else 0)
    found: list[str] = []

    def read():
        for line in proc.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found:
                found.append(m.group(0))
    threading.Thread(target=read, daemon=True).start()
    for _ in range(60):
        if found or proc.poll() is not None:
            break
        time.sleep(0.5)
    if not found:
        proc.kill()
        raise ShareError("Cloudflare's tunnel didn't start.")
    return proc, found[0]


def _wait_reachable(url: str, timeout: int = 60):
    """A new trycloudflare address takes a few seconds before it answers everywhere."""
    import requests
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            if r.status_code in (200, 206):
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise ShareError("The temporary address didn't come online.")


# ------------------------------------------------------------------ fallback: Litterbox (expires in 1 hour)
def _litterbox(path: Path) -> str:
    import requests
    with open(path, "rb") as f:
        r = requests.post(LITTERBOX, data={"reqtype": "fileupload", "time": "1h"},
                          files={"fileToUpload": ("video.mp4", f, "video/mp4")}, timeout=600)
    url = (r.text or "").strip()
    if r.status_code != 200 or not url.startswith("https://"):
        raise ShareError(f"The temporary file host said: {url[:120] or r.status_code}")
    return url


# ------------------------------------------------------------------ public entry point
@contextmanager
def share(path: Path, log=print):
    """with share(path) as url: ... (the address works until the with-block ends)"""
    path = Path(path)
    override = os.environ.get("CLIP_ENGINE_PUBLIC_BASE")  # tests: skip the tunnel, use this base address
    token = secrets.token_urlsafe(18)
    srv, rel = _file_server(path, token)
    proc = None
    try:
        try:
            if override:
                url = override.rstrip("/").replace("{port}", str(srv.server_address[1])) + rel
            else:
                proc, base = _start_tunnel(srv.server_address[1])
                url = base + rel
                _wait_reachable(url)
        except Exception as e:  # noqa: BLE001
            log(f"instagram: tunnel failed ({e}); using a temporary file host instead")
            if proc:
                proc.kill(); proc = None
            try:
                url = _litterbox(path)
            except ShareError:
                raise
            except Exception as e2:  # noqa: BLE001
                raise ShareError(f"Couldn't reach the temporary file host either ({type(e2).__name__}).") from None
        yield url
    finally:
        if proc:
            proc.kill()
        srv.shutdown()
        srv.server_close()
