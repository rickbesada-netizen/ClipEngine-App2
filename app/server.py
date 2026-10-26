"""Clip Engine local server. Run START.bat (Windows) or `python app/server.py`."""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

if getattr(_sys, "frozen", False) and (_sys.stdout is None or _sys.stderr is None):
    # Windowed ClipEngine.exe has no console: send output to a log file next to the data
    _log_dir = _Path(_sys.executable).resolve().parent / "ClipEngine data"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log = open(_log_dir / "clip-engine.log", "a", encoding="utf-8", buffering=1)
    _sys.stdout = _sys.stderr = _log

import json
import os
import re
import shutil
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
import campaign_import as CI  # noqa: E402
import kit as KIT  # noqa: E402
import cloudsync as CS  # noqa: E402

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
APP_VERSION = "1.3.1"

DEFAULT_SETTINGS = {
    "clips_per_video": 4, "min_len": 20, "max_len": 45, "layout": "auto", "captions": True,
    "hook_overlay": True, "whisper_model": "base", "max_height": 1080, "cookies_browser": "none",
    "cookies_file": "", "use_ollama": False, "youtube_privacy": "public", "ollama_model": "", "caption_style": {"upper": True, "size": 96},
}


def load_db() -> dict:
    for cand in (DB_PATH, DB_PATH.with_suffix(".tmp")):
        if cand.exists():
            try:
                d = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.setdefault("clips", []); d.setdefault("jobs", [])
                    return d
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                # never overwrite a save file we couldn't read: keep a copy
                try:
                    cand.replace(cand.with_name(f"db-unreadable-{int(time.time())}.json"))
                except OSError:
                    pass
    return {"jobs": [], "clips": [], "settings": dict(DEFAULT_SETTINGS), "campaign": None}


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
    SYNC_WAKE.set()


SYNC_WAKE = threading.Event()


def write_campaigns_text(text: str, encoding: str = "utf-8"):
    tmp = CAMPAIGNS_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(CAMPAIGNS_PATH)
    SYNC_WAKE.set()


def campaigns() -> list[dict]:
    try:
        lst = json.loads(CAMPAIGNS_PATH.read_text(encoding="utf-8"))
        if isinstance(lst, list) and lst:
            return lst
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    # damaged or missing: fall back to the built-in presets
    builtin = P.APP_DIR / "campaigns.json"
    lst = json.loads(builtin.read_text(encoding="utf-8"))
    try:
        if CAMPAIGNS_PATH != builtin:
            if CAMPAIGNS_PATH.exists():
                CAMPAIGNS_PATH.replace(CAMPAIGNS_PATH.with_name(f"campaigns-unreadable-{int(time.time())}.json"))
            CAMPAIGNS_PATH.write_text(json.dumps(lst, indent=2), encoding="utf-8")
    except OSError:
        pass
    return lst


def campaign_by_id(cid: str) -> dict:
    for c in campaigns():
        if c["id"] == cid:
            return c
    return campaigns()[0]


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "clip").lower()).strip("-")[:n] or "clip"



# ---------------------------------------------------------------- clip files: one folder per campaign, readable names
def campaign_folder_name(camp_id: str) -> str:
    camp = next((c for c in campaigns() if c["id"] == camp_id), None)
    name = camp["name"] if camp else camp_id
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(". ") or camp_id


def new_clip_relpath(camp_id: str, title: str, keep: str | None = None) -> str:
    folder = campaign_folder_name(camp_id)
    (P.CLIP_DIR / folder).mkdir(parents=True, exist_ok=True)
    base = re.sub(r"#\w+", "", title or "Clip")
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", base).strip().rstrip(". ")[:80].strip() or "Clip"
    n, k = base, 2
    while (P.CLIP_DIR / folder / f"{n}.mp4").exists() and f"{folder}/{n}.mp4" != keep:
        n = f"{base} ({k})"; k += 1
    return f"{folder}/{n}.mp4"


def clip_path(c: dict) -> Path:
    return P.CLIP_DIR / Path(c["file"])


def thumb_path(c: dict) -> Path:
    return P.THUMB_DIR / f"{c['id']}.jpg"


def organise_clips():
    """Moves clips from older versions (all in one folder) into campaign folders with readable names."""
    changed = False
    for c in DB["clips"]:
        try:
            if "/" not in c["file"]:
                old = P.CLIP_DIR / c["file"]
                if old.exists():
                    rel = new_clip_relpath(c["campaign"], c.get("hook") or c.get("source_title") or "Clip")
                    shutil.move(str(old), str(P.CLIP_DIR / rel))
                    c["file"] = rel
                    changed = True
                oldt = P.CLIP_DIR / (c.get("thumb") or "")
                if c.get("thumb") and oldt.is_file():
                    shutil.move(str(oldt), str(thumb_path(c)))
                c["thumb"] = f"{c['id']}.jpg"
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    if changed:
        save_db()



