"""The eye, step 2 of "improving detection" (the author, 2026-11-12): pixel math for the defects the vision models
can't see. One image token of Qwen covers ~32x32 screen px, so a seam one game pixel wide (3 px at 3x), a tile
shifted by a game pixel, a piece left at 1x or a few black specks hide inside a single token (E1 run 5: 25% found).

Each detector compares the HD crop with its original-art twin (CE's own drawing: 3x nearest, dithered, unlit)
and returns candidate boxes with a strength. Our light differs from CE's (sun, relief, film grain), so the
comparisons are made where light cancels: log brightness with the large-scale light ratio removed, normalized
cross-correlation, or the HD crop alone (blockiness).

  dark     black specks and holes: very dark HD pixels where the original is lit
  lowres   a piece at 1x: brightness jumps every 3rd pixel, flat in between
  shift    a piece moved: a block of HD matches the original best at an offset
  lines    thin straight lines and brightness steps along tile edges and verticals that the original lacks
  blobs    patches of wrong brightness or tint with an edge (hole, colour, a floating shadow)
  texture  more fine texture than the original's structure explains (fur, cracks), or less (smear)

Strengths are on each detector's own scale; thresholds are set on clean crops (eye_math_score in the CLI
step). The answers are written like a model's (0-1000 boxes), so eye-score scores them like a contestant.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

TILE_ANGLES = (-math.degrees(math.atan(0.25)), math.degrees(math.atan(0.75)), 90.0)  # tile edges rise 1:4, fall 3:4; walls


def luma(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def gauss(a: np.ndarray, sigma: float) -> np.ndarray:
    return cv2.GaussianBlur(a, (0, 0), sigma, borderType=cv2.BORDER_REFLECT)


def robust_sd(a: np.ndarray, floor: float = 1e-6) -> float:
    if a.size == 0:
        return floor
    med = float(np.median(a))
    return max(1.4826 * float(np.median(np.abs(a - med))), floor)


# Each detector scales its response by the crop's own spread, floored: a crop that is nearly all flat (void, a
# plain wall in the dark) would otherwise turn its faintest ripple into a defect. Floors: the 10% point of each
# spread over the answer key's lit crops (measured 2026-11-12, see SPREADS).
SD_FLOOR = {"lines": 0.0108, "blobs_lum": 0.0109, "blobs_tint": 0.0087, "texture": 0.232}
SPREADS: dict[str, list[float]] | None = None  # set to {} to record each crop's spreads (measuring the floors)


def _spread(name: str, a: np.ndarray, signed: bool = True) -> float:
    raw = robust_sd(a) if signed else (1.4826 * float(np.median(a)) + 1e-6 if a.size else 1e-6)
    if SPREADS is not None:
        SPREADS.setdefault(name, []).append(raw)
    return max(raw, SD_FLOOR.get(name, 0.0))


class Pair:
    """Shared maps for one crop pair (orig, hd: HxWx3 uint8, same size)."""

    def __init__(self, orig: np.ndarray, hd: np.ndarray):
        self.o_rgb = orig.astype(np.float32)
        self.h_rgb = hd.astype(np.float32)
        self.lo = luma(orig)
        self.lh = luma(hd)
        self.h, self.w = self.lh.shape
        # the original without its dither (2 game px = 6 px period) and the HD at the same blur
        self.o3 = gauss(self.lo, 3.0)
        self.h3 = gauss(self.lh, 3.0)
        # light: HD over original at a large scale (our sun, night, relief averaged out)
        self.light = (gauss(self.lh, 18.0) + 6) / (gauss(self.lo, 18.0) + 6)
        # not the void: CE draws it flat (12, 12, 12); flat and dark in the original is void, the rest is map
        mean = gauss(self.lo, 3.0)
        std = np.sqrt(np.clip(gauss(self.lo * self.lo, 3.0) - mean * mean, 0, None))
        void = (std < 0.8) & (mean < 22)
        self.lit = gauss((~void).astype(np.float32), 6.0) > 0.9
        self.lit_share = float(self.lit.mean())


def _boxes_from_mask(mask: np.ndarray, strength: np.ndarray, min_area: int, kind: str, detector: str,
                     reduce: str = "mean") -> list[dict]:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if area < min_area:
            continue
        vals = strength[labels == k]
        s = float(vals.mean() if reduce == "mean" else vals.sum())
        out.append({"box": [int(x), int(y), int(x + w), int(y + h)], "kind": kind, "detector": detector,
                    "score": round(s, 3), "area": int(area)})
    return out


# --- dark: specks and holes --------------------------------------------------------------------


def detect_dark(p: Pair) -> list[dict]:
    """Black specks (small dark dots, scored by how many sit together) and holes (one big dark patch), where
    the original, in our light, would be lit. Two detectors, so each gets its own threshold."""
    expected = p.light * p.o3
    # the original's darkest pixel nearby, in our light: HD dark detail next to dark original pixels is the art
    o_min = cv2.erode(p.lo, np.ones((9, 9), np.uint8)) * p.light
    dark = (p.lh < 30) & (expected > 40) & (p.lh < 0.3 * expected) & (o_min > 30) & p.lit
    if not dark.any():
        return []
    contrast = np.clip(1 - p.lh / (expected + 1e-6), 0, 1)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(dark.astype(np.uint8), connectivity=8)
    out = []
    dots = np.zeros(dark.shape, np.uint8)
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if area >= 400:
            member = labels == k
            out.append({"box": [int(x), int(y), int(x + w), int(y + h)], "kind": "hole", "detector": "hole",
                        "area": int(area), "score": round(float(contrast[member].sum()) / 100.0, 3)})
        elif area <= 60 and max(w, h) <= 12:
            dots[labels == k] = 1
    if dots.any():
        # specks come in groups: dots within ~30 px form one group, scored by the count of dots and their darkness
        joined = cv2.dilate(dots, np.ones((31, 31), np.uint8))
        gn, glabels, gstats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
        dot_n, dot_labels = cv2.connectedComponents(dots, connectivity=8)
        for g in range(1, gn):
            member = (glabels == g) & (dots > 0)
            count = len(np.unique(dot_labels[member]))
            if count < 2:
                continue
            ys, xs = np.nonzero(member)
            out.append({"box": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1], "kind": "specks",
                        "detector": "specks", "area": int(member.sum()),
                        "score": round(count * float(contrast[member].mean()), 3)})
    return out


# --- lowres: blockiness on a 3 px grid -----------------------------------------------------------


def _phase_ratio(d: np.ndarray, axis: int, win: int) -> np.ndarray:
    """d: abs differences along axis. For each phase p, the local mean of d at positions == p (mod 3);
    returns (best phase mean) / (mean of the other two + 2) and the best mean, per pixel."""
    n = d.shape[axis]
    idx = np.arange(n) % 3
    means = []
    for ph in range(3):
        m = (idx == ph).astype(np.float32)
        m = m[None, :] if axis == 1 else m[:, None]
        num = cv2.boxFilter(d * m, -1, (win, win), normalize=True, borderType=cv2.BORDER_REFLECT)
        den = cv2.boxFilter(np.broadcast_to(m, d.shape).astype(np.float32), -1, (win, win), normalize=True, borderType=cv2.BORDER_REFLECT)
        means.append(num / (den + 1e-6))
    stack = np.stack(means)
    best = stack.max(axis=0)
    rest = (stack.sum(axis=0) - best) / 2
    return best / (rest + 2.0), best


def detect_lowres(p: Pair, win: int = 31) -> list[dict]:
    lh = p.lh
    dx = np.abs(np.diff(lh, axis=1, append=lh[:, -1:]))
    dy = np.abs(np.diff(lh, axis=0, append=lh[-1:, :]))
    rx, bx = _phase_ratio(dx, 1, win)
    ry, by = _phase_ratio(dy, 0, win)
    ratio = np.minimum(rx, ry)
    textured = np.minimum(bx, by) > 6
    mask = (ratio > 2.2) & textured & p.lit
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    return _boxes_from_mask(mask, ratio, 900, "lowres", "lowres")


# --- shift: a block that matches the original best at an offset ---------------------------------


def detect_shift(p: Pair, block: int = 48, stride: int = 16, reach: int = 7, min_off: int = 3, min_gain: float = 0.06) -> list[dict]:
    """min_off 3 = one game pixel at 3x; smaller offsets (and gains) come from repeating textures, where the best
    match drifts by a pixel or two (p093 patrol: whole floors "moved" (2, -2) while the crop sat at (0, 0))."""
    h3 = gauss(p.lh, 2.0)
    o3 = gauss(p.lo, 2.5)
    rows = []
    for y in range(reach, p.h - block - reach, stride):
        for x in range(reach, p.w - block - reach, stride):
            tpl = h3[y : y + block, x : x + block]
            if tpl.std() < 6 or not p.lit[y + block // 2, x + block // 2]:
                continue
            region = o3[y - reach : y + block + reach, x - reach : x + block + reach]
            ncc = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
            _, best, _, (bx, by) = cv2.minMaxLoc(ncc)
            zero = float(ncc[reach, reach])
            dx, dy = bx - reach, by - reach
            if max(abs(dx), abs(dy)) >= min_off and best > 0.6 and best - zero > min_gain:
                rows.append((x, y, dx, dy, best - zero))
    if not rows:
        return []
    # neighbouring flagged blocks with about the same offset form one moved piece
    grid = np.zeros((p.h // stride + 2, p.w // stride + 2), np.uint8)
    gain = np.zeros(grid.shape, np.float32)
    for x, y, dx, dy, g in rows:
        grid[y // stride, x // stride] = 1
        gain[y // stride, x // stride] = g
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    out = []
    for k in range(1, n):
        cells = np.argwhere(labels == k)
        if len(cells) < 3:
            continue
        members = [r for r in rows if labels[r[1] // stride, r[0] // stride] == k]
        offsets = np.array([(r[2], r[3]) for r in members])
        med = np.median(offsets, axis=0)
        agree = int((np.abs(offsets - med).max(axis=1) <= 1).sum())
        if agree < 3:
            continue
        xs = [r[0] for r in members]
        ys = [r[1] for r in members]
        out.append({"box": [min(xs), min(ys), max(xs) + block, max(ys) + block], "kind": "seam", "detector": "shift",
                    "score": round(agree * float(np.mean([r[4] for r in members])) * 10, 3), "area": agree,
                    "offset": [int(med[0]), int(med[1])]})
    return out


# --- lines: thin straight lines and steps the original lacks ------------------------------------


def _line_kernel(length: int, angle: float, offset: float = 0.0) -> np.ndarray:
    size = length + 2 * int(abs(offset)) + 3
    k = np.zeros((size, size), np.float32)
    c = size // 2
    t = math.radians(angle)
    ux, uy = math.cos(t), math.sin(t)
    nx, ny = -uy, ux
    for s in np.linspace(-length / 2, length / 2, length * 2):
        x, y = c + s * ux + offset * nx, c + s * uy + offset * ny
        x0, y0 = int(math.floor(x)), int(math.floor(y))
        fx, fy = x - x0, y - y0
        for xx, yy, wgt in ((x0, y0, (1 - fx) * (1 - fy)), (x0 + 1, y0, fx * (1 - fy)), (x0, y0 + 1, (1 - fx) * fy), (x0 + 1, y0 + 1, fx * fy)):
            if 0 <= xx < size and 0 <= yy < size:
                k[yy, xx] += wgt
    return k / k.sum()


_KERNELS: dict = {}


def _kernels(length: int, angle: float):
    key = (length, angle)
    if key not in _KERNELS:
        _KERNELS[key] = {name: _line_kernel(length, angle, off) for name, off in
                         (("c", 0.0), ("l", -5.0), ("r", 5.0), ("sl", -3.0), ("sr", 3.0))}
    return _KERNELS[key]


def _line_responses(img: np.ndarray, length: int, angle: float) -> dict[str, np.ndarray]:
    k = _kernels(length, angle)
    c, l_, r, sl, sr = (cv2.filter2D(img, -1, k[n], borderType=cv2.BORDER_REFLECT) for n in ("c", "l", "r", "sl", "sr"))
    return {"ridge": np.abs(c - (l_ + r) / 2), "step": np.abs(sl - sr)}


def detect_lines(p: Pair, length: int = 61) -> list[dict]:
    """Lines and steps in the HD crop that the original lacks at the same place and angle. Both images get the
    same blur (2.5 px kills the original's dither), log brightness (our light multiplies), and the same line
    filters; a line counts by how much stronger it is in HD than the original's own line there (relief light
    makes real edges stronger, so the original's response is subtracted, not divided out)."""
    lh = np.log(gauss(p.lh, 2.5) + 6)
    lo = np.log(gauss(p.lo, 2.5) + 6)
    lh, lo = lh - gauss(lh, 12.0), lo - gauss(lo, 12.0)
    out = []
    for angle in TILE_ANGLES:
        rh, ro = _line_responses(lh, length, angle), _line_responses(lo, length, angle)
        for name in ("ridge", "step"):
            sd_h = _spread("lines", rh[name][p.lit])
            zh = rh[name] / sd_h
            zo = cv2.dilate(ro[name] / sd_h, np.ones((7, 7), np.uint8))  # the same unit, a few px of slack
            z = zh - 1.5 * zo
            mask = (z > 3.5) & p.lit
            mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            for b in _boxes_from_mask(mask, z, 120, "seam", "lines"):
                x0, y0, x1, y1 = b["box"]
                extent = math.hypot(x1 - x0, y1 - y0)
                if extent < 50:
                    continue
                b["score"] = round(b["score"] * extent / 100, 3)
                b["form"] = f"{name} {angle:.0f}"
                out.append(b)
    return _merge(out)


def _merge(found: list[dict], gap: int = 8) -> list[dict]:
    """Overlapping boxes of one detector become one, keeping the strongest score."""
    found = sorted(found, key=lambda b: -b["score"])
    kept: list[dict] = []
    for b in found:
        x0, y0, x1, y1 = b["box"]
        for k in kept:
            a0, b0, a1, b1 = k["box"]
            if x0 <= a1 + gap and a0 <= x1 + gap and y0 <= b1 + gap and b0 <= y1 + gap:
                k["box"] = [min(a0, x0), min(b0, y0), max(a1, x1), max(b1, y1)]
                break
        else:
            kept.append(dict(b))
    return kept


# --- blobs: patches of wrong brightness or tint -------------------------------------------------


def detect_blobs(p: Pair) -> list[dict]:
    out = []
    eps = 6.0
    lum = np.log(p.h3 + eps) - np.log(p.o3 + eps)
    lum_hp = lum - gauss(lum, 30.0)
    chans = []
    for c in range(3):
        hc, oc = gauss(p.h_rgb[..., c], 3.0), gauss(p.o_rgb[..., c], 3.0)
        dc = np.log(hc + eps) - np.log(oc + eps) - lum
        chans.append(np.abs(dc - gauss(dc, 30.0)))
    tint = np.max(np.stack(chans), axis=0)
    sd_lum = _spread("blobs_lum", lum_hp[p.lit])  # on the signed map: a clipped one has a zero median spread
    sd_tint = _spread("blobs_tint", tint[p.lit], signed=False)
    for name, resp, kind, sd in (("bright", np.clip(lum_hp, 0, None), "color", sd_lum),
                                 ("dark", np.clip(-lum_hp, 0, None), "shadow", sd_lum), ("tint", tint, "color", sd_tint)):
        z = resp / sd
        mask = (z > 5) & p.lit
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        for b in _boxes_from_mask(mask, z, 1200, kind, "blobs"):
            b["score"] = round(b["score"] * math.sqrt(b["area"]) / 60, 3)
            b["form"] = name
            out.append(b)
    return out


# --- texture: fine detail the original doesn't explain ------------------------------------------


def detect_texture(p: Pair) -> list[dict]:
    hf = p.lh - gauss(p.lh, 1.5)
    e_h = gauss(hf * hf, 8.0)
    band = gauss(p.lo, 2.5) - gauss(p.lo, 6.0)
    e_o = gauss(band * band, 8.0)
    t = np.log(e_h + 6) - np.log(e_o + 6)
    t_hp = t - gauss(t, 40.0)
    out = []
    sd = _spread("texture", t_hp[p.lit])
    for name, resp, kind in (("more", np.clip(t_hp, 0, None), "invented"), ("less", np.clip(-t_hp, 0, None), "smear")):
        z = resp / sd
        mask = (z > 4) & p.lit
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        for b in _boxes_from_mask(mask, z, 2500, kind, "texture"):
            b["score"] = round(b["score"] * math.sqrt(b["area"]) / 60, 3)
            b["form"] = name
            out.append(b)
    return out


# Per detector: a box counts above this score. Set 2026-11-12 on the answer key's 40 clean crops (0.9.3 tour
# p093-wide): each detector flags at most 4 of them (the 90% point of each crop's top score; 0 = fewer than 4 have any).
THRESHOLDS = {"specks": 1.57, "hole": 0.0, "lowres": 4.2, "shift": 4.13, "lines": 0.28, "blobs": 26.82, "texture": 0.0}


def kept(found: list[dict], thresholds: dict[str, float] = THRESHOLDS) -> list[dict]:
    """The boxes above their detector's threshold, with a severity: 1 up to twice the threshold, 2 up to five
    times, 3 above (a threshold of 0 counts as 1)."""
    out = []
    for d in found:
        t = thresholds.get(d["detector"], 0.0)
        if d["score"] > t:
            ratio = d["score"] / max(t, 1.0)
            out.append({**d, "severity": 1 if ratio <= 2 else 2 if ratio <= 5 else 3})
    return out


# "dark" gives two detectors' boxes: specks and hole
DETECTORS = {"dark": detect_dark, "lowres": detect_lowres, "shift": detect_shift, "lines": detect_lines,
             "blobs": detect_blobs, "texture": detect_texture}


def detect(orig: np.ndarray, hd: np.ndarray, only: tuple[str, ...] | None = None, min_lit: float = 0.15) -> list[dict]:
    p = Pair(orig, hd)
    out = []
    if p.lit_share < min_lit:  # almost all void: nothing to judge, and no spread to judge it by
        return out
    for name, fn in DETECTORS.items():
        if only is None or name in only:
            out.extend(fn(p))
    return out
