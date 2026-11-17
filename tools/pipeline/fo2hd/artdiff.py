"""P8.1: a game built on Fallout 2's files (Fallout Et Tu) against the base game's packed art.

    fo2hd art-diff [--game <folder>] [--base <ini or game folder>] [--base-art <pack folder>] [--out <folder>]

Every art file the game sees (`art\\...\\*.frm`, `.fr0`-`.fr5`, CE's indexed `.png`), in CE's
load order (`dat.GameFiles`, sfall mods included), is compared with the base game (Fallout 2
as the release ships it) by file and by frame. A frame is its palette indices and size; only
stored rotations count (one-direction art stores one). The engine finds a pack by the art's
name (`hd_art.cc packKey`) and draws a frame from it only when the frame's pixels hash as the
pack says, so pixels decide, not file bytes:

    same     the base game draws these frames under this name and has a pack: it applies as is
    nopack   the base game draws these frames under this name, without a pack (as in Fallout 2)
    twin     a base pack holds exactly these frames under another name: copy that pack, build nothing
    changed  the base game has this name with other frames (its pack falls back to the 1x art)
    new      a name the base game doesn't have

Frames of `changed` and `new` files are `reused` when some base pack holds the same picture and
`fresh` otherwise; the fresh pixels are what P8.4 builds.

Writes into <out> (default <data_root>\\artdiff):
    art_diff.json        totals, per category and art folder, and one row per file
    build_manifest.json  the changed and new files, as `fo2hd hdassets` / `pack --manifest` read them
    twins.json           name -> the base name whose pack to copy
"""

from __future__ import annotations

import collections
import hashlib
import json
import time
from pathlib import Path

from . import frm as frm_mod
from .dat import GameFiles, normalize
from .sample import CATEGORIES, FOLDER_CATEGORIES

ART_EXTENSIONS = (".frm", ".fr0", ".fr1", ".fr2", ".fr3", ".fr4", ".fr5", ".png")
CATS = ("same", "nopack", "twin", "changed", "new")


def is_art(game_path: str) -> bool:
    return game_path.startswith("art\\") and game_path.endswith(ART_EXTENSIONS)


def pack_key(game_path: str) -> str:
    """The engine's pack folder for an art path (hd_art.cc packKey): .frm drops, any other
    extension stays as _ext."""
    stem, ext = normalize(game_path).rsplit(".", 1)
    return stem if ext == "frm" else f"{stem}_{ext}"


def folder_of(game_path: str) -> str:
    parts = game_path.split("\\")
    return parts[1] if len(parts) > 2 else ""


def category_of(game_path: str) -> str:
    return FOLDER_CATEGORIES.get(folder_of(game_path), "misc")


class Art:
    """One FRM's stored frames: a key per frame, a key for the whole sequence."""

    __slots__ = ("frame_count", "rotations", "keys", "sizes", "seq", "pixels")

    def __init__(self, data: bytes):
        frm = frm_mod.decode(data)
        stored = []
        seen = set()
        for rotation, offset in enumerate(frm.data_offsets):
            if offset not in seen:
                seen.add(offset)
                stored.append(rotation)
        self.frame_count = frm.frame_count
        self.rotations = len(stored)
        self.keys: list[tuple[bytes, int, int]] = []  # (key, rotation index, frame index)
        self.sizes: list[int] = []
        seq = hashlib.blake2b(digest_size=16)
        for r_index, rotation in enumerate(stored):
            seq.update(b"|r")
            for f_index, fr in enumerate(frm.frames[rotation]):
                h = hashlib.blake2b(fr.pixels.tobytes(), digest_size=16)
                h.update(f"{fr.width}x{fr.height}".encode())
                key = h.digest()
                self.keys.append((key, r_index, f_index))
                self.sizes.append(fr.width * fr.height)
                seq.update(key)
        self.seq = seq.digest()
        self.pixels = sum(self.sizes)


def _decode(layer, game_path: str) -> Art | None:
    try:
        return Art(layer.read(game_path))
    except Exception:
        return None


