"""Clip Engine local server. Run START.bat (Windows) or `python app/server.py`."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as P  # noqa: E402
import youtube as YT  # noqa: E402

APP = Flask(__name__, static_folder=str(P.APP_DIR / "static"), static_url_path="/static")
DB_PATH = P.DATA / "db.json"
CAMPAIGNS_PATH = P.APP_DIR / "campaigns.json"
if getattr(sys, "frozen", False):
    # In the exe, keep an editable copy of the campaign presets beside the data folder.
    CAMPAIGNS_PATH = P.DATA / "campaigns.json"
    if not CAMPAIGNS_PATH.exists():
        CAMPAIGNS_PATH.write_text((P.APP_DIR / "campaigns.json").read_text(encoding="utf-8"), encoding="utf-8")
LOCK = threading.RLock()
PORT = int(os.environ.get("CLIP_ENGINE_PORT", "5050"))

DEFAULT_SETTINGS = {
    "clips_per_video": 4, "min_len": 20, "max_len": 45, "layout": "auto", "captions": True,
    "hook_overlay": True, "whisper_model": "base", "max_height": 1080, "cookies_browser": "none",
    "cookies_file": "", "use_ollama": False, "youtube_privacy": "public", "ollama_model": "", "caption_style": {"upper": True, "size": 96},
}


def load_db() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"jobs": [], "clips": [], "settings": DEFAULT_SETTINGS, "campaign": None}


DB = load_db()
DB.setdefault("settings", {})
for k, v in DEFAULT_SETTINGS.items():
    DB["settings"].setdefault(k, v)
for j in DB["jobs"]:
    if j["status"] in ("queued", "running"):
        j["status"], j["message"] = "stopped", "Stopped when the app closed. Press Retry."


def save_db():
    with LOCK:
        tmp = DB_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(DB, indent=1), encoding="utf-8")
        tmp.replace(DB_PATH)


def campaigns() -> list[dict]:
    return json.loads(CAMPAIGNS_PATH.read_text(encoding="utf-8"))


def campaign_by_id(cid: str) -> dict:
    for c in campaigns():
        if c["id"] == cid:
            return c
    return campaigns()[0]


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "clip").lower()).strip("-")[:n] or "clip"


# ---------------------------------------------------------------- worker
QUEUE: list[str] = []
WAKE = threading.Event()


def job(jid: str) -> dict | None:
    return next((j for j in DB["jobs"] if j["id"] == jid), None)


def process(j: dict):
    settings = {**DB["settings"], **j.get("settings", {})}
    camp = campaign_by_id(j["campaign"])
    cancel = lambda: job(j["id"]) is None or job(j["id"])["status"] == "cancelling"  # noqa: E731

    def progress(msg, frac=None):
        with LOCK:
            j["message"] = msg
            if frac is not None:
                j["progress"] = round(frac, 3)
        if cancel():
            raise P.Cancelled()

    src_url = j["url"]
    progress("Starting", 0.01)
    if re.match(r"^[a-zA-Z]:\\|^/|^\\\\", src_url) and not src_url.startswith("http"):
        path, meta = P.local_source(src_url)
    else:
        path, meta = P.download(src_url, settings, progress)
    j["title"] = meta["title"]
    info = P.probe(path)
    if not info["w"]:
        raise RuntimeError("Downloaded file has no video stream.")
    dur = info["duration"] or meta.get("duration") or 0
    progress("Measuring audio", 0.3)
    wav = P.WORK_DIR / f"{path.stem}.wav"
    P.extract_audio(path, wav)
    loud = P.loudness(wav)
    heat = P.heat_curve(meta.get("heatmap"), dur)
    n = int(settings["clips_per_video"])
    min_len, max_len = float(settings["min_len"]), float(settings["max_len"])
    windows = None
    if dur > 30 * 60:
        windows = P.find_windows(dur, loud, heat, max(6, n * 4), max_len + 20)
        progress(f"Long video: transcribing the {len(windows)} most promising parts", 0.3)
    segs = P.transcribe(wav, settings["whisper_model"], windows, progress, cancel)
    if not segs:
        raise RuntimeError("No speech found in this video.")
    progress("Finding the best moments", 0.72)
    cands = P.score_candidates(segs, loud, heat, min_len, max_len, camp.get("keywords", []))
    if settings.get("use_ollama") and settings.get("ollama_model"):
        progress("Asking the local AI to rate moments", 0.74)
        top = P.pick(cands, n * 3, gap=2)
        cands = P.ollama_rank(top, settings["ollama_model"], camp.get("look_for_short", ""))
    chosen = P.pick(cands, n)
    if not chosen:
        raise RuntimeError(f"No complete moments between {min_len:g} and {max_len:g} seconds. Try a wider length range.")
    words = [w for s in segs for w in s["words"]]
    made = 0
    used_hooks: set = set()
    for k, c in enumerate(chosen):
        progress(f"Rendering clip {k + 1} of {len(chosen)}", 0.76 + 0.24 * k / len(chosen))
        # trim to word boundaries with a little air
        c["start"] = max(0.0, c["start"] - 0.15)
        c["end"] = min(dur or c["end"] + 0.3, c["end"] + 0.35)
        hook = c.get("ai_title") or P.hook_line(c["text"], used_hooks)
        used_hooks.add(hook)
        cid = uuid.uuid4().hex[:8]
        name = f"{slug(camp['name'], 14)}-{slug(meta['title'], 30)}-{k + 1:02d}-{cid}.mp4"
        clip = {"id": cid, "job": j["id"], "campaign": camp["id"], "file": name, "thumb": name.replace(".mp4", ".jpg"),
                "source_title": meta["title"], "source_url": meta["url"], "source_path": str(path),
                "start": round(c["start"], 2), "end": round(c["end"], 2), "score": c["score"], "reasons": c["reasons"],
                "text": c["text"], "hook": hook, "caption": P.caption_text(hook, camp),
                "status": "ready", "posts": [], "created": time.time()}
        out = P.CLIP_DIR / name
        r = P.render_clip(path, info, clip, words, settings, out)
        clip.update(r)
        clip["words"] = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        clip["src_w"], clip["src_h"] = info["w"], info["h"]
        with LOCK:
            DB["clips"].insert(0, clip)
        save_db()
        made += 1
    progress(f"Done: {made} clip{'s' if made != 1 else ''}", 1.0)


def worker():
    while True:
        WAKE.wait(1.0)
        WAKE.clear()
        while QUEUE:
            jid = QUEUE.pop(0)
            j = job(jid)
            if not j or j["status"] != "queued":
                continue
            j["status"] = "running"
            save_db()
            try:
                process(j)
                j["status"] = "done"
            except P.Cancelled:
                j["status"], j["message"] = "stopped", "Stopped."
            except Exception as e:  # noqa: BLE001
                j["status"] = "error"
                j["message"] = friendly_error(e)
                traceback.print_exc()
            j["finished"] = time.time()
            save_db()


def friendly_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube wants a signed-in browser. In Settings set 'Use cookies from' to Firefox (signed in to YouTube) and retry."
    if "login" in low and "instagram" in low or "rate-limit" in low:
        return "Instagram needs you signed in. Set 'Use cookies from' to the browser where you're logged in to Instagram and retry."
    if "unsupported url" in low:
        return "That site or link type isn't supported. Paste a direct video, VOD or clip link."
    if "private" in low:
        return "That video is private or removed."
    return s[:400]


# ---------------------------------------------------------------- routes
@APP.get("/")
def index():
    return send_from_directory(P.APP_DIR / "static", "index.html")


@APP.get("/clips/<path:name>")
def clip_file(name):
    return send_from_directory(P.CLIP_DIR, name)


@APP.get("/api/state")
def state():
    with LOCK:
        clips = [{k: v for k, v in c.items() if k != "words"} for c in DB["clips"]]
        return jsonify({"jobs": DB["jobs"][-40:], "clips": clips, "settings": DB["settings"], "campaign": DB.get("campaign"),
                        "campaigns": campaigns(), "can_caption": P.CAN_CAPTION, "now": time.time()})


@APP.get("/api/ollama")
def ollama():
    return jsonify({"models": P.ollama_available()})


@APP.post("/api/settings")
def set_settings():
    body = request.get_json(force=True) or {}
    with LOCK:
        DB["settings"].update({k: v for k, v in body.get("settings", {}).items() if k in DEFAULT_SETTINGS})
        if body.get("campaign"):
            DB["campaign"] = body["campaign"]
    save_db()
    return jsonify(ok=True)


@APP.post("/api/campaigns")
def save_campaigns():
    body = request.get_json(force=True)
    if not isinstance(body, list) or not body:
        return jsonify(error="Expected a list of campaigns"), 400
    CAMPAIGNS_PATH.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return jsonify(ok=True)


@APP.post("/api/list")
def list_src():
    body = request.get_json(force=True)
    try:
        return jsonify(items=P.list_source(body["url"], int(body.get("limit", 15)), DB["settings"]))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=friendly_error(e)), 400


@APP.post("/api/jobs")
def add_jobs():
    body = request.get_json(force=True)
    urls = [u.strip() for u in body.get("urls", []) if u.strip()]
    if not urls:
        return jsonify(error="Add at least one link or file path."), 400
    made = []
    with LOCK:
        for u in urls:
            j = {"id": uuid.uuid4().hex[:8], "url": u, "title": u, "campaign": body.get("campaign") or campaigns()[0]["id"],
                 "settings": body.get("settings", {}), "status": "queued", "progress": 0, "message": "Waiting", "created": time.time()}
            DB["jobs"].append(j)
            QUEUE.append(j["id"])
            made.append(j["id"])
    save_db()
    WAKE.set()
    return jsonify(ok=True, jobs=made)


@APP.post("/api/jobs/<jid>/<action>")
def job_action(jid, action):
    with LOCK:
        j = job(jid)
        if not j:
            return jsonify(error="Not found"), 404
        if action == "cancel":
            if j["status"] == "queued":
                j["status"], j["message"] = "stopped", "Stopped."
            elif j["status"] == "running":
                j["status"] = "cancelling"
        elif action == "retry":
            j.update(status="queued", progress=0, message="Waiting")
            QUEUE.append(jid)
            WAKE.set()
        elif action == "remove":
            DB["jobs"] = [x for x in DB["jobs"] if x["id"] != jid]
    save_db()
    return jsonify(ok=True)


def clip_by_id(cid):
    return next((c for c in DB["clips"] if c["id"] == cid), None)


@APP.post("/api/clips/<cid>")
def update_clip(cid):
    body = request.get_json(force=True)
    with LOCK:
        c = clip_by_id(cid)
        if not c:
            return jsonify(error="Not found"), 404
        for k in ("caption", "hook", "status"):
            if k in body:
                c[k] = body[k]
        if "post" in body:
            p = body["post"]
            p["at"] = time.time()
            c["posts"].append(p)
            c["status"] = "posted"
        if "submitted" in body:
            for p in c["posts"]:
                if p.get("url") == body["submitted"] or body["submitted"] == "all":
                    p["submitted"] = time.time()
            if all(p.get("submitted") for p in c["posts"]):
                c["status"] = "submitted"
    save_db()
    return jsonify(ok=True)


@APP.post("/api/clips/<cid>/rerender")
def rerender(cid):
    body = request.get_json(force=True)
    c = clip_by_id(cid)
    if not c:
        return jsonify(error="Not found"), 404
    src = Path(c["source_path"])
    if not src.exists():
        return jsonify(error="The source video was deleted, so this clip can't be re-cut."), 400
    c2 = dict(c)
    c2["start"] = float(body.get("start", c["start"]))
    c2["end"] = float(body.get("end", c["end"]))
    if c2["end"] - c2["start"] < 3:
        return jsonify(error="Clip must be at least 3 seconds."), 400
    c2["hook"] = body.get("hook", c["hook"])
    settings = {**DB["settings"], "layout": body.get("layout") or DB["settings"]["layout"]}
    info = {"w": c["src_w"], "h": c["src_h"]}
    words = c.get("words", [])
    new_name = c["file"].rsplit("-", 1)[0] + f"-{uuid.uuid4().hex[:6]}.mp4"
    try:
        r = P.render_clip(src, info, c2, words, settings, P.CLIP_DIR / new_name)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[:300]), 500
    with LOCK:
        old = P.CLIP_DIR / c["file"]
        for f in (old, old.with_suffix(".jpg")):
            try:
                f.unlink()
            except OSError:
                pass
        c.update(start=c2["start"], end=c2["end"], hook=c2["hook"], file=new_name, thumb=new_name.replace(".mp4", ".jpg"), **r)
    save_db()
    return jsonify(ok=True)


@APP.delete("/api/clips/<cid>")
def delete_clip(cid):
    with LOCK:
        c = clip_by_id(cid)
        if c:
            for f in (P.CLIP_DIR / c["file"], P.CLIP_DIR / c["thumb"]):
                try:
                    f.unlink()
                except OSError:
                    pass
            DB["clips"] = [x for x in DB["clips"] if x["id"] != cid]
    save_db()
    return jsonify(ok=True)


@APP.post("/api/open")
def open_folder():
    body = request.get_json(silent=True) or {}
    target = P.CLIP_DIR
    if body.get("file"):
        target = P.CLIP_DIR / Path(body["file"]).name
    try:
        if os.name == "nt":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if target.is_file() else "", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@APP.post("/api/cleanup")
def cleanup():
    """Delete downloaded source videos to free disk space (clips are kept)."""
    freed = 0
    for f in P.DL_DIR.glob("*"):
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    for f in P.WORK_DIR.glob("*.wav"):
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return jsonify(ok=True, freed_mb=round(freed / 1e6))


# ---------------------------------------------------------------- account setup helpers
def _campaign_names(camp: dict) -> dict:
    name = camp.get("name") or "Creator"
    base = camp.get("handle_base") or re.sub(r"[^a-z0-9]", "", name.lower()) or "clips"
    niche = camp.get("niche") or "Best moments, quotes and funny clips"
    handles = [f"{base}clipsdaily", f"{base}clips", f"{base}.clips", f"{base}moments", f"daily{base}clips",
               f"{base}highlights", f"{base}daily", f"best.of.{base}", f"{base}reels", f"{base}_vault"]
    displays = [f"{name} Clips Daily", f"{name} Clips", f"{name} Moments", f"Best of {name}", f"{name} Highlights", f"Daily {name}"]
    bio = f"Daily clips of {name}. {niche}. Fan page, not affiliated."
    yt_desc = (f"The best moments from {name}, clipped daily. {niche}.\n\n"
               f"This is a fan clip channel and isn't run by {name}. New Shorts every day.")
    return {"handles": handles, "displays": displays, "bio": bio, "youtube_description": yt_desc}


def _http_status(url: str) -> tuple[int, str]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
                                               "accept-language": "en-US,en;q=0.9", "cookie": "CONSENT=YES+cb; SOCS=CAI"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(400000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def _check_handle(h: str) -> dict:
    out = {}
    code, _ = _http_status(f"https://www.youtube.com/@{h}")
    out["youtube"] = "free" if code == 404 else "taken" if code == 200 else "unknown"
    code, html = _http_status(f"https://www.tiktok.com/@{h}")
    if code == 404 or "Couldn't find this account" in html or '"statusCode":10221' in html or '"statusCode":10202' in html:
        out["tiktok"] = "free"
    elif code == 200 and f'"uniqueId":"{h}"' in html:
        out["tiktok"] = "taken"
    else:
        out["tiktok"] = "unknown"
    out["instagram"] = "unknown"
    return out


@APP.get("/api/accounts")
def accounts():
    camp = campaign_by_id(request.args.get("campaign") or (DB.get("campaign") or campaigns()[0]["id"]))
    acc = DB.setdefault("accounts", {}).get(camp["id"], {})
    return jsonify({**_campaign_names(camp), "saved": acc})


@APP.post("/api/accounts/check")
def accounts_check():
    from concurrent.futures import ThreadPoolExecutor
    hs = [re.sub(r"[^a-z0-9._]", "", h.lower())[:30] for h in (request.get_json(force=True).get("handles") or [])][:12]
    with ThreadPoolExecutor(6) as ex:
        res = dict(zip(hs, ex.map(_check_handle, hs)))
    return jsonify(res)


@APP.post("/api/accounts/save")
def accounts_save():
    body = request.get_json(force=True)
    with LOCK:
        DB.setdefault("accounts", {})[body["campaign"]] = body.get("data", {})
    save_db()
    return jsonify(ok=True)


# ---------------------------------------------------------------- YouTube upload
@APP.get("/api/youtube")
def yt_status():
    try:
        return jsonify(YT.status())
    except Exception as e:  # noqa: BLE001
        return jsonify({"has_secret": YT.SECRET.exists(), "connected": False, "error": str(e)[:200]})


@APP.post("/api/youtube/secret")
def yt_secret():
    try:
        YT.save_secret(request.get_json(force=True).get("text", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@APP.post("/api/youtube/connect")
def yt_connect():
    try:
        YT.connect_async()
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@APP.post("/api/youtube/disconnect")
def yt_disconnect():
    YT.disconnect()
    return jsonify(ok=True)


@APP.post("/api/clips/<cid>/youtube")
def yt_upload(cid):
    c = clip_by_id(cid)
    if not c:
        return jsonify(error="Not found"), 404
    if (c.get("yt_upload") or {}).get("state") == "uploading":
        return jsonify(error="Already uploading"), 400
    body = request.get_json(force=True) or {}
    privacy = body.get("privacy") or DB["settings"].get("youtube_privacy", "public")
    camp = campaign_by_id(c["campaign"])
    tags = list(dict.fromkeys([*camp.get("hashtags", []), *camp.get("extra_tags", [])]))
    req_tags = " ".join(t if t.startswith("#") else "#" + t for t in camp.get("hashtags", []))
    title = f"{c.get('hook') or c['source_title']} {req_tags} #Shorts".strip()
    c["yt_upload"] = {"state": "uploading", "progress": 0}

    def run():
        try:
            r = YT.upload(P.CLIP_DIR / c["file"], title, c.get("caption", ""), tags, privacy,
                          lambda f: c["yt_upload"].update(progress=round(f, 3)))
            with LOCK:
                c["yt_upload"] = {"state": "done", "progress": 1, **r}
                if r["privacy"] == "public" or privacy != "public":
                    c["posts"].append({"platform": "YouTube Shorts", "url": r["url"], "at": time.time(), "auto": True})
                    if r["privacy"] == "public":
                        c["status"] = "posted"
        except Exception as e:  # noqa: BLE001
            c["yt_upload"] = {"state": "error", "error": str(e)[:300]}
        save_db()

    threading.Thread(target=run, daemon=True).start()
    save_db()
    return jsonify(ok=True)


def selftest() -> int:
    """Checks every heavy dependency loads. Used by the Windows build to test the exe."""
    import cv2
    import faster_whisper  # noqa: F401
    import yt_dlp  # noqa: F401
    ok = True
    checks = {
        "ffmpeg": Path(P.FFMPEG).exists() or bool(P.shutil.which(P.FFMPEG)),
        "ffmpeg captions (ass filter)": P.CAN_CAPTION,
        "face detection": hasattr(cv2, "CascadeClassifier") and not cv2.CascadeClassifier(
            os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")).empty(),
        "screen files": (P.APP_DIR / "static" / "index.html").exists(),
        "caption font": (P.FONT_DIR / "Poppins-Bold.ttf").exists(),
        "campaigns": bool(campaigns()),
    }
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        checks["youtube upload"] = True
    except Exception as e:  # noqa: BLE001
        print("youtube import error:", e)
        checks["youtube upload"] = False
    try:
        from faster_whisper.utils import get_assets_path
        import onnxruntime  # noqa: F401  (voice detection)
        checks["transcription files"] = any(Path(get_assets_path()).glob("*.onnx"))
    except Exception as e:  # noqa: BLE001
        print("transcription import error:", e)
        checks["transcription files"] = False
    for k, v in checks.items():
        print(f"{'OK  ' if v else 'FAIL'} {k}")
        ok = ok and bool(v)
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    threading.Thread(target=worker, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Clip Engine is running at {url}\n  Keep this window open while you use it.\n")
    if not os.environ.get("CLIP_ENGINE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    APP.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
