"""Campaign finder: reads the public Content Rewards directory (contentrewards.com/discover, the Whop clipping
campaign list) the same way a browser does, one page every few hours. No login, nothing posted, nothing joined:
joining stays a click by the user on Whop.

Sources: the discover lists plus every agency's own page (contentrewards.com/<agency>), which lists all of
that agency's live campaigns. Agencies come from the site's sitemap and from the campaigns themselves.
The pages are Next.js pages; the campaign data is in their embedded data (self.__next_f).
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


def _item(c: dict, met: dict | None = None) -> dict | None:
    cid = c.get("id")
    if not cid or "availableBudgetRaw" not in c and "title" not in c:
        return None
    met = met or {}
    kind = c.get("type") or ""
    rate_label = _money(c.get("ratePer1kLabel"))
    return {
        "id": cid,
        "title": _money(c.get("title")).strip(),
        "brand": c.get("brand") or "",
        "agency": c.get("organizationProfileHandle") or "",
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
        "opens_at": c.get("opensAt"),
        "total_views": met.get("totalViews"),
        "url": CAMPAIGN_URL.format(id=cid),
    }


def parse(html: str) -> list[dict]:
    """Campaigns on a Content Rewards page: the discover list ({"campaign": {...}, "metrics": {...}})
    and agency pages (initialPage.campaigns: [{...}])."""
    f = _flight(html)
    dec = json.JSONDecoder()
    seen: dict = {}
    for m in re.finditer(r'\{"campaign":\{|\{"availableBudgetRaw":', f):
        try:
            obj, _ = dec.raw_decode(f, m.start())
        except ValueError:
            continue
        if "campaign" in obj:
            it = _item(obj.get("campaign") or {}, obj.get("metrics"))
        else:
            it = _item(obj)
        if it and (it["id"] not in seen or len(it.get("description", "")) > len(seen[it["id"]].get("description", ""))):
            seen[it["id"]] = it
    return list(seen.values())


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


SITE = "https://contentrewards.com"
LISTS = [SITE + "/discover", SITE + "/c/discover/clipping", SITE + "/c/discover/ugc"]
NOT_AGENCIES = {"articles", "pricing", "creators", "agencies", "launch-a-campaign", "cpm-calculator", "book-a-demo",
                "careers", "discord", "branding", "terms", "privacy-policy", "brands-terms", "ftc-compliance", "copyright",
                "c", "discover", "api", "home", "onboarding", "partners", "refer", "dev", "confirmation", "session-profile"}


def agencies_from_sitemap() -> list[str]:
    xml = _get(SITE + "/sitemap.xml")
    out = []
    for loc in re.findall(r"<loc>(.*?)</loc>", xml):
        m = re.match(r"https://contentrewards\.com/([\w-]+)/?$", loc.strip())
        if m and m.group(1).lower() not in NOT_AGENCIES:
            out.append(m.group(1))
    return out


def fetch_all(progress=None, known_agencies=(), pause: float = 1.0) -> tuple[list[dict], list[str]]:
    """Every live campaign we can see: the main lists, then each agency's own page (which lists all its campaigns).
    One page at a time with a short pause, like a person clicking through. Returns (campaigns, agencies)."""
    found: dict = {}
    errors = []

    def take(url):
        try:
            for it in parse(_get(url)):
                if it["id"] not in found or len(it.get("description", "")) > len(found[it["id"]].get("description", "")):
                    found[it["id"]] = it
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {str(e)[:80]}")

    for u in LISTS:
        take(u)
        time.sleep(pause)
    agencies = list(dict.fromkeys(list(known_agencies) + [it["agency"] for it in found.values() if it.get("agency")]))
    try:
        agencies = list(dict.fromkeys(agencies + agencies_from_sitemap()))
    except Exception as e:  # noqa: BLE001
        errors.append(f"sitemap: {str(e)[:80]}")
    done = set()
    i = 0
    while i < len(agencies):  # agency pages can reveal more agencies via their campaigns
        a = agencies[i]
        i += 1
        if a.lower() in done or a.lower() in NOT_AGENCIES:
            continue
        done.add(a.lower())
        if progress:
            progress(f"Checking {a} ({len(done)} of {len(agencies)})")
        take(f"{SITE}/{a}")
        for it in found.values():
            if it.get("agency") and it["agency"] not in agencies:
                agencies.append(it["agency"])
        time.sleep(pause)
    if not found:
        raise RuntimeError("Couldn't read any campaigns from Content Rewards. " + (errors[0] if errors else "Its layout may have changed."))
    return list(found.values()), sorted(done)


def fetch(timeout: int = 30) -> list[dict]:
    return parse(_get(DISCOVER_URL, timeout)) or []


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
