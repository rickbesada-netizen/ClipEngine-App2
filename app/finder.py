"""Campaign finder: reads the public Content Rewards directory (contentrewards.com/discover, the Whop clipping
campaign list) the same way a browser does, one page every few hours. No login, nothing posted, nothing joined:
joining stays a click by the user on Whop.

The page is a Next.js page; the campaign list is in its embedded data (self.__next_f), each item shaped
{"campaign": {...}, "metrics": {...}}.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

DISCOVER_URL = "https://contentrewards.com/discover"
CAMPAIGN_URL = "https://contentrewards.com/c/campaigns/{id}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 ClipEngine"

PLATFORM_NAMES = {"instagram": "Instagram Reels", "tiktok": "TikTok", "youtube": "YouTube Shorts", "x": "X", "twitter": "X"}


def _flight(html: str) -> str:
    out = []
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            a = json.loads(m.group(1))
        except ValueError:
            continue
        if len(a) > 1 and isinstance(a[1], str):
            out.append(a[1])
    return "".join(out)


def _money(s) -> str:
    return str(s or "").replace("$$", "$")


def _num(s) -> float | None:
    m = re.search(r"[\d.]+", str(s or "").replace(",", ""))
    return float(m.group(0)) if m else None


def parse(html: str) -> list[dict]:
    f = _flight(html)
    dec = json.JSONDecoder()
    seen: dict = {}
    for m in re.finditer(r'\{"campaign":\{', f):
        try:
            obj, _ = dec.raw_decode(f, m.start())
        except ValueError:
            continue
        c = obj.get("campaign") or {}
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        met = obj.get("metrics") or {}
        kind = c.get("type") or ""
        rate_label = _money(c.get("ratePer1kLabel"))
        seen[cid] = {
            "id": cid,
            "title": _money(c.get("title")).strip(),
            "brand": c.get("brand") or "",
            "category": c.get("category") or "",
            "type": kind,
            "rate": _num(rate_label) if kind == "cpm" else None,
            "rate_label": rate_label,
            "budget_total": c.get("budgetTotalRaw"),
            "budget_left": c.get("availableBudgetRaw"),
            "progress": c.get("progressPercentage") or 0,
            "platforms": [p for p in (c.get("platforms") or []) if isinstance(p, str)],
            "requires_application": bool(c.get("requiresApplication")),
            "verified": bool(c.get("isVerified")),
            "creators": c.get("creatorCountRaw") or met.get("creatorCount"),
            "description": (c.get("description") or "").strip(),
            "thumbnail": c.get("thumbnail") or "",
            "created_ms": c.get("createdAtMs"),
            "total_views": met.get("totalViews"),
            "url": CAMPAIGN_URL.format(id=cid),
        }
    return list(seen.values())


def fetch(timeout: int = 30) -> list[dict]:
    req = urllib.request.Request(DISCOVER_URL, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    items = parse(html)
    if not items:
        raise RuntimeError("The Content Rewards page loaded but no campaigns were found in it. Its layout may have changed.")
    return items


def clean_name(title: str, brand: str = "") -> str:
    """'TJR $23,100 Weekly Clipping Campaign' -> 'TJR'; '$10,000 Kai Cenat Campaign' -> 'Kai Cenat'."""
    t = re.split(r"\s+\|\s+", title)[0]
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    t = re.sub(r"\$[\d,.]+(?:\s*k\b)?", " ", t, flags=re.I)
    t = re.sub(r"\b(\d+\s*day|weekly|monthly|official|clipping|clip|campaign|ugc|october|november|december|v\d)\b", " ", t, flags=re.I)
    t = re.sub(r"\s*[|:–-]\s*$", "", re.sub(r"\s+", " ", t)).strip(" -|:")
    return (t or title or brand)[:40].strip(" ,-|")


def brief_text(item: dict) -> str:
    """Text for the campaign reader: title, pay and platforms first, then the description."""
    plats = ", ".join(PLATFORM_NAMES.get(p, p) for p in item.get("platforms", []))
    pay = f"{item['rate_label']} per 1K views" if item.get("rate_label") else ""
    return "\n".join(x for x in [
        f"{clean_name(item['title'], item.get('brand', ''))} campaign",
        item["title"],
        f"Run by {item['brand']}" if item.get("brand") else "",
        f"Pay: {pay}" if pay else "",
        f"Budget: ${item['budget_total']:,.0f}" if isinstance(item.get("budget_total"), (int, float)) else "",
        f"Platforms: {plats}" if plats else "",
        f"Category: {item['category']}" if item.get("category") else "",
        "",
        item.get("description", ""),
    ] if x is not None)


def rules_links(item: dict) -> list[str]:
    """Links in the description that are probably the rules page (not social profiles)."""
    out = []
    for u in re.findall(r"https?://[^\s\"'<>)]+", item.get("description", "")):
        u = u.rstrip(".,")
        if re.search(r"(youtube|youtu\.be|tiktok|instagram|x\.com|twitter|kick\.com|twitch|discord)", u, re.I):
            continue
        out.append(u)
    return out[:2]


def now() -> float:
    return time.time()
