"""Game fonts for HD text (PLAN.md P7b.5): the AAF fonts CE's interface text
uses (font_manager.cc), and candidate HD glyphs to choose a look from.

AAF: "AAFF", max height, letter spacing, word spacing, line spacing (u16,
big-endian), 256 glyphs (u16 width, u16 height, u32 offset into the data),
then one byte per glyph pixel. CE blends the text color over what's below
through color.cc's blend tables: level 0 leaves the pixel, levels 1..7 mix
in k/7 of the text color, 8 and up are darker shades (rare).

`fo2hd fonts-review` renders sample lines at the author's 3x: CE's pixels as
they look today (nearest) next to HD candidates:
  smooth  the glyph coverage upsampled (bicubic, 4x supersampled) through a
          soft contrast curve: clean anti-aliased edges, the same weight
  sharp   the same with a steeper curve: crisper, slightly thinner
  model   the art pipeline's upscaler on a sheet of the glyphs (white on
          black, run on the CPU so it doesn't compete with the batch)
Writes <data_root>/reports/fonts/index.html. Game-derived: local only.

`fo2hd fonts` builds what the engine's HD text draws with (P7b.5): per font,
style and tier (2x/3x/4x) an atlas of glyph coverage, see `build_atlases`.
The author chose the upscaler's glyphs ("model") on 2026-11-02 and liked smooth
and sharp too, so all three are built; [hd] ui_text_style picks one.
"""

from __future__ import annotations

import html
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .config import Paths
from .dat import GameFiles

FULL_LEVEL = 7
HEADER = 4 + 8 + 256 * 8  # data offsets count from here (CE: fileSize - 2060)
REVIEW_SCALE = 3
SUPERSAMPLE = 4

# Sample lines per font, roughly where the game uses them.
SAMPLES = {
    0: ["Small print: 1234567890 HP AC AP"],
    1: ["You see: Klamath Trapper. He looks unhurt.", "Hit Points 34/41   Armor Class 12   Action Points 8"],
    2: ["CHARACTER", "S.P.E.C.I.A.L."],
    3: ["1. Tell me about the Den.", "2. I'll be going now."],
    4: ["Fallout 2", "Pip-Boy 2000"],
}
TEXT_COLOR = (60, 248, 0)  # the interface's green
BACKGROUND = (24, 20, 12)


@dataclass
class AafFont:
    max_height: int
    letter_spacing: int
    word_spacing: int
    line_spacing: int
    glyphs: list[np.ndarray]  # 256 arrays of levels, (height, width) uint8
    sizes: list[tuple[int, int]]  # 256 (width, height) as the header has them


def parse_aaf(data: bytes) -> AafFont:
    if data[:4] != b"AAFF":
        raise ValueError("not an AAF font")
    max_height, letter, word, line = struct.unpack(">4H", data[4:12])
    glyphs = []
    sizes = []
    for index in range(256):
        width, height, offset = struct.unpack(">HHI", data[12 + index * 8: 20 + index * 8])
        sizes.append((width, height))
        start = HEADER + offset
        if width == 0 or height == 0 or start + width * height > len(data):
            glyphs.append(np.zeros((height, width) if width and height else (0, 0), np.uint8))
            continue
        glyphs.append(np.frombuffer(data, np.uint8, width * height, start).reshape(height, width).copy())
    return AafFont(max_height, letter, word, line, glyphs, sizes)


def font_hash(font: AafFont) -> int:
    """FNV-1a (32 bit) over each glyph's width and height (u16, little
    endian) and its level bytes: the engine hashes CE's loaded font the same
    way (hd/ui_text.cc) and uses the atlases only if they match."""
    h = 2166136261
    for (width, height), levels in zip(font.sizes, font.glyphs):
        for b in struct.pack("<HH", width, height) + (levels.tobytes() if levels.size == width * height else b""):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def coverage(levels: np.ndarray) -> np.ndarray:
    """Share of the text color per pixel (shades above full count as full)."""
    return np.minimum(levels.astype(np.float32), FULL_LEVEL) / FULL_LEVEL


