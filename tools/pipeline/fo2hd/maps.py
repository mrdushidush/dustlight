"""Map scan (P7): what every map file holds, read offline from the game data.

Two uses:
  - tile usage: which art/tiles FRMs any map draws as a floor and which as a
    roof. The art doesn't say; P4's bulk manifest called every tile a floor
    except the sample set's roofs, so most roofs missed the roof-only edge
    cleanup (hdassets.clean_tile_edges, P5.0).
  - the capture tour: for every map file and elevation with floors, a few
    camera spots (hex tiles) over the busiest parts of the map, in the
    format of tools/scripts/scenes.json.

Map files (map.cc mapLoad): a 236-byte big-endian header, then the global
and local variables (int32 each), then for each elevation present
100x100 squares of int32: low 16 bits = floor, high 16 bits = roof, each a
12-bit index into art/tiles/tiles.lst plus 4 flag bits. Header flag 0x2 << e
means elevation e is absent.
"""

from __future__ import annotations

import re
import struct

import numpy as np

from .dat import GameFiles

# map.h MapHeader: version, name[16], entering tile/elevation/rotation, local
# vars, script, flags, darkness, global vars, map index (9 int32), last
# visit (uint32), 44 int32 padding = 236 bytes. (Until 2026-11-04 this read
# 232 bytes, so every grid started one square early; see STATUS P7a.2.)
HEADER = struct.Struct(">i16s9iI44i")
SQUARE_GRID = 100
EMPTY_TILES = (0, 1)  # reserved.frm, grid000.frm: nothing drawn
INVALID_TILE = 0xFFF

# A view at 3440x1440, scale 3 shows 1146x480 logical px; spots are picked
# on a grid of cells that size (square screen offsets from tile.cc
# squareTileToScreenXY: +48,-12 per mirrored column, +32,+24 per row).
VIEW_W, VIEW_H = 1146, 480
SPOTS_PER_ELEVATION = 3
MIN_SQUARES_PER_SPOT = 40


def read_maps_txt(files: GameFiles) -> dict[str, dict]:
    """map file name (lower case, no extension) -> {index, lookup_name}."""
    text = files.read("data\\maps.txt").decode("latin-1")
    out = {}
    for m in re.finditer(r"^\[Map (\d+)\]\s*$(.*?)(?=^\[Map |\Z)", text, re.M | re.S):
        body = m.group(2)
        name = re.search(r"^\s*map_name\s*=\s*([^\s;]+)", body, re.M)
        lookup = re.search(r"^\s*lookup_name\s*=\s*([^;\r\n]+)", body, re.M)
        if name:
            out[name.group(1).lower()] = {"index": int(m.group(1)), "lookup_name": lookup.group(1).strip() if lookup else ""}
    return out


def parse(data: bytes) -> dict:
    """Header fields and the tile grid of each elevation present."""
    h = HEADER.unpack_from(data, 0)
    version, name, tile, elevation, rotation, local_vars, script, flags, darkness, global_vars, index, last_visit = h[:12]
    offset = HEADER.size + 4 * (max(global_vars, 0) + max(local_vars, 0))
    grids = {}
    for e in range(3):
        if flags & (2 << e):
            continue
        grid = np.frombuffer(data, ">u4", SQUARE_GRID * SQUARE_GRID, offset).astype(np.uint32)
        grids[e] = grid
        offset += 4 * SQUARE_GRID * SQUARE_GRID
    return {
        "version": version,
        "name": name.split(b"\0")[0].decode("latin-1"),
        "entering": {"tile": tile, "elevation": elevation, "rotation": rotation},
        "darkness": darkness,
        "index": index,
        "script": script,  # the map's own script: scripts.lst line, 1-based (0 = none)
        "grids": grids,
        "objects_offset": offset,  # where the map scripts and objects start (map_objects)
    }


def square_to_hex(square: int) -> int:
    """The hex tile under a square's top corner (2x2 hexes per square)."""
    sy, sx = divmod(square, SQUARE_GRID)
    return (2 * sy) * 2 * SQUARE_GRID + 2 * sx + 1


