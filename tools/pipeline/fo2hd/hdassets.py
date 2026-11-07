"""HD asset maps for the sample set (PLAN.md P1.7): albedo, normal, height,
emissive and palette-cycle masks at 4x.

Coordinate conventions (shared with fo2hd.compose):
  world u = screen right, v = toward the viewer along the ground (screen down),
  z = up. Normals are unit vectors in (u, v, z), stored as RGB = (n + 1) / 2.
  Height is the height above the sprite's own ground line in 1x screen pixels
  (float, stored x16 in a 16-bit PNG).

Heuristics first (deterministic), per category:
  floor, roof   flat: normal straight up, height 0 (roofs are placed at their
                roof height by the compositor); a little detail from the albedo.
  wall          vertical planes. The face direction comes from the slope of the
                wall's bottom silhouette, column by column, so corner pieces get
                two faces: bottom edge falling to the right -> faces south-west,
                rising -> south-east, flat -> faces the camera.
  scenery, critter, item, misc
                upright "pillow" billboards: the silhouette is inflated (distance
                transform) so edges turn away from the camera, plus albedo detail.
  height        walls/scenery: distance above the lowest opaque pixel of the same
                column; critters/items: above the frame's bottom row (the feet).

Emissive and cycle masks come straight from the palette indices (cycle.cc):
  229-232 slime, 233-237 monitors, 238-242 slow fire, 243-247 fast fire,
  248-253 shoreline (cycles, does not glow), 254 alarm.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import frm as frm_mod
from .config import Paths
from .dat import GameFiles
from .jobs import JobDB, fingerprint, write_atomic
from .stages import disk_path
from .upscale import Upscaler, upscale_mask

HDASSETS_VERSION = 3
SCALE = 4
DEFAULT_MODEL = "4x-UltraSharpV2.safetensors"

CYCLE_FIRST, CYCLE_LAST = 229, 254
SHORELINE = (248, 253)

# How strongly the albedo's luminance perturbs the heuristic normal.
DETAIL_STRENGTH = {"floor": 1.2, "roof": 1.0, "wall": 0.8, "scenery": 1.0, "critter": 0.6, "item": 0.8, "misc": 0.8}

# Floor and roof tiles cover the plane on the square-tile lattice (tile.cc
# squareTileToScreenXY: neighbors at (48, -12) and (32, 24) screen px). Upscaled
# alone, a tile's edges are borders to the model, which showed as faint
# straight lines between smooth tiles (P4.4 seam metric). So a tile is upscaled
# in the middle of a ring of copies of itself, TILE_CONTEXT 1x px wide, and
# cropped; its detail normals are computed on the same canvas.
#
# lattice-2 (roofs): some roof tiles (Arroyo's tents most) have dark specks
# or a dark outline on their outermost pixel ring. Lost in the original's
# dither, the upscale keeps them as dotted lines along the tile edges. Edge-
# ring pixels far from the median of the tile's own pixels just inside are
# replaced by that median before the canvas is built. Floors keep their
# edges: some (metal plates, carpets) have border lines on purpose.
TILED = ("floor", "roof")
TILE_LATTICE = ((48, -12), (32, 24))
TILE_CONTEXT = 16
TILE_CONTEXT_VERSION = {"floor": "lattice-1", "roof": "lattice-2"}
CLEAN_TILE_EDGES = ("roof",)
EDGE_SPECK_MIN = 24.0  # luma steps (0..255) an edge pixel may differ from the median inside
EDGE_SPECK_REL = 0.3  # or this share of the median, whichever is larger


def emissive_mask(indices: np.ndarray) -> np.ndarray:
    glowing = (indices >= CYCLE_FIRST) & (indices <= CYCLE_LAST)
    shoreline = (indices >= SHORELINE[0]) & (indices <= SHORELINE[1])
    return glowing & ~shoreline


def cycle_indices(indices: np.ndarray) -> np.ndarray:
    """Original palette index where it is a cycling color, else 0."""
    return np.where((indices >= CYCLE_FIRST) & (indices <= CYCLE_LAST), indices, 0).astype(np.uint8)


def _normalize(n: np.ndarray) -> np.ndarray:
    return n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)


def column_bottoms(mask: np.ndarray) -> np.ndarray:
    """Lowest opaque row per column (-1 where the column is empty)."""
    h = mask.shape[0]
    rows = np.where(mask, np.arange(h)[:, None], -1)
    return rows.max(axis=0)


def ground_line(mask: np.ndarray, slope: float = 0.5) -> np.ndarray:
    """Screen row of the ground under each column of an upright sprite.

    Starts from each column's lowest opaque pixel, then takes the lower
    envelope under the constraint that the ground can't rise faster than the
    isometric slope (0.5 px per px). Overhangs (a street lamp's arm, a tree's
    canopy) thereby inherit the ground of whatever holds them up instead of
    "standing" at their own lowest pixel."""
    h, w = mask.shape
    bottom = column_bottoms(mask).astype(np.float32)
    valid = bottom >= 0
    if not valid.any():
        return np.full(w, h - 1, np.float32)
    xs = np.arange(w, dtype=np.float32)
    vx = np.flatnonzero(valid).astype(np.float32)
    vb = bottom[valid]
    envelope = (vb[None, :] - slope * np.abs(xs[:, None] - vx[None, :])).max(axis=1)
    return cv2.GaussianBlur(envelope[None, :], (0, 0), 1.5)[0] if w > 2 else envelope


def height_map(category: str, mask: np.ndarray) -> np.ndarray:
    """1x height (screen px above the ground line), float32."""
    h, w = mask.shape
    if category in ("floor", "roof"):
        return np.zeros((h, w), np.float32)
    ys = np.arange(h, dtype=np.float32)[:, None]
    if category in ("wall", "scenery"):
        height = ground_line(mask)[None, :] - ys
    else:
        height = (h - 1) - ys + np.zeros((1, w), np.float32)
    return np.where(mask, np.maximum(height, 0.0), 0.0).astype(np.float32)


def base_normals(category: str, mask: np.ndarray) -> np.ndarray:
    """(h, w, 3) heuristic normals at the given resolution."""
    h, w = mask.shape
    if category in ("floor", "roof"):
        n = np.zeros((h, w, 3), np.float32)
        n[..., 2] = 1.0
        return n

    if category == "wall":
        bottom = column_bottoms(mask).astype(np.float32)
        valid = bottom >= 0
        if valid.sum() >= 2:
            bottom = np.interp(np.arange(w), np.flatnonzero(valid), bottom[valid]).astype(np.float32)
        slope = np.gradient(cv2.GaussianBlur(bottom[None, :], (0, 0), 3.0)[0]) if w > 2 else np.zeros(w, np.float32)
        # Iso walls run at +-0.5 px per px; map the slope to a facing blend.
        t = np.clip(slope / 0.5, -1.0, 1.0)
        sw = np.array([-1.0, 1.0, 0.0]) / np.sqrt(2)
        se = np.array([1.0, 1.0, 0.0]) / np.sqrt(2)
        front = np.array([0.0, 1.0, 0.0])
        cols = np.where(t[:, None] > 0, (1 - t[:, None]) * front + t[:, None] * sw, (1 + t[:, None]) * front - t[:, None] * se)
        n = np.broadcast_to(cols[None, :, :], (h, w, 3)).astype(np.float32).copy()
        return _normalize(n)

    # Pillow billboard: distance inside the silhouette, gradient points outward.
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    radius = max(2.0, float(np.percentile(dist[mask], 90)) if mask.any() else 2.0)
    bulge = np.sqrt(np.clip(1.0 - (1.0 - np.clip(dist / radius, 0, 1)) ** 2, 0, 1))
    gx = cv2.Sobel(bulge, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(bulge, cv2.CV_32F, 0, 1, ksize=3)
    k = 0.6
    # Facing the camera (v); screen x tilts toward u, screen up tilts toward z.
    n = np.dstack([-gx * k, np.ones_like(gx), gy * k]).astype(np.float32)
    return _normalize(n)


def add_detail(normals: np.ndarray, albedo_rgb: np.ndarray, strength: float, category: str) -> np.ndarray:
    """Perturbs normals with the albedo's luminance read as a height field."""
    lum = cv2.cvtColor(albedo_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    lum = cv2.GaussianBlur(lum, (0, 0), 1.0)
    dx = cv2.Sobel(lum, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(lum, cv2.CV_32F, 0, 1, ksize=3)
    if category in ("floor", "roof"):
        # Surface axes: u (screen x) and v (screen y, foreshortened 2:1).
        tangent_u, tangent_v = np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
        dv = dy * 0.5
    else:
        # Upright surfaces: screen x ~ u, screen down ~ -z.
        tangent_u, tangent_v = np.array([1.0, 0, 0]), np.array([0, 0, -1.0])
        dv = dy
    n = normals - strength * (dx[..., None] * tangent_u + dv[..., None] * tangent_v)
    return _normalize(n.astype(np.float32))


def encode_normals(n: np.ndarray) -> np.ndarray:
    return np.round((n * 0.5 + 0.5) * 255.0).clip(0, 255).astype(np.uint8)


def decode_normals(rgb: np.ndarray) -> np.ndarray:
    return _normalize(rgb.astype(np.float32) / 255.0 * 2.0 - 1.0)


def _png(array: np.ndarray, mode: str | None = None) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array, mode).save(buf, "PNG")
    return buf.getvalue()


def clean_tile_edges(indices: np.ndarray, palette: np.ndarray) -> tuple[np.ndarray, int]:
    """The tile's colors (h, w, 3) with specks on its outermost pixel ring
    replaced by the median of the inner pixels within 2 px; and how many."""
    rgb = palette[indices].astype(np.uint8)
    opaque = indices != 0
    inner = cv2.erode(opaque.astype(np.uint8), np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
    luma = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    h, w = indices.shape
    replaced = 0
    for y, x in zip(*np.nonzero(opaque & ~inner)):
        y0, y1, x0, x1 = max(y - 2, 0), min(y + 3, h), max(x - 2, 0), min(x + 3, w)
        near = inner[y0:y1, x0:x1]
        if near.sum() < 3:
            continue
        median = float(np.median(luma[y0:y1, x0:x1][near]))
        if abs(luma[y, x] - median) > max(EDGE_SPECK_MIN, EDGE_SPECK_REL * median):
            rgb[y, x] = np.median(rgb[y0:y1, x0:x1][near], axis=0)
            replaced += 1
    return rgb, replaced


def tile_context(indices: np.ndarray, palette: np.ndarray, clean: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """The tile at (TILE_CONTEXT, TILE_CONTEXT) of a canvas filled with copies
    of itself on the tile lattice (edge specks cleaned if `clean`): (rgb, mask)."""
    h, w = indices.shape
    m = TILE_CONTEXT
    rgb = np.zeros((h + 2 * m, w + 2 * m, 3), np.uint8)
    mask = np.zeros((h + 2 * m, w + 2 * m), bool)
    ys, xs = np.nonzero(indices != 0)
    colors = clean_tile_edges(indices, palette)[0][ys, xs] if clean else palette[indices[ys, xs]]
    (ax, ay), (bx, by) = TILE_LATTICE
    offsets = [(i * ax + j * bx, i * ay + j * by) for i in range(-2, 3) for j in range(-2, 3) if (i, j) != (0, 0)]
    for dx, dy in offsets + [(0, 0)]:  # the tile itself last, on top
        ty, tx = ys + dy + m, xs + dx + m
        ok = (ty >= 0) & (ty < rgb.shape[0]) & (tx >= 0) & (tx < rgb.shape[1])
        rgb[ty[ok], tx[ok]] = colors[ok]
        mask[ty[ok], tx[ok]] = True
    return rgb, mask


def model_input(indices: np.ndarray, palette: np.ndarray, category: str) -> np.ndarray | None:
    """What the model upscales for one frame (None: nothing, the frame is
    fully transparent). `Upscaler.crop` of the model's output is the
    `canvas` that `build_frame` takes."""
    if not (indices != 0).any():
        return None
    if category in TILED:
        return Upscaler.canvas_input(*tile_context(indices, palette, category in CLEAN_TILE_EDGES))
    return Upscaler.canvas_input(palette[indices], indices != 0)


def build_frame(upscaler: Upscaler, indices: np.ndarray, palette: np.ndarray, category: str, canvas: np.ndarray | None = None) -> dict[str, bytes]:
    """All maps of one frame. `canvas` is the model's output for it when it
    was computed in a batch; without it the model runs here."""
    mask1 = indices != 0
    h4, w4 = indices.shape[0] * SCALE, indices.shape[1] * SCALE
    strength = DETAIL_STRENGTH.get(category, 0.8)
    if category in TILED and mask1.any():
        if canvas is None:
            canvas = upscaler.canvas(*tile_context(indices, palette, category in CLEAN_TILE_EDGES))
        m = TILE_CONTEXT * SCALE
        albedo = np.dstack([canvas[m : m + h4, m : m + w4], upscale_mask(mask1, SCALE)])
        mask4 = albedo[..., 3] > 127
        normals = add_detail(base_normals(category, np.ones(canvas.shape[:2], bool)), canvas, strength, category)
        normals = normals[m : m + h4, m : m + w4].copy()
    else:
        albedo = upscaler.sprite(indices, palette, canvas)
        mask4 = albedo[..., 3] > 127
        normals = add_detail(base_normals(category, mask4), albedo[..., :3], strength, category)

    height1 = height_map(category, mask1)
    height4 = cv2.resize(height1, (w4, h4), interpolation=cv2.INTER_LINEAR)
    normals[~mask4] = (0.0, 0.0, 1.0)

    emissive = emissive_mask(indices).astype(np.uint8) * 255
    emissive4 = cv2.resize(emissive, (indices.shape[1] * SCALE, indices.shape[0] * SCALE), interpolation=cv2.INTER_LINEAR)
    cycle4 = cycle_indices(indices).repeat(SCALE, 0).repeat(SCALE, 1)

    return {
        "albedo.png": _png(albedo, "RGBA"),
        "normal.png": _png(encode_normals(normals), "RGB"),
        "height.png": _png(np.round(height4 * 16).clip(0, 65535).astype(np.uint16)),
        "emissive.png": _png(emissive4, "L"),
        "cycle.png": _png(cycle4, "L"),
    }


def asset_dir(paths: Paths, game_path: str) -> Path:
    d = disk_path(game_path)
    stem, ext = d.rsplit(".", 1)
    # As the game names packs (hd_art.cc packKey): every extension but .frm
    # is kept, so hmwarrab.fr0 -> hmwarrab_fr0, opbase_help.png -> opbase_help_png.
    if ext != "frm":
        stem += "_" + ext
    return paths.data_root / "hd" / stem


FRAME_FILES = ("albedo.png", "normal.png", "height.png", "emissive.png", "cycle.png")
CHUNK_FRAMES = 384  # frames gathered across FRMs for one batched model pass (P7)
# Frames that went through the model in tiles because they didn't fit the VRAM
# cap (`Upscaler._tiled`), one JSON line each: `fo2hd retry-tiled` builds them
# again in one piece with more VRAM while the PC is locked.
TILED_LOG = Path("bulk") / "tiled.jsonl"
POST_WORKERS = 3  # threads for the maps and PNG writes, overlapping the next model pass
_DONE: Future = Future()
_DONE.set_result(None)


def _copy_frame(source: dict, frame: dict) -> None:
    """A frame identical to one already built (critters repeat ~9% of their
    frames across FRMs): copy its files instead of running the model again."""
    source["future"].result()
    frame["out_dir"].mkdir(parents=True, exist_ok=True)  # every frame of an FRM may be a copy
    for suffix in FRAME_FILES:
        dst = frame["out_dir"] / f"{frame['name']}.{suffix}"
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(source["out_dir"] / f"{source['name']}.{suffix}", tmp)
        os.replace(tmp, dst)


def log_tiled(paths: Paths, game_path: str, frame: str, shape: tuple, model: str, reason: str = "tiled") -> None:
    log = paths.data_root / TILED_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    line = {"path": game_path, "frame": frame, "input": [int(shape[1]), int(shape[0])], "model": model, "reason": reason, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line) + "\n")


def _write_frame(upscaler: Upscaler, frame: dict, palette: np.ndarray, category: str) -> None:
    for suffix, png in build_frame(upscaler, frame["pixels"], palette, category, frame.pop("canvas", None)).items():
        write_atomic(frame["out_dir"] / f"{frame['name']}.{suffix}", png)


def build(manifest: dict, files: GameFiles, paths: Paths, jobs: JobDB, model_for: dict[str, Path], report=print, governor=None) -> dict:
    """Builds every asset of the manifest that isn't done yet. Frames of
    several FRMs go through the model together (`Upscaler.rgb_batch`); the
    maps and PNGs are made on worker threads while the GPU runs the next
    chunk. An FRM counts as done (meta.json + job record) only after all its
    files are written, so a kill at any point resumes cleanly."""
    palette_bytes = files.read("color.pal")
    palette = frm_mod.load_palette(palette_bytes)
    upscalers: dict[Path, Upscaler] = {}
    stats = {"assets": 0, "skipped": 0, "frames": 0, "repeated_frames": 0, "input_px": 0, "seconds": 0.0}
    first_built: dict[tuple[str, str], dict] = {}  # (category, frame key) -> the frame that was built
    started = time.perf_counter()

    def upscaler_for(model_path: Path) -> Upscaler:
        if model_path not in upscalers:
            upscalers[model_path] = Upscaler(model_path)
            upscalers[model_path].governor = governor
        return upscalers[model_path]

    def prepare(n: int, asset: dict, data: bytes, fp: str) -> dict:
        category = asset["category"]
        model_path = model_for[category]
        frm = frm_mod.decode(data)
        out_dir = asset_dir(paths, asset["path"])
        meta = {
            "version": HDASSETS_VERSION,
            "source": asset["path"],
            "category": category,
            "model": model_path.name,
            "scale": SCALE,
            "fps": frm.fps,
            "action_frame": frm.action_frame,
            "frame_count": frm.frame_count,
            "rotations": [],
        }
        if category in TILED:
            meta["context"] = f"{TILE_CONTEXT_VERSION[category]}, {TILE_CONTEXT} px"
        frames = []
        seen: dict[int, int] = {}
        for rotation in range(frm_mod.ROTATIONS):
            offset = frm.data_offsets[rotation]
            if offset in seen:
                meta["rotations"].append({"same_as": seen[offset]})
                continue
            seen[offset] = rotation
            entries = []
            for index, frame in enumerate(frm.frames[rotation]):
                name = f"r{rotation}_f{index:03d}"
                key = f"{frame.width}x{frame.height}:{hashlib.sha1(frame.pixels.tobytes()).hexdigest()}"
                frames.append({"name": name, "out_dir": out_dir, "pixels": frame.pixels, "key": key, "path": asset["path"]})
                entries.append({"name": name, "w": frame.width * SCALE, "h": frame.height * SCALE, "dx": frame.x * SCALE, "dy": frame.y * SCALE})
            meta["rotations"].append({"x_offset": frm.x_offsets[rotation] * SCALE, "y_offset": frm.y_offsets[rotation] * SCALE, "frames": entries})
        return {"n": n, "path": asset["path"], "category": category, "model": model_path, "fp": fp, "out_dir": out_dir, "meta": meta, "frames": frames}

    def run_chunk(assets: list[dict], pool: ThreadPoolExecutor) -> None:
        new = []
        for a in assets:
            for fr in a["frames"]:
                source = first_built.get((a["category"], fr["key"]))
                if source is None:
                    first_built[(a["category"], fr["key"])] = fr
                    new.append((a, fr))
                else:
                    fr["copy_of"] = source
        by_model: dict[Path, list[tuple[dict, np.ndarray]]] = {}
        for a, fr in new:
            x = model_input(fr["pixels"], palette, a["category"])
            if x is not None:
                by_model.setdefault(a["model"], []).append((fr, x))
            stats["input_px"] += fr["pixels"].size
        for model_path, items in by_model.items():
            up = upscaler_for(model_path)
            for (fr, _), out in zip(items, up.rgb_batch([x for _, x in items])):
                fr["canvas"] = up.crop(out)
            for k in up.last_tiled:
                fr, x = items[k]
                log_tiled(paths, fr["path"], fr["name"], x.shape, model_path.name)
        for a, fr in new:
            fr["future"] = pool.submit(_write_frame, upscaler_for(a["model"]), fr, palette, a["category"])
        for a in assets:
            for fr in a["frames"]:
                if "copy_of" in fr:
                    fr["future"] = pool.submit(_copy_frame, fr.pop("copy_of"), fr)
                    stats["repeated_frames"] += 1
                stats["frames"] += 1

    def finish(assets: list[dict]) -> None:
        for a in assets:
            for fr in a["frames"]:
                fr["future"].result()
            write_atomic(a["out_dir"] / "meta.json", json.dumps(a["meta"], indent=1).encode("utf-8"))
            jobs.mark_done("hdassets", a["path"], a["fp"], a["out_dir"] / "meta.json")
            for fr in a["frames"]:  # first_built keeps the record, not the art
                fr.pop("pixels", None)
                fr["future"] = _DONE
            stats["assets"] += 1
            if stats["assets"] % 25 == 0:
                report(f"    hd assets: {a['n'] + 1}/{len(manifest['assets'])} ({stats['frames']} frames, {(time.perf_counter() - started) / 60:.1f} min)")

    chunk: list[dict] = []
    in_flight: list[dict] = []
    try:
        with ThreadPoolExecutor(POST_WORKERS) as pool:
            for n, asset in enumerate(manifest["assets"]):
                category = asset["category"]
                model_path = model_for[category]
                data = files.read(asset["path"])
                context = (TILE_CONTEXT_VERSION[category], TILE_CONTEXT) if category in TILED else ()
                fp = fingerprint(data, palette_bytes, model_path.name, str(model_path.stat().st_size), category, HDASSETS_VERSION, *context)
                if jobs.is_done("hdassets", asset["path"], fp):
                    stats["skipped"] += 1
                    continue
                chunk.append(prepare(n, asset, data, fp))
                if sum(len(a["frames"]) for a in chunk) >= CHUNK_FRAMES:
                    run_chunk(chunk, pool)
                    finish(in_flight)  # its files were written while the GPU ran this chunk
                    in_flight, chunk = chunk, []
            run_chunk(chunk, pool)
            finish(in_flight)
            finish(chunk)
    finally:
        # Keeps the FRMs finished so far when a step fails (the CUDA OOM of
        # 2026-11-02 14:48 lost ~50 min of UI art that was done but not committed).
        jobs.commit()
    stats["seconds"] = sum(u.seconds for u in upscalers.values())
    stats["model_input_px"] = sum(u.input_pixels for u in upscalers.values())
    if governor:
        stats["governor_seconds"] = {k: round(v) for k, v in governor.seconds.items()}
    return stats


def map_checks(manifest: dict, paths: Paths, files: GameFiles) -> dict:
    """P1.7 automatic checks over the built maps (opaque pixels only):
    floor normals near straight up, wall normals near horizontal, and emissive
    masks equal to the palette-index rule."""
    angles, wall_z, emissive_bad, emissive_px = [], [], 0, 0
    for asset in manifest["assets"]:
        category = asset["category"]
        d = asset_dir(paths, asset["path"])
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        frm = frm_mod.decode(files.read(asset["path"]))
        for rotation, entry in enumerate(json.loads(meta_path.read_text(encoding="utf-8"))["rotations"]):
            for index, frame in enumerate(entry.get("frames", [])[:2]):
                base = d / frame["name"]
                alpha = np.asarray(Image.open(f"{base}.albedo.png"))[..., 3] > 127
                n = decode_normals(np.asarray(Image.open(f"{base}.normal.png").convert("RGB")))[alpha]
                if category == "floor" and n.size:
                    angles.append(float(np.degrees(np.arccos(np.clip(n[:, 2], -1, 1))).mean()))
                if category == "wall" and n.size:
                    wall_z.append(float(np.abs(n[:, 2]).mean()))
                indices = frm.frame(index, rotation).pixels
                expected = emissive_mask(indices).repeat(SCALE, 0).repeat(SCALE, 1)
                stored = np.asarray(Image.open(f"{base}.cycle.png")) > 0
                emissive_px += int(expected.sum())
                emissive_bad += int((stored & ~((indices >= CYCLE_FIRST) & (indices <= CYCLE_LAST)).repeat(SCALE, 0).repeat(SCALE, 1)).sum())
    return {
        "floor_normal_mean_angle_from_up_deg": round(float(np.mean(angles)), 2) if angles else None,
        "floor_normal_max_mean_angle_deg": round(float(np.max(angles)), 2) if angles else None,
        "wall_normal_mean_abs_z": round(float(np.mean(wall_z)), 3) if wall_z else None,
        "emissive_pixels": emissive_px,
        "cycle_mask_pixels_outside_ranges": emissive_bad,
    }