def layout(font: AafFont, text: str) -> tuple[list[tuple[int, int, int]], int]:
    """(character, x, y) of each glyph's top-left at 1x, as
    interfaceFontDrawImpl places them (spaces advance by the word spacing,
    glyphs sit on the bottom of the line), and the line's width."""
    placed = []
    x = 0
    for ch in text.encode("cp1252", errors="replace"):
        glyph = font.glyphs[ch]
        width = font.word_spacing if ch == 0x20 else font.sizes[ch][0]
        if ch != 0x20 and glyph.size:
            placed.append((ch, x, font.max_height - glyph.shape[0]))
        x += width + font.letter_spacing
    return placed, x


def render(font: AafFont, text: str, scale: int, hd: dict[int, np.ndarray] | None = None) -> np.ndarray:
    """Coverage of a line at `scale`: CE's glyphs repeated (nearest) or the
    HD glyphs (already at `scale`)."""
    placed, width = layout(font, text)
    out = np.zeros((font.max_height * scale, max(width, 1) * scale), np.float32)
    for ch, x, y in placed:
        g = hd[ch] if hd is not None else coverage(font.glyphs[ch]).repeat(scale, 0).repeat(scale, 1)
        h, w = g.shape
        region = out[y * scale: y * scale + h, x * scale: x * scale + w]
        np.maximum(region, g[: region.shape[0], : region.shape[1]], out=region)
    return out


