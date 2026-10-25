"""Clip Engine pipeline: download -> transcribe -> find moments -> render vertical clips."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

if getattr(sys, "frozen", False):
    # Packaged ClipEngine.exe: bundled files live in the unpack folder, user data sits next to the exe.
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    ROOT = Path(sys.executable).resolve().parent
    os.environ["PATH"] = str(APP_DIR) + os.pathsep + os.environ.get("PATH", "")  # bundled deno for yt-dlp
else:
    APP_DIR = Path(__file__).resolve().parent
    ROOT = APP_DIR.parent
DATA = ROOT / "ClipEngine data" if getattr(sys, "frozen", False) else ROOT / "data"
DL_DIR = DATA / "downloads"
CLIP_DIR = DATA / "clips"
THUMB_DIR = DATA / "thumbs"
WORK_DIR = DATA / "work"
FONT_DIR = APP_DIR / "fonts"
for d in (DL_DIR, CLIP_DIR, WORK_DIR, THUMB_DIR):
    d.mkdir(parents=True, exist_ok=True)

OUT_W, OUT_H = 1080, 1920


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- ffmpeg
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = ffmpeg_exe()
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run(cmd: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed: {tail}")
    return p


def has_filter(name: str) -> bool:
    p = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, creationflags=_NO_WINDOW)
    return re.search(rf"\s{name}\s", p.stdout) is not None


CAN_CAPTION = has_filter("ass")


def probe(path: Path) -> dict:
    """Width, height, duration via ffmpeg's banner (no ffprobe needed)."""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    txt = p.stderr
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotation of (-?\d+)", txt) or re.search(r"rotate\s*:\s*(-?\d+)", txt)
    if rot and abs(int(rot.group(1))) in (90, 270):
        w, h = h, w
    return {"w": w, "h": h, "duration": dur}


# ---------------------------------------------------------------- sources
def ydl_opts(settings: dict, extra: dict | None = None) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "ffmpeg_location": FFMPEG}
    cb = (settings.get("cookies_browser") or "").strip().lower()
    if cb and cb != "none":
        o["cookiesfrombrowser"] = (cb,)
    cf = (settings.get("cookies_file") or "").strip().strip('"')
    if cf and Path(cf).exists():
        o["cookiefile"] = cf
    if extra:
        o.update(extra)
    return o


