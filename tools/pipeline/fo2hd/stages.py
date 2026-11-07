"""Pipeline stages: extract (game files) and decode (FRM -> PNG frames + JSON).

Both are idempotent and resumable through the job DB (fo2hd.jobs). Outputs:

    <data_root>/extract/<game path>                 bytes as the game sees them
    <data_root>/decoded/<game path minus .frm>/
        meta.json                                   fps, action frame, offsets, frames
        r<rot>_f<frame>.png                         RGBA (index 0 -> alpha 0)
        r<rot>_f<frame>.idx.png                     palette indices (mode P)

Game paths are lower-cased with forward slashes on disk, e.g.
extract/art/critters/hmwarrab.frm.
"""

from __future__ import annotations

import fnmatch
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import frm as frm_mod
from .config import Paths
from .dat import Dat2, Folder, GameFiles, ZipArchive, normalize
from .jobs import JobDB, fingerprint, write_atomic

DECODE_VERSION = 2


def disk_path(game_path: str) -> str:
    return normalize(game_path).replace("\\", "/")


def matches(path: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(path, normalize(p)) for p in patterns)


def _source_fingerprint(layer, path: str) -> str:
    """Cheap identity of a file's source, without reading it."""
    if isinstance(layer, Dat2):
        e = layer.entries[path]
        return fingerprint("dat", str(layer.path), e.offset, e.packed_size, e.size, int(layer.path.stat().st_mtime))
    if isinstance(layer, ZipArchive):
        e = layer.entries[path]
        return fingerprint("zip", str(layer.path), e.CRC, e.file_size)
    if isinstance(layer, Folder):
        st = layer.entries[path].stat()
        return fingerprint("file", str(layer.entries[path]), st.st_size, int(st.st_mtime))
    raise TypeError(layer)


def extract(files: GameFiles, paths: Paths, patterns: list[str], jobs: JobDB, progress=print) -> tuple[int, int]:
    """Writes every visible file matching `patterns`. Returns (written, skipped)."""
    out_root = paths.data_root / "extract"
    written = skipped = 0
    visible = files.list()
    selected = sorted(p for p in visible if matches(p, patterns))
    for index, path in enumerate(selected):
        layer = visible[path]
        fp = _source_fingerprint(layer, path)
        out = out_root / disk_path(path)
        if jobs.is_done("extract", path, fp):
            skipped += 1
            continue
        write_atomic(out, layer.read(path))
        jobs.mark_done("extract", path, fp, out)
        written += 1
        if written % 500 == 0:
            progress(f"    extract: {index + 1}/{len(selected)}")
    jobs.commit()
    return written, skipped


def game_palette(files: GameFiles) -> np.ndarray:
    return frm_mod.load_palette(files.read("color.pal"))


def decode_frm(data: bytes, palette_rgb: np.ndarray) -> tuple[dict, dict[str, bytes]]:
    """FRM bytes -> (meta, {file name: PNG bytes})."""
    frm = frm_mod.decode(data)
    rgba_palette = np.concatenate([palette_rgb, np.full((256, 1), 255, np.uint8)], axis=1)
    rgba_palette[0, 3] = 0
    flat_palette = palette_rgb.reshape(-1).tolist()

    rotations = []
    seen_offsets: dict[int, int] = {}
    outputs: dict[str, bytes] = {}
    for rotation in range(frm_mod.ROTATIONS):
        offset = frm.data_offsets[rotation]
        if offset in seen_offsets:
            rotations.append({"same_as": seen_offsets[offset]})
            continue
        seen_offsets[offset] = rotation
        frames = []
        for index, frame in enumerate(frm.frames[rotation]):
            name = f"r{rotation}_f{index:03d}"
            rgba = Image.fromarray(rgba_palette[frame.pixels], "RGBA")
            buf = io.BytesIO()
            rgba.save(buf, "PNG")
            outputs[f"{name}.png"] = buf.getvalue()

            idx = Image.fromarray(frame.pixels, "P")
            idx.putpalette(flat_palette)
            buf = io.BytesIO()
            idx.save(buf, "PNG", transparency=0)
            outputs[f"{name}.idx.png"] = buf.getvalue()
            frames.append({"file": name, "w": frame.width, "h": frame.height, "dx": frame.x, "dy": frame.y})
        rotations.append({"x_offset": frm.x_offsets[rotation], "y_offset": frm.y_offsets[rotation], "frames": frames})

    meta = {
        "version": DECODE_VERSION,
        "fps": frm.fps,
        "action_frame": frm.action_frame,
        "frame_count": frm.frame_count,
        "rotations": rotations,
    }
    return meta, outputs


def decode(files: GameFiles, paths: Paths, patterns: list[str], jobs: JobDB, progress=print) -> tuple[int, int]:
    """Decodes every visible FRM matching `patterns`. Returns (decoded, skipped)."""
    out_root = paths.data_root / "decoded"
    palette_bytes = files.read("color.pal")
    palette = frm_mod.load_palette(palette_bytes)
    visible = files.list()
    selected = sorted(p for p in visible if p.endswith((".frm", ".fr0", ".fr1", ".fr2", ".fr3", ".fr4", ".fr5")) and matches(p, patterns))
    decoded = skipped = 0
    for index, path in enumerate(selected):
        layer = visible[path]
        fp = fingerprint(_source_fingerprint(layer, path), palette_bytes, DECODE_VERSION)
        out_dir = out_root / disk_path(path).rsplit(".", 1)[0]
        if path.endswith((".fr0", ".fr1", ".fr2", ".fr3", ".fr4", ".fr5")):
            out_dir = out_dir.with_name(out_dir.name + "_" + path[-3:])
        meta_path = out_dir / "meta.json"
        if jobs.is_done("decode", path, fp):
            skipped += 1
            continue
        meta, outputs = decode_frm(layer.read(path), palette)
        meta["source"] = path
        for name, data in outputs.items():
            write_atomic(out_dir / name, data)
        # meta.json last: its presence marks a complete folder.
        write_atomic(meta_path, json.dumps(meta, indent=1).encode("utf-8"))
        jobs.mark_done("decode", path, fp, meta_path)
        decoded += 1
        if decoded % 100 == 0:
            progress(f"    decode: {index + 1}/{len(selected)}")
    jobs.commit()
    return decoded, skipped