def recover_clips():
    """Adds clip videos found in the clips folders that the save file doesn't know about (e.g. after a lost save)."""
    known = {str(Path(c["file"])).lower() for c in DB["clips"]}
    by_name = {campaign_folder_name(c["id"]).lower(): c["id"] for c in campaigns()}
    added = 0
    for f in sorted(P.CLIP_DIR.rglob("*.mp4")):
        rel = f.relative_to(P.CLIP_DIR)
        if "YouTube uploads" in rel.parts or str(rel).lower() in known:
            continue
        top = rel.parts[0].lower() if len(rel.parts) > 1 else ""
        camp = by_name.get(top)
        if not camp:
            low = f.stem.lower()
            camp = next((cid for cid in by_name.values() if low.startswith(cid + "-")), None) or campaigns()[0]["id"]
        cid = uuid.uuid4().hex[:8]
        hook = re.sub(r"-[0-9a-f]{6,8}$", "", f.stem)
        hook = re.sub(r"[-_]+", " ", hook).strip() if "/" not in str(rel).replace("\\", "/") else f.stem
        c = {"id": cid, "job": None, "campaign": camp, "file": str(rel).replace("\\", "/"), "thumb": f"{cid}.jpg",
             "source_title": "(recovered clip)", "source_url": "", "source_path": "", "start": 0.0, "end": P.probe(f)["duration"] or 0.0,
             "score": 0, "reasons": ["recovered from your clips folder"], "text": "", "hook": hook,
             "caption": P.caption_text(hook, campaign_by_id(camp)), "status": "ready", "posts": [], "created": f.stat().st_mtime,
             "layout": "native", "src_w": 1080, "src_h": 1920}
        try:
            subprocess.run([P.FFMPEG, "-y", "-ss", "1.0", "-i", str(f), "-frames:v", "1", "-vf", "scale=360:-2", str(thumb_path(c))],
                           capture_output=True, creationflags=P._NO_WINDOW, timeout=60)
        except Exception:  # noqa: BLE001
            pass
        DB["clips"].append(c)
        added += 1
    if added:
        DB["clips"].sort(key=lambda c: c.get("created", 0), reverse=True)
        save_db()
        print(f"Recovered {added} clip(s) from the clips folder", flush=True)
    return added

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
    if P.DRIVE_RX.search(src_url):
        dest = P.DATA / "imports" / campaign_folder_name(camp["id"])
        vids = P.drive_import(src_url, dest, progress, cancel)
        if not vids:
            raise RuntimeError("No videos were downloaded from that Google Drive link.")
        with LOCK:
            for v in vids:
                nj = {"id": uuid.uuid4().hex[:8], "url": str(v), "title": v.name, "campaign": j["campaign"],
                      "settings": j.get("settings", {}), "status": "queued", "progress": 0, "message": "Waiting", "created": time.time()}
                DB["jobs"].append(nj)
                QUEUE.append(nj["id"])
        j["title"] = f"Google Drive: {len(vids)} video{'s' if len(vids) != 1 else ''}"
        progress(f"Downloaded {len(vids)} video{'s' if len(vids) != 1 else ''} to ClipEngine data\\imports\\{dest.name}. Clipping them now.", 1.0)
        WAKE.set()
        return
    slug = P.kick_channel(src_url)
    if slug:
        progress(f"Looking up {slug}'s past streams and clips on Kick", 0.05)
        items = P.kick_list(slug, settings, 30)
        streams = [i for i in items if i["kind"] == "stream"]
        pick = streams[:1] or [i for i in items if i["kind"] == "clip"][:5]
        if not pick:
            raise RuntimeError(f"{slug} has no past streams or clips on Kick yet, and a channel page only works while they're live. "
                               "Try again after their next stream.")
        with LOCK:
            for it in pick:
                nj = {"id": uuid.uuid4().hex[:8], "url": it["url"], "title": it["title"], "campaign": j["campaign"],
                      "settings": j.get("settings", {}), "status": "queued", "progress": 0, "message": "Waiting", "created": time.time()}
                DB["jobs"].append(nj)
                QUEUE.append(nj["id"])
        what = "latest past stream" if streams else f"{len(pick)} most-viewed clip{'s' if len(pick) != 1 else ''}"
        j["title"] = f"Kick: {slug}"
        progress(f"Added {slug}'s {what}. To choose others, press Browse next to the channel in Campaign sources "
                 f"(or add kick.com/{slug} as a source).", 1.0)
        WAKE.set()
        return
    if re.match(r"^[a-zA-Z]:\\|^/|^\\\\", src_url) and not src_url.startswith("http"):
        path, meta = P.local_source(src_url)
    else:
        path, meta = P.download(src_url, settings, progress)
        wb = P.WORKING_BROWSER.get("name")
        if wb and DB["settings"].get("cookies_browser", "none") in ("", "none"):
            with LOCK:
                DB["settings"]["cookies_browser"] = wb  # use it straight away next time
            save_db()
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
    spoken = sum(len(s_["words"]) or len(s_["text"].split()) for s_ in segs)
    progress("Finding the best moments", 0.72)
    if dur and dur <= max_len + 3:
        # short source (e.g. a supplied trailer or gameplay clip): use the whole thing
        chosen = [{"start": 0.0, "end": float(dur), "text": " ".join(s_["text"] for s_ in segs), "score": 0,
                   "reasons": ["whole video (short source)"]}]
    elif not segs or spoken < dur * 0.4:
        # little or no talking (gameplay, trailers): go by loudness and rewatch data instead of speech
        length = max(min_len, min(max_len, (min_len + max_len) / 2))
        chosen = P.action_windows(dur, loud, heat, n, length)
        for c_ in chosen:
            c_["text"] = " ".join(s_["text"] for s_ in segs if s_["end"] > c_["start"] and s_["start"] < c_["end"])
            if not segs:
                c_["reasons"] = c_["reasons"] + ["no talking in this video"]
    else:
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
        hook = c.get("ai_title") or (P.hook_line(c["text"], used_hooks) if c["text"].strip() else "")
        if not hook or hook in used_hooks:
            base = P.title_hook(meta["title"]) or camp["name"]
            hook = base if base not in used_hooks else f"{base} part {k + 1}"
        used_hooks.add(hook)
        cid = uuid.uuid4().hex[:8]
        name = new_clip_relpath(camp["id"], hook)
        clip = {"id": cid, "job": j["id"], "campaign": camp["id"], "file": name, "thumb": f"{cid}.jpg",
                "source_title": meta["title"], "source_url": meta["url"], "source_path": str(path),
                "start": round(c["start"], 2), "end": round(c["end"], 2), "score": c["score"], "reasons": c["reasons"],
                "text": c["text"], "hook": hook, "caption": P.caption_text(hook, camp),
                "status": "ready", "posts": [], "created": time.time()}
        r = P.render_clip(path, info, clip, words, settings, clip_path(clip), thumb_path(clip))
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