def diff(game: GameFiles, base: GameFiles, base_art: Path, log=print) -> dict:
    start = time.perf_counter()
    game_vis = {p: l for p, l in game.list().items() if is_art(p)}
    base_vis = {p: l for p, l in base.list().items() if is_art(p)}
    log(f"art-diff: {len(game_vis)} art files in the game, {len(base_vis)} in the base game")

    # The base game's packed art: frames by key, sequences by key.
    base_packed: dict[str, Art] = {}
    seq_index: dict[bytes, str] = {}
    frame_index: dict[bytes, tuple[str, int, int]] = {}
    stale = []
    for n, (path, layer) in enumerate(sorted(base_vis.items())):
        pack_json = base_art / pack_key(path) / "pack.json"
        if not pack_json.exists():
            continue
        art = _decode(layer, path)
        if art is None:
            continue
        # A pack built from other art than the base game's (its frames' sizes differ) never applies.
        try:
            rotations = json.loads(pack_json.read_text(encoding="utf-8"))["rotations"]
            sizes = [f[2] * f[3] for r in rotations for f in r.get("frames", ())]  # "same_as" rotations store none
        except (OSError, ValueError, KeyError, IndexError):
            sizes = None
        if sizes != art.sizes:
            stale.append(path)
            continue
        base_packed[path] = art
        seq_index.setdefault(art.seq, path)
        for key, r_index, f_index in art.keys:
            frame_index.setdefault(key, (path, r_index, f_index))
        if n % 5000 == 4999:
            log(f"  base: {n + 1} files read, {len(base_packed)} packed")
    log(f"  base: {len(base_packed)} packed files ({len(stale)} packs that don't match their art), "
        f"{len(frame_index)} distinct packed frames ({time.perf_counter() - start:.0f}s)")

    rows = []
    unreadable = []
    for path, layer in sorted(game_vis.items()):
        art = _decode(layer, path)
        if art is None:
            unreadable.append(path)
            continue
        twin = None
        if path in base_packed and base_packed[path].seq == art.seq:
            cat = "same"
        elif path in base_vis and path not in base_packed and (
            (b := _decode(base_vis[path], path)) is not None and b.seq == art.seq
        ):
            cat = "nopack"
        elif art.seq in seq_index:
            cat, twin = "twin", seq_index[art.seq]
        else:
            cat = "changed" if path in base_vis else "new"
        row = {
            "path": path,
            "cat": cat,
            "folder": folder_of(path),
            "category": category_of(path),
            "layer": str(layer.path),
            "frames": art.frame_count,
            "rotations": art.rotations,
            "pixels": art.pixels,
        }
        if twin:
            row["twin_of"] = twin
        if cat in ("changed", "new"):
            reused_frames = reused_px = 0
            sources = collections.Counter()
            for (key, _, _), size in zip(art.keys, art.sizes):
                hit = frame_index.get(key)
                if hit:
                    reused_frames += 1
                    reused_px += size
                    sources[hit[0]] += 1
            row["fresh_frames"] = len(art.keys) - reused_frames
            row["fresh_pixels"] = art.pixels - reused_px
            row["reused_frames"] = reused_frames
            row["reused_pixels"] = reused_px
            if sources:
                row["reused_from"] = dict(sources.most_common(3))
        rows.append(row)

    totals = {c: collections.Counter() for c in CATS}
    by_folder: dict[str, dict[str, collections.Counter]] = collections.defaultdict(lambda: {c: collections.Counter() for c in CATS})
    for row in rows:
        for bucket in (totals[row["cat"]], by_folder[row["folder"]][row["cat"]]):
            bucket["files"] += 1
            bucket["frames"] += row["frames"] * row["rotations"]
            bucket["pixels"] += row["pixels"]
            bucket["fresh_pixels"] += row.get("fresh_pixels", 0)
            bucket["reused_pixels"] += row.get("reused_pixels", 0)
    summary = {
        "game": str(game.game_dir),
        "base": str(base.game_dir),
        "base_art": str(base_art),
        "layers": [str(p) for p in game.layer_paths],
        "base_packed_files": len(base_packed),
        "base_packed_pixels": sum(a.pixels for a in base_packed.values()),
        "stale_base_packs": stale,
        "unreadable": unreadable,
        "totals": {c: dict(v) for c, v in totals.items()},
        "by_folder": {f: {c: dict(v) for c, v in cats.items() if v} for f, cats in sorted(by_folder.items())},
        "seconds": round(time.perf_counter() - start, 1),
    }
    return {"summary": summary, "rows": rows}


def build_manifest(result: dict) -> dict:
    """Every changed and new file, in sample.bulk's format (+ fresh counts): each needs a pack
    of its own, even one whose frames all exist in other packs (fresh_pixels 0: built from
    those frames' masters). Art under another language's folder (art\\german\\...: drawn
    only when the game runs in that language) is left out."""
    assets = []
    for row in result["rows"]:
        if row["folder"] not in FOLDER_CATEGORIES:
            continue
        if row["cat"] in ("changed", "new"):
            assets.append({k: row[k] for k in ("path", "category", "frames", "rotations", "pixels", "fresh_frames", "fresh_pixels", "cat")})
    categories = {}
    for category in CATEGORIES:
        items = [a for a in assets if a["category"] == category]
        categories[category] = {
            "files": len(items),
            "frames": sum(a["frames"] * a["rotations"] for a in items),
            "pixels": sum(a["pixels"] for a in items),
            "fresh_pixels": sum(a["fresh_pixels"] for a in items),
        }
    return {"source": "fo2hd art-diff", "folders": sorted({a["path"].split("\\")[1] for a in assets}), "categories": categories, "assets": assets}


