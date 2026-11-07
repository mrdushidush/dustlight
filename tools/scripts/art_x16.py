"""HD art made only from the original pixels: no model, nothing invented. The release uses it for the characters
(the author, 2026-11-06); the rest of the art stays the upscaler's. Other kinds can be built for tests with --kinds.

  art_x16.py --source <hdpack\\art> --game <game folder> --texconv <texconv.exe> --out <folder>
             [--kinds critters,...] [--tiers 2,3,4] [--threads 8] [--seed tier:folder ...]

Every frame goes through Scale2x four times (X16: each new pixel is a copy of the old pixel or a neighbor, following
diagonal edges), then is averaged down to the tier. More doublings change nothing visible: X64 differs from X16 by at
most 2 of 255.

Floors, roofs and walls tile against each other and the engine cuts them with the 1x mask (hard_alpha), so their color
runs past the mask: transparent pixels take the nearest color before scaling, or each piece's edge would get rounded
off. Everything else gets the same treatment on its silhouette as on its inside. Painted shadows stay.

--seed reuses finished tiers from another folder holding <kind>\\<name>\\albedo_<tier>x.dds, as hard links: same bytes,
no extra disk. Writes <out>\\art\\<kind>\\<name>\\{pack.json, albedo_<tier>x.dds}; each pack keeps the source pack's layout
and hashes, so the engine loads it like any other. A file already there is skipped, so a run can be resumed. While
the game runs (dustlight*.exe) it waits instead of compressing on the CPU.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from fo2hd import frm as frm_mod
from fo2hd.dat import GameFiles

KINDS = ("critters",)
BATCH = 128
DOUBLINGS = 4
MODEL = "edge16"  # what the pack says made it


def scale2x(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    B, D, F, H, E = p[:-2, 1:-1], p[1:-1, :-2], p[1:-1, 2:], p[2:, 1:-1], a
    go = (B != H) & (D != F)
    out = np.empty((a.shape[0] * 2, a.shape[1] * 2), a.dtype)
    out[0::2, 0::2] = np.where(go & (D == B), D, E)
    out[0::2, 1::2] = np.where(go & (B == F), F, E)
    out[1::2, 0::2] = np.where(go & (D == H), D, E)
    out[1::2, 1::2] = np.where(go & (H == F), F, E)
    return out


def nearest_fill(values: np.ndarray, known: np.ndarray) -> np.ndarray:
    """Unknown pixels take the value of the nearest known one (works on indexes and on rgb)."""
    if known.all() or not known.any():
        return values
    _, labels = cv2.distanceTransformWithLabels((~known).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lut = np.zeros((labels.max() + 1, *values.shape[2:]), values.dtype)
    lut[labels[known]] = values[known]
    return lut[labels]


def lut16_for(palette: np.ndarray) -> np.ndarray:
    return (palette.astype(np.uint32) * 257).astype(np.uint16).T.copy()


def frame_rgba(px: np.ndarray, lut16: np.ndarray, hard: bool, tier: int) -> np.ndarray:
    """16-bit planes rather than floats: big interface frames are thousands of pixels on a side at X16."""
    h, w = px.shape
    size = (w * tier, h * tier)
    big = nearest_fill(px, px != 0) if hard else px
    for _ in range(DOUBLINGS):
        big = scale2x(big)
    if hard:
        color = np.dstack([cv2.resize(lut16[c][big], size, interpolation=cv2.INTER_AREA) for c in range(3)]).astype(np.float32)
        rgb = np.round(color / 257.0).clip(0, 255).astype(np.uint8)
        alpha = ((px != 0) * 255).astype(np.uint8).repeat(tier, 0).repeat(tier, 1)
        return np.dstack([rgb, alpha])
    keep = big != 0
    a = cv2.resize(keep.astype(np.uint16) * 65535, size, interpolation=cv2.INTER_AREA).astype(np.float32)
    # Transparent pixels add no color to the average.
    color = np.dstack([cv2.resize(lut16[c][big] * keep, size, interpolation=cv2.INTER_AREA) for c in range(3)]).astype(np.float32)
    rgb = np.round(color / np.maximum(a[..., None], 1.0) * 255.0).clip(0, 255).astype(np.uint8)
    return np.dstack([nearest_fill(rgb, a > 0), np.round(a / 257.0).astype(np.uint8)])


def game_is_running() -> bool:
    return "dustlight" in subprocess.run(["tasklist", "/NH"], capture_output=True, text=True).stdout.lower()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--game", required=True)
    ap.add_argument("--texconv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--tiers", default="2,3,4")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--seed", action="append", default=[], help="tier:folder")
    args = ap.parse_args()
    ctypes.windll.kernel32.SetPriorityClass(ctypes.c_void_p(-1), 0x00004000)  # below normal

    source, art = Path(args.source), Path(args.out) / "art"
    tiers = [int(t) for t in args.tiers.split(",")]
    kinds = [k.strip() for k in args.kinds.split(",")]
    seeds = [(int(s.split(":", 1)[0]), Path(s.split(":", 1)[1])) for s in args.seed]
    files = GameFiles(Path(args.game))
    palette = frm_mod.load_palette(files.read("color.pal"))
    lut16 = lut16_for(palette)
    art.mkdir(parents=True, exist_ok=True)

    packs = [(k, d.name) for k in kinds for d in sorted((source / k).iterdir()) if (d / "pack.json").exists()]
    print(f"{len(packs)} packs, tiers {tiers}", flush=True)
    seeded = 0
    for kind, name in packs:
        target = art / kind / name
        for tier, folder in seeds:
            there = folder / kind / name / f"albedo_{tier}x.dds"
            if kind == "critters" and not there.exists():
                there = folder / name / f"albedo_{tier}x.dds"  # character folders hold packs directly
            mine = target / f"albedo_{tier}x.dds"
            if there.exists() and not mine.exists():
                target.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(there, mine)
                except OSError:
                    shutil.copy2(there, mine)
                seeded += 1
    print(f"{seeded} files reused", flush=True)

    start = time.perf_counter()

    def prepare(item: tuple[str, str], tier: int, staging: Path) -> tuple[str, str, Path, dict]:
        kind, name = item
        pack = json.loads((source / kind / name / "pack.json").read_text(encoding="utf-8"))
        frm = frm_mod.decode(files.read(pack["source"]))
        W, H = pack["layout"]
        atlas = np.zeros((H * tier, W * tier, 4), np.uint8)
        hard = bool(pack.get("hard_alpha"))
        for r, rot in enumerate(pack["rotations"]):
            for f, (x, y, w, h, _hash) in enumerate(rot.get("frames", [])):
                atlas[y * tier : (y + h) * tier, x * tier : (x + w) * tier] = frame_rgba(frm.frames[r][f].pixels, lut16, hard, tier)
        png = staging / f"{kind}__{name}.png"
        Image.fromarray(atlas, "RGBA").save(png, compress_level=1)
        pack["model"] = MODEL
        pack["tiers"] = tiers
        return kind, name, png, pack

    with tempfile.TemporaryDirectory(prefix="fo2hd-x16-", dir=args.out) as tmp:
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
                    for kind, name, png, pack in ready:
                        target = art / kind / name
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(comp / f"{png.stem}.dds"), str(target / dds))
                        png.unlink()
                    print(f"  tier {tier}: {begin + len(ready)}/{len(todo)}, {time.perf_counter() - start:.0f} s", flush=True)

    # Written last, for every pack, so a seeded pack gets one too and nothing says it's done early.
    missing = 0
    for kind, name in packs:
        pack = json.loads((source / kind / name / "pack.json").read_text(encoding="utf-8"))
        pack["model"] = MODEL
        pack["tiers"] = tiers
        (art / kind / name / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
        missing += sum(not (art / kind / name / f"albedo_{t}x.dds").exists() for t in tiers)
    print(f"done, {missing} files missing", flush=True)


if __name__ == "__main__":
    main()