# ---------------------------------------------------------------- YouTube Studio batch + post watcher
def yt_title(c: dict, camp: dict) -> str:
    tags = " ".join(t if t.startswith("#") else "#" + t for t in camp.get("hashtags", []))
    base = (c.get("hook") or c.get("source_title") or "Clip").strip().rstrip(".")
    title = f"{base} {tags}".strip()
    return title[:95]


def safe_filename(title: str) -> str:
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().rstrip(". ")
    return (t or "clip")[:120]


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"#\w+", " ", (t or "").lower())).strip()


def _channel_for(camp_id: str) -> str:
    acc = DB.setdefault("accounts", {}).get(camp_id, {})
    ch = (acc.get("yt_channel") or "").strip()
    if not ch and acc.get("handle"):
        ch = "@" + acc["handle"]
    if not ch:
        return ""
    m = re.search(r"youtube\.com/(channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)", ch)
    if m:
        return "https://www.youtube.com/" + m.group(1)
    handle = re.sub(r"^.*youtube\.com/", "", ch.split("?")[0]).strip("/").split("/")[0].lstrip("@")
    return "https://www.youtube.com/@" + handle if handle else ""


def _norm_channel(u: str) -> str:
    m = re.search(r"youtube\.com/(@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)", u or "")
    return m.group(1).lower().lstrip("@").replace("channel/", "") if m else (u or "").lower().strip("@/ ")


def open_path(target: Path):
    if os.name == "nt":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])


@APP.post("/api/ytbatch")
def yt_batch():
    import shutil
    body = request.get_json(force=True) or {}
    camp = campaign_by_id(body.get("campaign"))
    want = set(body.get("ids") or [])
    with LOCK:
        pool = [c for c in DB["clips"] if c["campaign"] == camp["id"] and clip_path(c).exists()
                and not any(p.get("platform") == "YouTube Shorts" for p in c.get("posts", []))
                and (c["id"] in want if want else not c.get("yt_batch"))]
    pool = sorted(pool, key=lambda c: c.get("created", 0))[:15]
    if not pool:
        return jsonify(error="No new clips to batch for this campaign. Make more clips, or every clip is already in a batch."), 400
    folder = P.CLIP_DIR / campaign_folder_name(camp["id"]) / "YouTube uploads" / time.strftime("%Y-%m-%d %H%M")
    folder.mkdir(parents=True, exist_ok=True)
    used = set()
    for c in pool:
        title = yt_title(c, camp)
        name = safe_filename(title)
        n, k = name, 2
        while n.lower() in used:
            n = f"{name} {k}"; k += 1
        used.add(n.lower())
        shutil.copy2(clip_path(c), folder / f"{n}.mp4")
        with LOCK:
            c["yt_batch"] = str(folder); c["yt_title"] = title
    save_db()
    try:
        open_path(folder)
    except Exception:  # noqa: BLE001
        pass
    tags = " ".join(dict.fromkeys([*camp.get("hashtags", []), *camp.get("extra_tags", [])]))
    defaults = (f"Daily clips of {camp['name']}: {(camp.get('niche') or 'best moments').lower()}.\n"
                f"Fan clip channel, not affiliated with {camp['name']}.\n\n{tags}").strip()
    return jsonify(ok=True, count=len(pool), folder=str(folder), defaults=defaults,
                   tags=", ".join(t.lstrip("#") for t in [*camp.get("hashtags", []), *camp.get("extra_tags", [])]))