def _curve(c: np.ndarray, softness: float) -> np.ndarray:
    t = np.clip((c - 0.5) / softness + 0.5, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def hd_smooth(font: AafFont, scale: int, softness: float) -> dict[int, np.ndarray]:
    """Bicubic upsampling of the coverage at scale x SUPERSAMPLE, a contrast
    curve around 1/2, then an area downscale: smooth, crisp edges."""
    glyphs = {}
    s = scale * SUPERSAMPLE
    for ch, levels in enumerate(font.glyphs):
        if levels.size == 0:
            continue
        c = np.pad(coverage(levels), 1)
        up = cv2.resize(c, (c.shape[1] * s, c.shape[0] * s), interpolation=cv2.INTER_CUBIC)
        up = _curve(up, softness)
        down = cv2.resize(up, (c.shape[1] * scale, c.shape[0] * scale), interpolation=cv2.INTER_AREA)
        glyphs[ch] = down[scale:-scale, scale:-scale]
    return glyphs


def hd_model(font: AafFont, scale: int, upscaler, model_scale: int = 4, pad: int = 4) -> dict[int, np.ndarray]:
    """The upscaler on one sheet of all glyphs (white on black, `pad` px
    apart), area-downscaled from its 4x to `scale`."""
    return {ch: _area(g, scale) for ch, g in hd_model_4x(font, upscaler, model_scale, pad).items()}


def _area(coverage_4x: np.ndarray, scale: int) -> np.ndarray:
    h, w = coverage_4x.shape[0] // 4, coverage_4x.shape[1] // 4
    if scale == 4:
        return coverage_4x
    return np.clip(cv2.resize(coverage_4x, (w * scale, h * scale), interpolation=cv2.INTER_AREA), 0.0, 1.0)


def hd_model_4x(font: AafFont, upscaler, model_scale: int = 4, pad: int = 4) -> dict[int, np.ndarray]:
    """The upscaler's glyphs at 4x (`hd_model` before the downscale)."""
    chars = [ch for ch, g in enumerate(font.glyphs) if g.size]
    columns = 16
    cell_w = max(font.glyphs[ch].shape[1] for ch in chars) + 2 * pad
    cell_h = font.max_height + 2 * pad
    rows = (len(chars) + columns - 1) // columns
    sheet = np.zeros((rows * cell_h, columns * cell_w), np.float32)
    places = {}
    for i, ch in enumerate(chars):
        g = coverage(font.glyphs[ch])
        y = (i // columns) * cell_h + pad
        x = (i % columns) * cell_w + pad
        sheet[y: y + g.shape[0], x: x + g.shape[1]] = g
        places[ch] = (x, y, g.shape[1], g.shape[0])
    rgb = np.repeat((sheet * 255.0 + 0.5).astype(np.uint8)[..., None], 3, axis=2)
    up = upscaler.rgb(rgb).astype(np.float32).mean(axis=2) / 255.0
    glyphs = {}
    for ch, (x, y, w, h) in places.items():
        cell = up[y * model_scale: (y + h) * model_scale, x * model_scale: (x + w) * model_scale]
        if model_scale != 4:
            cell = cv2.resize(cell, (w * 4, h * 4), interpolation=cv2.INTER_AREA)
        glyphs[ch] = np.clip(cell, 0.0, 1.0)
    return glyphs


FONTS_VERSION = 1
ATLAS_TIERS = (2, 3, 4)
STYLES = ("model", "smooth", "sharp")  # model = the default (the author, 2026-11-02)
SOFTNESS = {"smooth": 0.35, "sharp": 0.18}
ATLAS_COLUMNS = 16
FONT_COUNT = 16  # CE's interface fonts font0..font15.aaf (font numbers 100..115)


def atlas_layout(font: AafFont) -> tuple[list[tuple[int, int, int, int, int]], int, int]:
    """Where each glyph with pixels sits in the atlas, in 1x units: a grid of
    ATLAS_COLUMNS cells, each the widest glyph + 1 by the font height + 1.
    The same layout serves every tier (x, y, w, h times the tier) and style.
    Returns ([(ch, x, y, w, h)], atlas width, atlas height)."""
    chars = [ch for ch, g in enumerate(font.glyphs) if g.size]
    cell_w = max(font.sizes[ch][0] for ch in chars) + 1
    cell_h = font.max_height + 1
    places = [(ch, (i % ATLAS_COLUMNS) * cell_w, (i // ATLAS_COLUMNS) * cell_h, font.sizes[ch][0], font.sizes[ch][1]) for i, ch in enumerate(chars)]
    rows = (len(chars) + ATLAS_COLUMNS - 1) // ATLAS_COLUMNS
    return places, ATLAS_COLUMNS * cell_w, rows * cell_h


def coverage_psnr(font: AafFont, glyphs: dict[int, np.ndarray], scale: int) -> float:
    """HD glyphs area-averaged back to 1x vs CE's coverage (k/7), in 8-bit
    coverage units: how far the HD text strays from the original where the
    engine's HD UI self-test compares them."""
    errors = []
    for ch, g in glyphs.items():
        h, w = font.glyphs[ch].shape
        back = g.reshape(h, scale, w, scale).mean(axis=(1, 3))
        errors.append(((back - coverage(font.glyphs[ch])) * 255.0).ravel())
    mse = float(np.mean(np.concatenate(errors) ** 2)) if errors else 0.0
    return 99.0 if mse == 0 else 10.0 * np.log10(255.0 * 255.0 / mse)


def build_atlases(paths: Paths, files: GameFiles, model_path: Path | None, report=print) -> Path:
    """HD glyph atlases for the engine's HD text (P7b.5): per interface font,
    style and tier an 8-bit coverage PNG <data_root>/hdpack/fonts/font<N>-<style>-<tier>x.png,
    and fonts.json with the layout and a hash of the font they were made from."""
    out = paths.data_root / "hdpack" / "fonts"
    out.mkdir(parents=True, exist_ok=True)
    upscaler = None
    if model_path is not None:
        from .upscale import Upscaler

        upscaler = Upscaler(model_path, device="cpu", half=False)
    index = {"version": FONTS_VERSION, "model": model_path.name if model_path is not None else "(none)", "tiers": list(ATLAS_TIERS),
             "styles": [s for s in STYLES if s != "model" or upscaler], "fonts": []}
    for number in range(FONT_COUNT):
        name = f"font{number}.aaf"
        if not files.exists(name):
            continue
        font = parse_aaf(files.read(name))
        places, atlas_w, atlas_h = atlas_layout(font)
        levels_over_full = sum(int((g > FULL_LEVEL).sum()) for g in font.glyphs)
        entry = {"file": name, "index": number, "hash": font_hash(font), "max_height": font.max_height,
                 "atlas": [atlas_w, atlas_h], "glyphs": [list(p) for p in places], "psnr": {}}
        model_4x = hd_model_4x(font, upscaler) if upscaler is not None else None
        for style in index["styles"]:
            for tier in ATLAS_TIERS:
                glyphs = {ch: _area(g, tier) for ch, g in model_4x.items()} if style == "model" else hd_smooth(font, tier, SOFTNESS[style])
                atlas = np.zeros((atlas_h * tier, atlas_w * tier), np.uint8)
                for ch, x, y, w, h in places:
                    atlas[y * tier: (y + h) * tier, x * tier: (x + w) * tier] = (glyphs[ch] * 255.0 + 0.5).astype(np.uint8)
                file = f"font{number}-{style}-{tier}x.png"
                Image.fromarray(atlas, "L").save(out / file)
                entry["psnr"][f"{style}-{tier}x"] = round(coverage_psnr(font, glyphs, tier), 2)
        index["fonts"].append(entry)
        report(f"{name}: {len(places)} glyphs, height {font.max_height}, atlas {atlas_w}x{atlas_h} at 1x"
               f"{f', {levels_over_full} px darker than full' if levels_over_full else ''}; coverage PSNR vs 1x: "
               + ", ".join(f"{k} {v:.1f}" for k, v in entry["psnr"].items() if k.endswith("3x")))
    (out / "fonts.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return out / "fonts.json"


def _colorize(c: np.ndarray) -> np.ndarray:
    fg = np.array(TEXT_COLOR, np.float32)
    bg = np.array(BACKGROUND, np.float32)
    return (bg + (fg - bg) * c[..., None] + 0.5).astype(np.uint8)


def review(paths: Paths, files: GameFiles, model_path: Path | None) -> Path:
    out = paths.reports / "fonts"
    out.mkdir(parents=True, exist_ok=True)
    upscaler = None
    if model_path is not None:
        from .upscale import Upscaler

        upscaler = Upscaler(model_path, device="cpu", half=False)
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>HD text candidates</title><style>",
        "body{background:#1b1a17;color:#e4dfd4;font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1500px;padding:16px}",
        "h2{color:#d8a14a;font-size:19px;margin:28px 0 6px}.dim{color:#a39d90}figure{margin:8px 0}figcaption{color:#a39d90;font-size:13px}",
        "img{image-rendering:pixelated;max-width:100%}</style></head><body>",
        "<h1>HD text: candidates</h1>",
        f"<p class='dim'>The game's five interface fonts at your {REVIEW_SCALE}× scale, 1:1 pixels. "
        "“today” is how CE's pixels look at 3× now; the others are HD glyph candidates. Text color and background are "
        "the interface's green on a dark panel; in the game the HD text would blend over the HD art below it.</p>",
    ]
    for index in range(5):
        name = f"font{index}.aaf"
        if not files.exists(name):
            continue
        font = parse_aaf(files.read(name))
        variants = [("today (CE's pixels, nearest)", None),
                    ("smooth", hd_smooth(font, REVIEW_SCALE, 0.35)),
                    ("sharp", hd_smooth(font, REVIEW_SCALE, 0.18))]
        if upscaler is not None:
            variants.append(("model (art upscaler)", hd_model(font, REVIEW_SCALE, upscaler)))
        parts.append(f"<h2>{e(name)}</h2><p class='dim'>height {font.max_height} px, letter spacing {font.letter_spacing}, "
                     f"word spacing {font.word_spacing}</p>")
        for label, glyphs in variants:
            lines = [render(font, text, REVIEW_SCALE, glyphs) for text in SAMPLES.get(index, ["The quick brown fox"])]
            width = max(line.shape[1] for line in lines) + 24
            gap = 6 * REVIEW_SCALE
            image = np.zeros((sum(line.shape[0] + gap for line in lines) + gap, width), np.float32)
            y = gap
            for line in lines:
                image[y: y + line.shape[0], 12: 12 + line.shape[1]] = line
                y += line.shape[0] + gap
            file = f"font{index}-{label.split()[0]}.png"
            Image.fromarray(_colorize(image)).save(out / file)
            parts.append(f"<figure><img src='{file}' alt=''><figcaption>{e(label)}</figcaption></figure>")
    parts.append("</body></html>")
    page = out / "index.html"
    page.write_text("\n".join(parts), encoding="utf-8")
    return page
