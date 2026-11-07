"""Size of the whole game's art (for P1.10's full-batch projection)."""

from __future__ import annotations

import struct
from collections import defaultdict

from .dat import GameFiles

FRM_EXTENSIONS = (".frm", ".fr0", ".fr1", ".fr2", ".fr3", ".fr4", ".fr5")


def count(files: GameFiles) -> dict:
    """Frames and pixels per art folder (critters, scenery, ...), every stored direction."""
    stats: dict[str, dict] = defaultdict(lambda: {"files": 0, "frames": 0, "pixels": 0})
    for path, layer in files.list().items():
        if not path.startswith("art\\") or not path.endswith(FRM_EXTENSIONS):
            continue
        folder = path.split("\\")[1]
        data = layer.read(path)
        frame_count = struct.unpack_from(">h", data, 8)[0]
        offsets = struct.unpack_from(">6i", data, 34)
        pixels = frames = 0
        for offset in sorted(set(offsets)):
            pos = 62 + offset
            for _ in range(frame_count):
                if pos + 12 > len(data):
                    break
                w, h, size = struct.unpack_from(">hhi", data, pos)
                pixels += w * h
                frames += 1
                pos += 12 + size
        s = stats[folder]
        s["files"] += 1
        s["frames"] += frames
        s["pixels"] += pixels
    return dict(stats)