def match_posts(camp_id: str) -> dict:
    """Finds this campaign's clips on your public channel and fills in their post links."""
    url = _channel_for(camp_id)
    if not url:
        return {"error": "Add your YouTube channel (handle or link) first."}
    camp = campaign_by_id(camp_id)
    srcs = {_norm_channel(u) for u in camp.get("sources", []) if "youtube.com" in u}
    if _norm_channel(url) in srcs:
        return {"error": f"That's {camp['name']}'s own channel, the one you clip FROM. Put the channel YOU upload your clips to here (your clip channel, e.g. @{camp.get('handle_base') or 'yourname'}clipsdaily)."}
    try:
        items = P.list_source(url + "/shorts", 40, DB["settings"])
    except Exception as e:  # noqa: BLE001
        return {"error": friendly_error(e)}
    found = 0
    with LOCK:
        waiting = [c for c in DB["clips"] if c["campaign"] == camp_id
                   and not any(p.get("platform") == "YouTube Shorts" for p in c.get("posts", []))]
        taken = {p.get("url") for c in DB["clips"] for p in c.get("posts", [])}
        for it in items:
            if not it.get("id"):
                continue
            link = f"https://www.youtube.com/shorts/{it['id']}"
            if link in taken:
                continue
            t = _norm(it.get("title"))
            if not t:
                continue
            for c in waiting:
                h = _norm(c.get("yt_title") or c.get("hook"))
                if h and (h == t or (len(h) > 12 and (h in t or t in h))):
                    c["posts"].append({"platform": "YouTube Shorts", "url": link, "at": time.time(), "auto": True})
                    c["status"] = "posted"
                    waiting.remove(c); taken.add(link); found += 1
                    break
    if found:
        save_db()
    unmatched = [it.get("title") for it in items if it.get("id") and f"https://www.youtube.com/shorts/{it['id']}" not in taken][:5]
    with LOCK:
        DB.setdefault("watch", {})[camp_id] = {"checked": time.time(), "found": found, "channel": url}
    return {"found": found, "checked": len(items), "channel": url, "waiting": len(waiting), "unmatched": unmatched}


@APP.post("/api/clips/move")
def move_clips():
    body = request.get_json(force=True) or {}
    src, dst = body.get("from"), body.get("to")
    if not src or not dst or src == dst:
        return jsonify(error="Pick two different campaigns."), 400
    camp = campaign_by_id(dst)
    n = 0
    with LOCK:
        for c in DB["clips"]:
            if c["campaign"] == src:
                c["campaign"] = dst
                try:
                    if clip_path(c).exists():
                        rel = new_clip_relpath(dst, c.get("hook") or "Clip")
                        shutil.move(str(clip_path(c)), str(P.CLIP_DIR / rel))
                        c["file"] = rel
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                c["caption"] = P.caption_text(c.get("hook") or "", camp)
                c.pop("yt_batch", None); c.pop("yt_title", None)
                n += 1
    save_db()
    return jsonify(ok=True, moved=n)


@APP.post("/api/ytmatch")
def yt_match():
    body = request.get_json(force=True) or {}
    cid = body.get("campaign") or DB.get("campaign")
    if body.get("channel") is not None:
        with LOCK:
            DB.setdefault("accounts", {}).setdefault(cid, {})["yt_channel"] = body["channel"].strip()
        save_db()
    r = match_posts(cid)
    return (jsonify(error=r["error"]), 400) if r.get("error") else jsonify(r)


@APP.get("/api/ytwatch")
def yt_watch_state():
    cid = request.args.get("campaign") or DB.get("campaign")
    acc = DB.setdefault("accounts", {}).get(cid, {})
    w = DB.get("watch", {}).get(cid, {})
    waiting = sum(1 for c in DB["clips"] if c["campaign"] == cid and c.get("yt_batch")
                  and not any(p.get("platform") == "YouTube Shorts" for p in c.get("posts", [])))
    return jsonify(channel=acc.get("yt_channel") or ("@" + acc["handle"] if acc.get("handle") else ""),
                   resolved=_channel_for(cid), last=w, waiting=waiting)


