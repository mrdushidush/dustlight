"""The eye, E1: the answer key the local vision models are scored on.

Plan: <data_root>/reports/art-lab/eye/PLAN.md (release data root). The models only ever get pixels: no
file names, no versions, no answers. Three parts:

- the author's blind votes (rounds 1, 2, 4, 6, 7, 7 sun, 7 round, 8b; 188 votes) on the look pages: the pair he
  saw, once in his order and once swapped; his pick is the answer. Source: eye/author_votes.json (his
  pasted "Copy my picks and votes" text, recovered from the session logs; tallies checked against it).
- the author's 37 artifact marks on the 50 crops of reports/artifact-crops (original on the left, HD on the
  right, every pixel doubled).
- Planted defects on clean 880x600 map crops of tour/p093-wide (66 views), each with its box and kind;
  untouched crops as controls; identical pairs (answer "same"); clean against planted (answer: the clean one).

`build()` writes <key>/key.jsonl (one item per line), the planted and split crops next to it, and
<key>/planted_sheet.png to look at the plants by eye. CPU only.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

CROP_W, CROP_H = 880, 600  # Qwen takes this size whole (~560 image tokens); the author's mark crops are this size too
MAP_H = 1140  # tour stills: the map above the interface bar at 1440 px tall

ROUND_TITLES = {
    "1": "round 1: broad sweep",
    "2": "round 2: UltraSharpV2 vs 8-flip (the author: 'almost identical')",
    "4": "round 4: mix per piece, in game",
    "6": "round 6: roof joins (flawed page: crops mostly people and cows)",
    "7": "round 7: joins",
    "7sun": "round 7: sun path (no preference)",
    "7round": "round 7: round body ('very hard to tell')",
    "8b": "round 8b: painted shadows",
}

# A Fallout floor tile at 3x (80x36 at 1x): left, top, right, bottom corners. Its edges rise 1:4 and fall 3:4.
TILE = np.array([(0, 36), (144, 0), (240, 72), (96, 108)], np.float32)
TILE_STEP = (TILE[1] - TILE[0], TILE[2] - TILE[1])  # to the next tile along each edge

# (variant, kind the truth carries): what is planted and what it should be called
VARIANTS = [
    ("line", "seam"),  # a thin dark or light line along a tile edge
    ("shift", "seam"),  # one tile moved by a game pixel or two: steps on all its edges
    ("hole", "hole"),
    ("lowres", "lowres"),  # a piece left at 1x
    ("outline", "outline"),  # dark outline on tiles
    ("halo", "outline"),  # light ring around dark shapes
    ("specks", "specks"),
    ("smear", "smear"),
    ("cracks", "invented"),
    ("fur", "invented"),  # stroke noise, the 11-06 "fur"
    ("color", "color"),
    ("shadow", "shadow"),  # a soft dark blot: a floating shadow
]

# What the model may call each planted kind and still be counted right ("loose").
LOOSE_KINDS = {
    "seam": {"seam", "outline", "smear", "other"},
    "hole": {"hole", "shadow", "color", "other"},
    "lowres": {"lowres", "smear", "specks", "invented", "other"},
    "outline": {"outline", "seam", "other"},
    "specks": {"specks", "invented", "hole", "other"},
    "smear": {"smear", "lowres", "other"},
    "invented": {"invented", "specks", "smear", "other"},
    "color": {"color", "shadow", "other"},
    "shadow": {"shadow", "color", "hole", "other"},
}


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def _save(arr: np.ndarray, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(np.round(arr), 0, 255).astype(np.uint8)).save(path, compress_level=3)
    return str(path)


def _luma(arr: np.ndarray) -> np.ndarray:
    a = arr.astype(np.float32)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def pair_diff(a: np.ndarray, b: np.ndarray) -> dict:
    """How far apart two versions are: mean luma change (of 255) and share of pixels changed by more than 8."""
    if a.shape != b.shape:
        return {"mean": None, "share8": None}
    d = np.abs(_luma(a) - _luma(b))
    return {"mean": round(float(d.mean()), 3), "share8": round(float((d > 8).mean()), 4)}


# --- the author's votes -----------------------------------------------------------------------------


def parse_vote(text: str) -> tuple[str, str, str, str]:
    crop, pair, winner = text.rsplit(":", 2)
    left, right = pair.split("-")
    return crop, left, right, winner


def vote_items(lab: Path, votes_file: Path, rng: random.Random) -> list[dict]:
    record = json.loads(votes_file.read_text(encoding="utf-8"))
    items = []
    for rid, rnd in record["rounds"].items():
        page = lab / rnd["page"]
        votes = rnd["votes"]
        core = set(rng.sample(range(len(votes)), max(4, math.ceil(len(votes) / 3))))
        for i, text in enumerate(votes):
            crop, left, right, winner = parse_vote(text)
            a_path, b_path = page / crop / f"{left}.png", page / crop / f"{right}.png"
            ref = page / crop / "orig.png"
            diff = pair_diff(_rgb(a_path), _rgb(b_path))
            pair_id = f"vote-{rid}-{i:03d}"
            for order in ("ab", "ba"):
                first, second = (a_path, b_path) if order == "ab" else (b_path, a_path)
                if winner == "s":
                    better = "same"
                else:
                    author_first = (winner == "l") == (order == "ab")
                    better = "A" if author_first else "B"
                items.append({
                    "id": f"{pair_id}-{order}", "pair": pair_id, "task": "vote", "source": "author", "round": rid,
                    "crop": crop, "versions": [left, right] if order == "ab" else [right, left], "order": order,
                    "images": {"a": str(first), "b": str(second), "ref": str(ref) if ref.exists() else None},
                    "truth": {"better": better, "author": winner}, "diff": diff, "core": i in core,
                })
    return items


def identical_items(vote_list: list[dict], rng: random.Random, n: int = 16, n_core: int = 8) -> list[dict]:
    """The same picture on both sides: the answer must be 'same'. One per crop, spread over the rounds."""
    by_crop: dict[tuple, dict] = {}
    for item in vote_list:
        by_crop.setdefault((item["round"], item["crop"]), item)
    chosen = rng.sample(sorted(by_crop), min(n, len(by_crop)))
    items = []
    for j, key in enumerate(chosen):
        src = by_crop[key]
        items.append({
            "id": f"same-{j:02d}", "pair": f"same-{j:02d}", "task": "vote", "source": "identical", "round": src["round"],
            "crop": src["crop"], "images": {"a": src["images"]["a"], "b": src["images"]["a"], "ref": src["images"]["ref"]},
            "truth": {"better": "same"}, "core": j < n_core,
        })
    return items


# --- the author's marks -----------------------------------------------------------------------------

MARK_HEADER, MARK_GAP = 34, 10  # tools/scripts/artifact_crops.py: header strip, gap between the halves


def mark_items(crops_dir: Path, key_dir: Path, rng: random.Random, n_core_unmarked: int = 4) -> list[dict]:
    marks = json.loads((crops_dir / "artifact-marks.json").read_text(encoding="utf-8"))
    crops = json.loads((crops_dir / "crops.json").read_text(encoding="utf-8"))
    out = key_dir / "marks"
    items = []
    unmarked = []
    for crop in crops:
        n = str(crop["n"])
        img = _rgb(crops_dir / crop["file"])
        h, w = img.shape[:2]
        half = (w - MARK_GAP) // 2
        orig = img[MARK_HEADER:, :half]
        hd = img[MARK_HEADER:, half + MARK_GAP:]
        boxes = []
        for m in marks.get(n, {}).get("marks", []):
            x0 = m["x"] * w - (half + MARK_GAP)
            y0 = m["y"] * h - MARK_HEADER
            x1, y1 = x0 + m["w"] * w, y0 + m["h"] * h
            box = [max(0, round(x0)), max(0, round(y0)), min(hd.shape[1], round(x1)), min(hd.shape[0], round(y1))]
            if box[2] > box[0] and box[3] > box[1]:
                boxes.append(box)
        stem = Path(crop["file"]).stem
        item = {
            "id": f"mark-{int(n):02d}", "task": "defects", "source": "marks", "crop": stem, "place": crop.get("place"),
            "images": {"orig": _save(orig, out / f"{stem}-orig.png"), "hd": _save(hd, out / f"{stem}-hd.png")},
            "size": [hd.shape[1], hd.shape[0]],
            # unmarked crops are unknown, not clean: The author marked crops 1-37 and gave no "nothing bothers me" ticks
            "truth": {"boxes": boxes, "status": "marked" if boxes else "unmarked"},
            "core": bool(boxes),
        }
        items.append(item)
        if not boxes:
            unmarked.append(item)
    for item in rng.sample(unmarked, min(n_core_unmarked, len(unmarked))):
        item["core"] = True
    return items


# --- planted defects ---------------------------------------------------------------------------


def _poly_mask(h: int, w: int, pts: np.ndarray) -> np.ndarray:
    canvas = np.zeros((h, w), np.uint8)
    cv2.fillPoly(canvas, [np.round(pts * 16).astype(np.int32)], 255, lineType=cv2.LINE_AA, shift=4)
    return canvas.astype(np.float32) / 255


def _outline_mask(h: int, w: int, polys: list[np.ndarray], width: int) -> np.ndarray:
    canvas = np.zeros((h, w), np.uint8)
    cv2.polylines(canvas, [np.round(p * 16).astype(np.int32) for p in polys], True, 255, width, cv2.LINE_AA, shift=4)
    return canvas.astype(np.float32) / 255


def _ellipse_mask(h: int, w: int, cx: float, cy: float, ax: float, ay: float, feather: float) -> np.ndarray:
    canvas = np.zeros((h, w), np.uint8)
    cv2.ellipse(canvas, (round(cx), round(cy)), (round(ax), round(ay)), 0, 0, 360, 255, -1, cv2.LINE_AA)
    mask = canvas.astype(np.float32) / 255
    return cv2.GaussianBlur(mask, (0, 0), feather) if feather > 0 else mask


def _diamond(cx: float, cy: float, scale: float) -> np.ndarray:
    return (TILE - TILE.mean(axis=0)) * scale + np.array([cx, cy], np.float32)


def _blend(img: np.ndarray, mask: np.ndarray, new: np.ndarray | tuple) -> np.ndarray:
    m = mask[..., None]
    new_arr = np.broadcast_to(np.asarray(new, np.float32), img.shape) if isinstance(new, tuple) else new
    return img * (1 - m) + new_arr * m


def _bbox(mask: np.ndarray, thresh: float = 0.05) -> list[int] | None:
    ys, xs = np.nonzero(mask > thresh)
    if len(xs) == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def plant(hd: np.ndarray, orig: np.ndarray, variant: str, level: int, cx: float, cy: float,
          rng: random.Random) -> tuple[np.ndarray, list[int] | None, dict]:
    """One defect at (cx, cy). level 1 = subtle, 2 = clear. Returns the image, the defect's box in px, notes."""
    img = hd.astype(np.float32)
    h, w = img.shape[:2]
    lv = level - 1
    note: dict = {}
    if variant == "line":
        slope = rng.choice([-0.25, 0.75])
        length = (240, 420)[lv]
        dx = length / 2 / math.sqrt(1 + slope * slope)
        canvas = np.zeros((h, w), np.uint8)
        p0 = (round((cx - dx) * 16), round((cy - dx * slope) * 16))
        p1 = (round((cx + dx) * 16), round((cy + dx * slope) * 16))
        cv2.line(canvas, p0, p1, 255, (2, 3)[lv], cv2.LINE_AA, shift=4)
        mask = canvas.astype(np.float32) / 255 * (0.5, 0.85)[lv]
        tone = rng.choice(["dark", "light"])
        new = img * 0.3 if tone == "dark" else img * 0.45 + 0.55 * 235
        out = _blend(img, mask, new)
        note = {"tone": tone, "slope": slope}
    elif variant == "shift":
        mask = _poly_mask(h, w, _diamond(cx, cy, 1.0))
        step = (3, 6)[lv]  # one or two game pixels at 3x
        dxy = rng.choice([(step, 0), (0, step), (-step, 0), (0, -step)])
        moved = np.roll(img, shift=(dxy[1], dxy[0]), axis=(0, 1))
        out = _blend(img, mask, moved)
        note = {"shift": list(dxy)}
    elif variant == "hole":
        mask = _poly_mask(h, w, _diamond(cx, cy, (0.5, 1.0)[lv]))
        out = _blend(img, mask, (6.0, 6.0, 6.0))
    elif variant == "lowres":
        mask = _poly_mask(h, w, _diamond(cx, cy, (1.0, 1.3)[lv]))
        if level == 1:  # every third HD pixel, blown up: a piece at 1x without the palette dither
            small = img[1::3, 1::3][: h // 3, : w // 3]
            blocky = cv2.resize(small, (w // 3 * 3, h // 3 * 3), interpolation=cv2.INTER_NEAREST)
            new = img.copy()
            new[: blocky.shape[0], : blocky.shape[1]] = blocky
        else:  # the original art's own pixels (CE's drawing, 3x nearest, dithered)
            new = orig.astype(np.float32)
        out = _blend(img, mask, new)
    elif variant == "outline":
        base = _diamond(cx, cy, 1.0)
        polys = [base] if level == 1 else [base - TILE_STEP[1] / 2, base + TILE_STEP[1] / 2]  # two tiles side by side
        mask = _outline_mask(h, w, polys, (2, 3)[lv]) * (0.6, 0.9)[lv]
        out = _blend(img, mask, img * 0.22)
    elif variant == "halo":
        x0, y0 = max(0, round(cx - (110, 140)[lv])), max(0, round(cy - (80, 100)[lv]))
        x1, y1 = min(w, round(cx + (110, 140)[lv])), min(h, round(cy + (80, 100)[lv]))
        window = cv2.GaussianBlur(_luma(img[y0:y1, x0:x1]), (0, 0), 5)
        dark = (window < np.percentile(window, 25)).astype(np.uint8)
        dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        n_parts, labels, stats, _ = cv2.connectedComponentsWithStats(dark)
        mask = np.zeros((h, w), np.float32)
        if n_parts > 1:  # one dark shape, the biggest, gets a light ring like a sprite's halo
            shape = (labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
            if shape.sum() >= 1500:
                ring = cv2.dilate(shape, np.ones((2 * (2, 3)[lv] + 1,) * 2, np.uint8)) - shape
                mask[y0:y1, x0:x1] = cv2.GaussianBlur(ring.astype(np.float32), (0, 0), 0.8) * (0.55, 0.85)[lv]
        if mask.sum() < 300:  # nothing dark to ring: a light tile outline instead
            mask = _outline_mask(h, w, [_diamond(cx, cy, 1.0)], (2, 4)[lv]) * (0.5, 0.85)[lv]
            note = {"fallback": "light outline"}
        out = _blend(img, mask, (228.0, 226.0, 214.0))
    elif variant == "specks":
        mask = np.zeros((h, w), np.float32)
        rx, ry = (100, 120)[lv], (75, 90)[lv]
        for _ in range((7, 20)[lv]):
            px, py = cx + rng.uniform(-rx, rx), cy + rng.uniform(-ry, ry)
            r = rng.choice((1, 2)) if level == 1 else rng.choice((2, 2, 3))
            cv2.circle(mask, (round(px), round(py)), r, 1.0, -1, cv2.LINE_AA)
        out = _blend(img, np.clip(mask, 0, 1), (8.0, 8.0, 8.0))
    elif variant == "smear":
        mask = _ellipse_mask(h, w, cx, cy, (80, 120)[lv], (55, 80)[lv], 4)
        blurred = cv2.GaussianBlur(img, (0, 0), (2.5, 5.0)[lv])
        out = _blend(img, mask, blurred)
    elif variant == "cracks":
        canvas = np.zeros((h, w), np.uint8)
        rx, ry = (100, 130)[lv], (75, 95)[lv]
        for _ in range((3, 6)[lv]):
            x, y = cx + rng.uniform(-rx * 0.7, rx * 0.7), cy + rng.uniform(-ry * 0.7, ry * 0.7)
            angle = rng.uniform(0, 2 * math.pi)
            pts = [(x, y)]
            for _ in range(rng.randint(10, 18)):
                angle += rng.uniform(-0.6, 0.6)
                x, y = x + 8 * math.cos(angle), y + 8 * math.sin(angle)
                x, y = min(max(x, cx - rx), cx + rx), min(max(y, cy - ry), cy + ry)
                pts.append((x, y))
            arr = np.round(np.array(pts) * 16).astype(np.int32)
            cv2.polylines(canvas, [arr], False, 255, (1, 2)[lv], cv2.LINE_AA, shift=4)
        mask = canvas.astype(np.float32) / 255 * (0.55, 0.85)[lv]
        out = _blend(img, mask, img * 0.3)
    elif variant == "fur":
        noise = np.random.default_rng(rng.randrange(1 << 30)).normal(0, 1, (h, w)).astype(np.float32)
        k = 11
        kernel = np.zeros((k, k), np.float32)
        angle = rng.uniform(0, math.pi)
        c, s = math.cos(angle), math.sin(angle)
        for t in range(-(k // 2), k // 2 + 1):
            kernel[round(k // 2 + t * s), round(k // 2 + t * c)] = 1
        strokes = cv2.filter2D(noise, -1, kernel / kernel.sum())
        strokes /= strokes.std() + 1e-6
        mask = _ellipse_mask(h, w, cx, cy, (90, 130)[lv], (60, 90)[lv], 6)
        out = img + (strokes * (16, 32)[lv])[..., None] * mask[..., None]
        note = {"angle": round(angle, 2)}
    elif variant == "color":
        mask = cv2.GaussianBlur(_poly_mask(h, w, _diamond(cx, cy, (1.0, 1.3)[lv])), (0, 0), 1.5)
        how = rng.choice(["bright", "dark", "tint"])
        if how == "bright":
            new = img * (1.25, 1.55)[lv]
        elif how == "dark":
            new = img * (0.78, 0.55)[lv]
        else:
            tint = rng.choice([(1.2, 0.88, 1.18), (0.85, 1.18, 0.88), (0.85, 0.95, 1.25)])
            strength = (1.0, 1.6)[lv]
            new = img * np.array([1 + (t - 1) * strength for t in tint], np.float32)
        out = _blend(img, mask, new)
        note = {"how": how}
    elif variant == "shadow":
        mask = _ellipse_mask(h, w, cx, cy, (100, 130)[lv], (40, 55)[lv], 5)
        out = _blend(img, mask * (0.38, 0.58)[lv], img * 0.0)
    else:
        raise ValueError(variant)
    out = np.clip(out, 0, 255)
    changed = np.abs(_luma(out) - _luma(img)) > 10  # what a viewer can see; dark on black is not
    box = _bbox(changed.astype(np.float32), 0.5)
    note["changed_px"] = int(changed.sum())
    return out, box, note


def base_crops(run: Path, rng: random.Random, per_view: int = 4) -> list[dict]:
    """Up to per_view 880x600 windows per view, on the map (no void), from a 4 x 2 grid."""
    bases = []
    views = sorted(p.name[: -len("-hd.png")] for p in run.glob("*-hd.png") if "-fail-" not in p.name)
    for view in views:
        hd = _rgb(run / f"{view}-hd.png")[:MAP_H]
        orig = _rgb(run / f"{view}-orig.png")[:MAP_H]
        h, w = hd.shape[:2]
        xs = [round(i * (w - CROP_W) / 3) for i in range(4)]
        ys = [0, h - CROP_H]
        good = []
        for y in ys:
            for x in xs:
                o = orig[y : y + CROP_H, x : x + CROP_W]
                cover = float((_luma(o) > 6).mean())
                if cover >= 0.9:
                    good.append((x, y, cover))
        for x, y, cover in rng.sample(good, min(per_view, len(good))):
            bases.append({"view": view, "x": x, "y": y, "cover": round(cover, 3),
                          "hd": hd[y : y + CROP_H, x : x + CROP_W].copy(), "orig": orig[y : y + CROP_H, x : x + CROP_W].copy()})
    return bases


def _place(orig: np.ndarray, rng: random.Random, margin: int = 150, edges: bool = False) -> tuple[float, float]:
    """A spot on lit map (not the black void), away from the crop's edges. With edges: the most structured
    of the lit spots (a tile shift or a piece at 1x on plain sand is invisible to anyone)."""
    h, w = orig.shape[:2]
    luma = _luma(orig)
    grad = np.abs(cv2.Sobel(luma, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(luma, cv2.CV_32F, 0, 1)) if edges else None
    best = None
    for _ in range(60):
        cx, cy = rng.uniform(margin, w - margin), rng.uniform(margin * 0.8, h - margin * 0.8)
        window = (slice(int(cy) - 60, int(cy) + 60), slice(int(cx) - 90, int(cx) + 90))
        lit = float((luma[window] > 25).mean())
        if lit > 0.95 and not edges:
            return cx, cy
        score = lit + (float(np.percentile(grad[window], 90)) / 400 if edges and lit > 0.9 else 0)
        if best is None or score > best[0]:
            best = (score, cx, cy)
    return best[1], best[2]


# px a plant must visibly change (level 1, 2), else it is moved or dropped (a plant on black is invisible)
MIN_CHANGED = {"specks": (30, 120), "cracks": (150, 400), "line": (300, 600)}
MIN_CHANGED_DEFAULT = (300, 800)


def planted_items(run: Path, key_dir: Path, rng: random.Random, n_clean: int = 40, n_clean_core: int = 16,
                  core_per_combo: int = 3, vote_pairs_per_variant: int = 2) -> list[dict]:
    out = key_dir / "planted"
    bases = base_crops(run, rng)
    rng.shuffle(bases)
    # dark crops (night, caves) hide many plants: half the clean controls are dark ones, half from the rest
    dark = [b for b in bases if float(_luma(b["orig"]).mean()) < 40]
    lit = [b for b in bases if float(_luma(b["orig"]).mean()) >= 40]
    clean = dark[: n_clean // 2] + lit[: n_clean - min(len(dark), n_clean // 2)]
    rest = dark[n_clean // 2 :] + lit[n_clean - min(len(dark), n_clean // 2) :]
    rng.shuffle(clean)
    rng.shuffle(rest)
    items = []
    for j, b in enumerate(clean):
        stem = f"{b['view']}-{b['x']}-{b['y']}"
        items.append({
            "id": f"clean-{j:03d}", "task": "defects", "source": "clean", "crop": stem,
            "images": {"orig": _save(b["orig"], out / f"{stem}-orig.png"), "hd": _save(b["hd"], out / f"{stem}-hd.png")},
            "size": [CROP_W, CROP_H], "truth": {"boxes": []}, "core": j < n_clean_core,
        })
    combos = [(v, k, lv) for v, k in VARIANTS for lv in (1, 2)]
    count: dict[tuple, int] = {}
    votes_made: dict[str, int] = {}
    for j, b in enumerate(rest):
        variant, kind, level = combos[j % len(combos)]
        stem = f"{b['view']}-{b['x']}-{b['y']}"
        for _ in range(6):
            cx, cy = _place(b["orig"], rng, edges=variant in ("shift", "lowres", "halo"))
            planted, box, note = plant(b["hd"], b["orig"], variant, level, cx, cy, rng)
            if box is not None and note["changed_px"] >= MIN_CHANGED.get(variant, MIN_CHANGED_DEFAULT)[level - 1]:
                break
        else:
            continue
        n = count.get((variant, level), 0)
        count[(variant, level)] = n + 1
        clean_hd = _save(b["hd"], out / f"{stem}-hd.png")
        orig_path = _save(b["orig"], out / f"{stem}-orig.png")
        item_id = f"plant-{variant}-{level}-{n:02d}"
        planted_path = _save(planted, out / f"{item_id}.png")
        items.append({
            "id": item_id, "task": "defects", "source": "planted", "crop": stem, "variant": variant, "level": level,
            "images": {"orig": orig_path, "hd": planted_path, "clean": clean_hd}, "size": [CROP_W, CROP_H],
            "truth": {"boxes": [box], "kind": kind}, "note": note, "core": n < core_per_combo,
        })
        if level == 2 and votes_made.get(variant, 0) < vote_pairs_per_variant:
            k = votes_made.get(variant, 0)
            votes_made[variant] = k + 1
            pair_id = f"pvote-{variant}-{k}"
            for order in ("ab", "ba"):
                a, bb = (clean_hd, planted_path) if order == "ab" else (planted_path, clean_hd)
                items.append({
                    "id": f"{pair_id}-{order}", "pair": pair_id, "task": "vote", "source": "planted", "variant": variant,
                    "order": order, "crop": stem, "images": {"a": a, "b": bb, "ref": orig_path},
                    "truth": {"better": "A" if order == "ab" else "B"}, "core": k == 0,
                })
    return items


def planted_sheet(items: list[dict], path: Path, per_row: int = 6, thumb: int = 300) -> None:
    """Every planted defect, cut around its box at 1:1 (up to thumb px), for looking at them by eye."""
    rows = [i for i in items if i["source"] == "planted" and i["task"] == "defects"]
    rows.sort(key=lambda i: (i["variant"], i["level"], i["id"]))
    cols = per_row * 2
    sheet = Image.new("RGB", (cols * (thumb + 6), math.ceil(len(rows) / per_row) * (thumb + 22)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for n, item in enumerate(rows):
        x0, y0, x1, y1 = item["truth"]["boxes"][0]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        win = (max(0, min(CROP_W - thumb, cx - thumb // 2)), max(0, min(CROP_H - thumb, cy - thumb // 2)))
        box = (win[0], win[1], win[0] + thumb, win[1] + thumb)
        clean = Image.open(item["images"]["clean"]).crop(box)
        planted = Image.open(item["images"]["hd"]).crop(box)
        gx, gy = (n % per_row) * 2 * (thumb + 6), (n // per_row) * (thumb + 22)
        sheet.paste(clean, (gx, gy + 18))
        sheet.paste(planted, (gx + thumb + 6, gy + 18))
        draw.rectangle((gx + thumb + 6 + x0 - win[0], gy + 18 + y0 - win[1], gx + thumb + 6 + x1 - win[0], gy + 18 + y1 - win[1]),
                       outline=(255, 210, 0))
        draw.text((gx + 2, gy + 2), f"{item['id']}  {item['crop']}", fill=(230, 230, 230))
    sheet.save(path, compress_level=3)


def build(lab: Path, crops_dir: Path, run: Path, key_dir: Path, seed: int = 1112) -> dict:
    rng = random.Random(seed)
    key_dir.mkdir(parents=True, exist_ok=True)
    votes = vote_items(lab, lab / "eye" / "author_votes.json", rng)
    items = votes + identical_items(votes, rng) + mark_items(crops_dir, key_dir, rng) + planted_items(run, key_dir, rng)
    hashes: dict[str, str] = {}
    for item in items:
        item["sha1"] = {}
        for role, p in item["images"].items():
            if p:
                item["sha1"][role] = hashes.setdefault(p, sha1_file(Path(p)))
    with open(key_dir / "key.jsonl", "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")
    planted_sheet(items, key_dir / "planted_sheet.png")
    summary: dict = {}
    for item in items:
        k = f"{item['task']}/{item['source']}"
        s = summary.setdefault(k, {"full": 0, "core": 0})
        s["full"] += 1
        s["core"] += int(item["core"])
    (key_dir / "key_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def load_key(key_dir: Path) -> list[dict]:
    with open(key_dir / "key.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