def _screen(squares: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sy, sx = np.divmod(squares, SQUARE_GRID)
    col = SQUARE_GRID - 1 - sx
    return 48 * col + 32 * sy, -12 * col + 24 * sy


def spots(floor_ids: np.ndarray, entering_tile: int | None = None) -> list[tuple[int, int]]:
    """Camera spots for one elevation: (hex tile, floor squares in its view).
    The map's entering tile first (on its elevation), then the view-sized
    cells with the most floor, nearest the middle of the map's floor first
    (many cells are full; corners are often off-limits to the camera)."""
    filled = np.flatnonzero(~np.isin(floor_ids, EMPTY_TILES + (INVALID_TILE,)))
    if filled.size == 0:
        return []
    px, py = _screen(filled)
    x0, y0 = px.min(), py.min()
    cell = ((px - x0) // VIEW_W) * 100000 + (py - y0) // VIEW_H
    out = []
    taken = set()
    if entering_tile is not None and entering_tile >= 0:
        hy, hx = divmod(entering_tile, 2 * SQUARE_GRID)
        ex, ey = _screen(np.array([(hy // 2) * SQUARE_GRID + hx // 2]))
        k = int(((ex[0] - x0) // VIEW_W) * 100000 + (ey[0] - y0) // VIEW_H)
        out.append((entering_tile, int((cell == k).sum())))
        taken.add(k)
    keys, counts = np.unique(cell, return_counts=True)
    cx, cy = px.mean(), py.mean()
    dist = []
    for k in keys:
        m = cell == k
        dist.append((px[m].mean() - cx) ** 2 + (py[m].mean() - cy) ** 2)
    for k in keys[np.lexsort((np.array(dist), -counts))]:
        if len(out) >= SPOTS_PER_ELEVATION:
            break
        if int(k) in taken:
            continue
        members = filled[cell == k]
        if members.size < MIN_SQUARES_PER_SPOT and out:
            break
        # The member square nearest the cell's mean screen position.
        m_px, m_py = px[cell == k], py[cell == k]
        i = int(np.argmin((m_px - m_px.mean()) ** 2 + (m_py - m_py.mean()) ** 2))
        out.append((square_to_hex(int(members[i])), int(members.size)))
    return out


def scan(files: GameFiles) -> dict:
    tiles_lst = files.read("art\\tiles\\tiles.lst").decode("latin-1").splitlines()
    listed = read_maps_txt(files)
    usage: dict[int, dict] = {}
    maps = []
    for path in sorted(p for p in files.list() if p.startswith("maps\\") and p.endswith(".map")):
        stem = path.split("\\")[-1][:-4]
        m = parse(files.read(path))
        entry = {
            "file": stem + ".map",
            "listed": stem in listed,
            "lookup_name": listed.get(stem, {}).get("lookup_name", ""),
            "entering": m["entering"],
            "darkness": m["darkness"],
            "elevations": {},
        }
        for e, grid in m["grids"].items():
            floor = (grid & 0xFFFF) & 0xFFF
            roof = (grid >> 16) & 0xFFF
            for ids, kind in ((floor, "floor"), (roof, "roof")):
                for tid, n in zip(*np.unique(ids, return_counts=True)):
                    tid = int(tid)
                    if tid in EMPTY_TILES or tid == INVALID_TILE:
                        continue
                    u = usage.setdefault(tid, {"floor": 0, "roof": 0, "maps": set()})
                    u[kind] += int(n)
                    u["maps"].add(stem)
            entry["elevations"][str(e)] = {
                "floor_squares": int((~np.isin(floor, EMPTY_TILES + (INVALID_TILE,))).sum()),
                "roof_squares": int((~np.isin(roof, EMPTY_TILES + (INVALID_TILE,))).sum()),
                "spots": spots(floor, m["entering"]["tile"] if m["entering"]["elevation"] == e else None),
            }
        maps.append(entry)

    tiles = {}
    for tid, u in sorted(usage.items()):
        name = tiles_lst[tid].strip().lower() if tid < len(tiles_lst) else f"#{tid}"
        tiles[f"art\\tiles\\{name}"] = {"id": tid, "floor": u["floor"], "roof": u["roof"], "maps": len(u["maps"])}
    return {"maps": maps, "tiles": tiles}


def tour(scan_result: dict, time: int | None = None) -> list[dict]:
    """Capture tour scenes (tools/scripts/scenes.json format)."""
    scenes = []
    for m in scan_result["maps"]:
        stem = m["file"][:-4]
        for e, info in m["elevations"].items():
            for k, (tile, _) in enumerate(info["spots"]):
                scene = {"name": f"{stem}_e{e}_{k}", "map": m["file"], "elevation": int(e), "tile": tile}
                if time is not None:
                    scene["time"] = time
                scenes.append(scene)
    return scenes


def roof_tiles(scan_result: dict) -> list[str]:
    """Tiles any map draws as a roof more often than as a floor."""
    return sorted(p for p, u in scan_result["tiles"].items() if u["roof"] > u["floor"])