def watcher():
    """Every 3 minutes, look for newly posted Shorts for clips that are waiting in an upload batch."""
    while True:
        time.sleep(180)
        try:
            camps = {c["campaign"] for c in DB["clips"] if c.get("yt_batch")
                     and not any(p.get("platform") == "YouTube Shorts" for p in c.get("posts", []))}
            for cid in camps:
                if _channel_for(cid):
                    match_posts(cid)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def friendly_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube blocked the download and none of your browsers' sign-ins worked. Fix: open Firefox, sign in to YouTube, "
                "close Firefox, then press Retry. (Chrome, Edge and Brave lock their sign-ins, so Firefox is the reliable one. "
                "Or use a cookies.txt file in Settings.)")
    if "login" in low and "instagram" in low or "rate-limit" in low:
        return "Instagram needs you signed in. Set 'Use cookies from' to the browser where you're logged in to Instagram and retry."
    if "tiktok" in low and ("unexpected response" in low or "unable to extract" in low or "blocked" in low):
        return "TikTok blocked the download. TikTok often stops downloaders: if this is footage you're meant to clip, ask the campaign for the original file, then drag it in."
    if "instagram" in low and ("login" in low or "rate" in low or "unable" in low or "unexpected" in low):
        return "Instagram blocked the download. Sign in to Instagram in Firefox, set Settings > Use cookies from > Firefox, then retry. Or ask the campaign for the original file."
    if "not currently live" in low or "is offline" in low:
        return "That channel isn't live right now. Paste a link to one of their past streams or clips instead, or press Browse next to the channel."
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


@APP.get("/thumbs/<name>")
def thumb_file(name):
    return send_from_directory(P.THUMB_DIR, Path(name).name)


@APP.get("/api/state")
def state():
    try:
        with LOCK:
            clips = [{**{k: v for k, v in c.items() if k != "words"}, "here": clip_path(c).exists()} for c in DB["clips"]]
            return jsonify({"jobs": DB["jobs"][-40:], "clips": clips, "settings": DB["settings"], "campaign": DB.get("campaign"),
                            "campaigns": campaigns(), "can_caption": P.CAN_CAPTION, "now": time.time(),
                            "data_dir": str(P.DATA), "version": APP_VERSION})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(error=f"Clip Engine hit a problem loading your data: {str(e)[:200]}"), 500


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
    write_campaigns_text(json.dumps(body, indent=2), encoding="utf-8")
    return jsonify(ok=True)


@APP.post("/api/list")
def list_src():
    body = request.get_json(force=True)
    try:
        return jsonify(items=P.list_source(body["url"], int(body.get("limit", 15)), DB["settings"]))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=friendly_error(e)), 400


