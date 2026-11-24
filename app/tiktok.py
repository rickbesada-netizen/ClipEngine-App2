"""TikTok: each campaign posts to its own client's TikTok account (TikTok Content Posting API, Direct Post).

Rick's own free TikTok developer app (developers.tiktok.com) holds the Client key and Client secret. Each client
signs in once (Login Kit, scopes user.info.basic + video.publish); Clip Engine keeps a refresh token (365 days) and
uploads the clip file straight from this laptop.

Until TikTok audits the app, TikTok only allows posting to accounts set to private, and every post is private
(SELF_ONLY). After the audit the same code posts publicly: the privacy choices come from TikTok itself.

The sign-in comes back through the same relay page as Instagram (buildable-clip-factory.netlify.app/oauth/meta.html),
which forwards to http://127.0.0.1:<port>/oauth/meta. The state is "<port>.tt<nonce>" so Clip Engine knows it's TikTok.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path

import pipeline as P

CONFIG = P.DATA / "tiktok_app.json"
ACCOUNTS = P.DATA / "tiktok_accounts"
RELAY = "https://buildable-clip-factory.netlify.app/oauth/meta.html"
AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = os.environ.get("CLIP_ENGINE_TIKTOK_API") or "https://open.tiktokapis.com"
SCOPES = ["user.info.basic", "video.publish"]
_PENDING: dict = {}


class TikTokError(Exception):
    pass


def _req():
    import requests
    return requests


def app_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_app(client_key: str, client_secret: str):
    client_key, client_secret = (client_key or "").strip(), (client_secret or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", client_key):
        raise TikTokError("The Client key is under App details > Credentials on developers.tiktok.com (press the eye to show it).")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", client_secret):
        raise TikTokError("The Client secret is just below the Client key (press the eye to show it).")
    CONFIG.write_text(json.dumps({"client_key": client_key, "client_secret": client_secret}), encoding="utf-8")


def has_app() -> bool:
    c = app_config()
    return bool(c.get("client_key") and c.get("client_secret"))


# ---------------------------------------------------------------- per-campaign accounts
def _acct_path(campaign: str) -> Path:
    return ACCOUNTS / (re.sub(r"[^a-z0-9_-]", "", campaign.lower())[:40] + ".json")


def _load(campaign: str) -> dict:
    try:
        return json.loads(_acct_path(campaign).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(campaign: str, a: dict):
    ACCOUNTS.mkdir(parents=True, exist_ok=True)
    _acct_path(campaign).write_text(json.dumps(a), encoding="utf-8")


def disconnect(campaign: str):
    try:
        _acct_path(campaign).unlink()
    except OSError:
        pass


def owns_state(state: str) -> bool:
    nonce = (state or "").split(".", 1)[-1].split("#")[0]
    return nonce.startswith("tt") and nonce in _PENDING


def login_url(campaign: str, port: int) -> str:
    cfg = app_config()
    if not has_app():
        raise TikTokError("Add your TikTok Client key and Client secret first (TikTok setup in Settings).")
    nonce = "tt" + secrets.token_urlsafe(16)
    _PENDING[nonce] = {"campaign": campaign, "at": time.time()}
    q = {"client_key": cfg["client_key"], "response_type": "code", "scope": ",".join(SCOPES),
         "redirect_uri": RELAY, "state": f"{port}.{nonce}", "disable_auto_auth": "1"}
    return AUTH + "?" + urllib.parse.urlencode(q)


def _check(r, what: str) -> dict:
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        raise TikTokError(f"{what} failed: TikTok said {r.status_code}.") from None
    err = j.get("error")
    if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
        msg = err.get("message") or err.get("code")
        if err.get("code") == "unaudited_client_can_only_post_to_private_accounts":
            msg = ("TikTok only lets apps that haven't passed TikTok's audit post to private accounts. Set this TikTok "
                   "account to private (Settings > Privacy > Private account), or wait until your app passes the audit.")
        elif err.get("code") in ("spam_risk_too_many_posts", "spam_risk_user_banned_from_posting"):
            msg = "TikTok says this account has posted too much today. Try again tomorrow."
        raise TikTokError(f"{what} failed: {msg}")
    if isinstance(err, str) and err:  # the oauth endpoint uses {"error": "...", "error_description": "..."}
        raise TikTokError(f"{what} failed: {j.get('error_description') or err}")
    if r.status_code >= 400:
        raise TikTokError(f"{what} failed: TikTok said {r.status_code}.")
    return j


def finish_login(code: str, state: str) -> str:
    """Called when TikTok sends the user back. Returns the campaign id."""
    nonce = state.split(".", 1)[-1].split("#")[0]
    pend = _PENDING.pop(nonce, None)
    if not pend or time.time() - pend["at"] > 1800:
        raise TikTokError("That TikTok sign-in link expired. Press Connect TikTok again in Clip Engine.")
    cfg = app_config()
    t = _check(_req().post(f"{API}/v2/oauth/token/", data={
        "client_key": cfg["client_key"], "client_secret": cfg["client_secret"], "code": urllib.parse.unquote(code),
        "grant_type": "authorization_code", "redirect_uri": RELAY}, timeout=30), "Signing in to TikTok")
    if "video.publish" not in (t.get("scope") or ""):
        raise TikTokError("TikTok didn't give permission to post. Press Connect TikTok again and tick every permission.")
    now = time.time()
    a = {"open_id": t.get("open_id"), "access_token": t["access_token"], "refresh_token": t["refresh_token"],
         "expires_at": now + int(t.get("expires_in", 86400)) - 300,
         "refresh_expires_at": now + int(t.get("refresh_expires_in", 31536000)), "scope": t.get("scope", "")}
    _save(pend["campaign"], a)
    try:
        info = creator_info(pend["campaign"])
        a = _load(pend["campaign"])
        a.update(username=info.get("creator_username", ""), nickname=info.get("creator_nickname", ""))
        _save(pend["campaign"], a)
    except TikTokError:
        pass
    return pend["campaign"]


def _token(campaign: str) -> str:
    a = _load(campaign)
    if not a.get("access_token"):
        raise TikTokError("This campaign's TikTok isn't connected. Press Connect TikTok in Settings.")
    if time.time() < a.get("expires_at", 0):
        return a["access_token"]
    if time.time() > a.get("refresh_expires_at", 0):
        raise TikTokError("This campaign's TikTok sign-in expired (after a year). Press Connect TikTok again.")
    cfg = app_config()
    t = _check(_req().post(f"{API}/v2/oauth/token/", data={
        "client_key": cfg["client_key"], "client_secret": cfg["client_secret"], "grant_type": "refresh_token",
        "refresh_token": a["refresh_token"]}, timeout=30), "Keeping TikTok signed in")
    now = time.time()
    a.update(access_token=t["access_token"], refresh_token=t.get("refresh_token") or a["refresh_token"],
             expires_at=now + int(t.get("expires_in", 86400)) - 300,
             refresh_expires_at=now + int(t.get("refresh_expires_in", 31536000)))
    _save(campaign, a)
    return a["access_token"]


def _post(path: str, campaign: str, body: dict, what: str, timeout: int = 60) -> dict:
    tok = _token(campaign)
    return _check(_req().post(f"{API}{path}", json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"}),
                  what).get("data") or {}


def creator_info(campaign: str) -> dict:
    """Who will post, and what TikTok allows right now (privacy choices, max length, comments/duet/stitch)."""
    return _post("/v2/post/publish/creator_info/query/", campaign, {}, "Reading the TikTok account")


def public_status(campaign: str) -> dict:
    a = _load(campaign)
    if not a.get("access_token"):
        return {"connected": False}
    return {"connected": True, "username": a.get("username", ""), "nickname": a.get("nickname", ""),
            "expired": time.time() > a.get("refresh_expires_at", 0)}


def _chunks(size: int) -> tuple[int, int]:
    """TikTok's rules: chunks of 5-64 MB, files up to 64 MB may go in one piece; the last chunk takes the remainder."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 10 * 1024 * 1024
    return chunk, size // chunk


