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
TOKEN = P.DATA / "youtube_token.json"
STATE = {"connecting": False, "error": None, "channel": None}
_LOCK = threading.Lock()


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def _service():
    from googleapiclient.discovery import build

    creds = _creds()
    if not creds:
        raise RuntimeError("YouTube isn't connected. Connect your channel in Settings first.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def status() -> dict:
    out = {"has_secret": SECRET.exists(), "connected": False, "channel": STATE.get("channel"),
           "channel_id": STATE.get("channel_id"), "can_brand": _granted_branding(),
           "connecting": STATE["connecting"], "error": STATE["error"]}
    if TOKEN.exists():
        try:
            out["connected"] = _creds() is not None
            if out["connected"] and not out["channel"]:
                r = _service().channels().list(part="snippet", mine=True).execute()
                items = r.get("items") or []
                STATE["channel"] = out["channel"] = items[0]["snippet"]["title"] if items else "(no channel on this Google account)"
                STATE["channel_id"] = out["channel_id"] = items[0]["id"] if items else None
        except Exception as e:  # noqa: BLE001
            out["error"] = f"Connection expired: {str(e)[:150]}. Press Connect again."
            out["connected"] = False
    return out


def save_secret(text: str) -> None:
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise ValueError("That isn't the client secret JSON file from Google Cloud.") from e
    if "installed" not in data:
        raise ValueError('Wrong kind of OAuth client. In Google Cloud create an OAuth client ID of type "Desktop app" and download its JSON.')
    SECRET.write_text(json.dumps(data), encoding="utf-8")
    if TOKEN.exists():
        TOKEN.unlink()
    STATE.update(channel=None, error=None)


def connect_async() -> None:
    """Opens Google's sign-in in the browser; runs in the background until the user approves."""
    if not SECRET.exists():
        raise RuntimeError("Add your client secret JSON first.")
    with _LOCK:
        if STATE["connecting"]:
            return
        STATE.update(connecting=True, error=None, channel=None)

    def run():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                          success_message="YouTube is connected to Clip Engine. You can close this tab.")
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            STATE["error"] = f"Connecting failed: {str(e)[:200]}"
        finally:
            STATE["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def disconnect() -> None:
    if TOKEN.exists():
        TOKEN.unlink()
    STATE.update(channel=None, error=None)


def upload(path: Path, title: str, description: str, tags: list[str], privacy: str, progress) -> dict:
    from googleapiclient.http import MediaFileUpload

    yt = _service()
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
            "locked": privacy != "private" and actual == "private"}


def _granted_branding() -> bool:
    try:
        data = json.loads(TOKEN.read_text(encoding="utf-8"))
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


def apply_branding(banner: Path | None, description: str | None, keywords: str | None) -> dict:
    """Sets the channel banner, description and keywords. YouTube's API can't change the profile picture."""
    from googleapiclient.http import MediaFileUpload
    from PIL import Image

    if not _granted_branding():
        raise RuntimeError("Clip Engine needs one more permission to edit your channel. In Settings press Disconnect, then Connect YouTube channel again.")
    yt = _service()
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
