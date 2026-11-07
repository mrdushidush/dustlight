"""Character art made only from the original pixels: no model, nothing invented.

  character_packs.py --shipped <hd\\art\\critters> --game <game folder> --texconv <texconv.exe>
                     --out <folder> --mode edge|edge16 [--tier 3] [--only name,name] [--limit N]
                     [--shadow-alpha 0.43]

Every frame goes through the AdvMAME/Scale family (Scale3x at tier 3, repeated Scale2x otherwise):
each new pixel is a copy of the old pixel or of a neighbor, following diagonal edges.
  edge    copies straight to the tier's size. No new colors at all, before compression.
  edge16  copies to 16x, then averaged down to the tier. Softer stair-steps, some in-between colors.

--shadow-alpha turns the shadow painted into each frame see-through, so it doesn't sit as a black blob under the
game's own soft shadow. Cutting it out leaves holes in feet, since feet are outlined in the same color; a pixel
wrongly taken for shadow only gets lighter here.

Each pack keeps the shipped pack.json's layout and hashes, so the engine loads it like any other.
Only the tier asked for is written. A folder with both files is skipped, so a run can be resumed.
"""

from __future__ import annotations

import argparse
import ctypes
import json
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

BATCH = 128
SHADOW_INDEXES = (207, 100)  # the near-blacks the painted shadows are made of (outlines use them too); some characters' are 100
SHADOW_MARK = 256  # stands in for a shadow pixel while scaling: equal only to itself, like a palette index


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


def scale3x(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    A, B, C = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    D, E, F = p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:]
    G, H, I = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    go = (B != H) & (D != F)

    def pick(cond: np.ndarray, src: np.ndarray) -> np.ndarray:
        return np.where(go & cond, src, E)

    out = np.empty((a.shape[0] * 3, a.shape[1] * 3), a.dtype)
    out[0::3, 0::3] = pick(D == B, D)
    out[0::3, 1::3] = pick(((D == B) & (E != C)) | ((B == F) & (E != A)), B)
    out[0::3, 2::3] = pick(B == F, F)
    out[1::3, 0::3] = pick(((D == B) & (E != G)) | ((D == H) & (E != A)), D)
    out[1::3, 1::3] = E
    out[1::3, 2::3] = pick(((B == F) & (E != I)) | ((H == F) & (E != C)), F)
    out[2::3, 0::3] = pick(D == H, D)
    out[2::3, 1::3] = pick(((D == H) & (E != I)) | ((H == F) & (E != G)), H)
    out[2::3, 2::3] = pick(H == F, F)
    return out


