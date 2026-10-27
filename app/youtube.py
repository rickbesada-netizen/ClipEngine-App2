"""YouTube upload through the official YouTube Data API (free, uses your own Google Cloud project)."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pipeline as P

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube"]  # last one lets the app update channel branding
SECRET = P.DATA / "youtube_client_secret.json"
TOKEN = P.DATA / "youtube_token.json"            # the main channel, used by every campaign without its own
TOKEN_DIR = P.DATA / "youtube_channels"          # <campaign id>.json: a campaign's own channel
NAMES = TOKEN_DIR / "names.json"                 # remembered channel names, so listing them needs no API calls
_STATES: dict = {}
_LOCK = threading.Lock()


def _key(key):
    return key or "__main__"


def _state(key) -> dict:
    return _STATES.setdefault(_key(key), {"connecting": False, "error": None, "channel": None, "channel_id": None})


STATE = _state(None)  # kept for older callers


def token_path(key=None) -> Path:
    if not key:
        return TOKEN
    safe = re.sub(r"[^a-z0-9_-]", "", str(key).lower())[:40] or "campaign"
    return TOKEN_DIR / f"{safe}.json"


def has_own(key) -> bool:
    return bool(key) and token_path(key).exists()


def resolve(key=None):
    """The campaign's own channel if it has one, otherwise the main channel."""
    return key if has_own(key) else None


