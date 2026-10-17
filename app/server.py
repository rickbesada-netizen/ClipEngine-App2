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

APP = Flask(__name__, static_folder="static", static_url_path="/static")
DB_PATH = P.DATA / "db.json"
CAMPAIGNS_PATH = P.APP_DIR / "campaigns.json"
LOCK = threading.RLock()
PORT = int(os.environ.get("CLIP_ENGINE_PORT", "5050"))

DEFAULT_SETTINGS = {
    "clips_per_video": 4, "min_len": 20, "max_len": 45, "layout": "auto", "captions": True,
    "hook_overlay": True, "whisper_model": "base", "max_height": 1080, "cookies_browser": "none",
    "cookies_file": "", "use_ollama": False, "ollama_model": "", "caption_style": {"upper": True, "size": 96},
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


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Clip Engine is running at {url}\n  Keep this window open while you use it.\n")
    if not os.environ.get("CLIP_ENGINE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    APP.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
