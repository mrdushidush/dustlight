"""The world's art with less of the model's invented detail (the author, 2026-11-06): each upscaled master mixed with the X16
copy of the same frame. 60% X16 on floors and roofs, where grass turned to fur and floors grew cracks; 35% on walls and
scenery, which stay sharp. Talking heads, interface, items and fonts aren't touched.

  art_mix.py --source <hdpack\\art> --masters <hd\\art> --game <game folder> --texconv <texconv.exe> --out <folder>
             [--mix tiles=0.6,walls=0.35,scenery=0.35] [--tone] [--grain <grain.json> [--grain-ramp 0.45,0.8]
             [--grain-low 0.25]] [--joins] [--tiers 2,3,4] [--threads 6]

--tone takes the masters' slow color drift out first (tone_true), as in the look tests of 2026-11-08.
--joins takes the seams the model draws along floor and roof tile edges out of the masters (join_clean, 2026-11-09).
--grain gives each pack its own mix (look tests round 4): grain-heavy originals (sand, dirt, grass) keep the --mix
level, structured or flat ones (pavement, bricks, roads) get --grain-low of it, with a smooth ramp between the two
grain scores of --grain-ramp. grain.json: {kind: {name: [score 0..1, category]}}, from the look tests' look_grain.py.
--alt-masters <dir> --alt <list.json> takes the masters of the listed packs (["tiles/plk2000", ...]) from <dir> instead
(same layout as --masters): roof tiles whose joins look better upscaled without the edge cleanup (look_roof_plain.py).

Writes <out>\\art\\<kind>\\<name>\\{pack.json, albedo_<tier>x.dds}. Each pack keeps the source's layout, hashes and model,
since the art is still made with it, and adds "x16_mix". A file already there is skipped, so a run can be resumed.
While the game runs it waits instead of compressing on the CPU.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cv2
import numpy as np
from art_x16 import frame_rgba, game_is_running, lut16_for, nearest_fill
from PIL import Image

from fo2hd import frm as frm_mod
from fo2hd.dat import GameFiles

BATCH = 128


TONE_VERSION = 2


def tone_true(master: np.ndarray, px: np.ndarray, palette: np.ndarray, sigma: float = 1.5, iters: int = 2) -> np.ndarray:
    """The master with its slow color and brightness drift taken out (look tests, 2026-11-08): seen through a blur of a
    few original pixels it matches the original; the model's fine detail and the original's dither are left alone.

    Version 2 (2026-11-09, the author's lines on Broken Hills' roads and roofs): the master's own pixels only. A floor or roof
    master keeps the model's view of its neighbors (copies of itself) outside the tile, and the blur took them in: where
    a tile's opposite edges differ, version 1 drew a dark or light band about one original pixel wide along the join.
    Outside the tile both sides now see the nearest pixel inside it, as the target always did."""
    h, w = px.shape
    known = px != 0
    ref = nearest_fill(palette[px], known).astype(np.float32)
    target = cv2.GaussianBlur(ref, (0, 0), sigma)
    rgb = master[..., :3].astype(np.float32)
    for _ in range(iters):
        small = nearest_fill(cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA), known)
        err = target - cv2.GaussianBlur(small, (0, 0), sigma)
        rgb = np.clip(rgb + cv2.resize(err, (w * 4, h * 4), interpolation=cv2.INTER_LINEAR), 0, 255)
    return np.dstack([np.round(rgb).astype(np.uint8), master[..., 3]])


# Joins (2026-11-09): a floor or roof tile is upscaled amid copies of itself (hdassets lattice context), so along an
# edge where the tile doesn't continue into itself the model draws a seam into the tile's outermost pixels, a straight
# line in the game wherever the real neighbor does continue. join_clean takes out of each edge band what runs along the
# edge in the master but not in the X16 copy: per edge, the master's difference from X16 is averaged along the edge
# (a gaussian of JOIN_ALONG px) per distance from the join; what of that differs from the level further in
# (JOIN_LEVEL) is subtracted, fully up to JOIN_BAND px from the join, fading out at JOIN_FADE. Detail that doesn't run
# along the edge averages out and stays. Lines the original has along its edges are in X16 too and stay. Roofs compare
# with X16 of the edge-cleaned original (hdassets lattice-2), so the specks the cleanup took out don't come back.
JOIN_VERSION = 1
JOIN_LATTICE = ((48, -12), (32, 24))  # hdassets.TILE_LATTICE
JOIN_BAND, JOIN_FADE, JOIN_LEVEL, JOIN_ALONG, JOIN_BIN = 6, 10, (12, 24), 32.0, 4
_join_geo: dict = {}


def _join_geometry(mask1: np.ndarray) -> list:
    """Per edge of the tile: (flat index of its own 4x pixels within JOIN_LEVEL[1] of the neighbor, distance, bin
    along the edge, number of bins)."""
    key = (mask1.shape, mask1.tobytes())
    if key in _join_geo:
        return _join_geo[key]
    m = np.repeat(np.repeat(mask1, 4, 0), 4, 1)
    H, W = m.shape
    yy, xx = np.mgrid[0:H, 0:W]
    out = []
    (ax, ay), (bx, by) = JOIN_LATTICE
    for (dx, dy), (ux, uy) in (((ax, ay), (bx, by)), ((-ax, -ay), (bx, by)), ((bx, by), (ax, ay)), ((-bx, -by), (ax, ay))):
        nb = np.zeros_like(m)
        sx, sy = dx * 4, dy * 4
        if abs(sx) >= W or abs(sy) >= H:
            continue  # a frame smaller than the step to its neighbor: no join inside it
        nb[max(sy, 0) : H + min(sy, 0), max(sx, 0) : W + min(sx, 0)] = m[max(-sy, 0) : H + min(-sy, 0), max(-sx, 0) : W + min(-sx, 0)]
        if not nb.any():
            continue
        dist = cv2.distanceTransform((~nb).astype(np.uint8), cv2.DIST_L2, 5)
        sel = m & (dist <= JOIN_LEVEL[1])
        if (sel & (dist <= JOIN_BAND)).sum() < 16:
            continue
        p = (xx[sel] * ux + yy[sel] * uy) / np.hypot(ux, uy)
        pb = ((p - p.min()) // JOIN_BIN).astype(np.int32)
        out.append((np.flatnonzero(sel), np.round(dist[sel]).astype(np.int32), pb, int(pb.max()) + 1))
    _join_geo[key] = out
    return out


def join_ref(px: np.ndarray, palette: np.ndarray, lut16: np.ndarray, cleaned: bool) -> np.ndarray:
    """The X16 copy (4x, float rgb) that join_clean compares with: of the edge-cleaned original where the master was
    made from it (roofs, hdassets lattice-2; not the --alt masters, which are made without the cleanup)."""
    if cleaned:
        from fo2hd.hdassets import clean_tile_edges

        rgb, n = clean_tile_edges(px, palette)
        if n:
            px = px.copy()
            changed = (rgb != palette[px]).any(axis=2) & (px != 0)
            cand = palette[1:229].astype(np.float32)
            d = ((rgb[changed].astype(np.float32)[:, None, :] - cand[None]) ** 2).sum(axis=2)
            px[changed] = (d.argmin(axis=1) + 1).astype(px.dtype)
    return frame_rgba(px, lut16, True, 4)[..., :3].astype(np.float32)


def join_clean(master: np.ndarray, px: np.ndarray, ref: np.ndarray) -> np.ndarray:
    rgb = master[..., :3].astype(np.float32).reshape(-1, 3)
    flat_ref = ref.reshape(-1, 3)
    out = rgb.copy()
    fade = np.clip((JOIN_FADE - np.arange(JOIN_LEVEL[1] + 1)) / (JOIN_FADE - JOIN_BAND), 0.0, 1.0).astype(np.float32)
    for idx, t, pb, nbins in _join_geometry(px != 0):
        d = rgb[idx] - flat_ref[idx]
        T = JOIN_LEVEL[1] + 1
        sums = np.zeros((T, nbins, 3), np.float32)
        counts = np.zeros((T, nbins), np.float32)
        np.add.at(sums, (t, pb), d)
        np.add.at(counts, (t, pb), 1.0)
        centers = np.arange(nbins, dtype=np.float32) * JOIN_BIN
        g = np.exp(-0.5 * ((centers[:, None] - centers[None, :]) / JOIN_ALONG) ** 2).astype(np.float32)
        s2 = np.einsum("tpc,pq->tqc", sums, g)
        c2 = counts @ g
        mean = s2 / np.maximum(c2, 1e-3)[..., None]
        lw = c2[JOIN_LEVEL[0] :]
        level = (mean[JOIN_LEVEL[0] :] * lw[..., None]).sum(axis=0) / np.maximum(lw.sum(axis=0), 1e-3)[:, None]
        ok = (lw.sum(axis=0) >= 1.0)[None, :, None] & (c2 >= 0.5)[..., None]
        corr = np.where(ok, (mean - level[None]) * fade[:, None, None], 0.0)
        out[idx] -= corr[t, pb]
    rgb8 = np.round(out.reshape(master.shape[0], master.shape[1], 3)).clip(0, 255).astype(np.uint8)
    return np.dstack([rgb8, master[..., 3]])


def mixed_frame(px: np.ndarray, master: np.ndarray, lut16: np.ndarray, hard: bool, tier: int, mix: float) -> np.ndarray:
    h, w = px.shape
    size = (w * tier, h * tier)
    ai = master if tier == 4 else cv2.resize(master, size, interpolation=cv2.INTER_AREA)
    x16 = frame_rgba(px, lut16, hard, tier)
    rgb = ai[..., :3].astype(np.float32) * (1.0 - mix) + x16[..., :3].astype(np.float32) * mix
    if hard:
        # Coverage comes from the 1x mask, and the edge fix in the engine draws a little past it: color must run on.
        alpha = x16[..., 3]
        rgb8 = np.round(rgb).clip(0, 255).astype(np.uint8)
        return np.dstack([nearest_fill(rgb8, alpha > 0), alpha])
    alpha = np.round(ai[..., 3].astype(np.float32) * (1.0 - mix) + x16[..., 3].astype(np.float32) * mix).clip(0, 255).astype(np.uint8)
    rgb8 = np.round(rgb).clip(0, 255).astype(np.uint8)
    return np.dstack([nearest_fill(rgb8, alpha > 0), alpha])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--masters", required=True)
    ap.add_argument("--game", required=True)
    ap.add_argument("--texconv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mix", default="tiles=0.6,walls=0.35,scenery=0.35")
    ap.add_argument("--tiers", default="2,3,4")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--only", default="", help="pack names, for a test")
    ap.add_argument("--tone", action="store_true", help="tone-true masters before mixing")
    ap.add_argument("--joins", action="store_true", help="take the seams along floor and roof tile edges out")
    ap.add_argument("--grain", default="", help="per-pack grain scores: mix chosen per pack")
    ap.add_argument("--grain-ramp", default="0.45,0.8")
    ap.add_argument("--grain-low", type=float, default=0.25)
    ap.add_argument("--alt-masters", default="")
    ap.add_argument("--alt", default="", help="JSON list of kind/name whose masters come from --alt-masters")
    ap.add_argument("--roof-mix-min", type=float, default=0.0,
                    help="roof tiles get at least this much X16 (look tests round 7: ribbed roofs differ tile by tile)")
    args = ap.parse_args()
    ctypes.windll.kernel32.SetPriorityClass(ctypes.c_void_p(-1), 0x00004000)  # below normal

    source, masters, art = Path(args.source), Path(args.masters), Path(args.out) / "art"
    mixes = {k: float(v) for k, v in (item.split("=") for item in args.mix.split(","))}
    tiers = [int(t) for t in args.tiers.split(",")]
    only = {n.strip().lower() for n in args.only.split(",") if n.strip()}
    files = GameFiles(Path(args.game))
    palette = frm_mod.load_palette(files.read("color.pal"))
    lut16 = lut16_for(palette)
    packs = [(k, d.name) for k in mixes for d in sorted((source / k).iterdir()) if (d / "pack.json").exists() and (not only or d.name.lower() in only)]
    grain = json.loads(Path(args.grain).read_text(encoding="utf-8")) if args.grain else None
    g0, g1 = (float(v) for v in args.grain_ramp.split(","))
    alt = set(json.loads(Path(args.alt).read_text(encoding="utf-8"))) if args.alt else set()
    alt_masters = Path(args.alt_masters) if args.alt_masters else None
    if alt and alt_masters is None:
        raise SystemExit("--alt needs --alt-masters")

    categories: dict = {}

    def category_of(kind: str, name: str) -> str:
        if (kind, name) not in categories:
            pack = json.loads((source / kind / name / "pack.json").read_text(encoding="utf-8"))
            categories[(kind, name)] = pack.get("category", "")
        return categories[(kind, name)]

    def mix_of(kind: str, name: str) -> float:
        if grain is None:
            mix = mixes[kind]
        else:
            t = min(max((grain[kind][name][0] - g0) / (g1 - g0), 0.0), 1.0)
            mix = round(mixes[kind] * (args.grain_low + (1.0 - args.grain_low) * t * t * (3.0 - 2.0 * t)), 4)
        if args.roof_mix_min > 0.0 and category_of(kind, name) == "roof":
            mix = max(mix, args.roof_mix_min)
        return mix

    print(f"{len(packs)} packs, mix {mixes}, tiers {tiers}" + (f", per pack from {args.grain}, ramp {g0}-{g1}, low {args.grain_low}" if grain else ""), flush=True)
    start = time.perf_counter()

    def prepare(item: tuple[str, str], tier: int, staging: Path) -> tuple[str, str, Path]:
        kind, name = item
        pack = json.loads((source / kind / name / "pack.json").read_text(encoding="utf-8"))
        meta = json.loads((masters / kind / name / "meta.json").read_text(encoding="utf-8"))
        frm = frm_mod.decode(files.read(pack["source"]))
        W, H = pack["layout"]
        atlas = np.zeros((H * tier, W * tier, 4), np.uint8)
        hard = bool(pack.get("hard_alpha"))
        for r, rot in enumerate(pack["rotations"]):
            for f, (x, y, w, h, _hash) in enumerate(rot.get("frames", [])):
                base = alt_masters / name if f"{kind}/{name}" in alt else masters / kind / name
                master = np.asarray(Image.open(base / f"{meta['rotations'][r]['frames'][f]['name']}.albedo.png").convert("RGBA"))
                if args.tone:
                    master = tone_true(master, frm.frames[r][f].pixels, palette)
                if args.joins and kind == "tiles":
                    fpx = frm.frames[r][f].pixels
                    cleaned = pack.get("category") == "roof" and f"{kind}/{name}" not in alt
                    master = join_clean(master, fpx, join_ref(fpx, palette, lut16, cleaned))
                atlas[y * tier : (y + h) * tier, x * tier : (x + w) * tier] = mixed_frame(frm.frames[r][f].pixels, master, lut16, hard, tier, mix_of(kind, name))
        png = staging / f"{kind}__{name}.png"
        Image.fromarray(atlas, "RGBA").save(png, compress_level=1)
        return kind, name, png

    Path(args.out).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fo2hd-mix-", dir=args.out) as tmp:
        staging = Path(tmp)
        comp = staging / "out"
        comp.mkdir()
        with ThreadPoolExecutor(args.threads) as pool:
            for tier in tiers:
                dds = f"albedo_{tier}x.dds"
                todo = [p for p in packs if not (art / p[0] / p[1] / dds).exists()]
                print(f"tier {tier}: {len(todo)} to build", flush=True)
                for begin in range(0, len(todo), BATCH):
                    ready = list(pool.map(lambda it: prepare(it, tier, staging), todo[begin : begin + BATCH]))
                    while game_is_running():
                        time.sleep(20)
                    listing = comp / "files.txt"
                    listing.write_text("\n".join(str(r[2]) for r in ready), encoding="utf-8")
                    result = subprocess.run([args.texconv, "-nologo", "-y", "-f", "BC7_UNORM", "-m", "1", "-o", str(comp), "-flist", str(listing)], capture_output=True, text=True)
                    if result.returncode != 0:
                        raise RuntimeError(f"texconv failed ({result.returncode}): {result.stdout[-1500:]} {result.stderr[-500:]}")
                    for kind, name, png in ready:
                        target = art / kind / name
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(comp / f"{png.stem}.dds"), str(target / dds))
                        png.unlink()
                    print(f"  tier {tier}: {begin + len(ready)}/{len(todo)}, {time.perf_counter() - start:.0f} s", flush=True)

    missing = 0
    for kind, name in packs:
        pack = json.loads((source / kind / name / "pack.json").read_text(encoding="utf-8"))
        pack["x16_mix"] = mix_of(kind, name)
        if grain is not None:
            pack["grain"] = grain[kind][name][0]
        if f"{kind}/{name}" in alt:
            pack["master_alt"] = "no edge cleanup"
        if args.tone:
            pack["tone_true"] = TONE_VERSION
        if args.joins and kind == "tiles":
            pack["join_clean"] = JOIN_VERSION
        (art / kind / name / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
        missing += sum(not (art / kind / name / f"albedo_{t}x.dds").exists() for t in tiers)
    print(f"done, {missing} files missing", flush=True)


if __name__ == "__main__":
    main()
