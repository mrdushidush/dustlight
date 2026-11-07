"""Store page media (PLAN.md R7), from the views of tools/scripts/media_views.json
(chosen by eye: the most detailed view of a map turned out to be gravel, and a daytime
pick is black at night) run by tour.ps1 on the release art:

  compare-<view>.jpg   1920x1080, pixel for pixel: the same part of the screen
                       with the original art and light (left) and in HD (right)
  shot-<view>.jpg      the whole HD screen

and index.html to choose from. A view counts only if all its checks passed and
its HD still shows the finished capture setup (report7.hd_still_after_setup).

Game-derived images: local until the author publishes them.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .report7 import hd_still_after_setup

SIDE_W, SIDE_H = 960, 1080
JPEG_QUALITY = 92


def view_ok(run: Path, name: str) -> bool:
    """All the view's checks passed, it ran some, and its HD still is from after the setup."""
    result = run / f"{name}.json"
    if not result.exists():
        return False
    r = json.loads(result.read_text(encoding="utf-8-sig"))
    if r.get("failed", 1) != 0 or r.get("checks", 0) == 0:
        return False
    return hd_still_after_setup(run, name) is not False and (run / f"{name}-hd.png").exists() and (run / f"{name}-orig.png").exists()


def _font(size: int):
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except OSError:
        return ImageFont.load_default()


def compare(orig: Image.Image, hd: Image.Image, crop_x: int | None, name: str) -> Image.Image:
    """The same part of the screen twice (the middle, unless the view gives its left edge),
    labelled, with a thin line between."""
    h = min(SIDE_H, hd.height)
    x = (hd.width - SIDE_W) // 2 if crop_x is None else crop_x
    x = max(0, min(x, hd.width - SIDE_W))
    box = (x, 0, x + SIDE_W, h)
    out = Image.new("RGB", (SIDE_W * 2, h))
    out.paste(orig.crop(box), (0, 0))
    out.paste(hd.crop(box), (SIDE_W, 0))
    draw = ImageDraw.Draw(out)
    draw.rectangle((SIDE_W - 1, 0, SIDE_W, h), fill=(0, 0, 0))
    font = _font(26)
    for label, left in (("Original", 0), (name, SIDE_W)):
        tw = draw.textlength(label, font=font)
        draw.rectangle((left + 14, 14, left + 34 + tw, 56), fill=(0, 0, 0))
        draw.text((left + 24, 20), label, fill=(235, 235, 235), font=font)
    return out


def build(run: Path, views_file: Path, out: Path, mod_name: str = "Dustlight", report=print) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    for v in json.loads(views_file.read_text(encoding="utf-8")):
        name = v["name"]
        if not view_ok(run, name):
            report(f"  {v['place']}: {name} has no passing result, left out")
            continue
        hd = Image.open(run / f"{name}-hd.png").convert("RGB")
        orig = Image.open(run / f"{name}-orig.png").convert("RGB")
        if hd.size != orig.size:
            report(f"  {name}: stills differ in size, left out")
            continue
        compare(orig, hd, v.get("crop_x"), mod_name).save(out / f"compare-{name}.jpg", quality=JPEG_QUALITY, subsampling=0)
        hd.save(out / f"shot-{name}.jpg", quality=JPEG_QUALITY, subsampling=0)
        cards.append({"place": v["place"], "view": name})
        report(f"  {v['place']}: {name}")
    parts = ["<!doctype html><meta charset=utf-8><title>Store page media</title>",
             "<style>body{background:#111;color:#ddd;font:15px system-ui;margin:24px}"
             "h2{font-size:17px;margin:30px 0 6px;color:#fff}img{max-width:100%;display:block}"
             "a{color:#8cf}p{color:#aaa;max-width:70ch}video{max-width:100%}</style>",
             "<h1>Store page media</h1>",
             f"<p>{len(cards)} places from tour run {html.escape(run.name)}. Each image: original on the left, {html.escape(mod_name)} on the right, "
             "pixel for pixel, the same part of the screen. Local only until you publish them.</p>"]
    if (out / "clip.mp4").exists():
        parts.append("<h2>The clip</h2><video controls src='clip.mp4'></video>")
    for i, c in enumerate(cards, 1):
        parts.append(f"<h2>{i}. {html.escape(c['place'])} <small>({html.escape(c['view'])})</small></h2>"
                     f"<img loading=lazy src='compare-{html.escape(c['view'])}.jpg'>"
                     f"<p><a href='shot-{html.escape(c['view'])}.jpg'>the whole HD screen</a></p>")
    page = out / "index.html"
    page.write_text("\n".join(parts), encoding="utf-8")
    return page