def list_source(url: str, limit: int, settings: dict) -> list[dict]:
    """Recent videos from a channel / playlist URL (or the single video)."""
    from yt_dlp import YoutubeDL

    u = url.strip()
    if re.search(r"youtube\.com/@[^/]+/?$", u):
        u = u.rstrip("/") + "/videos"
    with YoutubeDL(ydl_opts(settings, {"extract_flat": "in_playlist", "playlistend": limit})) as y:
        info = y.extract_info(u, download=False)
    entries = info.get("entries") if info else None
    if not entries:
        return [{"url": info.get("webpage_url") or url, "title": info.get("title"), "duration": info.get("duration"), "id": info.get("id")}]
    out = []
    for e in list(entries)[:limit]:
        if not e:
            continue
        link = e.get("url") or e.get("webpage_url")
        if link and not link.startswith("http") and e.get("id"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        out.append({"url": link, "title": e.get("title"), "duration": e.get("duration"), "id": e.get("id")})
    return out


AUTO_BROWSERS = ["firefox", "brave", "edge", "chrome", "opera", "vivaldi"]
WORKING_BROWSER: dict = {"name": None}


def download(url: str, settings: dict, progress) -> tuple[Path, dict]:
    from yt_dlp import YoutubeDL

    height = int(settings.get("max_height") or 1080)

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if tot:
                progress(f"Downloading {got / tot * 100:.0f}%", got / tot * 0.3)

    extra = {
        "format": f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(DL_DIR / "%(extractor_key)s-%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "overwrites": False,
    }

    def attempt(st):
        with YoutubeDL(ydl_opts(st, extra)) as y:
            inf = y.extract_info(url, download=True)
            if inf.get("entries"):
                inf = inf["entries"][0]
            rd = inf.get("requested_downloads") or []
            pth = Path(rd[0]["filepath"]) if rd and rd[0].get("filepath") else Path(y.prepare_filename(inf)).with_suffix(".mp4")
            return inf, pth

    try:
        info, path = attempt(settings)
    except Exception as first:  # noqa: BLE001
        msg = str(first).lower()
        if not ("sign in" in msg or "not a bot" in msg or "login" in msg or "cookies" in msg):
            raise
        # YouTube asked for a signed-in browser: try each installed browser's sign-in automatically
        tried = {(settings.get("cookies_browser") or "none").lower()}
        last = first
        for b in AUTO_BROWSERS:
            if b in tried:
                continue
            progress(f"YouTube wants a sign-in, trying your {b.title()} sign-in…")
            try:
                info, path = attempt({**settings, "cookies_browser": b, "cookies_file": ""})
                WORKING_BROWSER["name"] = b
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise last
    meta = {
        "id": info.get("id"), "title": info.get("title") or path.stem, "url": info.get("webpage_url") or url,
        "channel": info.get("uploader") or info.get("channel"), "duration": info.get("duration"),
        "heatmap": info.get("heatmap") or [], "source": info.get("extractor_key"),
    }
    return path, meta


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
DRIVE_RX = re.compile(r"(drive|docs)\.google\.com/", re.I)


def drive_import(url: str, dest: Path, progress, cancel) -> list[Path]:
    """Downloads the videos from a shared Google Drive folder or file link into dest."""
    import importlib.metadata as _md
    _real_version = _md.version

    def _version(name):  # gdown asks for its own version number when it loads; never let that stop a download
        try:
            return _real_version(name)
        except _md.PackageNotFoundError:
            return "0"
    _md.version = _version
    try:
        import gdown
    finally:
        _md.version = _real_version

    dest.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []

    def cb_for(name, i, n):
        def cb(done, total):
            if cancel():
                raise Cancelled()
            if total:
                progress(f"Downloading from Google Drive: {name} ({i}/{n}) {done / total * 100:.0f}%", 0.9 * ((i - 1) + done / total) / n)
        return cb

    try:
        if "/folders/" in url:
            files = gdown.download_folder(url=url, output=str(dest), skip_download=True, quiet=True, use_cookies=False)
            vids = [f for f in (files or []) if str(f.path).lower().endswith(VIDEO_EXT)]
            if not vids:
                raise RuntimeError("That Google Drive folder has no video files in it (or they're inside sub-folders it couldn't open).")
            for i, f in enumerate(vids, 1):
                if cancel():
                    raise Cancelled()
                lp = Path(f.local_path)
                lp.parent.mkdir(parents=True, exist_ok=True)
                if not (lp.exists() and lp.stat().st_size > 0):
                    gdown.download(id=f.id, output=str(lp), quiet=True, use_cookies=False, resume=True,
                                   progress=cb_for(lp.name, i, len(vids)))
                got.append(lp)
        else:
            out = gdown.download(url=url, output=str(dest) + os.sep, quiet=True, use_cookies=False, fuzzy=True,
                                 progress=cb_for("file", 1, 1))
            if out:
                got.append(Path(out))
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "50 files" in msg or "MaximumLimit" in type(e).__name__:
            raise RuntimeError("That Drive folder has more than 50 files. Open it in Drive, pick the videos, press Download, then drag the zip into Clip Engine.") from e
        raise RuntimeError("Couldn't download from that Google Drive link. It's probably not shared publicly. Fix: open it in Google Drive, "
                           "select the videos, press Download, then drag the downloaded zip or videos into Clip Engine. (" + msg[:120] + ")") from e
    return [g for g in got if g.exists() and g.stat().st_size > 0]


def extract_videos_from_zip(zpath: Path, dest: Path) -> list[Path]:
    import zipfile

    out: list[Path] = []
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(VIDEO_EXT) or name.startswith("._"):
                continue
            target = dest / name
            k = 2
            while target.exists():
                target = dest / f"{Path(name).stem} ({k}){Path(name).suffix}"; k += 1
            with z.open(info) as src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh, 4 * 1024 * 1024)
            out.append(target)
    return out


def local_source(path: str) -> tuple[Path, dict]:
    p = Path(path.strip().strip('"'))
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    return p, {"id": p.stem, "title": p.stem, "url": str(p), "channel": None, "duration": None, "heatmap": [], "source": "file"}


# ---------------------------------------------------------------- audio
def extract_audio(src: Path, dst: Path) -> Path:
    if not dst.exists():
        run([FFMPEG, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])
    return dst


def loudness(wav_path: Path) -> np.ndarray:
    """RMS per second, as z-scores."""
    with wave.open(str(wav_path)) as w:
        sr = w.getframerate()
        n = w.getnframes()
        rms = []
        while True:
            buf = w.readframes(sr)
            if not buf:
                break
            a = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            rms.append(float(np.sqrt(np.mean(a * a)) + 1e-9))
    r = 20 * np.log10(np.array(rms) + 1e-9)
    sd = r.std() or 1.0
    return (r - r.mean()) / sd


def heat_curve(heatmap: list, duration: float) -> np.ndarray | None:
    if not heatmap or not duration:
        return None
    n = int(math.ceil(duration))
    h = np.zeros(n)
    for b in heatmap:
        s, e, v = int(b.get("start_time", 0)), int(math.ceil(b.get("end_time", 0))), float(b.get("value", 0))
        h[max(0, s):min(n, e)] = v
    if h.max() > 0:
        h = h / h.max()
    return h


# ---------------------------------------------------------------- transcription
_MODELS: dict = {}


def whisper(model_name: str):
    if model_name not in _MODELS:
        from faster_whisper import WhisperModel
        try:
            _MODELS[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Couldn't load the '{model_name}' transcription model. The first run downloads it (75 MB to 1.5 GB), so check your internet connection and retry. ({str(e)[:120]})") from e
    return _MODELS[model_name]


def transcribe(wav: Path, model_name: str, windows: list[tuple[float, float]] | None, progress, cancel) -> list[dict]:
    """Returns segments: {start,end,text,words:[{s,e,w}]} in source time."""
    model = whisper(model_name)
    spans = windows or [(0.0, None)]
    segs: list[dict] = []
    total = sum(((b or 0) - a) for a, b in spans) if windows else None
    done = 0.0
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        nframes = w.getnframes()
        for a, b in spans:
            if cancel():
                raise Cancelled()
            start_f = int(a * sr)
            end_f = nframes if b is None else min(nframes, int(b * sr))
            w.setpos(start_f)
            audio = np.frombuffer(w.readframes(end_f - start_f), dtype=np.int16).astype(np.float32) / 32768.0
            length = len(audio) / sr
            it, _ = model.transcribe(audio, language="en", word_timestamps=True, vad_filter=True, beam_size=1)
            for s in it:
                if cancel():
                    raise Cancelled()
                words = [{"s": round(a + x.start, 2), "e": round(a + x.end, 2), "w": x.word.strip()} for x in (s.words or []) if x.word.strip()]
                segs.append({"start": round(a + s.start, 2), "end": round(a + s.end, 2), "text": s.text.strip(), "words": words})
                frac = ((done + s.end) / total) if total else (s.end / max(1.0, length))
                progress(f"Transcribing {min(100, frac * 100):.0f}%", 0.3 + 0.4 * min(1.0, frac))
            done += length
    segs.sort(key=lambda x: x["start"])
    return segs


# ---------------------------------------------------------------- moment finding
HOOK_RX = re.compile(r"^(listen|look|here'?s|this is (why|how|what)|the (reason|truth|secret|problem)|nobody|everyone|if you|stop|never|why|what|how|you need|i (lost|made|blew|turned)|let me tell you|the biggest|most people)", re.I)
LAUGH_RX = re.compile(r"\b(ha(ha)+|lol|lmao|\[laugh\w*\]|\(laugh\w*\))", re.I)
STRONG_RX = re.compile(r"(\$\s?\d|\d+\s?(k|grand|million|percent|%)|!|never|always|nobody|everyone|crazy|insane|biggest|worst|best|truth|secret|mistake|lost|broke|rich|million)", re.I)


def find_windows(dur: float, loud: np.ndarray, heat: np.ndarray | None, k: int, length: float) -> list[tuple[float, float]]:
    """Pick the k most promising windows in a long video to transcribe."""
    n = len(loud)
    score = np.clip(loud, -2, 3)
    if heat is not None and len(heat):
        hh = np.interp(np.arange(n), np.arange(len(heat)), heat)
        score = 0.4 * score + 2.5 * hh
    ker = max(1, int(length))
    sm = np.convolve(score, np.ones(ker) / ker, mode="same")
    chosen: list[tuple[float, float]] = []
    order = np.argsort(-sm)
    for i in order:
        a, b = max(0.0, i - length), min(dur, i + length)
        if all(b <= x or a >= y for x, y in chosen):
            chosen.append((float(a), float(b)))
        if len(chosen) >= k:
            break
    return sorted(chosen)


def action_windows(dur: float, loud: np.ndarray, heat: np.ndarray | None, n: int, length: float) -> list[dict]:
    """For footage with little talking (gameplay, trailers): pick the loudest / most rewatched stretches."""
    nsec = len(loud)
    sc = np.clip(loud, -2, 3)
    if heat is not None and len(heat):
        sc = 0.5 * sc + 2.5 * np.interp(np.arange(nsec), np.arange(len(heat)), heat)
    ker = max(1, int(length))
    sm = np.convolve(sc, np.ones(ker) / ker, mode="same")
    out: list[dict] = []
    for i in np.argsort(-sm):
        a = max(0.0, min(dur - length, i - length * 0.55))
        b = min(dur, a + length)
        if all(b + 2 <= x["start"] or a >= x["end"] + 2 for x in out):
            out.append({"start": float(a), "end": float(b), "text": "", "score": round(float(sm[i]), 2),
                        "reasons": ["loudest / most action"]})
        if len(out) >= n:
            break
    return sorted(out, key=lambda c: c["start"])


def title_hook(title: str) -> str:
    t = re.sub(r"\.(mp4|mov|mkv|webm|avi)$", "", title or "", flags=re.I)
    t = re.sub(r"[-_]+\d{2,4}$", "", t)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:60]


def score_candidates(segs: list[dict], loud: np.ndarray, heat: np.ndarray | None, min_len: float, max_len: float, keywords: list[str]) -> list[dict]:
    kw = [k.strip().lower() for k in keywords if k.strip()]
    cands = []
    for i, s0 in enumerate(segs):
        text_parts, j = [], i
        while j < len(segs) and segs[j]["end"] - s0["start"] <= max_len:
            if j > i and segs[j]["start"] - segs[j - 1]["end"] > 2.5:
                break  # long silence breaks a clip
            text_parts.append(segs[j]["text"])
            dur = segs[j]["end"] - s0["start"]
            if dur >= min_len:
                cands.append((i, j, " ".join(text_parts)))
            j += 1
    out = []
    for i, j, text in cands:
        a, b = segs[i]["start"], segs[j]["end"]
        dur = b - a
        lo, hi = int(a), max(int(a) + 1, int(b))
        loud_sc = float(np.mean(loud[lo:hi])) if hi <= len(loud) else 0.0
        peak = float(np.max(loud[lo:hi])) if hi <= len(loud) and hi > lo else 0.0
        heat_sc = float(np.mean(heat[lo:hi])) if heat is not None and hi <= len(heat) else 0.0
        first = segs[i]["text"]
        words = sum(len(segs[x]["words"]) or len(segs[x]["text"].split()) for x in range(i, j + 1))
        wps = words / max(dur, 1)
        reasons = []
        sc = 0.0
        if heat is not None:
            sc += 4.0 * heat_sc
            if heat_sc > 0.6:
                reasons.append("viewers rewatch this part")
        sc += 0.6 * max(-1, min(2, loud_sc)) + 0.3 * max(0, peak - 1.5)
        if peak > 2:
            reasons.append("energy spike")
        if HOOK_RX.search(first):
            sc += 1.2
            reasons.append("strong opening line")
        if "?" in first:
            sc += 0.5
        laughs = len(LAUGH_RX.findall(text))
        if laughs:
            sc += 0.8 * min(laughs, 3)
            reasons.append("laughter")
        strong = len(STRONG_RX.findall(text))
        sc += 0.25 * min(strong, 6)
        low = text.lower()
        hits = [k for k in kw if re.search(r"\b" + re.escape(k) + r"\b", low)]
        if hits:
            sc += 0.35 * min(len(hits), 5)
            reasons.append("on-topic: " + ", ".join(sorted(set(hits))[:3]))
        if re.search(r"[.!?]$", segs[j]["text"]):
            sc += 0.6
        else:
            sc -= 0.4
        if wps < 1.4:
            sc -= 1.0
        # mild preference for the middle of the allowed length
        mid = (min_len + max_len) / 2
        sc -= 0.02 * abs(dur - mid)
        out.append({"start": a, "end": b, "text": text, "score": round(sc, 2), "reasons": reasons or ["clear complete thought"], "seg_i": i, "seg_j": j})
    out.sort(key=lambda c: -c["score"])
    return out


def pick(cands: list[dict], n: int, gap: float = 4.0) -> list[dict]:
    chosen: list[dict] = []
    for c in cands:
        if all(c["end"] + gap <= x["start"] or c["start"] >= x["end"] + gap for x in chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda c: c["start"])


def hook_line(text: str, avoid: set | None = None) -> str:
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if avoid:
        sents = [s for s in sents if s.rstrip(".") not in avoid] or sents
    good = [s for s in sents if 3 <= len(s.split()) <= 11]
    pool = good or sents or [text]
    best = max(pool, key=lambda s: (bool(STRONG_RX.search(s)) + bool(HOOK_RX.search(s)), -abs(len(s.split()) - 7)))
    best = best.rstrip(".")
    words = best.split()
    if len(words) > 11:
        best = " ".join(words[:11]) + "…"
    return best


def ollama_rank(cands: list[dict], model: str, look_for: str) -> list[dict]:
    """Optional: ask a free local model (Ollama) to rate and title each moment."""
    import urllib.request

    for c in cands:
        prompt = (
            "You pick moments for short-form clips. Rate this transcript excerpt 1-10 for how "
            f"{look_for or 'funny, memorable or quotable'} it is as a standalone 30 second clip, and write a short hook title (max 9 words, no hashtags, no emojis).\n"
            'Reply only with JSON like {"rating": 7, "title": "..."}.\n\nExcerpt:\n' + c["text"][:1500]
        )
        body = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode()
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(json.loads(r.read())["response"])
            rating = float(res.get("rating", 0))
            c["score"] = round(c["score"] + (rating - 5) * 0.6, 2)
            if res.get("title"):
                c["ai_title"] = str(res["title"]).strip().strip('"')[:80]
            c["reasons"] = c["reasons"] + [f"local AI rating {rating:g}/10"]
        except Exception:
            pass
    return sorted(cands, key=lambda c: -c["score"])


def ollama_available() -> list[str]:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


# ---------------------------------------------------------------- framing
def analyse_faces(src: Path, a: float, b: float, w: int, h: int) -> list[tuple[float, float, float, float, float]]:
    """(t, cx, cy, fw, fh) of the largest face, sampled ~2x per second (normalised 0..1)."""
    import cv2

    if not hasattr(cv2, "CascadeClassifier"):
        return []  # face detection unavailable in this OpenCV build; falls back to full-frame layout
    cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    cap = cv2.VideoCapture(str(src))
    out = []
    t = a
    step = 0.5
    while t < b:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            t += step
            continue
        fh, fw = frame.shape[:2]
        sc = 640 / fw
        small = cv2.resize(frame, (640, int(fh * sc)))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(28, 28))
        if len(faces):
            x, y, ww, hh = max(faces, key=lambda f: f[2] * f[3])
            W, H = small.shape[1], small.shape[0]
            out.append((t, (x + ww / 2) / W, (y + hh / 2) / H, ww / W, hh / H))
        t += step
    cap.release()
    return out


