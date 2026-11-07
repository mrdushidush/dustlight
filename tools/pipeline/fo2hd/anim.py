"""Animation stability for upscaled critters (PLAN.md P1.8).

Variants of the tribal walk cycle (hmwarrab.frm, 6 directions x 8 frames):
  original    the 1x frames, nearest 4x (reference)
  hd          the HD albedo, upscaled frame by frame
  stabilized  each HD frame blended with its neighbors, warped onto it with
              optical flow measured on the *original* frames, only where the
              original says the warp is right
  master2x    the HD frames reduced to a 2x master and shown at 4x (bilinear)

Flicker metric: for consecutive frames, warp frame t+1 onto t with optical flow
(measured on the original frames), take the mean absolute RGB difference over
pixels opaque in both. Real motion is compensated, so what is left is change
the flow can't explain: shimmer. Reported relative to the original (1.0 = as
stable as the original pixel art; lower is calmer, higher shimmers more).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import imageio.v3 as iio
import numpy as np
from PIL import Image

from . import frm as frm_mod
from . import hdassets
from .config import Paths
from .dat import GameFiles

S = 4
BACKGROUND = np.array([85, 82, 75], np.float32) / 255.0
WALK = "art\\critters\\hmwarrab.frm"


def _layout(frm: frm_mod.Frm, rotation: int) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    """1x top-left of each frame on a shared canvas, walking in place.

    Frames are anchored at their bottom center plus the accumulated per-frame
    deltas (as the engine moves the object), minus the cycle's average drift."""
    frames = frm.frames[rotation]
    n = len(frames)
    cx = cy = 0.0
    anchors = []
    for f in frames:
        cx += f.x
        cy += f.y
        anchors.append((cx, cy))
    drift_x, drift_y = anchors[-1][0] / n, anchors[-1][1] / n
    tops = []
    for i, f in enumerate(frames):
        ax = anchors[i][0] - drift_x * (i + 1)
        ay = anchors[i][1] - drift_y * (i + 1)
        tops.append((int(round(ax - f.width / 2)), int(round(ay - f.height + 1))))
    min_x = min(t[0] for t in tops) - 4
    min_y = min(t[1] for t in tops) - 4
    max_x = max(t[0] + f.width for t, f in zip(tops, frames)) + 4
    max_y = max(t[1] + f.height for t, f in zip(tops, frames)) + 4
    return [(t[0] - min_x, t[1] - min_y) for t in tops], (max_x - min_x, max_y - min_y)


def _canvas(rgba: np.ndarray, top: tuple[int, int], size: tuple[int, int]) -> np.ndarray:
    """Places a 4x RGBA frame on a transparent 4x canvas."""
    w, h = size[0] * S, size[1] * S
    out = np.zeros((h, w, 4), np.float32)
    x, y = top[0] * S, top[1] * S
    fh, fw = rgba.shape[:2]
    out[y : y + fh, x : x + fw] = rgba
    return out


def _over(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4]
    return rgba[..., :3] * a + BACKGROUND * (1 - a)


