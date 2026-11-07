"""FRM sprite decoding (big-endian, as read by src/art.cc artReadHeader).

    header (62 bytes): version i32, fps i16, action_frame i16, frame_count i16,
                       x_offsets i16[6], y_offsets i16[6], data_offsets i32[6], data_size i32
    frame:             width i16, height i16, size i32, x i16, y i16, pixels u8[w*h]

Rotation r's frames start at 62 + data_offsets[r]. Art with one direction has
the same offset for all six rotations; per-direction critter files
(.fr0-.fr5) hold one rotation each and are addressed by their own fid.
Pixels are palette indices; index 0 is transparent.

`decode` also reads CE's indexed PNG art (art.cc artLoadIndexedPng: ce.dat's
invbox2.png, loot2.png, opbase_help.png, partyslot.png). CE takes the indices
as they are, ignoring the PNG's palette, into a one-frame, one-direction Art,
so it comes back here as that FRM would.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

HEADER_SIZE = 62
ROTATIONS = 6


@dataclass
class Frame:
    width: int
    height: int
    x: int  # per-frame hotspot delta, accumulated by the animation code
    y: int
    pixels: np.ndarray  # (height, width) uint8 palette indices


@dataclass
class Frm:
    version: int
    fps: int
    action_frame: int
    frame_count: int
    x_offsets: list[int]
    y_offsets: list[int]
    data_offsets: list[int]
    frames: list[list[Frame]] = field(default_factory=list)  # [rotation][frame]

    def frame(self, index: int, rotation: int) -> Frame:
        return self.frames[rotation][index]

    @property
    def rotation_count(self) -> int:
        """Distinct directions stored in the file (1 or 6)."""
        return len(set(self.data_offsets))


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_indexed_png(data: bytes) -> Frm:
    """An indexed PNG as CE loads it: one frame, all six rotations the same
    data, no offsets."""
    image = Image.open(io.BytesIO(data))
    if image.mode != "P":
        raise ValueError(f"not an indexed PNG (mode {image.mode}); CE accepts only palette PNGs")
    pixels = np.array(image, dtype=np.uint8)
    frm = Frm(4, 0, 0, 1, [0] * ROTATIONS, [0] * ROTATIONS, [0] * ROTATIONS)
    frames = [Frame(image.width, image.height, 0, 0, pixels)]
    frm.frames = [frames] * ROTATIONS
    return frm


def decode(data: bytes) -> Frm:
    if data[: len(PNG_SIGNATURE)] == PNG_SIGNATURE:
        return decode_indexed_png(data)
    version, fps, action_frame, frame_count = struct.unpack_from(">ihhh", data, 0)
    x_offsets = list(struct.unpack_from(">6h", data, 10))
    y_offsets = list(struct.unpack_from(">6h", data, 22))
    data_offsets = list(struct.unpack_from(">6i", data, 34))
    frm = Frm(version, fps, action_frame, frame_count, x_offsets, y_offsets, data_offsets)

    by_offset: dict[int, list[Frame]] = {}
    for rotation in range(ROTATIONS):
        offset = data_offsets[rotation]
        if offset not in by_offset:
            frames = []
            pos = HEADER_SIZE + offset
            for _ in range(frame_count):
                width, height, size, x, y = struct.unpack_from(">hhihh", data, pos)
                pos += 12
                pixels = np.frombuffer(data, dtype=np.uint8, count=width * height, offset=pos).reshape(height, width)
                pos += size
                frames.append(Frame(width, height, x, y, pixels))
            by_offset[offset] = frames
        frm.frames.append(by_offset[offset])
    return frm


# cycle.cc's color tables (8-bit; CE stores them >> 2). color.pal leaves index
# 0 and 229-255 as placeholders (255) that the game fills in at runtime.
CYCLE_COLORS = {
    229: [(0, 108, 0), (11, 115, 7), (27, 123, 15), (43, 131, 27)],  # slime
    233: [(107, 107, 111), (99, 103, 127), (87, 107, 143), (0, 147, 163), (107, 187, 255)],  # monitors
    238: [(255, 0, 0), (215, 0, 0), (147, 43, 11), (255, 119, 0), (255, 59, 0)],  # fire_slow
    243: [(71, 0, 0), (123, 0, 0), (179, 0, 0), (123, 0, 0), (71, 0, 0)],  # fire_fast
    248: [(83, 63, 43), (75, 59, 43), (67, 55, 39), (63, 51, 39), (55, 47, 35), (51, 43, 35)],  # shoreline
}
ALARM_INDEX = 254


def load_palette(data: bytes) -> np.ndarray:
    """color.pal -> (256, 3) uint8 RGB as the game shows it (cycle phase 0).

    The file stores 6-bit components; index 0 is black at runtime and the
    cycling range gets cycle.cc's colors (quantized to 6 bits like the game)."""
    raw = np.frombuffer(data[:768], dtype=np.uint8).reshape(256, 3).copy()
    raw[0] = 0
    for first, colors in CYCLE_COLORS.items():
        for i, rgb in enumerate(colors):
            raw[first + i] = np.array(rgb) >> 2
    raw[ALARM_INDEX] = (63, 0, 0)
    raw[255] = 0
    return palette_6bit_to_rgb(raw)


def palette_6bit_to_rgb(raw: np.ndarray) -> np.ndarray:
    # CE presents palettes as value << 2 (svga.cc directDrawSetPalette).
    return ((raw.astype(np.uint16) << 2) & 0xFF).astype(np.uint8)