def choose_layout(faces: list, samples: int, w: int, h: int, wanted: str) -> str:
    if h > w * 1.2:
        return "native"
    if wanted in ("face", "split", "fit"):
        if wanted != "fit" and not faces:
            return "fit"
        return wanted
    if samples == 0 or len(faces) < 0.4 * samples:
        return "fit"
    med_h = float(np.median([f[4] for f in faces]))
    return "face" if med_h >= 0.16 else "split"


def smooth_track(faces: list, a: float, b: float) -> list[tuple[float, float]]:
    """Keyframes (t_rel, cx) with dead-zone smoothing so the crop doesn't jitter."""
    if not faces:
        return [(0.0, 0.5)]
    pts = []
    cur = faces[0][1]
    for t, cx, *_ in faces:
        if abs(cx - cur) > 0.06:
            cur = cur + (cx - cur) * 0.7
        pts.append((t - a, cur))
    # merge points with near-identical x
    keep = [pts[0]]
    for p in pts[1:]:
        if abs(p[1] - keep[-1][1]) > 0.01:
            keep.append(p)
    return keep


def crop_x_expr(track: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    def px(cx):
        return int(min(max(cx * src_w - crop_w / 2, 0), src_w - crop_w))

    if len(track) == 1:
        return str(px(track[0][1]))
    parts = []
    move = 0.4  # seconds to glide between positions
    prev_x = px(track[0][1])
    expr = str(prev_x)
    for t, cx in track[1:]:
        x = px(cx)
        t0, t1 = max(0.0, t - move), t
        expr = f"if(lt(t,{t0:.2f}),{expr},if(lt(t,{t1:.2f}),{prev_x}+({x}-{prev_x})*(t-{t0:.2f})/{move},{x}))"
        prev_x = x
    return expr


def even(v: float) -> int:
    v = int(round(v))
    return v - (v % 2)


def build_filter(layout: str, w: int, h: int, faces: list, a: float, b: float) -> tuple[str, int]:
    """Returns filtergraph ending in [v] and the caption margin (px from bottom)."""
    bg = f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.18"
    if layout == "native":
        return f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},setsar=1[v]", 520
    if layout == "face":
        cw = even(h * 9 / 16)
        track = smooth_track(faces, a, b)
        x = crop_x_expr(track, w, cw)
        return f"[0:v]crop=w={cw}:h={h}:x='{x}':y=0,scale={OUT_W}:{OUT_H},setsar=1[v]", 560
    if layout == "split":
        fx = float(np.median([f[1] for f in faces])); fy = float(np.median([f[2] for f in faces]))
        fhn = float(np.median([f[4] for f in faces]))
        panel_h = 840
        ch = min(h, even(fhn * h * 2.6))
        cw = even(ch * OUT_W / panel_h)
        if cw > w:
            cw = even(w); ch = even(cw * panel_h / OUT_W)
        cx = even(min(max(fx * w - cw / 2, 0), w - cw)); cy = even(min(max(fy * h - ch * 0.45, 0), h - ch))
        full_h = even(OUT_W * h / w)
        return (f"[0:v]split=3[a][b][c];[a]{bg}[bg];[b]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{panel_h},setsar=1[face];"
                f"[c]scale={OUT_W}:{full_h},setsar=1[full];[bg][face]overlay=0:0[t1];[t1][full]overlay=0:{panel_h}[v]"), 250
    full_h = even(OUT_W * h / w)
    return f"[0:v]split=2[a][b];[a]{bg}[bg];[b]scale={OUT_W}:{full_h},setsar=1[fg];[bg][fg]overlay=0:(H-h)/2[v]", 380


