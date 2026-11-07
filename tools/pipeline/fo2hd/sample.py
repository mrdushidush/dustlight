"""Phase 1 sample set (PLAN.md P1.5): every FRM the benchmark captures draw,
grouped by category, plus the player's walk cycle.

Categories come from the art type in the fid (art\\<type>\\...) and, for tiles,
from the draw op (floor vs roof). Interface art (cursors) is left out: it is UI.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import capture as capture_mod
from . import frm as frm_mod
from .dat import GameFiles, normalize

# FrmId object types (art.h / obj_types.h).
ART_TYPES = {0: "item", 1: "critter", 2: "scenery", 3: "wall", 4: "tile", 5: "misc", 6: "interface"}
WALK_CYCLE = "art\\critters\\hmwarrab.frm"  # tribal male, unarmed walk
CATEGORIES = ["floor", "roof", "wall", "scenery", "critter", "item", "misc", "interface"]


def category_of(draw: dict) -> str | None:
    if draw["op"] == "floor":
        return "floor"
    if draw["op"] == "roof":
        return "roof"
    if draw["op"] not in ("object", "outline"):
        return None
    art_type = ART_TYPES.get((draw["fid"] >> 24) & 0x0F)
    if art_type in (None, "interface", "tile"):
        return None
    return art_type


def build(captures: list[Path], files: GameFiles) -> dict:
    assets: dict[str, dict] = {}
    used_by: dict[str, set] = defaultdict(set)
    for path in captures:
        cap = capture_mod.load(path)
        for draw in cap.draws:
            category = category_of(draw)
            if category is None:
                continue
            game_path = normalize(cap.art[draw["fid"]])
            if game_path.endswith("grid000.frm"):
                continue  # edge squares / empty floor: never visible as art
            entry = assets.setdefault(game_path, {"path": game_path, "category": category, "frames_used": set()})
            if draw["op"] in ("object", "outline"):
                entry["frames_used"].add((draw["rotation"], draw["frame"]))
            else:
                entry["frames_used"].add((0, 0))
            used_by[game_path].add(cap.path.stem)

    if files.exists(WALK_CYCLE):
        entry = assets.setdefault(normalize(WALK_CYCLE), {"path": normalize(WALK_CYCLE), "category": "critter", "frames_used": set()})
        used_by[entry["path"]].add("walk_cycle")
        entry["walk_cycle"] = True

    manifest = {"captures": [p.stem for p in captures], "walk_cycle": normalize(WALK_CYCLE), "categories": {}, "assets": []}
    for game_path in sorted(assets):
        entry = assets[game_path]
        frm = frm_mod.decode(files.read(game_path))
        stored = sorted(set(frm.data_offsets))
        pixels = sum(f.width * f.height for rotation in {frm.data_offsets.index(o) for o in stored} for f in frm.frames[rotation])
        manifest["assets"].append(
            {
                "path": game_path,
                "category": entry["category"],
                "frames": frm.frame_count,
                "rotations": len(stored),
                "pixels": pixels,
                "frames_used": sorted(entry["frames_used"]),
                "used_by": sorted(used_by[game_path]),
                "walk_cycle": entry.get("walk_cycle", False),
            }
        )
    for category in CATEGORIES:
        items = [a for a in manifest["assets"] if a["category"] == category]
        manifest["categories"][category] = {
            "files": len(items),
            "frames": sum(a["frames"] * a["rotations"] for a in items),
            "pixels": sum(a["pixels"] for a in items),
        }
    return manifest


# Art folder -> category for bulk manifests. Tiles are floors unless the
# sample set saw them drawn as roofs (the art itself doesn't say).
FOLDER_CATEGORIES = {"tiles": "floor", "walls": "wall", "scenery": "scenery", "items": "item", "misc": "misc", "critters": "critter"}
# UI art (P7b HD skin): talking heads, interface panels, inventory and
# Skilldex pictures, full-screen backgrounds. Only the albedo is used.
FOLDER_CATEGORIES.update({"heads": "interface", "intrface": "interface", "inven": "interface", "skilldex": "interface", "backgrnd": "interface"})
FRM_EXTENSIONS = (".frm", ".fr0", ".fr1", ".fr2", ".fr3", ".fr4", ".fr5")
# Plus CE's indexed PNG art (frm.decode reads it as a one-frame FRM).
ART_EXTENSIONS = FRM_EXTENSIONS + (".png",)


def bulk(files: GameFiles, folders: list[str], known: dict | None = None, roofs: list[str] | None = None) -> dict:
    """Every FRM in the given art folders (P4 bulk pass), in the sample
    manifest's format. `known` (a sample manifest) supplies categories for
    assets it already has, so their HD output stays valid. `roofs` (P7, from
    `fo2hd maps`: tiles the maps draw mostly as roofs) makes those tiles
    roofs unless the sample set says otherwise."""
    known_categories = {p: "roof" for p in roofs or []}
    known_categories.update({a["path"]: a["category"] for a in (known or {}).get("assets", [])})
    manifest = {"folders": folders, "categories": {}, "assets": []}
    for game_path in sorted(files.list()):
        parts = game_path.split("\\")
        if len(parts) != 3 or parts[0] != "art" or parts[1] not in folders or not game_path.endswith(ART_EXTENSIONS):
            continue
        if game_path.endswith("grid000.frm"):
            continue  # edge squares / empty floor: never visible as art
        frm = frm_mod.decode(files.read(game_path))
        stored = sorted(set(frm.data_offsets))
        pixels = sum(f.width * f.height for rotation in {frm.data_offsets.index(o) for o in stored} for f in frm.frames[rotation])
        manifest["assets"].append(
            {
                "path": game_path,
                "category": known_categories.get(game_path, FOLDER_CATEGORIES[parts[1]]),
                "frames": frm.frame_count,
                "rotations": len(stored),
                "pixels": pixels,
            }
        )
    for category in CATEGORIES:
        items = [a for a in manifest["assets"] if a["category"] == category]
        manifest["categories"][category] = {
            "files": len(items),
            "frames": sum(a["frames"] * a["rotations"] for a in items),
            "pixels": sum(a["pixels"] for a in items),
        }
    return manifest


def write(manifest: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