def post_video(campaign: str, path: Path, caption: str, privacy: str, allow_comment: bool, allow_duet: bool,
               allow_stitch: bool, progress=lambda f: None) -> dict:
    """Direct Post: uploads the file from this laptop and publishes it to the campaign's TikTok."""
    path = Path(path)
    info = creator_info(campaign)
    opts = info.get("privacy_level_options") or []
    if privacy not in opts:
        raise TikTokError(f"Choose who can see this video (TikTok allows: {', '.join(opts) or 'nothing right now'}).")
    maxd = info.get("max_video_post_duration_sec")
    if maxd:
        try:
            dur = float(P.probe(path).get("duration") or 0)
        except Exception:  # noqa: BLE001
            dur = 0
        if dur and dur > maxd:
            raise TikTokError(f"This clip is {dur:.0f}s, longer than this account can post ({maxd}s).")
    size = path.stat().st_size
    chunk, count = _chunks(size)
    progress(0.05)
    d = _post("/v2/post/publish/video/init/", campaign, {
        "post_info": {"title": (caption or "")[:2200], "privacy_level": privacy,
                      "disable_comment": bool(info.get("comment_disabled")) or not allow_comment,
                      "disable_duet": bool(info.get("duet_disabled")) or not allow_duet,
                      "disable_stitch": bool(info.get("stitch_disabled")) or not allow_stitch,
                      "video_cover_timestamp_ms": 1000},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    }, "Starting the TikTok upload")
    pid, url = d["publish_id"], d["upload_url"]
    rq = _req()
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = rq.put(url, data=body, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)), "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"Uploading to TikTok failed (TikTok said {r.status_code}).")
            progress(0.1 + 0.6 * (i + 1) / count)
    for i in range(120):  # TikTok processes the video, usually under a minute
        st = _post("/v2/post/publish/status/fetch/", campaign, {"publish_id": pid}, "Checking the TikTok post", 30)
        s = st.get("status")
        if s == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
            user = _load(campaign).get("username", "")
            link = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else (f"https://www.tiktok.com/@{user}" if user else "https://www.tiktok.com/")
            return {"url": link, "publish_id": pid, "private": privacy == "SELF_ONLY"}
        if s == "FAILED":
            raise TikTokError(f"TikTok couldn't post the video: {st.get('fail_reason') or 'unknown reason'}.")
        progress(0.7 + 0.25 * min(1, i / 30))
        time.sleep(4)
    raise TikTokError("TikTok is still processing the video after 8 minutes. Check the account in a few minutes.")
