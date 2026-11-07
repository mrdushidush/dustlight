"""Loads captures written by the engine's draw-list recorder (src/hd/capture.cc).

A capture is <stem>.json plus <stem>.png (the 8-bit map window). Binary tables
are base64 in the JSON; this module decodes them into numpy arrays.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

FORMAT = "fo2hd-capture"
VERSION = 1


def _b64(text: str, dtype=np.uint8) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype=dtype)


@dataclass
class Capture:
    path: Path
    meta: dict
    draws: list[dict]
    width: int
    height: int
    palette_system: np.ndarray  # (256, 3) 6-bit
    palette_screen: np.ndarray  # (256, 3) 6-bit, after gamma
    intensity_color: np.ndarray  # (256, 256)
    mix_add: np.ndarray  # (256, 256)
    color_15bit: np.ndarray  # (32768,)
    blend: dict[str, np.ndarray]  # name -> (4096,)
    gray: dict[str, np.ndarray]  # name -> (256,)
    floor_intensity_map: np.ndarray  # (3280,) int32, state before the first floor
    hex_light: np.ndarray  # (200*200,) int32, unclamped
    hex_screen: np.ndarray  # (200*200, 2) int32
    art: dict[int, str]  # fid -> game path
    lights: list[dict]

    @property
    def png_path(self) -> Path:
        return self.path.with_suffix(".png")

    def window_indices(self) -> np.ndarray:
        """The captured 8-bit map window as palette indices (height, width)."""
        image = Image.open(self.png_path)
        if image.mode != "P":
            raise ValueError(f"{self.png_path} is not an indexed PNG")
        return np.asarray(image, dtype=np.uint8)

    def screen_rgb(self) -> np.ndarray:
        """(256, 3) 8-bit palette exactly as the game presented it."""
        return ((self.palette_screen.astype(np.uint16) << 2) & 0xFF).astype(np.uint8)

    def color(self, rgb: int) -> int:
        """CE's COLOR_* macros: nearest palette index of a 24-bit color."""
        rgb555 = ((rgb >> 19) & 0x1F) << 10 | ((rgb >> 11) & 0x1F) << 5 | ((rgb >> 3) & 0x1F)
        return int(self.color_15bit[rgb555])


def load(path: Path | str) -> Capture:
    path = Path(path)
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    meta = json.loads(path.read_text(encoding="utf-8"))
    if meta.get("format") != FORMAT or meta.get("version") != VERSION:
        raise ValueError(f"{path}: not a {FORMAT} v{VERSION} capture")

    tables = meta.pop("tables")
    palette = meta.pop("palette")
    capture = Capture(
        path=path,
        meta=meta,
        draws=meta.pop("draws"),
        width=meta["window"]["width"],
        height=meta["window"]["height"],
        palette_system=_b64(palette["system"]).reshape(256, 3),
        palette_screen=_b64(palette["screen"]).reshape(256, 3),
        intensity_color=_b64(tables["intensity_color"]).reshape(256, 256),
        mix_add=_b64(tables["mix_add"]).reshape(256, 256),
        color_15bit=_b64(tables["color_15bit"]),
        blend={name: _b64(value) for name, value in tables["blend"].items()},
        gray={name: _b64(value) for name, value in tables["gray"].items()},
        floor_intensity_map=_b64(meta.pop("floor_intensity_map")["int32le"], np.int32).copy(),
        hex_light=_b64(meta.pop("hex_light")["int32le"], np.int32),
        hex_screen=_b64(meta.pop("hex_screen")["int32le_xy"], np.int32).reshape(-1, 2),
        art={int(fid): p for fid, p in meta.pop("art").items()},
        lights=meta.pop("lights"),
    )
    return capture