def bleed(rgb: np.ndarray, opaque: np.ndarray) -> np.ndarray:
    """Transparent pixels take their nearest opaque color, so compression spends nothing on them."""
    if opaque.all() or not opaque.any():
        return rgb
    _, labels = cv2.distanceTransformWithLabels((~opaque).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lut = np.zeros((labels.max() + 1, 3), np.uint8)
    lut[labels[opaque]] = rgb[opaque]
    return lut[labels]


def baked_shadow(pixels: np.ndarray) -> np.ndarray:
    """The frame's painted shadow: patches of the shadow color reaching its lower part. Thin outlines
    higher up stay out, as they don't reach down there or are too small."""
    h = pixels.shape[0]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(np.isin(pixels, SHADOW_INDEXES).astype(np.uint8), connectivity=8)
    keep = [i for i in range(1, n) if stats[i][1] + stats[i][3] >= h * 0.6 and stats[i][4] >= 3]
    return np.isin(labels, keep)


def frame_rgba(pixels: np.ndarray, palette: np.ndarray, mode: str, tier: int, shadow_alpha: float | None = None) -> np.ndarray:
    h, w = pixels.shape
    if shadow_alpha is not None:
        marked = pixels.astype(np.uint16)
        marked[baked_shadow(pixels)] = SHADOW_MARK
        pixels = marked
        palette = np.vstack([palette, palette[SHADOW_INDEXES[0]]])
    if mode == "edge":
        big = {2: lambda a: scale2x(a), 3: scale3x, 4: lambda a: scale2x(scale2x(a))}[tier](pixels)
        rgb = palette[big].astype(np.uint8)
        opaque = big != 0
        alpha8 = opaque.astype(np.uint8) * 255
        if shadow_alpha is not None:
            alpha8[big == SHADOW_MARK] = round(shadow_alpha * 255)
        return np.dstack([bleed(rgb, opaque), alpha8])
    big = scale2x(scale2x(scale2x(scale2x(pixels))))
    rgb = palette[big].astype(np.float32)
    alpha = (big != 0).astype(np.float32)
    if shadow_alpha is not None:
        alpha[big == SHADOW_MARK] = shadow_alpha
    size = (w * tier, h * tier)
    pm = cv2.resize(rgb * alpha[..., None], size, interpolation=cv2.INTER_AREA)
    a = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    color = np.round(pm / np.maximum(a[..., None], 1e-3)).clip(0, 255).astype(np.uint8)
    return np.dstack([bleed(color, a > 0), np.round(a * 255).astype(np.uint8)])


def atlas_for(pack: dict, frm, palette: np.ndarray, mode: str, tier: int, shadow_alpha: float | None = None) -> np.ndarray:
    W, H = pack["layout"]
    atlas = np.zeros((H * tier, W * tier, 4), np.uint8)
    for r, rot in enumerate(pack["rotations"]):
        for f, (x, y, w, h, _hash) in enumerate(rot.get("frames", [])):
            pixels = frm.frames[r][f].pixels
            atlas[y * tier : (y + h) * tier, x * tier : (x + w) * tier] = frame_rgba(pixels, palette, mode, tier, shadow_alpha)
    return atlas


def game_is_running() -> bool:
    out = subprocess.run(["tasklist", "/NH"], capture_output=True, text=True).stdout
    return "dustlight" in out.lower()  # dustlight.exe and the test builds beside it


def compress(texconv: Path, pngs: list[Path], out_dir: Path) -> None:
    listing = out_dir / "files.txt"
    listing.write_text("\n".join(str(p) for p in pngs), encoding="utf-8")
    cmd = [str(texconv), "-nologo", "-y", "-f", "BC7_UNORM", "-m", "1", "-o", str(out_dir), "-flist", str(listing)]
    if game_is_running():
        cmd.insert(1, "-nogpu")  # leaves the GPU to whoever is playing
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed ({result.returncode}): {result.stdout[-1500:]} {result.stderr[-500:]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shipped", required=True)
    parser.add_argument("--game", required=True)
    parser.add_argument("--texconv", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", choices=("edge", "edge16"), required=True)
    parser.add_argument("--tier", type=int, default=3)
    parser.add_argument("--only", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shadow-alpha", type=float, default=None)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--high-priority", action="store_true", help="the author's OK needed: the PC comes second")
    args = parser.parse_args()
    # High or below normal. The handle needs its real size, or the call quietly fails.
    ctypes.windll.kernel32.SetPriorityClass(ctypes.c_void_p(-1), 0x00000080 if args.high_priority else 0x00004000)

    shipped, out = Path(args.shipped), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = GameFiles(Path(args.game))
    palette = frm_mod.load_palette(files.read("color.pal"))
    names = sorted(p.name for p in shipped.iterdir() if (p / "pack.json").exists())
    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        names = [n for n in names if n.lower() in wanted or n[:6].lower() in wanted]
    if args.limit:
        names = names[: args.limit]
    dds_name = f"albedo_{args.tier}x.dds"
    todo = [n for n in names if not ((out / n / "pack.json").exists() and (out / n / dds_name).exists())]
    print(f"{len(names)} packs, {len(todo)} to build, mode {args.mode}, tier {args.tier}", flush=True)
    start = time.perf_counter()
    done = 0
    with tempfile.TemporaryDirectory(prefix="fo2hd-chars-", dir=out) as tmp:
        staging = Path(tmp)
        comp = staging / "out"
        comp.mkdir()

        def prepare(name: str) -> tuple[str, dict, Path]:
            pack = json.loads((shipped / name / "pack.json").read_text(encoding="utf-8"))
            frm = frm_mod.decode(files.read(pack["source"]))
            atlas = atlas_for(pack, frm, palette, args.mode, args.tier, args.shadow_alpha)
            png = staging / f"{name}.png"
            Image.fromarray(atlas, "RGBA").save(png, compress_level=1)
            pack["model"] = args.mode
            pack["hard_alpha"] = False
            pack["tiers"] = [args.tier]
            return name, pack, png

        with ThreadPoolExecutor(args.threads) as pool:
            for begin in range(0, len(todo), BATCH):
                ready = list(pool.map(prepare, todo[begin : begin + BATCH]))
                compress(Path(args.texconv), [png for *_, png in ready], comp)
                for name, pack, png in ready:
                    target = out / name
                    target.mkdir(exist_ok=True)
                    shutil.move(str(comp / (png.stem + ".dds")), str(target / dds_name))
                    (target / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
                    png.unlink()
                done += len(ready)
                print(f"  {done}/{len(todo)}, {time.perf_counter() - start:.0f} s", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