def _names() -> dict:
    try:
        return json.loads(NAMES.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _remember_name(key, name, cid):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    n = _names()
    n[_key(key)] = {"name": name, "id": cid}
    NAMES.write_text(json.dumps(n), encoding="utf-8")


def _creds(key=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tp = token_path(key)
    if not tp.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tp.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def _service(key=None):
    from googleapiclient.discovery import build

    creds = _creds(key)
    if not creds:
        raise RuntimeError("YouTube isn't connected. Connect your channel in Settings first.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def status(campaign=None) -> dict:
    """Status of the channel a campaign uploads to (its own, or the main one)."""
    key = resolve(campaign)
    stt = _state(key)
    pending = _state(campaign) if campaign else stt  # a campaign connecting its own channel
    out = {"has_secret": SECRET.exists(), "connected": False, "channel": stt.get("channel"),
           "channel_id": stt.get("channel_id"), "can_brand": _granted_branding(key), "own": key is not None,
           "connecting": pending["connecting"] or stt["connecting"], "error": pending["error"] or stt["error"]}
    if token_path(key).exists():
        try:
            out["connected"] = _creds(key) is not None
            if out["connected"] and not out["channel"]:
                r = _service(key).channels().list(part="snippet", mine=True).execute()
                items = r.get("items") or []
                stt["channel"] = out["channel"] = items[0]["snippet"]["title"] if items else "(no channel on this Google account)"
                stt["channel_id"] = out["channel_id"] = items[0]["id"] if items else None
                _remember_name(key, stt["channel"], stt["channel_id"])
        except Exception as e:  # noqa: BLE001
            out["error"] = f"Connection expired: {str(e)[:150]}. Press Connect again."
            out["connected"] = False
    main = _names().get("__main__", {})
    out["main_channel"] = main.get("name") if TOKEN.exists() else None
    return out


def overview(campaign_ids: list[str]) -> dict:
    """Which channel each campaign uploads to, from remembered names (no API calls)."""
    names = _names()
    main = names.get("__main__", {}).get("name") if TOKEN.exists() else None
    out = {}
    for cid in campaign_ids:
        if has_own(cid):
            out[cid] = {"own": True, "channel": (names.get(cid) or {}).get("name") or "its own channel"}
        else:
            out[cid] = {"own": False, "channel": main}
    return out


def save_secret(text: str) -> None:
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise ValueError("That isn't the client secret JSON file from Google Cloud.") from e
    if "installed" not in data:
        raise ValueError('Wrong kind of OAuth client. In Google Cloud create an OAuth client ID of type "Desktop app" and download its JSON.')
    SECRET.write_text(json.dumps(data), encoding="utf-8")
    # a new Google project: every saved sign-in belonged to the old one
    for tp in [TOKEN, *TOKEN_DIR.glob("*.json")] if TOKEN_DIR.exists() else [TOKEN]:
        if tp.exists() and tp != NAMES:
            tp.unlink()
    NAMES.unlink(missing_ok=True)
    _STATES.clear()


def connect_async(key=None) -> None:
    """Opens Google's sign-in in the browser; runs in the background until the user approves.
    key = a campaign id to give that campaign its own channel, or None for the main channel.
    Google shows a channel picker when the account has several channels (Brand Accounts)."""
    if not SECRET.exists():
        raise RuntimeError("Add your client secret JSON first.")
    stt = _state(key)
    with _LOCK:
        if stt["connecting"]:
            return
        stt.update(connecting=True, error=None, channel=None, channel_id=None)

    def run():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account",
                                          success_message="YouTube is connected to Clip Engine. You can close this tab.")
            tp = token_path(key)
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            stt["error"] = f"Connecting failed: {str(e)[:200]}"
        finally:
            stt["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def disconnect(key=None) -> None:
    tp = token_path(key)
    if tp.exists():
        tp.unlink()
    n = _names()
    if n.pop(_key(key), None) is not None:
        NAMES.write_text(json.dumps(n), encoding="utf-8")
    _state(key).update(channel=None, channel_id=None, error=None)


def upload(path: Path, title: str, description: str, tags: list[str], privacy: str, progress, campaign=None) -> dict:
    from googleapiclient.http import MediaFileUpload

    key = resolve(campaign)
    yt = _service(key)
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": [t.lstrip("#") for t in tags][:15], "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        st, resp = req.next_chunk()
        if st:
            progress(st.progress())
    vid = resp["id"]
    actual = privacy
    try:
        r = yt.videos().list(part="status", id=vid).execute()
        actual = r["items"][0]["status"].get("privacyStatus", privacy)
    except Exception:  # noqa: BLE001
        pass
    return {"id": vid, "url": f"https://www.youtube.com/shorts/{vid}", "privacy": actual,
            "locked": privacy != "private" and actual == "private", "channel": _state(key).get("channel")}


def _granted_branding(key=None) -> bool:
    try:
        data = json.loads(token_path(key).read_text(encoding="utf-8"))
        return "https://www.googleapis.com/auth/youtube" in (data.get("scopes") or [])
    except Exception:  # noqa: BLE001
        return False


def _keywords(text: str) -> str:
    """YouTube wants space-separated keywords, with multi-word ones in quotes."""
    out = []
    for k in re.split(r"[,\n]+", text or ""):
        k = k.strip().strip('"').replace('"', "")
        if k:
            out.append(f'"{k}"' if " " in k else k)
    kw = " ".join(dict.fromkeys(out))
    return kw[:500]


def apply_branding(banner: Path | None, description: str | None, keywords: str | None, campaign=None) -> dict:
    """Sets the channel banner, description and keywords. YouTube's API can't change the profile picture."""
    from googleapiclient.http import MediaFileUpload
    from PIL import Image

    key = resolve(campaign)
    if not _granted_branding(key):
        raise RuntimeError("Clip Engine needs one more permission to edit your channel. In Settings press Disconnect, then Connect YouTube channel again.")
    yt = _service(key)
    r = yt.channels().list(part="brandingSettings", mine=True).execute()
    items = r.get("items") or []
    if not items:
        raise RuntimeError("This Google account has no YouTube channel.")
    ch = items[0]
    bs = ch.get("brandingSettings") or {}
    chan = dict(bs.get("channel") or {})
    done = []
    if description is not None:
        chan["description"] = description[:1000]; done.append("description")
    if keywords is not None:
        chan["keywords"] = _keywords(keywords); done.append("keywords")
    body = {"id": ch["id"], "brandingSettings": {"channel": chan}}
    if banner is not None:
        jpg = banner.with_suffix(".upload.jpg")
        im = Image.open(banner).convert("RGB")
        if im.size[0] < 2048 or abs(im.size[0] / im.size[1] - 16 / 9) > 0.01:
            im = im.resize((2560, 1440), Image.LANCZOS)
        q = 92
        im.save(jpg, "JPEG", quality=q)
        while jpg.stat().st_size > 5_800_000 and q > 50:
            q -= 10; im.save(jpg, "JPEG", quality=q)
        res = yt.channelBanners().insert(media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")).execute()
        jpg.unlink(missing_ok=True)
        body["brandingSettings"]["image"] = {"bannerExternalUrl": res["url"]}
        done.append("banner")
    yt.channels().update(part="brandingSettings", body=body).execute()
    return {"updated": done, "channel_id": ch["id"]}