@APP.post("/api/upload")
def upload_video():
    """Receives a video dragged into the page and saves it under ClipEngine data/imports."""
    name = Path(request.args.get("name") or "video.mp4").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip() or "video.mp4"
    dest_dir = P.DATA / "imports"
    if request.args.get("campaign"):
        dest_dir = dest_dir / campaign_folder_name(request.args["campaign"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    stem, suf, k = dest.stem, dest.suffix, 2
    while dest.exists():
        dest = dest_dir / f"{stem} ({k}){suf}"; k += 1
    with open(dest, "wb") as fh:
        while True:
            chunk = request.stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return jsonify(error="That file was empty."), 400
    if dest.suffix.lower() == ".zip":
        try:
            vids = P.extract_videos_from_zip(dest, dest_dir / dest.stem)
        except Exception as e:  # noqa: BLE001
            return jsonify(error=f"Couldn't open that zip: {str(e)[:150]}"), 400
        finally:
            dest.unlink(missing_ok=True)
        if not vids:
            return jsonify(error="That zip has no video files in it."), 400
        return jsonify(paths=[str(v) for v in vids])
    return jsonify(path=str(dest), paths=[str(dest)])


@APP.post("/api/pickfiles")
def pick_files():
    """Opens the normal Windows 'choose files' box on this PC and returns the chosen video paths."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(parent=root, title="Choose video files for Clip Engine",
                                            filetypes=[("Videos", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("All files", "*.*")])
        root.destroy()
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Couldn't open the file chooser ({str(e)[:120]}). Paste the file paths instead."), 500
    return jsonify(paths=list(paths))


@APP.post("/api/jobs")
def add_jobs():
    body = request.get_json(force=True)
    urls = [u.strip() for u in body.get("urls", []) if u.strip()]
    if not urls:
        return jsonify(error="Add at least one link or file path."), 400
    share = [u for u in urls if re.search(r"frame\.io|dropbox\.com|wetransfer|mega\.nz|onedrive|sharepoint|box\.com", u, re.I)]
    if share:
        return jsonify(error="That's a download page (Frame.io, Dropbox…), not a video. Download the files to your PC first, "
                             "then drag them (or the zip) into Clip Engine. Google Drive links work directly."), 400
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
    tmp = P.WORK_DIR / f"rerender-{c['id']}.mp4"
    try:
        r = P.render_clip(src, info, c2, words, settings, tmp, thumb_path(c))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[:300]), 500
    with LOCK:
        old = clip_path(c)
        try:
            old.unlink()
        except OSError:
            pass
        new_name = new_clip_relpath(c["campaign"], c2["hook"], keep=c["file"])
        shutil.move(str(tmp), str(P.CLIP_DIR / new_name))
        c.update(start=c2["start"], end=c2["end"], hook=c2["hook"], file=new_name, thumb=f"{c['id']}.jpg", **r)
    save_db()
    return jsonify(ok=True)


@APP.delete("/api/clips/<cid>")
def delete_clip(cid):
    with LOCK:
        c = clip_by_id(cid)
        if c:
            for f in (clip_path(c), thumb_path(c)):
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
    if body.get("campaign"):
        target = P.CLIP_DIR / campaign_folder_name(body["campaign"])
        target.mkdir(parents=True, exist_ok=True)
    if body.get("file"):
        cand = (P.CLIP_DIR / body["file"]).resolve()
        if P.CLIP_DIR.resolve() in cand.parents:
            target = cand
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


@APP.post("/api/youtube/brand")
def yt_brand():
    body = request.get_json(force=True) or {}
    camp = campaign_by_id(body.get("campaign"))
    banner = None
    if body.get("banner"):
        banner = KIT.KIT_DIR / camp["id"] / Path(body["banner"]).name
        if not banner.exists():
            return jsonify(error="That banner file is gone. Make a new set."), 400
    try:
        r = YT.apply_branding(banner, body.get("description"), body.get("keywords"))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(error=str(e)[:300]), 400
    return jsonify(ok=True, **r)


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
    title = yt_title(c, camp)
    c["yt_title"] = title
    c["yt_upload"] = {"state": "uploading", "progress": 0}

    def run():
        try:
            r = YT.upload(clip_path(c), title, c.get("caption", ""), tags, privacy,
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


# ---------------------------------------------------------------- campaigns from briefs
@APP.post("/api/campaigns/import")
def campaign_import():
    try:
        if request.files.get("file"):
            f = request.files["file"]
            text = CI.text_from_file(f.filename or "brief", f.read())
        else:
            body = request.get_json(force=True) or {}
            if body.get("url"):
                text = CI.text_from_url(body["url"].strip())
            else:
                text = body.get("text", "")
        if len(text.strip()) < 30:
            return jsonify(error="Couldn't find readable text in that. Try a PDF, a Word file, a screenshot, or paste the text."), 400
        draft = CI.parse(text)
        st = DB["settings"]
        if st.get("use_ollama") and st.get("ollama_model"):
            draft = CI.refine_with_ollama(text, draft, st["ollama_model"])
        existing = {c["id"] for c in campaigns()}
        if draft["id"] in existing:
            draft["exists"] = True
        return jsonify(campaign=draft)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(error=str(e)[:300]), 400


@APP.post("/api/campaigns/upsert")
def campaign_upsert():
    c = (request.get_json(force=True) or {}).get("campaign") or {}
    if not c.get("name"):
        return jsonify(error="Give the campaign a name."), 400
    c["id"] = re.sub(r"[^a-z0-9]+", "", (c.get("id") or c["name"]).lower())[:24] or uuid.uuid4().hex[:6]
    c.pop("exists", None)
    for k in ("sources", "hashtags", "extra_tags", "keywords", "look_for", "platforms", "rules"):
        v = c.get(k) or []
        c[k] = [x.strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if str(x).strip()]
    c["hashtags"] = [h if h.startswith("#") else "#" + h for h in c["hashtags"]]
    c["extra_tags"] = [h if h.startswith("#") else "#" + h for h in c["extra_tags"]]
    c["submit_within_min"] = int(c.get("submit_within_min") or 0)
    c.setdefault("look_for_short", "funny, memorable or quotable")
    c.setdefault("handle_base", c["id"][:14])
    lst = campaigns()
    idx = next((i for i, x in enumerate(lst) if x["id"] == c["id"]), None)
    if idx is None:
        lst.insert(0, c)
    else:
        lst[idx] = {**lst[idx], **c}
    write_campaigns_text(json.dumps(lst, indent=2), encoding="utf-8")
    with LOCK:
        DB["campaign"] = c["id"]
    save_db()
    return jsonify(ok=True, id=c["id"])


@APP.delete("/api/campaigns/<cid>")
def campaign_delete(cid):
    lst = campaigns()
    if len(lst) <= 1:
        return jsonify(error="Keep at least one campaign."), 400
    lst = [c for c in lst if c["id"] != cid]
    write_campaigns_text(json.dumps(lst, indent=2), encoding="utf-8")
    with LOCK:
        if DB.get("campaign") == cid:
            DB["campaign"] = lst[0]["id"]
    save_db()
    return jsonify(ok=True)


# ---------------------------------------------------------------- channel kit
def _kit_names(camp):
    acc = DB.setdefault("accounts", {}).get(camp["id"], {})
    names = _campaign_names(camp)
    return acc.get("display") or names["displays"][0], acc.get("handle") or names["handles"][0]


@APP.get("/api/kit")
def kit_get():
    camp = campaign_by_id(request.args.get("campaign") or DB.get("campaign") or campaigns()[0]["id"])
    display, handle = _kit_names(camp)
    d = KIT.KIT_DIR / camp["id"]
    imgs = sorted([p.name for p in d.glob("*.png")], key=lambda n: (d / n).stat().st_mtime, reverse=True) if d.exists() else []
    return jsonify(texts=KIT.texts(camp, display, handle), images=[{"name": n, "kind": "banner" if n.startswith("banner") else "avatar",
                                                                    "url": f"/kit/{camp['id']}/{n}"} for n in imgs[:14]])


@APP.post("/api/kit/generate")
def kit_generate():
    body = request.get_json(force=True) or {}
    camp = campaign_by_id(body.get("campaign"))
    display, _ = _kit_names(camp)
    seed = int(body.get("seed") or int(time.time()) % 100000)
    d = KIT.KIT_DIR / camp["id"]
    if d.exists():
        for f in d.glob("*.png"):
            f.unlink(missing_ok=True)
    try:
        KIT.avatars(camp, display, seed)
        KIT.banners(camp, display, seed)
        clip = next((c for c in DB["clips"] if c["campaign"] == camp["id"] and clip_path(c).exists()), None)
        if clip:
            KIT.avatar_from_clip(camp, clip_path(clip), min(2.0, (clip["end"] - clip["start"]) / 2), seed)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(error=str(e)[:300]), 500
    return jsonify(ok=True, from_clip=bool(clip))


@APP.get("/kit/<cid>/<name>")
def kit_file(cid, name):
    return send_from_directory(KIT.KIT_DIR / Path(cid).name, Path(name).name, as_attachment=request.args.get("download") == "1")


# ---------------------------------------------------------------- cloud sync
SYNC = CS.Sync(P.DATA / "sync.json")


def _sync_snapshot() -> dict:
    with LOCK:
        snap = {
            "campaigns": {c["id"]: c for c in campaigns()},
            "clips": {c["id"]: {k: v for k, v in c.items() if k not in CS.LOCAL_CLIP_FIELDS} for c in DB["clips"]},
            "settings": {k: v for k, v in DB["settings"].items() if k not in CS.LOCAL_SETTINGS},
            "accounts": DB.get("accounts", {}),
        }
        return json.loads(json.dumps(snap))


def _sync_apply(merged: dict, snap: dict):
    def same(a, b):
        return CS._h(CS._strip_meta(a)) == CS._h(CS._strip_meta(b))

    def combine(sec, current: dict) -> dict:
        """merged result, but keep anything that changed here while the sync was talking to the server"""
        m, sn = merged.get(sec, {}), snap.get(sec, {})
        out = {}
        for k, v in current.items():
            if k not in sn or not same(v, sn[k]):
                out[k] = v  # new or edited during the sync: keep, it goes up next time
            elif k in m:
                out[k] = m[k]
        for k, v in m.items():
            if k not in current and k not in sn:
                out[k] = v  # from another computer
        return out

    with LOCK:
        # campaigns
        cur = {c["id"]: c for c in campaigns()}
        newc = combine("campaigns", cur)
        if newc and not same(newc, cur):
            order = [k for k in cur if k in newc] + [k for k in newc if k not in cur]
            write_campaigns_text(json.dumps([newc[k] for k in order], indent=2), encoding="utf-8")
        # clips: keep this computer's own fields; delete files of clips deleted on another computer
        curc = {c["id"]: c for c in DB["clips"]}
        view = {k: {kk: vv for kk, vv in c.items() if kk not in CS.LOCAL_CLIP_FIELDS} for k, c in curc.items()}
        newv = combine("clips", view)
        clips = []
        for k, v in newv.items():
            loc = curc.get(k, {})
            nc = {**v, **{f: loc[f] for f in CS.LOCAL_CLIP_FIELDS if f in loc}}
            if loc and loc.get("file") != nc.get("file"):
                # moved to another campaign on another computer: move this computer's copy too
                src, dst = clip_path(loc), clip_path(nc)
                if src.exists() and not dst.exists():
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                    except OSError:
                        traceback.print_exc()
            clips.append(nc)
        for k, c in curc.items():
            if k not in newv:
                for f in (clip_path(c), thumb_path(c)):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        clips.sort(key=lambda c: c.get("created", 0), reverse=True)
        DB["clips"] = clips
        # settings and accounts
        curs = {k: v for k, v in DB["settings"].items() if k not in CS.LOCAL_SETTINGS}
        for k, v in combine("settings", curs).items():
            DB["settings"][k] = v
        DB["accounts"] = combine("accounts", DB.get("accounts", {}))
        tmp = DB_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(DB, indent=1), encoding="utf-8")
        tmp.replace(DB_PATH)


def sync_now():
    SYNC.run(_sync_snapshot, _sync_apply)


def sync_loop():
    while True:
        SYNC_WAKE.wait(60)
        if SYNC_WAKE.is_set():
            time.sleep(8)  # let a burst of changes settle
        SYNC_WAKE.clear()
        if SYNC.signed_in:
            try:
                sync_now()
            except Exception as e:  # noqa: BLE001
                print("sync:", e, flush=True)


@APP.get("/api/sync")
def sync_status():
    return jsonify(SYNC.status())


@APP.post("/api/sync/login")
def sync_login():
    b = request.get_json(force=True) or {}
    try:
        SYNC.login(b.get("email", ""), b.get("password", ""), bool(b.get("create")))
        sync_now()
    except CS.SyncError as e:
        return jsonify(error=str(e)), 400
    return jsonify(SYNC.status())


@APP.post("/api/sync/now")
def sync_now_route():
    try:
        sync_now()
    except CS.SyncError as e:
        return jsonify(error=str(e)), 400
    return jsonify(SYNC.status())


@APP.post("/api/sync/logout")
def sync_logout():
    SYNC.logout()
    return jsonify(SYNC.status())


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
        "tray icon image": (P.APP_DIR / "tray.png").exists(),
    }
    try:
        import gdown  # noqa: F401
        from gdown import download, download_folder  # noqa: F401
        checks["google drive download"] = True
    except Exception as e:  # noqa: BLE001
        print("gdown import error:", e)
        checks["google drive download"] = False
    if os.name == "nt":
        try:
            import pystray  # noqa: F401
            checks["system tray"] = True
        except Exception as e:  # noqa: BLE001
            print("tray import error:", e)
            checks["system tray"] = False
    try:
        from PIL import Image
        import pypdfium2  # noqa: F401
        import docx  # noqa: F401
        CI._ocr()
        checks["read campaign briefs (PDF, Word, scans)"] = True
        checks["channel kit images"] = Path(KIT.FONT).exists() and Image.new("RGB", (4, 4)) is not None
    except Exception as e:  # noqa: BLE001
        print("brief/kit import error:", e)
        checks["read campaign briefs (PDF, Word, scans)"] = False
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        from googleapiclient.discovery import build
        build("youtube", "v3", developerKey="selftest", static_discovery=True, cache_discovery=False)
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


def _already_running(port: int) -> bool:
    """True only if the SAME version of Clip Engine is already running and answering."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=4) as r:
            return r.status == 200 and json.loads(r.read()).get("version") == APP_VERSION
    except Exception:  # noqa: BLE001
        return False


def _close_other_copies():
    """Closes older or stuck ClipEngine.exe copies so this one can take over (Windows exe only)."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    mine = {os.getpid(), os.getppid()}
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='ClipEngine.exe'\" | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                             capture_output=True, text=True, timeout=20, creationflags=P._NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        traceback.print_exc(); return
    others = []
    for row in out.strip().splitlines():
        try:
            pid, ppid = (int(x) for x in row.split())
        except ValueError:
            continue
        if pid in mine or ppid == os.getpid():
            continue
        others.append(pid)
    for pid in others:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=P._NO_WINDOW)
    if others:
        print(f"Closed {len(others)} older Clip Engine process(es)", flush=True)
        time.sleep(2)


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        return sk.connect_ex(("127.0.0.1", port)) != 0


def _run_tray(url: str, stop) -> bool:
    """Shows Clip Engine in the Windows system tray. Returns False if a tray isn't available."""
    if os.environ.get("CLIP_ENGINE_NO_TRAY"):
        return False
    try:
        import pystray
        from PIL import Image
        img = Image.open(P.APP_DIR / "tray.png")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False

    def open_app(icon=None, item=None):
        webbrowser.open(url)

    def open_folder_(icon=None, item=None):
        try:
            open_path(P.CLIP_DIR)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def quit_app(icon, item=None):
        icon.visible = False
        icon.stop()
        stop()

    menu = pystray.Menu(pystray.MenuItem("Open Clip Engine", open_app, default=True),
                        pystray.MenuItem("Open clips folder", open_folder_),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem("Quit Clip Engine", quit_app))
    icon = pystray.Icon("ClipEngine", img, "Clip Engine", menu)

    def ready(ic):
        ic.visible = True
        try:
            ic.notify("Clip Engine is running. Click this icon to open it, right-click to quit.", "Clip Engine")
        except Exception:  # noqa: BLE001
            pass
    try:
        icon.run(setup=ready)
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        code = selftest()
        try:
            (P.DATA / "selftest.txt").write_text("PASSED" if code == 0 else "FAILED", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(code)
    port = PORT
    if not _already_running(port):
        _close_other_copies()
    if _already_running(port):
        # Clip Engine is already open: just show it
        if not os.environ.get("CLIP_ENGINE_NO_BROWSER"):
            webbrowser.open(f"http://127.0.0.1:{port}")
        sys.exit(0)
    while not _port_free(port) and port < PORT + 10:
        port += 1
    organise_clips()
    try:
        if recover_clips():
            organise_clips()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    SYNC_WAKE.set()
    url = f"http://127.0.0.1:{port}"
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, APP, threaded=True)
    print(f"Clip Engine is running at {url}", flush=True)
    if not os.environ.get("CLIP_ENGINE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    def stop():
        threading.Thread(target=srv.shutdown, daemon=True).start()
        threading.Timer(1.5, lambda: os._exit(0)).start()

    if getattr(sys, "frozen", False):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if not _run_tray(url, stop):
            while True:  # no tray available: keep serving
                time.sleep(3600)
        os._exit(0)
    else:
        print("  Keep this window open while you use it. Press Ctrl+C to stop.", flush=True)
        srv.serve_forever()
