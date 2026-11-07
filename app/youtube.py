"""YouTube upload through the official YouTube Data API (free, uses your own Google Cloud project)."""
from __future__ import annotations

import json
import os
import time
import re
import threading
from pathlib import Path

import pipeline as P

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube",  # lets the app update channel branding
          "openid", "https://www.googleapis.com/auth/userinfo.email"]  # so we can show which Google account is connected
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


def _remember_name(key, name, cid, email=None):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    n = _names()
    old = n.get(_key(key)) or {}
    entry = {"name": name, "id": cid}
    # Keep a previously-stored email if this update didn't bring a new one.
    entry["email"] = email or old.get("email")
    n[_key(key)] = entry
    NAMES.write_text(json.dumps(n), encoding="utf-8")


def _fetch_email(creds) -> str | None:
    """The signed-in Google account's email (needs the userinfo.email scope)."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return (json.loads(r.read().decode("utf-8")) or {}).get("email")
    except Exception:  # noqa: BLE001
        return None


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
           "connecting": any(x.get("connecting") and time.time() - x.get("started", 0) < 900 for x in (pending, stt)),
           "error": pending["error"] or stt["error"]}
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
    out["email"] = (_names().get(_key(key)) or {}).get("email")
    return out


def overview(campaign_ids: list[str]) -> dict:
    """Which channel each campaign uploads to, from remembered names (no API calls)."""
    names = _names()
    main = names.get("__main__", {}) if TOKEN.exists() else {}
    out = {}
    for cid in campaign_ids:
        if has_own(cid):
            info = names.get(cid) or {}
            out[cid] = {"own": True, "channel": info.get("name") or "its own channel", "email": info.get("email")}
        else:
            out[cid] = {"own": False, "channel": main.get("name"), "email": main.get("email")}
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


_FLOWS: dict = {}  # state -> {"flow", "key", "at"}
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # Google may return the scopes in a different set/order: not an error


def start_connect(key, port: int) -> str:
    """Returns Google's sign-in link for a campaign (key) or the default channel (None). Google sends the answer to
    http://127.0.0.1:<port>/oauth/google on the laptop running Clip Engine. Signed in on another laptop or phone,
    that page won't load there, but its address holds the answer: pasting it into Clip Engine finishes the job
    (finish_from_url)."""
    if not SECRET.exists():
        raise RuntimeError("Add your client secret JSON first.")
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(SECRET), SCOPES, redirect_uri=f"http://127.0.0.1:{port}/oauth/google",
                                         autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account", include_granted_scopes="true")
    now = time.time()
    for s_, v in list(_FLOWS.items()):
        if now - v["at"] > 3600:
            _FLOWS.pop(s_, None)
    _FLOWS[state] = {"flow": flow, "key": key, "at": now}
    _state(key).update(connecting=True, error=None, channel=None, channel_id=None, started=now)
    return url


def finish(code: str, state: str):
    """Swaps Google's one-time code for a lasting sign-in. Returns the campaign key."""
    pend = _FLOWS.pop(state, None)
    if not pend:
        raise RuntimeError("That sign-in link is old or came from another Clip Engine. Press Connect again and use the new link.")
    key = pend["key"]
    stt = _state(key)
    try:
        pend["flow"].fetch_token(code=code)
        creds = pend["flow"].credentials
        tp = token_path(key)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(creds.to_json(), encoding="utf-8")
        stt.update(error=None, channel=None, channel_id=None)
        # Remember which Google account this is + its channel, so the list can
        # show it without any further API calls.
        email = _fetch_email(creds)
        name, cid = None, None
        try:
            from googleapiclient.discovery import build
            svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
            items = (svc.channels().list(part="snippet", mine=True).execute() or {}).get("items") or []
            name = items[0]["snippet"]["title"] if items else "(no channel on this Google account)"
            cid = items[0]["id"] if items else None
        except Exception:  # noqa: BLE001
            pass
        _remember_name(key, name, cid, email)
        stt["channel"], stt["channel_id"] = name, cid
    except Exception as e:  # noqa: BLE001
        stt["error"] = f"Connecting failed: {str(e)[:200]}"
        raise RuntimeError(stt["error"]) from e
    finally:
        stt["connecting"] = False
    return key


def finish_from_url(url: str):
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url.strip()).query)
    if q.get("error"):
        raise RuntimeError(f"Google said: {q['error'][0]}. Press Connect and try again.")
    if not q.get("code") or not q.get("state"):
        raise RuntimeError("That isn't the address from the sign-in page. Copy the whole address (it contains code=...).")
    return finish(q["code"][0], q["state"][0])


def connect_async(key=None) -> None:  # older callers
    raise RuntimeError("Use start_connect")


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
