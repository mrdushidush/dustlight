"""The shadows painted into the original world art, as masks for the engine (hd/painted_shadows.json).

  painted_shadows.py --game <game folder> --out <hd\\painted_shadows.json> [--list painted_shadows.txt]
                     [--sheet <png>]

Fallout 2 paints a shadow into many scenery sprites: a patch of the two near-black palette colors 100 and 207
on the ground beside the object, solid or as a checkerboard with see-through pixels. It always falls to the
lower right and is hard black, so it clashes with the engine's own shadows, which follow the sun.

Candidates are found from the original pixels: wide patches of those two colors (1-2 px outlines drop out),
checkerboard holes filled, open to the transparent outside. The same colors also paint outlines, dark doorways,
cave mouths, black walls and dark leaves, so a candidate counts only when the list says so:
painted_shadows.txt names, per sprite, one pixel inside each patch that is a shadow. The list comes from a
review of every candidate sprite by eye (Claude, 2026-11-10); small contact shadows right under an object were
left out on purpose, as they look right at any sun angle.

Every frame and direction of a listed sprite gets a mask: in frame 0 of direction 0 the patches holding a listed
pixel, in the others the patches mostly overlapping those; each grown up to 3 px into touching shadow-colored
pixels (the thin bits the patch finder drops would stay behind as black specks). The engine draws masked pixels faint
([hd_light] painted_shadows) and leaves them out of the object's shape and of the sun's shadow casters.

Output: {"version": 1, "sprites": {"scenery/ncrbill": [{"rotation": r, "frame": f, "width": w, "height": h,
"runs": [y, x, length, ...]}, ...]}}; only directions whose pixels differ from direction 0 are written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from fo2hd import frm as frm_mod  # noqa: E402
from fo2hd.dat import GameFiles  # noqa: E402

SHADOW_COLORS = (100, 207)
MIN_AREA = 12
GROW = 3
K3 = np.ones((3, 3), np.uint8)


def patches(px: np.ndarray) -> list[np.ndarray]:
    """Candidate shadow patches of one frame, largest first."""
    m = np.isin(px, SHADOW_COLORS).astype(np.uint8)
    # checkerboard shadows: a see-through pixel with shadow color on 3 or 4 sides belongs to the shadow
    p = np.pad(m, 1)
    sides = p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
    m = m | ((px == 0) & (sides >= 3)).astype(np.uint8)
    core = cv2.morphologyEx(m, cv2.MORPH_OPEN, K3)
    grown = cv2.dilate(core, K3) & m
    n, lab, st, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    # the transparent outside: see-through pixels reachable from the frame's border
    pad = np.pad((px == 0).astype(np.uint8), 1, constant_values=1)
    _, olab = cv2.connectedComponents(pad, connectivity=4)
    outside = (olab == olab[0, 0])[1:-1, 1:-1] & (px == 0)
    near_outside = cv2.dilate(outside.astype(np.uint8), K3).astype(bool)
    found = []
    for i in range(1, n):
        comp = lab == i
        if st[i][4] >= MIN_AREA and (comp & near_outside).sum() >= 3:
            found.append(comp)
    found.sort(key=lambda c: -int(c.sum()))
    return found


def grown(px: np.ndarray, mask: np.ndarray, steps: int = GROW) -> np.ndarray:
    """The listed patches plus the thin shadow-colored bits touching them (up to `steps` px out), which the
    opening left out: faded without them, they stay behind as black specks along the shadow's edge."""
    colors = np.isin(px, SHADOW_COLORS)
    m = mask.copy()
    for _ in range(steps):
        m |= cv2.dilate(m.astype(np.uint8), K3).astype(bool) & colors
    return m


def runs(mask: np.ndarray) -> list[int]:
    out = []
    for y in range(mask.shape[0]):
        row = np.concatenate([[0], mask[y].astype(np.int8), [0]])
        d = np.diff(row)
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            out += [y, int(s), int(e - s)]
    return out


def read_list(path: Path) -> dict[str, list[tuple[int, int]]]:
    listed = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, *points = line.split()
        listed[key] = [tuple(int(v) for v in p.split(",")) for p in points]
    return listed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--list", default=str(Path(__file__).with_name("painted_shadows.txt")))
    args = parser.parse_args()

    files = GameFiles(Path(args.game))
    listed = read_list(Path(args.list))
    sprites = {}
    frames_written = pixels = 0
    for key, points in sorted(listed.items()):
        kind, name = key.split("/")
        art = frm_mod.decode(files.read(f"art\\{kind}\\{name}.frm"))
        first = art.frames[0][0].pixels
        base = np.zeros(first.shape, bool)
        for comp in patches(first):
            if any(comp[y, x] for x, y in points):
                base |= comp
        missing = [p for p in points if not base[p[1], p[0]]]
        if missing:
            raise SystemExit(f"{key}: listed pixel(s) {missing} are in no candidate patch; check the list")
        entries = []
        for rotation in range(len(art.frames)):
            if rotation > 0 and all(
                a.pixels.shape == b.pixels.shape and np.array_equal(a.pixels, b.pixels)
                for a, b in zip(art.frames[rotation], art.frames[0])
            ):
                continue  # the same pixels as direction 0: the engine uses its mask
            for index, frame in enumerate(art.frames[rotation]):
                px = frame.pixels
                if rotation == 0 and index == 0:
                    mask = grown(px, base)
                else:
                    mask = np.zeros(px.shape, bool)
                    if px.shape == first.shape:
                        for comp in patches(px):
                            if (comp & base).sum() >= 0.5 * comp.sum():
                                mask |= comp
                    mask = grown(px, mask)
                if mask.any():
                    entries.append({"rotation": rotation, "frame": index, "width": int(px.shape[1]),
                                    "height": int(px.shape[0]), "runs": runs(mask)})
                    frames_written += 1
                    pixels += int(mask.sum())
        sprites[key] = entries
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"version": 1, "sprites": sprites}, separators=(",", ":")), encoding="utf-8")
    print(f"{len(sprites)} sprites, {frames_written} frames, {pixels} shadow pixels -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
