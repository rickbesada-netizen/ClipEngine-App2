"""Instagram Reels and Facebook Page Reels, one Facebook/Instagram account per campaign.

Uses the user's own free Meta developer app (App ID + App Secret, stored only on this computer). Each campaign
signs in with Facebook (the client's account), picks the Facebook Page, and the Instagram professional account
linked to that Page comes with it. Videos are uploaded straight from this computer (resumable upload), so no
file ever goes through a website.

Meta only lets an app in Development mode act for people who have a role on it, so each client's Facebook
account is added under App roles once (the setup steps in Settings say how).
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.parse
from pathlib import Path

import pipeline as P

GRAPH = __import__("os").environ.get("CLIP_ENGINE_GRAPH") or "https://graph.facebook.com/v24.0"
DIALOG = "https://www.facebook.com/v24.0/dialog/oauth"
RELAY = "https://buildable-clip-factory.netlify.app/oauth/meta.html"  # forwards Meta's answer back to this computer
SCOPES = ["email", "pages_show_list", "pages_read_engagement", "pages_manage_posts", "instagram_basic",
          "instagram_content_publish", "business_management"]
CONFIG = P.DATA / "meta_app.json"
ACCOUNTS = P.DATA / "meta_accounts"
_PENDING: dict = {}  # state nonce -> campaign id


class MetaError(Exception):
    pass


def _req():
    import requests
    return requests


def _check(r, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        msg = err.get("error_user_msg") or err.get("message") or r.text[:200]
        code = err.get("code")
        hint = ""
        if code in (190, 102):
            hint = " The sign-in expired: press Connect again for this campaign."
        elif code in (10, 200) or "permission" in str(msg).lower():
            hint = (" Meta says the app isn't allowed to do this yet. Check the campaign's Facebook account has a role on your "
                    "Meta app (App roles) and that you ticked every permission when signing in.")
        elif "not a business" in str(msg).lower() or "professional" in str(msg).lower():
            hint = " The Instagram account must be a Professional (Business or Creator) account linked to the Facebook Page."
        raise MetaError(f"{what} failed: {msg}.{hint}")
    return data


# ---------------------------------------------------------------- app settings
def app_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_app(app_id: str, app_secret: str):
    app_id, app_secret = (app_id or "").strip(), (app_secret or "").strip()
    if not re.fullmatch(r"\d{6,20}", app_id):
        raise MetaError("The App ID is the long number at the top of your app in developers.facebook.com.")
    if not re.fullmatch(r"[0-9a-f]{32}", app_secret):
        raise MetaError("The App Secret is 32 letters and numbers (App settings > Basic > App secret > Show).")
    CONFIG.write_text(json.dumps({"app_id": app_id, "app_secret": app_secret}), encoding="utf-8")


# ---------------------------------------------------------------- per-campaign accounts
def _acct_path(campaign: str) -> Path:
    return ACCOUNTS / (re.sub(r"[^a-z0-9_-]", "", campaign.lower())[:40] + ".json")


def account(campaign: str) -> dict:
    try:
        return json.loads(_acct_path(campaign).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_account(campaign: str, data: dict):
    ACCOUNTS.mkdir(parents=True, exist_ok=True)
    _acct_path(campaign).write_text(json.dumps(data), encoding="utf-8")


def disconnect(campaign: str):
    _acct_path(campaign).unlink(missing_ok=True)


def public_status(campaign: str) -> dict:
    a = account(campaign)
    page = a.get("page") or {}
    return {
        "connected": bool(page.get("token")),
        "user": a.get("user_name", ""),
        "email": a.get("user_email", ""),
        "page": page.get("name", ""),
        "instagram": (page.get("ig") or {}).get("username", ""),
        "choices": [{"id": p["id"], "name": p["name"], "instagram": (p.get("ig") or {}).get("username", "")}
                    for p in a.get("pages", [])] if not page and a.get("pages") else [],
    }


def login_url(campaign: str, port: int) -> str:
    cfg = app_config()
    if not cfg.get("app_id"):
        raise MetaError("Add your Meta App ID and App Secret first.")
    nonce = secrets.token_urlsafe(16)
    _PENDING[nonce] = {"campaign": campaign, "at": time.time()}
    state = f"{port}.{nonce}"
    q = {"client_id": cfg["app_id"], "redirect_uri": RELAY, "state": state, "response_type": "code",
         "scope": ",".join(SCOPES), "auth_type": "rerequest"}
    return DIALOG + "?" + urllib.parse.urlencode(q)


def finish_login(code: str, state: str) -> str:
    """Called when Meta sends the user back. Returns the campaign id."""
    nonce = state.split(".", 1)[-1]
    pend = _PENDING.pop(nonce, None)
    if not pend or time.time() - pend["at"] > 1800:
        raise MetaError("That sign-in link expired. Press Connect again in Clip Engine.")
    cfg = app_config()
    rq = _req()
    tok = _check(rq.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": cfg["app_id"], "client_secret": cfg["app_secret"], "redirect_uri": RELAY, "code": code}, timeout=30),
        "Signing in")["access_token"]
    long_tok = _check(rq.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": cfg["app_id"], "client_secret": cfg["app_secret"],
        "fb_exchange_token": tok}, timeout=30), "Keeping you signed in").get("access_token", tok)
    me = _check(rq.get(f"{GRAPH}/me", params={"fields": "id,name,email", "access_token": long_tok}, timeout=30), "Reading your account")
    pages = _check(rq.get(f"{GRAPH}/me/accounts", params={
        "fields": "id,name,access_token,instagram_business_account{id,username}", "limit": 100,
        "access_token": long_tok}, timeout=30), "Listing your Facebook Pages").get("data", [])
    plist = [{"id": p["id"], "name": p.get("name", ""), "token": p.get("access_token", ""),
              "ig": p.get("instagram_business_account") or {}} for p in pages]
    data = {"user_id": me.get("id"), "user_name": me.get("name", ""), "user_email": me.get("email", ""), "user_token": long_tok, "pages": plist,
            "connected_at": time.time()}
    if len(plist) == 1:
        data["page"] = plist[0]
    _save_account(pend["campaign"], data)
    if not plist:
        raise MetaError("Signed in, but that Facebook account has no Pages it can post to (or you unticked them). "
                        "Instagram posting also needs the Instagram account linked to a Facebook Page.")
    return pend["campaign"]


def choose_page(campaign: str, page_id: str):
    a = account(campaign)
    for p in a.get("pages", []):
        if p["id"] == page_id:
            a["page"] = p
            _save_account(campaign, a)
            return
    raise MetaError("That Page isn't on this account any more. Press Connect again.")


# ---------------------------------------------------------------- posting
def _upload_binary(url: str, token: str, path: Path, progress):
    """Meta's resumable upload: send the file in one stream with offset 0."""
    rq = _req()
    size = path.stat().st_size

    def gen():
        sent = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                sent += len(chunk)
                progress(0.1 + 0.6 * sent / size)
                yield chunk

    r = rq.post(url, data=gen(), timeout=600, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"})
    return _check(r, "Uploading the video")


def post_instagram(campaign: str, path: Path, caption: str, progress) -> dict:
    a = account(campaign)
    page = a.get("page") or {}
    ig = (page.get("ig") or {}).get("id")
    if not ig:
        raise MetaError("This campaign's Facebook Page has no Instagram professional account linked. In Instagram: "
                        "Settings > Account type > switch to Professional, then link it to the Facebook Page, then press Connect again.")
    tok = page["token"]
    rq = _req()
    progress(0.05)
    c = _check(rq.post(f"{GRAPH}/{ig}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                                                    "share_to_feed": "true", "access_token": tok}, timeout=60),
               "Starting the Instagram upload")
    _upload_binary(c["uri"], tok, path, progress)
    for i in range(120):  # Instagram processes the video, usually under a minute
        st = _check(rq.get(f"{GRAPH}/{c['id']}", params={"fields": "status_code,status", "access_token": tok}, timeout=30),
                    "Checking the upload")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise MetaError(f"Instagram couldn't process the video: {st.get('status', 'unknown error')}")
        progress(0.7 + 0.25 * min(1, i / 40))
        time.sleep(5)
    else:
        raise MetaError("Instagram is still processing the video after 10 minutes. Check the account in a few minutes.")
    pub = _check(rq.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": c["id"], "access_token": tok}, timeout=60),
                 "Publishing on Instagram")
    link = ""
    try:
        link = _check(rq.get(f"{GRAPH}/{pub['id']}", params={"fields": "permalink", "access_token": tok}, timeout=30),
                      "Getting the link").get("permalink", "")
    except MetaError:
        pass
    progress(1.0)
    return {"id": pub["id"], "url": link or f"https://www.instagram.com/{(page.get('ig') or {}).get('username', '')}/",
            "account": (page.get("ig") or {}).get("username", "")}


def post_facebook(campaign: str, path: Path, caption: str, progress) -> dict:
    a = account(campaign)
    page = a.get("page") or {}
    if not page.get("token"):
        raise MetaError("Connect this campaign's Facebook Page first (Settings).")
    tok, pid = page["token"], page["id"]
    rq = _req()
    progress(0.05)
    st = _check(rq.post(f"{GRAPH}/{pid}/video_reels", data={"upload_phase": "start", "access_token": tok}, timeout=60),
                "Starting the Facebook upload")
    _upload_binary(st["upload_url"], tok, path, progress)
    _check(rq.post(f"{GRAPH}/{pid}/video_reels", data={"upload_phase": "finish", "video_id": st["video_id"],
                                                       "video_state": "PUBLISHED", "description": caption[:2200],
                                                       "access_token": tok}, timeout=60), "Publishing on Facebook")
    progress(1.0)
    return {"id": st["video_id"], "url": f"https://www.facebook.com/reel/{st['video_id']}", "account": page.get("name", "")}