def write(result: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "art_diff.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    manifest = build_manifest(result)
    (out / "build_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    twins = {r["path"]: r["twin_of"] for r in result["rows"] if r["cat"] == "twin"}
    (out / "twins.json").write_text(json.dumps(twins, indent=1), encoding="utf-8")
    return manifest


def skeleton(game_path: str, data: bytes, category: str) -> dict:
    """pack.json for an FRM without a pack anywhere (P8.4): the layout and frame hashes `fo2hd pack`
    would give it, no atlases. tools/scripts/art_x16.py --source builds its tiers from the
    original pixels."""
    from .pack import HARD_ALPHA, PACK_VERSION, fnv1a, layout

    frm = frm_mod.decode(data)
    first: dict[int, int] = {}
    rotations: list[dict] = []
    stored = []
    for r, offset in enumerate(frm.data_offsets):
        if offset in first:
            rotations.append({"same_as": first[offset]})
            continue
        first[offset] = r
        rotations.append({"frames": []})
        stored.extend((r, fr) for fr in frm.frames[r])
    width, height, positions = layout([(fr.width, fr.height) for _, fr in stored])
    for (r, fr), (x, y) in zip(stored, positions):
        rotations[r]["frames"].append([x, y, fr.width, fr.height, fnv1a(fr.pixels)])
    return {
        "version": PACK_VERSION,
        "source": game_path,
        "category": category,
        "hard_alpha": category in HARD_ALPHA,
        "model": "skeleton",
        "master_version": 0,
        "tiers": [],
        "layout": [width, height],
        "rotations": rotations,
    }


def write_skeletons(manifest: dict, files: GameFiles, out: Path, folders: list[str], log=print) -> int:
    """<out>\\art\\<folder>\\<name>\\pack.json for the manifest's files in these art folders."""
    written = 0
    for asset in manifest["assets"]:
        if folder_of(asset["path"]) not in folders:
            continue
        target = out / pack_key(asset["path"]) / "pack.json"
        if target.exists():
            continue
        pack = skeleton(asset["path"], files.read(asset["path"]), asset["category"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
        written += 1
        if written % 500 == 0:
            log(f"  {written} skeletons")
    return written


def copy_twins(twins: dict[str, str], base_art: Path, out: Path) -> tuple[int, int]:
    """Each twin's base pack under the twin's own name: the atlases hard-linked (same bytes, no
    disk), pack.json with its source set to the twin. Returns (packs, already there)."""
    import os
    import shutil

    done = skipped = 0
    for path, base in sorted(twins.items()):
        source, target = base_art / pack_key(base), out / pack_key(path)
        if (target / "pack.json").exists():
            skipped += 1
            continue
        target.mkdir(parents=True, exist_ok=True)
        for dds in source.glob("albedo_*x.dds"):
            try:
                os.link(dds, target / dds.name)
            except OSError:
                shutil.copy2(dds, target / dds.name)
        pack = json.loads((source / "pack.json").read_text(encoding="utf-8"))
        pack["source"] = path
        pack["twin_of"] = base
        (target / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
        done += 1
    return done, skipped


def report(result: dict, manifest: dict, log=print) -> None:
    s = result["summary"]
    mpx = lambda v: f"{v / 1e6:8.1f} Mpx"  # noqa: E731
    log("\nBy category (files, frames over stored rotations, 1x pixels):")
    for cat in CATS:
        c = s["totals"][cat]
        extra = f"   fresh {mpx(c.get('fresh_pixels', 0))}, reused {mpx(c.get('reused_pixels', 0))}" if cat in ("changed", "new") else ""
        log(f"  {cat:8} {c.get('files', 0):6} files {c.get('frames', 0):8} frames {mpx(c.get('pixels', 0))}{extra}")
    log("\nTo build (fresh pixels of the build manifest's changed + new files), by art folder:")
    by_folder: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for a in manifest["assets"]:
        by_folder[folder_of(a["path"])].update(files=1, fresh=a["fresh_pixels"])
    for folder, c in sorted(by_folder.items()):
        log(f"  {folder:10} {c['files']:5} files {mpx(c['fresh'])}")
    total = sum(c["fresh"] for c in by_folder.values())
    skipped = sum(1 for r in result["rows"] if r["folder"] not in FOLDER_CATEGORIES and r["cat"] in ("changed", "new"))
    if skipped:
        log(f"  (left out: {skipped} files under other languages' art folders)")
    log(f"  {'total':10} {'':11} {mpx(total)}   (Fallout 2's packed art: {mpx(s['base_packed_pixels'])}, "
        f"{total / max(s['base_packed_pixels'], 1):.0%})")
    log(f"\nBuild manifest: {len(manifest['assets'])} files; twins (copy a base pack): {s['totals']['twin'].get('files', 0)}; "
        f"unreadable: {len(s['unreadable'])}; base packs that don't match their art: {len(s['stale_base_packs'])}")