def _gray(rgba: np.ndarray) -> np.ndarray:
    return cv2.cvtColor((_over(rgba) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)


def _flow(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Flow mapping pixels of frame a to frame b (sample b at p + flow)."""
    return cv2.calcOpticalFlowFarneback(_gray(a), _gray(b), None, 0.5, 4, 21, 5, 7, 1.5, 0)


def _warp(image: np.ndarray, flow: np.ndarray) -> np.ndarray:
    h, w = flow.shape[:2]
    grid = np.dstack(np.meshgrid(np.arange(w), np.arange(h))).astype(np.float32)
    m = grid + flow
    return cv2.remap(image, m[..., 0], m[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def flicker(frames: list[np.ndarray], flows: list[np.ndarray]) -> float:
    total, count = 0.0, 0
    n = len(frames)
    for t in range(n):
        a, b = frames[t], frames[(t + 1) % n]
        warped = _warp(b, flows[t])
        both = (a[..., 3] > 0.5) & (warped[..., 3] > 0.5)
        if both.any():
            total += float(np.abs(_over(a) - _over(warped))[both].mean())
            count += 1
    return total / max(count, 1)


def stabilize(hd: list[np.ndarray], original: list[np.ndarray], flows_fwd: list[np.ndarray], flows_bwd: list[np.ndarray]) -> list[np.ndarray]:
    n = len(hd)
    out = []
    for t in range(n):
        acc = hd[t].copy() * 0.5
        weight = np.full(hd[t].shape[:2], 0.5, np.float32)
        for neighbor, flow in (((t + 1) % n, flows_fwd[t]), ((t - 1) % n, flows_bwd[t])):
            warped_hd = _warp(hd[neighbor], flow)
            warped_orig = _warp(original[neighbor], flow)
            # Trust the warp only where it reproduces the original frame.
            err = np.abs(_over(warped_orig) - _over(original[t])).mean(-1)
            w = np.clip(1.0 - err / 0.08, 0.0, 1.0) * 0.25
            w *= (warped_hd[..., 3] > 0.5) & (hd[t][..., 3] > 0.5)
            acc += warped_hd * w[..., None]
            weight += w
        out.append(acc / weight[..., None])
    return out


def master2x(hd: list[np.ndarray]) -> list[np.ndarray]:
    out = []
    for frame in hd:
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        out.append(cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR))
    return out


def run(files: GameFiles, paths: Paths, out_dir: Path, gif_rotation: int = 2, report=print) -> dict:
    frm = frm_mod.decode(files.read(WALK))
    palette = frm_mod.load_palette(files.read("color.pal"))
    hd_dir = hdassets.asset_dir(paths, WALK)
    if not (hd_dir / "meta.json").exists():
        raise SystemExit(f"no HD assets for {WALK}; run `fo2hd hdassets` first")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict = {"asset": WALK, "fps": frm.fps, "rotations": {}}
    gif_rows = {}
    for rotation in range(6):
        tops, size = _layout(frm, rotation)
        original, hd = [], []
        for i, f in enumerate(frm.frames[rotation]):
            rgba1 = np.dstack([palette[f.pixels], np.where(f.pixels != 0, 255, 0)]).astype(np.float32) / 255.0
            original.append(_canvas(rgba1.repeat(S, 0).repeat(S, 1), tops[i], size))
            albedo = np.asarray(Image.open(hd_dir / f"r{rotation}_f{i:03d}.albedo.png").convert("RGBA"), np.float32) / 255.0
            hd.append(_canvas(albedo, tops[i], size))
        n = len(original)
        flows_fwd = [_flow(original[t], original[(t + 1) % n]) for t in range(n)]
        flows_bwd = [_flow(original[t], original[(t - 1) % n]) for t in range(n)]
        variants = {"original": original, "hd": hd, "stabilized": stabilize(hd, original, flows_fwd, flows_bwd), "master2x": master2x(hd)}
        base = flicker(original, flows_fwd)
        results["rotations"][rotation] = {name: round(flicker(frames, flows_fwd) / base, 3) for name, frames in variants.items()}
        if rotation == gif_rotation:
            gif_rows = variants

    names = ["original", "hd", "stabilized", "master2x"]
    results["mean"] = {name: round(float(np.mean([r[name] for r in results["rotations"].values()])), 3) for name in names}

    # Side-by-side GIF of the four variants, plus one GIF per variant.
    duration = int(1000 / max(frm.fps, 1))
    frames_side = []
    for t in range(len(gif_rows["original"])):
        tiles = [(_over(gif_rows[name][t]) * 255).astype(np.uint8) for name in names]
        gap = np.full((tiles[0].shape[0], 8, 3), 30, np.uint8)
        row = np.concatenate(sum(([tile, gap] for tile in tiles), [])[:-1], axis=1)
        frames_side.append(row)
    iio.imwrite(out_dir / "walk_side_by_side.gif", np.stack(frames_side), duration=duration, loop=0)
    for name in names:
        stack = np.stack([(_over(f) * 255).astype(np.uint8) for f in gif_rows[name]])
        iio.imwrite(out_dir / f"walk_{name}.gif", stack, duration=duration, loop=0)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return results
