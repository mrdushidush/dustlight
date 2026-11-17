"""The eye, E2 trial: defect patrol over a tour run's stills with one model (the champion).

Each view's map area (3438x1140 of a 1440 px still) is cut into the 4 x 2 windows of 880x600 the answer
key uses; each window is asked the defect question next to its original-art twin (eye.ask_defects), through
eye_contest.run_model (the author's saved config, exact weights checked, resumable answers).

E1 showed the champion flags every crop (~4 boxes each) but points at clear defects (box centre on 83% of
clear planted ones against 17% by luck), so its flags are leads for Claude to check by eye, most severe first:
`sheets()` draws them on the crops, original left, HD right.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import eye_contest, eye_key
from .eye_key import CROP_H, CROP_W, MAP_H

KIND_COLOURS = {
    "seam": (255, 70, 70), "hole": (255, 0, 255), "lowres": (0, 200, 255), "outline": (255, 160, 0),
    "specks": (255, 255, 0), "smear": (120, 255, 120), "invented": (255, 120, 200), "shadow": (160, 120, 255),
    "color": (255, 255, 255), "other": (180, 180, 180),
}


# What draws each map pixel last: the map self-test's `<view>-fail-<pair>-kind.png` (1x, gray = code * 16;
# map_layer.cc pixelKinds), in code order.
PIXEL_KINDS = ("none", "floor", "roof", "wall", "scenery", "item", "critter", "misc", "interface", "translucent",
               "text", "outline")
# Drawn from the original pixels or moving on purpose, so flags there are noise: characters (edged from their own
# pixels by the author's pick of 11-06), exit grids (checkerboard kept), the target marker, text and outlines (CE's own
# pixels). A flag whose box lies mostly on these (grown by SKIP_GROW 1x pixels for edges) is dropped.
SKIP_KINDS = ("critter", "misc", "interface", "text", "outline")
SKIP_SHARE = 0.5
SKIP_GROW = 2


def windows(width: int, height: int) -> list[tuple[int, int]]:
    xs = [round(i * (width - CROP_W) / 3) for i in range(4)]
    return [(x, y) for y in (0, height - CROP_H) for x in xs]


def build_items(run: Path, out: Path, pair: str | None = None) -> list[dict]:
    """The crops of every view (written once), as items run_model can answer.

    pair: a map self-test label (e.g. "auto-000"): crop its same-frame images (<view>-fail-<pair>-hd.png, the HD
    map layer before the post effects, and -orig.png, CE's own drawing; map area only) instead of the two stills,
    which are seconds apart (critters walk, text floats) and carry the film grain. Views without the pair are left
    out. A pair with a kind map (`-kind.png`, builds since 2026-11-13) gives each crop a skip mask ("skip": a PNG,
    255 where the pixel is one of SKIP_KINDS) for drop_skipped()."""
    items = []
    views = sorted(p.name[: -len("-hd.png")] for p in run.glob("*-hd.png") if "-fail-" not in p.name)
    tail = f"-fail-{pair}" if pair else ""
    for view in views:
        if pair and not all((run / f"{view}{tail}-{k}.png").exists() for k in ("hd", "orig")):
            continue
        kind_path = run / f"{view}{tail}-kind.png"
        has_kind = bool(pair) and kind_path.exists()
        hd = orig = skip = None
        for x, y in windows(3438, MAP_H):
            stem = f"{view}-{x}-{y}"
            paths = {"orig": out / f"{stem}-orig.png", "hd": out / f"{stem}-hd.png"}
            skip_path = out / f"{stem}-skip.png"
            if not all(p.exists() for p in paths.values()) or (has_kind and not skip_path.exists()):
                if hd is None:
                    hd = np.asarray(Image.open(run / f"{view}{tail}-hd.png").convert("RGB"))[:MAP_H]
                    orig = np.asarray(Image.open(run / f"{view}{tail}-orig.png").convert("RGB"))[:MAP_H]
                    if has_kind:
                        skip = skip_mask(kind_path, hd.shape[1], hd.shape[0])
                out.mkdir(parents=True, exist_ok=True)
                Image.fromarray(orig[y : y + CROP_H, x : x + CROP_W]).save(paths["orig"], compress_level=3)
                Image.fromarray(hd[y : y + CROP_H, x : x + CROP_W]).save(paths["hd"], compress_level=3)
                if has_kind:
                    Image.fromarray(skip[y : y + CROP_H, x : x + CROP_W]).save(skip_path, compress_level=3)
            item = {
                "id": stem, "task": "defects", "source": "patrol", "view": view, "x": x, "y": y,
                "images": {k: str(v) for k, v in paths.items()}, "size": [CROP_W, CROP_H], "core": True,
                "sha1": {k: eye_key.sha1_file(v) for k, v in paths.items()},
            }
            if has_kind:
                item["skip"] = str(skip_path)
            items.append(item)
    return items


def skip_mask(kind_path: Path, width: int, height: int) -> np.ndarray:
    """The kind map's SKIP_KINDS pixels, grown by SKIP_GROW, scaled up to the pair's size: 255 = skip."""
    import cv2

    codes = np.rint(np.asarray(Image.open(kind_path).convert("L")).astype(np.float32) / 16).astype(np.uint8)
    mask = np.isin(codes, [PIXEL_KINDS.index(k) for k in SKIP_KINDS]).astype(np.uint8) * 255
    if SKIP_GROW:
        mask = cv2.dilate(mask, np.ones((2 * SKIP_GROW + 1, 2 * SKIP_GROW + 1), np.uint8))
    tier = round(width / mask.shape[1])
    mask = np.repeat(np.repeat(mask, tier, axis=0), tier, axis=1)
    return mask[:height, :width]


_SKIP_CACHE: dict[str, np.ndarray] = {}


def skip_share(item: dict, box) -> float:
    """How much of a crop-pixel box lies on skipped pixels (0 without a skip mask)."""
    path = item.get("skip")
    if not path:
        return 0.0
    if path not in _SKIP_CACHE:
        if len(_SKIP_CACHE) > 64:
            _SKIP_CACHE.clear()
        _SKIP_CACHE[path] = np.asarray(Image.open(path)) > 0
    mask = _SKIP_CACHE[path]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    region = mask[max(y0, 0) : max(y1, 0), max(x0, 0) : max(x1, 0)]
    return float(region.mean()) if region.size else 0.0


def drop_skipped(items: list[dict], found: list[dict]) -> list[dict]:
    """The flags (each with "crop" and "box") that don't lie mostly on SKIP_KINDS."""
    by_id = {i["id"]: i for i in items}
    return [f for f in found if skip_share(by_id[f["crop"]], f["box"]) < SKIP_SHARE]


def flags(items: list[dict], answers: dict[str, dict], order: str = "xy") -> list[dict]:
    """Every reported defect, in crop pixels, with its crop; empty or unreadable answers give none."""
    by_id = {i["id"]: i for i in items}
    out = []
    for item_id, row in answers.items():
        item = by_id.get(item_id)
        if item is None:
            continue
        for call in row["calls"]:
            for d in (call.get("data") or {}).get("defects", []) or []:
                boxes = eye_contest.boxes_px([{**call, "data": {"defects": [d]}}], order, tuple(item["size"]))
                if not boxes:
                    continue
                box, kind = boxes[0]
                out.append({"crop": item_id, "view": item["view"], "box": [round(v) for v in box], "kind": kind,
                            "severity": d.get("severity"), "note": str(d.get("note", ""))[:200]})
    return out


def sheets(items: list[dict], found: list[dict], out: Path, min_severity: int = 1, per_sheet: int = 6) -> list[Path]:
    """Crops with their flags drawn on the HD side (original left), the most severe crops first."""
    by_id = {i["id"]: i for i in items}
    per_crop: dict[str, list[dict]] = {}
    for f in found:
        if (f["severity"] or 0) >= min_severity:
            per_crop.setdefault(f["crop"], []).append(f)
    ranked = sorted(per_crop.items(), key=lambda kv: (-max(f["severity"] or 0 for f in kv[1]), kv[0]))
    out.mkdir(parents=True, exist_ok=True)
    written = []
    gap, header = 10, 22
    for s in range(math.ceil(len(ranked) / per_sheet)):
        chunk = ranked[s * per_sheet : (s + 1) * per_sheet]
        sheet = Image.new("RGB", (2 * CROP_W + gap, len(chunk) * (CROP_H + header)), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        for n, (crop_id, crop_flags) in enumerate(chunk):
            item = by_id[crop_id]
            top = n * (CROP_H + header)
            sheet.paste(Image.open(item["images"]["orig"]), (0, top + header))
            sheet.paste(Image.open(item["images"]["hd"]), (CROP_W + gap, top + header))
            labels = []
            for k, f in enumerate(crop_flags, 1):
                x0, y0, x1, y1 = f["box"]
                colour = KIND_COLOURS.get(f["kind"], (180, 180, 180))
                ox, oy = CROP_W + gap, top + header
                draw.rectangle((ox + x0, oy + y0, ox + x1, oy + y1), outline=colour, width=2)
                draw.text((ox + x0 + 3, oy + y0 + 2), f"{k}", fill=colour)
                labels.append(f"{k} {f['kind']} s{f['severity']}")
            draw.text((4, top + 4), f"{crop_id}   " + "   ".join(labels), fill=(235, 235, 235))
        path = out / f"sheet_{s:03d}.png"
        sheet.save(path, compress_level=3)
        written.append(path)
    (out / "flags.json").write_text(json.dumps(ranked, indent=1), encoding="utf-8")
    return written
