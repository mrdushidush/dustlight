"""The world's art with less of the model's invented detail (the author, 2026-11-06): each upscaled master mixed with the X16
copy of the same frame. 60% X16 on floors and roofs, where grass turned to fur and floors grew cracks; 35% on walls and
scenery, which stay sharp. Talking heads, interface, items and fonts aren't touched.

  art_mix.py --source <hdpack\\art> --masters <hd\\art> --game <game folder> --texconv <texconv.exe> --out <folder>
             [--mix tiles=0.6,walls=0.35,scenery=0.35] [--tiers 2,3,4] [--threads 6]

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
    args = ap.parse_args()
    ctypes.windll.kernel32.SetPriorityClass(ctypes.c_void_p(-1), 0x00004000)  # below normal

    source, masters, art = Path(args.source), Path(args.masters), Path(args.out) / "art"
    mixes = {k: float(v) for k, v in (item.split("=") for item in args.mix.split(","))}
    tiers = [int(t) for t in args.tiers.split(",")]
    only = {n.strip().lower() for n in args.only.split(",") if n.strip()}
    files = GameFiles(Path(args.game))
    lut16 = lut16_for(frm_mod.load_palette(files.read("color.pal")))
    packs = [(k, d.name) for k in mixes for d in sorted((source / k).iterdir()) if (d / "pack.json").exists() and (not only or d.name.lower() in only)]
    print(f"{len(packs)} packs, mix {mixes}, tiers {tiers}", flush=True)
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
                master = np.asarray(Image.open(masters / kind / name / f"{meta['rotations'][r]['frames'][f]['name']}.albedo.png").convert("RGBA"))
                atlas[y * tier : (y + h) * tier, x * tier : (x + w) * tier] = mixed_frame(frm.frames[r][f].pixels, master, lut16, hard, tier, mixes[kind])
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
        pack["x16_mix"] = mixes[kind]
        (art / kind / name / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
        missing += sum(not (art / kind / name / f"albedo_{t}x.dds").exists() for t in tiers)
    print(f"done, {missing} files missing", flush=True)


if __name__ == "__main__":
    main()
