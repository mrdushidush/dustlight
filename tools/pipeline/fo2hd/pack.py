"""Runtime HD art packs (PLAN.md P4.1).

Turns the 4x masters written by `fo2hd hdassets` into what the engine loads:
one atlas per tier (2x, 3x, 4x) holding every stored frame of the FRM,
BC7-compressed by Microsoft's texconv, plus `pack.json`.

  <data_root>/hdpack/<art path>/pack.json
  <data_root>/hdpack/<art path>/albedo_2x.dds, albedo_3x.dds, albedo_4x.dds

All tiers share one layout, given in 1x pixels: a frame sits at (X, Y) with
X and Y multiples of 4 and its cell rounded up to multiples of 4, so at any
tier t the frame starts at (t*X, t*Y) on a BC7 block boundary and no block
mixes two frames. The 2x and 3x tiers are area-downscaled from the 4x master
(pre-filtered, so the runtime samples 1 texel : 1 pixel; ADR-0003).

pack.json lists, per stored rotation, each frame's cell and size plus an
FNV-1a hash of its original palette indices. The engine compares that hash
with the art it actually draws and falls back to the original art on a
mismatch (a patch or mod replaced the FRM).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import frm as frm_mod
from . import hdassets
from .config import Paths
from .dat import GameFiles
from .jobs import JobDB, fingerprint, write_atomic

PACK_VERSION = 1
TIERS = (2, 3, 4)
MAX_ATLAS_1X = 1024  # 4096 texels wide at 4x
HARD_ALPHA = {"floor", "roof", "wall"}  # tile against neighbors: the engine uses the 1x mask
TEXCONV_BATCH = 192


def texconv_path(paths: Paths) -> Path:
    tools = paths.data_root / "tools"
    found = sorted(tools.glob("texconv-*/texconv.exe"))
    if not found:
        raise SystemExit(f"texconv.exe not found under {tools}\\texconv-<release>\\ (see tools/pipeline/README.md)")
    return found[-1]


def pack_dir(paths: Paths, game_path: str) -> Path:
    return paths.data_root / "hdpack" / hdassets.asset_dir(paths, game_path).relative_to(paths.data_root / "hd")


def fnv1a(pixels: np.ndarray) -> int:
    """32-bit FNV-1a over the frame's palette indices, row by row (the
    engine hashes artGetFrameData the same way)."""
    h = 2166136261
    for b in np.ascontiguousarray(pixels).tobytes():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _ceil4(v: int) -> int:
    return (v + 3) & ~3


def layout(sizes: list[tuple[int, int]]) -> tuple[int, int, list[tuple[int, int]]]:
    """Shelf-packs frame cells (1x pixels). Returns atlas width, height and
    each frame's (X, Y), all multiples of 4."""
    cells = [(_ceil4(max(w, 1)), _ceil4(max(h, 1))) for w, h in sizes]
    area = sum(w * h for w, h in cells)
    # The cap only limits the square-ish guess: a frame wider than the cap
    # (f2_res's 1280-wide screens) widens its own atlas.
    width = max(max(w for w, _ in cells), min(MAX_ATLAS_1X, _ceil4(int(math.sqrt(area) * 1.15))))
    order = sorted(range(len(cells)), key=lambda i: (-cells[i][1], -cells[i][0]))
    pos = [(0, 0)] * len(cells)
    x = y = shelf = 0
    for i in order:
        w, h = cells[i]
        if x + w > width:
            x, y, shelf = 0, y + shelf, 0
        pos[i] = (x, y)
        x += w
        shelf = max(shelf, h)
    return width, _ceil4(y + shelf), pos


def _resize(rgba: np.ndarray, w: int, h: int) -> np.ndarray:
    if rgba.shape[1] == w and rgba.shape[0] == h:
        return rgba
    return cv2.resize(rgba, (w, h), interpolation=cv2.INTER_AREA)


def build_one(game_path: str, data: bytes, master: Path, out_dir: Path, staging: Path, tag: str) -> tuple[dict, list[tuple[Path, Path]]]:
    """Writes the tier PNGs into `staging` and returns pack.json plus the
    (png, final dds) pairs still to be compressed."""
    meta = json.loads((master / "meta.json").read_text(encoding="utf-8"))
    frm = frm_mod.decode(data)
    stored = []  # (rotation, frame index, Frame, master name)
    rotations = []
    for r, rot in enumerate(meta["rotations"]):
        if "same_as" in rot:
            rotations.append({"same_as": rot["same_as"]})
            continue
        rotations.append({"frames": []})
        for f, fmeta in enumerate(rot["frames"]):
            stored.append((r, f, frm.frames[r][f], fmeta["name"]))

    width1, height1, positions = layout([(fr.width, fr.height) for _, _, fr, _ in stored])
    for (r, f, fr, _), (x, y) in zip(stored, positions):
        rotations[r]["frames"].append([x, y, fr.width, fr.height, fnv1a(fr.pixels)])

    atlases = {t: np.zeros((height1 * t, width1 * t, 4), np.uint8) for t in TIERS}
    for (r, f, fr, name), (x, y) in zip(stored, positions):
        rgba4 = np.asarray(Image.open(master / f"{name}.albedo.png").convert("RGBA"))
        if rgba4.shape[0] != fr.height * 4 or rgba4.shape[1] != fr.width * 4:
            raise ValueError(f"{game_path} {name}: master is {rgba4.shape[1]}x{rgba4.shape[0]}, expected 4x {fr.width}x{fr.height}")
        for t in TIERS:
            atlases[t][y * t : y * t + fr.height * t, x * t : x * t + fr.width * t] = _resize(rgba4, fr.width * t, fr.height * t)

    jobs = []
    for t in TIERS:
        png = staging / f"{tag}_albedo_{t}x.png"
        Image.fromarray(atlases[t], "RGBA").save(png, compress_level=1)
        jobs.append((png, out_dir / f"albedo_{t}x.dds"))

    pack = {
        "version": PACK_VERSION,
        "source": game_path,
        "category": meta["category"],
        "hard_alpha": meta["category"] in HARD_ALPHA,
        "model": meta["model"],
        "master_version": meta["version"],
        "tiers": list(TIERS),
        "layout": [width1, height1],
        "rotations": rotations,
    }
    return pack, jobs