# ---------------------------------------------------------------- captions (ASS)
def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[dict], a: float, b: float, margin_v: int, hook: str | None, style: dict) -> str:
    size = int(style.get("size", 96))
    upper = style.get("upper", True)
    hi = style.get("highlight", "&H00E5FF&")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Poppins,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,{margin_v},1
Style: Hook,Poppins,66,&H00111111,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.2, b - a))},Hook,,0,0,0,,{{\\fad(120,200)}}{ass_escape(hook)}")
    ws = [w for w in words if w["e"] > a and w["s"] < b]
    groups, cur = [], []
    for w in ws:
        txt = w["w"]
        if cur and (len(cur) >= 3 or sum(len(x["w"]) for x in cur) + len(txt) > 16 or w["s"] - cur[-1]["e"] > 0.6):
            groups.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.!?,]$", txt):
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        g_end = g[-1]["e"]
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
        if nxt is not None and nxt - g_end < 0.35:
            g_end = nxt
        for wi, w in enumerate(g):
            s = w["s"]
            e = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
            parts = []
            for k, x in enumerate(g):
                t = ass_escape(x["w"])
                t = t.upper() if upper else t
                parts.append(f"{{\\c{hi}}}{t}{{\\c&HFFFFFF&}}" if k == wi else t)
            pop = "{\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(s - a)},{ass_time(max(e, s + 0.05) - a)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- render
