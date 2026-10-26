"""Cloud sync: keeps campaigns, settings, accounts and the clip list the same on every computer.

Video files stay on the computer that made them (too big to sync for free); their records sync so captions,
posted links and Whop deadlines show everywhere.

Merging is three-way per item: we remember what each item looked like at the last sync (its hash), so we can
tell which side changed it. If only one side changed it, that side wins; if both did, the newest edit wins.
Deleting on one computer deletes everywhere.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

SYNC_URL = __import__("os").environ.get("CLIP_ENGINE_SYNC_URL") or "https://buildable-clip-factory.netlify.app/api/sync"

# Settings that belong to one computer and never sync
LOCAL_SETTINGS = {"cookies_browser", "cookies_file", "whisper_model", "max_height"}
# Clip fields that only make sense on the computer that made the clip
LOCAL_CLIP_FIELDS = {"words", "source_path"}


SECTIONS = ("campaigns", "clips", "settings", "accounts")


class SyncError(Exception):
    pass


def _h(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _call(payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(SYNC_URL, data=data, method="POST",
                                 headers={"content-type": "application/json", "user-agent": "ClipEngine"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if e.code == 409 and body.get("error") == "changed":
            raise _Changed()
        raise SyncError(body.get("error") or f"Sync server said {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SyncError("Can't reach the sync server. Check your internet connection.") from e


class _Changed(Exception):
    pass


# ------------------------------------------------------------------ merging
def merge_items(local: dict, remote: dict, base: dict, prefer_remote: bool = False) -> dict:
    """Three-way merge of {id: item}. base is {id: hash at last sync}.
    prefer_remote: first sync on a computer, so the account's copy wins over this computer's defaults."""
    out: dict = {}
    for k in set(local) | set(remote):
        l, r, b = local.get(k), remote.get(k), base.get(k)
        lh = _h(_strip_meta(l)) if l is not None else None
        rh = _h(_strip_meta(r)) if r is not None else None
        l_changed = l is not None and lh != b
        r_changed = r is not None and rh != b
        if l is not None and r is not None:
            if lh == rh:
                out[k] = l
            elif l_changed and not r_changed:
                out[k] = l
            elif r_changed and not l_changed:
                out[k] = r
            elif prefer_remote:
                out[k] = r
            else:
                out[k] = l if (l.get("_edited", 0) if isinstance(l, dict) else 0) >= (r.get("_edited", 0) if isinstance(r, dict) else 0) else r
        elif l is not None:  # not in the cloud
            if b is None or l_changed:
                out[k] = l  # new here, or edited here after another computer deleted it
            # else: deleted on another computer
        else:  # only in the cloud
            if b is None or r_changed:
                out[k] = r  # new from another computer
            # else: deleted here
    return out


def _strip_meta(item):
    if isinstance(item, dict):
        return {k: v for k, v in item.items() if k != "_edited"}
    return item


def stamp_edits(items: dict, base: dict, now: float):
    """Marks items changed here since the last sync with the time, so 'newest edit wins' works."""
    for k, v in items.items():
        if isinstance(v, dict) and _h(_strip_meta(v)) != base.get(k):
            v["_edited"] = now


# ------------------------------------------------------------------ the sync itself
class Sync:
    def __init__(self, state_path: Path):
        self.path = state_path
        self.st = {}
        try:
            self.st = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.st = {}
        self.last_ok = 0.0
        self.last_error = ""
        self.busy = False

    # state file: {email, token, rev, base: {section: {id: hash}}}
    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def signed_in(self) -> bool:
        return bool(self.st.get("token"))

    def status(self) -> dict:
        return {"signed_in": self.signed_in, "email": self.st.get("email", ""), "last_ok": self.last_ok,
                "error": self.last_error, "busy": self.busy}

    def login(self, email: str, password: str, create: bool):
        r = _call({"action": "signup" if create else "login", "email": email, "password": password})
        self.st = {"email": email.strip().lower(), "token": r["token"], "rev": 0, "base": {}, "first": True}
        self.save()

    def logout(self):
        tok = self.st.get("token")
        self.st = {}
        self.save()
        if tok:
            try:
                _call({"action": "logout", "token": tok}, timeout=10)
            except Exception:  # noqa: BLE001
                pass

    def run(self, snapshot, apply):
        """snapshot() -> deep copy of the local sections; apply(merged, snap) writes the merge back,
        keeping anything changed locally while the sync was talking to the server."""
        if not self.signed_in:
            return
        self.busy = True
        try:
            for _ in range(4):
                pulled = _call({"action": "pull", "token": self.st["token"]})
                remote = pulled.get("data") or {}
                rev = pulled.get("rev", 0)
                now = time.time()
                first = bool(self.st.get("first"))
                base = {} if first else self.st.get("base", {})
                merged = {}
                local = snapshot()  # deep copy, safe to change
                snap = json.loads(json.dumps(local))
                for sec in SECTIONS:
                    lb = base.get(sec, {})
                    stamp_edits(local.get(sec, {}), lb, now)
                    merged[sec] = merge_items(local.get(sec, {}), remote.get(sec, {}), lb, prefer_remote=first)
                if _h(merged) == _h({sec: remote.get(sec, {}) for sec in SECTIONS}):
                    new_rev = rev  # the cloud already has exactly this: nothing to upload
                else:
                    payload = {**merged, "saved": now}
                    try:
                        new_rev = _call({"action": "push", "token": self.st["token"], "rev": rev, "data": payload}, timeout=60)["rev"]
                    except _Changed:
                        continue  # another computer synced in between: pull again and re-merge
                apply(merged, snap)
                self.st["rev"] = new_rev
                self.st["base"] = {sec: {k: _h(_strip_meta(v)) for k, v in merged[sec].items()} for sec in merged}
                self.st.pop("first", None)
                self.save()
                self.last_ok = time.time()
                self.last_error = ""
                return
            raise SyncError("Another computer kept syncing at the same moment. It'll try again shortly.")
        except SyncError as e:
            self.last_error = str(e)
            if "Signed out" in str(e) or "Not signed in" in str(e):
                self.st = {}
                self.save()
            raise
        finally:
            self.busy = False
