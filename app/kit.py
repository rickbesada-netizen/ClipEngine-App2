"""Channel kit: profile pictures, YouTube banner and per-platform text for a campaign's clip accounts."""
from __future__ import annotations

import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import pipeline as P

KIT_DIR = P.DATA / "kit"
FONT = P.FONT_DIR / "Poppins-Bold.ttf"

PALETTES = [  # background, background 2, text, accent
    ("#0E1116", "#1C2430", "#FFFFFF", "#F5C518"),
    ("#0B3D2E", "#13261F", "#FFFFFF", "#3DDC84"),
    ("#1A1036", "#3B1C6E", "#FFFFFF", "#FF4FA3"),
    ("#F5C518", "#FFB000", "#111111", "#111111"),
    ("#0A2540", "#114B7A", "#FFFFFF", "#5CC8FF"),
    ("#2B0A0A", "#6E1414", "#FFFFFF", "#FF6B3D"),
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _gradient(w: int, h: int, c1: str, c2: str, diagonal=True) -> Image.Image:
    a, b = _hex(c1), _hex(c2)
    base = Image.new("RGB", (w, h), a)
    top = Image.new("RGB", (w, h), b)
    mask = Image.linear_gradient("L").resize((w, h))
    if diagonal:
        mask = mask.rotate(45, expand=False, fillcolor=128).resize((w, h))
    return Image.composite(top, base, mask)


def _fit_text(draw, text, max_w, start, min_size=20):
    size = start
    while size > min_size:
        f = _font(size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(min_size)


def _short(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    if len(name) <= 6:
        return name.upper()
    if len(words) >= 2:
        return "".join(w[0] for w in words[:3]).upper()
    return name[:5].upper()


def avatars(camp: dict, display: str, seed: int = 0) -> list[Path]:
    out_dir = KIT_DIR / camp["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    pals = PALETTES[:]
    rnd.shuffle(pals)
    word = _short(camp.get("name", display))
    S = 800
    paths = []
    # 1: bold initials + CLIPS ribbon
    bg, bg2, fg, ac = pals[0]
    im = _gradient(S, S, bg, bg2)
    d = ImageDraw.Draw(im)
    f = _fit_text(d, word, S * 0.72, 300)
    d.text((S / 2, S * 0.44), word, font=f, fill=fg, anchor="mm")
    d.rounded_rectangle((S * 0.22, S * 0.62, S * 0.78, S * 0.76), radius=30, fill=ac)
    d.text((S / 2, S * 0.69), "CLIPS", font=_font(70), fill=bg if ac != fg else "#FFFFFF", anchor="mm")
    paths.append(out_dir / f"avatar-1-{seed}.png"); im.save(paths[-1])
    # 2: ring with play triangle
    bg, bg2, fg, ac = pals[1]
    im = _gradient(S, S, bg, bg2, diagonal=False)
    d = ImageDraw.Draw(im)
    d.ellipse((60, 60, S - 60, S - 60), outline=ac, width=34)
    tri = [(S * 0.43, S * 0.24), (S * 0.43, S * 0.40), (S * 0.57, S * 0.32)]
    d.polygon(tri, fill=ac)
    f = _fit_text(d, word, S * 0.62, 230)
    d.text((S / 2, S * 0.58), word, font=f, fill=fg, anchor="mm")
    d.text((S / 2, S * 0.74), "DAILY", font=_font(56), fill=ac, anchor="mm")
    paths.append(out_dir / f"avatar-2-{seed}.png"); im.save(paths[-1])
    # 3: split block
    bg, bg2, fg, ac = pals[2]
    im = Image.new("RGB", (S, S), _hex(bg))
    d = ImageDraw.Draw(im)
    d.rectangle((0, S * 0.58, S, S), fill=ac)
    f = _fit_text(d, word, S * 0.76, 280)
    d.text((S / 2, S * 0.33), word, font=f, fill=fg, anchor="mm")
    d.text((S / 2, S * 0.75), "MOMENTS", font=_fit_text(d, "MOMENTS", S * 0.7, 96), fill=bg, anchor="mm")
    paths.append(out_dir / f"avatar-3-{seed}.png"); im.save(paths[-1])
    # 4: minimal monogram with stripes
    bg, bg2, fg, ac = pals[3]
    im = _gradient(S, S, bg, bg2)
    d = ImageDraw.Draw(im, "RGBA")
    for x in range(-S, S * 2, 70):
        d.line([(x, 0), (x + S, S)], fill=(255, 255, 255, 18), width=24)
    f = _fit_text(d, word, S * 0.7, 320)
    d.text((S / 2 + 8, S / 2 + 10), word, font=f, fill=(0, 0, 0, 120), anchor="mm")
    d.text((S / 2, S / 2), word, font=f, fill=fg, anchor="mm")
    paths.append(out_dir / f"avatar-4-{seed}.png"); im.save(paths[-1])
    return paths


def avatar_from_clip(camp: dict, clip_path: Path, at: float = 1.5, seed: int = 0) -> Path:
    """Face-centred square from one of your rendered clips, with a small CLIPS label."""
    import subprocess
    import cv2

    out_dir = KIT_DIR / camp["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_png = out_dir / f"frame-{seed}.png"
    subprocess.run([P.FFMPEG, "-y", "-ss", f"{at:.2f}", "-i", str(clip_path), "-frames:v", "1", str(frame_png)],
                   capture_output=True, creationflags=P._NO_WINDOW)
    im = Image.open(frame_png).convert("RGB")
    W, H = im.size
    cx, cy, side = W / 2, H * 0.35, min(W, H) * 0.8
    try:
        gray = cv2.cvtColor(cv2.imread(str(frame_png)), cv2.COLOR_BGR2GRAY)
        casc = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        faces = casc.detectMultiScale(gray, 1.15, 6, minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx, cy, side = x + w / 2, y + h / 2, min(min(W, H), w * 2.6)
    except Exception:  # noqa: BLE001
        pass
    l = max(0, min(W - side, cx - side / 2)); t = max(0, min(H - side, cy - side / 2))
    sq = im.crop((int(l), int(t), int(l + side), int(t + side))).resize((800, 800), Image.LANCZOS)
    d = ImageDraw.Draw(sq)
    bg, _, fg, ac = PALETTES[seed % len(PALETTES)]
    d.rounded_rectangle((250, 640, 550, 730), radius=26, fill=ac)
    d.text((400, 685), "CLIPS", font=_font(62), fill=bg if ac != fg else "#FFFFFF", anchor="mm")
    out = out_dir / f"avatar-clip-{seed}.png"
    sq.save(out)
    frame_png.unlink(missing_ok=True)
    return out


def banners(camp: dict, display: str, seed: int = 0) -> list[Path]:
    """2560x1440 YouTube banners; text kept inside the 1546x423 area that shows on every device."""
    out_dir = KIT_DIR / camp["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed + 7)
    pals = PALETTES[:]
    rnd.shuffle(pals)
    W, H, SW, SH = 2560, 1440, 1546, 423
    x0, y0 = (W - SW) / 2, (H - SH) / 2
    niche = camp.get("niche") or "Best moments, quotes and funny clips"
    paths = []
    for k, (bg, bg2, fg, ac) in enumerate(pals[:2]):
        im = _gradient(W, H, bg, bg2)
        d = ImageDraw.Draw(im, "RGBA")
        if k == 0:
            for x in range(-H, W + H, 120):
                d.line([(x, 0), (x + H, H)], fill=(255, 255, 255, 14), width=40)
        else:
            for r in range(300, 1600, 160):
                d.ellipse((W / 2 - r, H / 2 - r, W / 2 + r, H / 2 + r), outline=(255, 255, 255, 16), width=6)
        f = _fit_text(d, display.upper(), SW * 0.95, 190)
        d.text((W / 2, y0 + SH * 0.36), display.upper(), font=f, fill=fg, anchor="mm")
        sub = f"{niche.upper()}  ·  NEW CLIPS DAILY"
        fs = _fit_text(d, sub, SW * 0.9, 56)
        tw = d.textlength(sub, font=fs)
        d.rounded_rectangle((W / 2 - tw / 2 - 30, y0 + SH * 0.70, W / 2 + tw / 2 + 30, y0 + SH * 0.70 + 92), radius=46, fill=ac)
        d.text((W / 2, y0 + SH * 0.70 + 46), sub, font=fs, fill=bg if ac != fg else "#FFFFFF", anchor="mm")
        p = out_dir / f"banner-{k + 1}-{seed}.png"
        im.save(p, optimize=True)
        paths.append(p)
    return paths


def texts(camp: dict, display: str, handle: str) -> dict:
    name = camp.get("name") or display
    niche = camp.get("niche") or "Best moments, quotes and funny clips"
    req = " ".join(camp.get("hashtags", []))
    yt = (f"{display} posts the best moments from {name} every day: {niche.lower()}.\n\n"
          f"Short clips of the funniest, most memorable and most quotable parts, so you don't have to watch hours of content to catch the good bits.\n\n"
          f"This is a fan clip channel and isn't run by or affiliated with {name}. All credit to {name}.\n\n"
          f"New Shorts daily. Subscribe so you don't miss one.").strip()
    tiktok = f"Daily {name} clips 🎬 {niche.split(',')[0]} · fan page"
    if len(tiktok) > 80:
        tiktok = f"Daily {name} clips 🎬 fan page"
    insta = f"Daily clips of {name} 🎬\n{niche}\nFan page · not affiliated"
    words = [name, f"{name} clips", f"{name} shorts", f"{name} moments", f"best of {name}"] + [t.lstrip("#") for t in camp.get("extra_tags", [])] + camp.get("keywords", [])[:6]
    keywords = ", ".join(dict.fromkeys(w for w in words if w))[:480]
    return {
        "display": display, "handle": handle,
        "youtube_description": yt[:1000], "tiktok_bio": tiktok[:80], "instagram_bio": insta[:150],
        "youtube_keywords": keywords, "caption_hashtags": req,
        "limits": {"youtube_description": 1000, "tiktok_bio": 80, "instagram_bio": 150},
    }