def render_clip(src: Path, info: dict, clip: dict, words: list[dict], settings: dict, out_path: Path, thumb: Path | None = None) -> dict:
    a, b = float(clip["start"]), float(clip["end"])
    w, h = info["w"], info["h"]
    faces = []
    samples = max(1, int((b - a) / 0.5))
    if h <= w * 1.2:
        faces = analyse_faces(src, a, b, w, h)
    layout = choose_layout(faces, samples, w, h, settings.get("layout", "auto"))
    vf, margin = build_filter(layout, w, h, faces, a, b)
    import uuid
    work = WORK_DIR / ("r" + uuid.uuid4().hex[:10])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    fonts = work / "fonts"
    if not fonts.exists():
        shutil.copytree(FONT_DIR, fonts)
    captions = settings.get("captions", True) and CAN_CAPTION
    hook = clip.get("hook") if settings.get("hook_overlay", True) else None
    final = "[v]"
    if captions or hook:
        (work / "subs.ass").write_text(build_ass(words if captions else [], a, b, margin, hook, settings.get("caption_style", {})), encoding="utf-8")
        vf += ";[v]ass=subs.ass:fontsdir=fonts[vo]"
        final = "[vo]"
    cmd = [FFMPEG, "-y", "-ss", f"{a:.2f}", "-i", str(src), "-t", f"{b - a:.2f}",
           "-filter_complex", vf, "-map", final, "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-movflags", "+faststart", str(out_path)]
    run(cmd, cwd=work)
    thumb = thumb or out_path.with_suffix(".jpg")
    try:
        run([FFMPEG, "-y", "-ss", "1.0", "-i", str(out_path), "-frames:v", "1", "-vf", "scale=360:-2", str(thumb)])
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)
    return {"layout": layout, "faces_found": len(faces), "captions": bool(captions)}


def caption_text(hook: str, campaign: dict) -> str:
    req = [t if t.startswith("#") else "#" + t for t in campaign.get("hashtags", []) if t]
    extra = [t if t.startswith("#") else "#" + t for t in campaign.get("extra_tags", []) if t]
    tags = list(dict.fromkeys(req + extra))[:6]
    return f"{hook}\n\n{' '.join(tags)}".strip()
