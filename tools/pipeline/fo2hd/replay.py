"""Re-rasterizes a capture at 1x from the original FRMs (PLAN.md P1.2 replay test).

Each function ports one CE draw routine, integer for integer, so a correct and
complete draw list reproduces the captured 8-bit map window exactly:

    floor    tile.cc tileRenderFloor (incl. the per-vertex light interpolation)
    roof     tile.cc tileRenderRoof (incl. the egg cut-out)
    object   object.cc _obj_render_object (+ _dark_trans / _dark_translucent /
             _intensity_mask buf_to_buf, blitBufferToBufferTrans)
    outline  object.cc objectDrawOutline
    text     text_object.cc textObjectsRenderInRect
    fill     bufferFill (map clear, hi-res stencil)

Everything works on palette indices; nothing here knows about RGB.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import frm as frm_mod
from .capture import Capture
from .dat import GameFiles

OBJ_TYPE_INTERFACE = 6
FIRST_UNLIT_COLOR = 0xE5  # _dark_trans_buf_to_buf leaves 229..255 (cycling colors) unlit

# tile.cc floor-light tables (verticies, triangles, row tables).
VERTEX_MAP_OFFSET = [16, 48, 960, 992, 1024, 1936, 1968, 2000, 2912, 2944]
RIGHTSIDE_UP_TRIANGLES = [(2, 3, 0), (3, 4, 1), (5, 6, 3), (6, 7, 4), (8, 9, 6)]
UPSIDE_DOWN_TRIANGLES = [(0, 3, 1), (2, 5, 3), (3, 6, 4), (5, 8, 6), (6, 9, 7)]
RIGHTSIDE_UP_TABLE = [(-1, 2), (78, 2), (76, 6), (73, 8), (71, 10), (68, 14), (65, 16), (63, 18), (61, 20), (58, 24), (55, 26), (53, 28), (50, 32)]
UPSIDE_DOWN_TABLE = [(0, 32), (48, 32), (49, 30), (52, 26), (55, 24), (57, 22), (60, 18), (63, 16), (65, 14), (67, 12), (70, 8), (73, 6), (75, 4)]
INTENSITY_MAP_WIDTH = 80

OUTLINE_PALETTED = 0x40000000
OUTLINE_TYPE_MASK = 0xFFFFFF


def cdiv(a: int, b: int) -> int:
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def intersect(a, b):
    """geometry.cc rectIntersection: inclusive rects, None when disjoint."""
    if a[0] <= b[2] and b[0] <= a[2] and b[3] >= a[1] and b[1] <= a[3]:
        return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return None


@dataclass
class ArtCache:
    files: GameFiles
    capture: Capture
    _frms: dict[int, frm_mod.Frm] = field(default_factory=dict)

    def get(self, fid: int) -> frm_mod.Frm:
        frm = self._frms.get(fid)
        if frm is None:
            frm = frm_mod.decode(self.files.read(self.capture.art[fid]))
            self._frms[fid] = frm
        return frm


class Rasterizer:
    def __init__(self, capture: Capture, files: GameFiles):
        self.capture = capture
        self.art = ArtCache(files, capture)
        self.buffer = np.zeros((capture.height, capture.width), dtype=np.uint8)
        # Per pixel: the light intensity the original applied (65536 = unlit),
        # and the index of the draw that wrote it (-1 = none). The compositor
        # uses these for the "original-equivalent" lighting.
        self.light = np.full((capture.height, capture.width), 65536, dtype=np.int32)
        self.owner = np.full((capture.height, capture.width), -1, dtype=np.int32)
        self.current = -1
        self.intensity_map = [int(v) for v in capture.floor_intensity_map]
        self.ict = capture.intensity_color
        self.skipped: list[dict] = []

    # --- blitters -------------------------------------------------------

    def dark_trans(self, src: np.ndarray, x: int, y: int, intensity: int):
        """_dark_trans_buf_to_buf: light all but the cycling colors, skip index 0."""
        h, w = src.shape
        dest = self.buffer[y : y + h, x : x + w]
        lit = self.ict[src, intensity // 512]
        lit = np.where(src < FIRST_UNLIT_COLOR, lit, src)
        np.copyto(dest, lit, where=src != 0)
        self._mark(x, y, src != 0, np.where(src < FIRST_UNLIT_COLOR, (intensity // 512) * 512, 65536))

    def translucent(self, src: np.ndarray, x: int, y: int, intensity: int, blend: np.ndarray, gray: np.ndarray):
        """_dark_translucent_trans_buf_to_buf."""
        h, w = src.shape
        dest = self.buffer[y : y + h, x : x + w]
        index = blend[(gray[src].astype(np.int32) << 8) + dest]
        np.copyto(dest, self.ict[index, intensity // 512], where=src != 0)
        self._mark(x, y, src != 0, (intensity // 512) * 512)

    def intensity_mask(self, src: np.ndarray, x: int, y: int, mask: np.ndarray, intensity: int):
        """_intensity_mask_buf_to_buf: the egg cross-fade into what's behind."""
        h, w = src.shape
        dest = self.buffer[y : y + h, x : x + w]
        color = self.ict[src, intensity // 512]
        m = mask.astype(np.int32)
        behind = self.ict[dest, 128 - m]
        front = self.ict[color, m]
        mixed = self.capture.mix_add[front, behind]
        color = np.where(m != 0, mixed, color)
        np.copyto(dest, color, where=src != 0)
        self._mark(x, y, src != 0, (intensity // 512) * 512)

    def trans_copy(self, src: np.ndarray, x: int, y: int):
        """blitBufferToBufferTrans."""
        h, w = src.shape
        dest = self.buffer[y : y + h, x : x + w]
        np.copyto(dest, src, where=src != 0)
        self._mark(x, y, src != 0, 65536)

    def _mark(self, x: int, y: int, where: np.ndarray, light) -> None:
        h, w = where.shape
        np.copyto(self.light[y : y + h, x : x + w], light, where=where)
        self.owner[y : y + h, x : x + w][where] = self.current

    def egg_blit(self, pixels: np.ndarray, rect, origin_x: int, origin_y: int, light: int, egg_fid: int, egg_rect) -> bool:
        """Shared egg branch of tileRenderRoof and _obj_render_object.

        `rect` is the clipped destination rect, `pixels` the full frame whose
        top-left lands at (origin_x, origin_y). Returns False if the egg does
        not intersect (the caller then draws normally)."""
        cut = intersect(egg_rect, rect)
        if cut is None:
            return False

        def src_of(r):
            return pixels[r[1] - origin_y : r[3] - origin_y + 1, r[0] - origin_x : r[2] - origin_x + 1]

        rects = [
            (rect[0], rect[1], rect[2], cut[1] - 1),
            (rect[0], cut[1], cut[0] - 1, cut[3]),
            (cut[2] + 1, cut[1], rect[2], cut[3]),
            (rect[0], cut[3] + 1, rect[2], rect[3]),
        ]
        for r in rects:
            if r[0] <= r[2] and r[1] <= r[3]:
                self.dark_trans(src_of(r), r[0], r[1], light)

        egg = self.art.get(egg_fid).frame(0, 0).pixels
        mask = egg[cut[1] - egg_rect[1] : cut[3] - egg_rect[1] + 1, cut[0] - egg_rect[0] : cut[2] - egg_rect[0] + 1]
        self.intensity_mask(src_of(cut), cut[0], cut[1], mask, light)
        return True

    # --- draw ops -------------------------------------------------------

    def op_fill(self, d):
        l, t, r, b = d["rect"]
        self.buffer[t : b + 1, l : r + 1] = d["color"]
        self.light[t : b + 1, l : r + 1] = 65536
        self.owner[t : b + 1, l : r + 1] = self.current

    def op_floor(self, d):
        frame = self.art.get(d["fid"]).frame(0, 0)
        W, H = self.capture.width, self.capture.height
        clip = d["clip"]
        x, y = d["x"], d["y"]

        left, top = max(clip[0], 0), max(clip[1], 0)
        width = clip[2] - clip[0] + 1
        height = clip[3] - clip[1] + 1
        if clip[0] < 0:
            width = clip[2] - clip[0] + 1  # CE keeps the unclamped width here
        if left + width > W:
            width = W - left
        if top + height > H:
            height = H - top
        if x >= W or x > clip[2] or y >= H or y > clip[3]:
            return

        fw, fh = frame.width, frame.height
        if left < x:
            sx = 0
            w = fw if fw + x <= left + width else left + width - x
        else:
            sx = left - x
            x = left
            w = min(fw - sx, width)
        if top < y:
            sy = 0
            h = fh if fh + y <= top + height else top + height - y
        else:
            sy = top - y
            y = top
            h = min(fh - sy, height)
        if w <= 0 or h <= 0:
            return

        src = frame.pixels[sy : sy + h, sx : sx + w]
        light = d["vertex_light"]
        if all(v == light[0] for v in light):
            self.dark_trans(src, x, y, light[0])
            return

        imap = self.intensity_map
        for f0, f4, f8 in RIGHTSIDE_UP_TRIANGLES:
            value = light[f8]
            step_x = cdiv(light[f4] - light[f0], 32)
            step_y = cdiv(light[f0] - value, 13)
            p = VERTEX_MAP_OFFSET[f8]
            for skip, count in RIGHTSIDE_UP_TABLE:
                v = value
                p += skip
                for _ in range(count):
                    imap[p] = v
                    p += 1
                    v += step_x
                value += step_y
        for f0, f4, f8 in UPSIDE_DOWN_TRIANGLES:
            value = light[f0]
            step_x = cdiv(light[f8] - value, 32)
            step_y = cdiv(light[f4] - value, 13)
            p = VERTEX_MAP_OFFSET[f0]
            for skip, count in UPSIDE_DOWN_TABLE:
                v = value
                p += skip
                for _ in range(count):
                    imap[p] = v
                    p += 1
                    v += step_x
                value += step_y

        rows = np.arange(h)[:, None]
        cols = np.arange(w)[None, :]
        index = 160 + INTENSITY_MAP_WIDTH * (sy + rows) + sx + cols
        intensities = np.asarray(imap, dtype=np.int64)[index] >> 9
        dest = self.buffer[y : y + h, x : x + w]
        np.copyto(dest, self.ict[src, intensities], where=src != 0)
        self._mark(x, y, src != 0, intensities * 512)

    def op_roof(self, d):
        frame = self.art.get(d["fid"]).frame(0, 0)
        x, y = d["x"], d["y"]
        rect = intersect((x, y, x + frame.width - 1, y + frame.height - 1), d["clip"])
        if rect is None:
            return
        light = d["light"]
        egg_fid = self.capture.meta["egg"]["fid"]
        if self.egg_blit(frame.pixels, rect, x, y, light, egg_fid, d["egg_rect"]):
            return
        src = frame.pixels[rect[1] - y : rect[3] - y + 1, rect[0] - x : rect[2] - x + 1]
        self.dark_trans(src, rect[0], rect[1], light)

    def op_object(self, d):
        frame = self.art.get(d["fid"]).frame(d["frame"], d["rotation"])
        if (frame.width, frame.height) != (d["w"], d["h"]):
            raise ValueError(f"frame size mismatch for fid {d['fid']} ({self.capture.art[d['fid']]})")
        sx, sy = d["x"], d["y"]
        rect = intersect((sx, sy, sx + frame.width - 1, sy + frame.height - 1), d["clip"])
        if rect is None:
            return
        src = frame.pixels[rect[1] - sy : rect[3] - sy + 1, rect[0] - sx : rect[2] - sx + 1]
        light = d["light"]
        mode = d["mode"]

        if mode == "interface":
            self.trans_copy(src, rect[0], rect[1])
            return
        if "egg_rect" in d and self.egg_blit(frame.pixels, rect, sx, sy, light, d["egg_fid"], d["egg_rect"]):
            return

        blend, gray = self.capture.blend, self.capture.gray
        if mode == "normal":
            self.dark_trans(src, rect[0], rect[1], light)
        elif mode == "trans_wall":
            self.translucent(src, rect[0], rect[1], 0x10000, blend["wall"], gray["common"])
        elif mode == "trans_glass":
            self.translucent(src, rect[0], rect[1], light, blend["glass"], gray["glass"])
        elif mode == "trans_red":
            self.translucent(src, rect[0], rect[1], light, blend["red"], gray["common"])
        elif mode == "trans_steam":
            self.translucent(src, rect[0], rect[1], light, blend["steam"], gray["common"])
        elif mode == "trans_energy":
            self.translucent(src, rect[0], rect[1], light, blend["energy"], gray["common"])
        else:
            raise ValueError(f"unknown blend mode {mode}")

    def op_text(self, d):
        pixels = np.frombuffer(__import__("base64").b64decode(d["pixels"]), dtype=np.uint8).reshape(d["h"], d["w"])
        x, y = d["x"], d["y"]
        rect = intersect((x, y, x + d["w"] - 1, y + d["h"] - 1), d["clip"])
        if rect is None:
            return
        src = pixels[rect[1] - y : rect[3] - y + 1, rect[0] - x : rect[2] - x + 1]
        self.trans_copy(src, rect[0], rect[1])

    def op_outline(self, d):
        """objectDrawOutline, ported with its pointer arithmetic on the flat buffer."""
        cap = self.capture
        frame = self.art.get(d["fid"]).frame(d["frame"], d["rotation"])
        fw, fh = frame.width, frame.height
        sx, sy = d["x"], d["y"]
        oc = intersect((sx, sy, sx + fw - 1, sy + fh - 1), d["clip"])
        if oc is None:
            return
        vis_left = oc[0] - sx
        vis_top = oc[1] - sy
        vis_right = vis_left + (oc[2] - oc[0])
        vis_bottom = vis_top + (oc[3] - oc[1])

        pitch = cap.width
        flat = self.buffer.reshape(-1)
        size = flat.size
        src = frame.pixels.reshape(-1)
        dest = pitch * sy + sx
        dest_step = pitch - fw

        outline = d["outline"]
        paletted = outline & OUTLINE_PALETTED
        kind = outline & OUTLINE_TYPE_MASK
        gray_table = blend_table = None
        animated_count = 0
        if kind == 0x01:  # hostile
            color, paletted, animated_count, band = 243, 0, 5, fh // 5
        elif kind == 0x02:  # same team
            color, band = cap.color(0xFF0000), 0
            if paletted:
                gray_table, blend_table = cap.gray["common"], cap.blend["red"]
        elif kind == 0x04:  # body
            color, band = cap.color(0x7B7B7B), 0
            if paletted:
                gray_table, blend_table = cap.gray["common"], cap.blend["wall"]
        elif kind == 0x08:  # friendly
            animated_count, band, color, paletted = 4, fh // 4, 229, 0
        elif kind == 0x10:  # item
            band, color = 0, cap.color(0xEFEF42)
            if paletted:
                gray_table, blend_table = cap.gray["common"], cap.blend["red"]
        elif kind == 0x20:  # blocked
            color, paletted, animated_count, band = 61, 0, 1, fh
        else:
            color, paletted, band = cap.color(0xFF00FF), 0, 0

        def blended(c, under):
            return int(blend_table[(int(gray_table[c]) << 8) + int(under)])

        # Horizontal pass (left and right edges of each row).
        outline_color = color
        d_ptr, s_ptr = dest, 0
        for y in range(fh):
            cycle = True
            if band != 0:
                if y % band == 0:
                    outline_color = (outline_color + 1) & 0xFF
                if outline_color > animated_count + color - 1:
                    outline_color = color
            y_visible = vis_top <= y <= vis_bottom
            for x in range(fw):
                dest_offset = d_ptr
                s = src[s_ptr]
                if s != 0 and cycle:
                    if y_visible and vis_left <= x <= vis_right and dest_offset > 0 and dest_offset % pitch != 0:
                        flat[d_ptr - 1] = blended(outline_color, flat[d_ptr - 1]) if paletted else outline_color
                    cycle = False
                elif s == 0 and not cycle:
                    if y_visible and vis_left <= x <= vis_right:
                        flat[d_ptr] = (blended(outline_color, flat[d_ptr]) if paletted else outline_color) & 0xFF
                    cycle = True
                d_ptr += 1
                s_ptr += 1
            if src[s_ptr - 1] != 0:
                if 0 <= d_ptr < size and d_ptr % pitch != 0:
                    if y_visible and vis_left <= fw - 1 <= vis_right:
                        flat[d_ptr] = blended(outline_color, flat[d_ptr]) if paletted else outline_color
            d_ptr += dest_step

        # Vertical pass (top and bottom edges of each column).
        for x in range(fw):
            if x < vis_left or x > vis_right:
                continue
            cycle = True
            column_color = color
            d_ptr = dest + x
            s_ptr = x
            for y in range(fh):
                if band != 0:
                    if y % band == 0:
                        column_color = (column_color + 1) & 0xFF
                    if column_color > color + animated_count - 1:
                        column_color = color
                y_visible = vis_top <= y <= vis_bottom
                s = src[s_ptr]
                if s != 0 and cycle:
                    if y_visible:
                        above = d_ptr - pitch
                        if above >= 0:
                            flat[above] = blended(column_color, flat[above]) if paletted else column_color
                    cycle = False
                elif s == 0 and not cycle:
                    if y_visible:
                        flat[d_ptr] = blended(column_color, flat[d_ptr]) if paletted else column_color
                    cycle = True
                d_ptr += pitch
                s_ptr += fw
            if src[s_ptr - fw] != 0:
                if 0 <= d_ptr < size:
                    if vis_top <= fh - 1 <= vis_bottom:
                        flat[d_ptr] = blended(column_color, flat[d_ptr]) if paletted else column_color

    def run(self) -> np.ndarray:
        handlers = {
            "fill": self.op_fill,
            "floor": self.op_floor,
            "roof": self.op_roof,
            "object": self.op_object,
            "outline": self.op_outline,
            "text": self.op_text,
        }
        for index, draw in enumerate(self.capture.draws):
            op = draw["op"]
            if op == "pass":
                continue
            self.current = index
            if op == "outline":
                # Outlines write pixel by pixel; mark whatever they changed.
                before = self.buffer.copy()
                handlers[op](draw)
                changed = before != self.buffer
                self.owner[changed] = index
                self.light[changed] = 65536
            else:
                handlers[op](draw)
        return self.buffer


@dataclass
class ReplayResult:
    replayed: np.ndarray
    captured: np.ndarray

    @property
    def diff_mask(self) -> np.ndarray:
        return self.replayed != self.captured

    @property
    def diff_pixels(self) -> int:
        return int(self.diff_mask.sum())


def replay(capture: Capture, files: GameFiles) -> ReplayResult:
    rasterizer = Rasterizer(capture, files)
    replayed = rasterizer.run()
    return ReplayResult(replayed=replayed, captured=capture.window_indices())