def compress(texconv: Path, pngs: list[Path], out_dir: Path) -> None:
    """BC7 (UNORM: the art's values are used as stored, like the palette)."""
    flist = out_dir / "files.txt"
    flist.write_text("\n".join(str(p) for p in pngs), encoding="utf-8")
    cmd = [str(texconv), "-nologo", "-y", "-f", "BC7_UNORM", "-m", "1", "-o", str(out_dir), "-flist", str(flist)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed ({result.returncode}): {result.stdout[-2000:]} {result.stderr[-2000:]}")


def build(manifest: dict, files: GameFiles, paths: Paths, jobs: JobDB, report=print, governor=None) -> dict:
    texconv = texconv_path(paths)
    stats = {"packed": 0, "skipped": 0, "missing_master": 0, "bytes": {str(t): 0 for t in TIERS}}
    pending: list[tuple[str, str, Path, dict, list[tuple[Path, Path]]]] = []

    with tempfile.TemporaryDirectory(prefix="fo2hd-pack-") as tmp:
        staging = Path(tmp)

        def flush():
            if not pending:
                return
            out = staging / "out"
            out.mkdir(exist_ok=True)
            if governor:
                governor.wait()
            started = time.perf_counter()
            compress(texconv, [png for *_, todo in pending for png, _ in todo], out)
            if governor:
                governor.after(time.perf_counter() - started)
            for game_path, fp, out_dir, pack, todo in pending:
                for png, dds in todo:
                    produced = out / (png.stem + ".dds")
                    dds.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(produced), str(dds))
                    stats["bytes"][dds.stem.split("_")[-1][:-1]] += dds.stat().st_size
                    png.unlink()
                write_atomic(out_dir / "pack.json", json.dumps(pack, separators=(",", ":")).encode("utf-8"))
                jobs.mark_done("pack", game_path, fp, out_dir / "pack.json")
                stats["packed"] += 1
            pending.clear()
            jobs.commit()

        for n, asset in enumerate(manifest["assets"]):
            game_path = asset["path"]
            master = hdassets.asset_dir(paths, game_path)
            meta_path = master / "meta.json"
            if not meta_path.exists():
                stats["missing_master"] += 1
                continue
            data = files.read(game_path)
            fp = fingerprint(data, meta_path.read_bytes(), PACK_VERSION, ",".join(map(str, TIERS)), texconv.parent.name)
            if jobs.is_done("pack", game_path, fp):
                stats["skipped"] += 1
                continue
            out_dir = pack_dir(paths, game_path)
            pack, todo = build_one(game_path, data, master, out_dir, staging, f"a{n:05d}")
            pending.append((game_path, fp, out_dir, pack, todo))
            if sum(len(p[4]) for p in pending) >= TEXCONV_BATCH:
                flush()
                report(f"    pack: {n + 1}/{len(manifest['assets'])}")
        flush()
    return stats


def roundtrip_check(paths: Paths, game_paths: list[str], tier: int = 3) -> dict:
    """Decodes some packed atlases back to PNG with texconv and compares them
    with the uncompressed tier image: BC7 error where alpha > 0."""
    texconv = texconv_path(paths)
    errors = []
    with tempfile.TemporaryDirectory(prefix="fo2hd-pack-check-") as tmp:
        tmp = Path(tmp)
        for i, game_path in enumerate(game_paths):
            d = pack_dir(paths, game_path)
            pack = json.loads((d / "pack.json").read_text(encoding="utf-8"))
            subprocess.run([str(texconv), "-nologo", "-y", "-ft", "png", "-f", "R8G8B8A8_UNORM", "-o", str(tmp), str(d / f"albedo_{tier}x.dds")], check=True, capture_output=True)
            decoded = np.asarray(Image.open(tmp / f"albedo_{tier}x.png").convert("RGBA")).astype(np.float32)
            (tmp / f"albedo_{tier}x.png").unlink()
            master = hdassets.asset_dir(paths, game_path)
            meta = json.loads((master / "meta.json").read_text(encoding="utf-8"))
            for r, rot in enumerate(pack["rotations"]):
                for f, (x, y, w, h, _) in enumerate(rot.get("frames", [])):
                    name = meta["rotations"][r]["frames"][f]["name"]
                    ref = _resize(np.asarray(Image.open(master / f"{name}.albedo.png").convert("RGBA")), w * tier, h * tier).astype(np.float32)
                    got = decoded[y * tier : y * tier + h * tier, x * tier : x * tier + w * tier]
                    a = ref[..., 3] > 0
                    if a.any():
                        mse = float(((got[..., :3] - ref[..., :3])[a] ** 2).mean())
                        errors.append({"path": game_path, "frame": name, "psnr": 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse), "alpha_max_err": float(np.abs(got[..., 3] - ref[..., 3]).max())})
    psnrs = [e["psnr"] for e in errors]
    return {
        "tier": tier,
        "frames": len(errors),
        "psnr_min": min(psnrs) if psnrs else None,
        "psnr_mean": sum(psnrs) / len(psnrs) if psnrs else None,
        "alpha_max_err": max((e["alpha_max_err"] for e in errors), default=None),
        "worst": sorted(errors, key=lambda e: e["psnr"])[:5],
    }
