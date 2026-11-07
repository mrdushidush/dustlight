"""4x sprite upscaling with spandrel models (PLAN.md P1.6).

Sprites are palette images where index 0 is transparent. Feeding the model a
transparent hole (black, or any key color) bleeds that color into the edges as
a halo, so:

1. every transparent pixel takes the color of its nearest opaque pixel
   ("dilation"), and the image is edge-padded, so the model only sees
   plausible continuations of the sprite;
2. the RGB is upscaled by the model;
3. the opacity mask is upscaled separately (bicubic, then a smoothstep that
   narrows the ramp to an anti-aliased ~1-2 output pixels).

Small sprites (critter frames are ~36x61) leave the GPU mostly idle one at a
time, so `canvases` runs many of them per model call (P7): images are grouped
by size rounded up to BUCKET px, edge-padded on the right and bottom to their
group's size and stacked. Each image is still processed on its own (a batch
dimension, nothing mixes), but the extra padding changes the input a little;
DAT's channel attention sees the whole input, so <1% of pixels end up more
than 2 levels from the one-at-a-time result. 4x faster on critter frames.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import torch

PAD = 8
BUCKET = 16
BATCH_PIXELS = 256 * 1024  # padded input pixels per model call (peak ~4.9 GB VRAM with DAT)
BATCH_MAX = 64
TILE = 320  # input px per side when one image doesn't fit the VRAM (`Upscaler._tiled`)
TILE_OVERLAP = 32  # context around each tile's kept centre


def fill_transparent(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Gives each transparent pixel the color of the nearest opaque one."""
    if mask.all() or not mask.any():
        return rgb
    src = (~mask).astype(np.uint8)  # 0 = opaque (the seeds)
    _, labels = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lut = np.zeros((labels.max() + 1, 3), dtype=np.uint8)
    lut[labels[mask]] = rgb[mask]
    return lut[labels]


def upscale_mask(mask: np.ndarray, scale: int) -> np.ndarray:
    """Binary mask -> anti-aliased uint8 alpha at `scale`x."""
    h, w = mask.shape
    soft = cv2.resize(mask.astype(np.float32), (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    t = np.clip((soft - 0.3) / 0.4, 0.0, 1.0)
    alpha = t * t * (3.0 - 2.0 * t)
    return np.round(alpha * 255.0).astype(np.uint8)


class Upscaler:
    def __init__(self, model_path: Path, device: str = "cuda", half: bool = True):
        from spandrel import ImageModelDescriptor, ModelLoader

        # A .chain file (PLAN.md R1) lists model files, one per line, that run
        # one after another as one model: e.g. a 1x dedither, then a 4x upscaler.
        files = [Path(model_path)]
        if Path(model_path).suffix == ".chain":
            base = Path(model_path).parent
            files = []
            for line in Path(model_path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    files.append(Path(line) if Path(line).is_absolute() else base / line)
        loader = ModelLoader(device=device)
        self.descriptors = []
        for f in files:
            d = loader.load_from_file(str(f))
            if not isinstance(d, ImageModelDescriptor):
                raise TypeError(f"{f} is not an image-to-image model")
            self.descriptors.append(d)
        descriptor = self.descriptors[-1]
        self.name = Path(model_path).stem
        self.descriptor = descriptor
        self.scale = 1
        for d in self.descriptors:
            self.scale *= d.scale
        self.device = device
        self.half = half and all(d.supports_half for d in self.descriptors)
        for d in self.descriptors:
            d.model.eval()
            if self.half:
                d.model.half()
        self.seconds = 0.0
        self.input_pixels = 0
        self.governor = None  # fo2hd.governor.Governor: paces long batches
        self.tiled = 0  # calls to `_tiled` (smaller tiles nest)
        self.last_tiled: list[int] = []  # the last `rgb_batch` call's images that may have been tiled

    @torch.inference_mode()
    def _run(self, batch: np.ndarray, tile: int = TILE) -> np.ndarray:
        """(N, H, W, 3) uint8 -> (N, H*s, W*s, 3) uint8, one model call (two
        half batches if VRAM runs out, e.g. under a governor's VRAM cap; one
        image that doesn't fit goes in tiles of `tile` px, smaller tiles if
        those don't either)."""
        if self.governor:
            self.governor.wait()
        start = time.perf_counter()
        t = torch.from_numpy(np.ascontiguousarray(batch)).to(self.device).permute(0, 3, 1, 2).float().div_(255.0)
        if self.half:
            t = t.half()
        try:
            out = t
            for d in self.descriptors:
                out = d(out)
        except torch.OutOfMemoryError:
            del t
            torch.cuda.empty_cache()
            if batch.shape[0] == 1:
                if max(batch.shape[1], batch.shape[2]) <= tile:
                    tile //= 2  # already a tile that didn't fit
                if tile < 4 * TILE_OVERLAP:
                    raise
                return self._tiled(batch[0], tile)[None]
            half = batch.shape[0] // 2
            return np.concatenate([self._run(batch[:half], tile), self._run(batch[half:], tile)])
        out = out.float().clamp_(0.0, 1.0).mul_(255.0).round_().byte().permute(0, 2, 3, 1).cpu().numpy()
        if self.device == "cuda":
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        self.seconds += elapsed
        self.input_pixels += batch.shape[0] * batch.shape[1] * batch.shape[2]
        if self.governor:
            self.governor.after(elapsed)
        return out

    def _tiled(self, image: np.ndarray, tile: int) -> np.ndarray:
        """One image too big for the VRAM (a 640x480 UI background under the
        governor's cap, P7): overlapping tiles, each one's centre kept. Only
        images that don't fit take this path, so everything else stays
        identical to the one-call result. Counted in `tiled`: the batch logs
        these images, and `fo2hd retry-tiled` builds them again in one piece
        with more VRAM while the PC is locked."""
        self.tiled += 1
        h, w = image.shape[:2]
        s = self.scale
        step = tile - 2 * TILE_OVERLAP
        out = np.zeros((h * s, w * s, 3), np.uint8)
        for y0 in range(0, h, step):
            for x0 in range(0, w, step):
                y1, x1 = min(y0 + step, h), min(x0 + step, w)
                ty0, tx0 = max(0, y0 - TILE_OVERLAP), max(0, x0 - TILE_OVERLAP)
                ty1, tx1 = min(h, y1 + TILE_OVERLAP), min(w, x1 + TILE_OVERLAP)
                part = self._run(image[None, ty0:ty1, tx0:tx1], tile)[0]
                out[y0 * s : y1 * s, x0 * s : x1 * s] = part[(y0 - ty0) * s : (y1 - ty0) * s, (x0 - tx0) * s : (x1 - tx0) * s]
        return out

    def rgb(self, rgb: np.ndarray) -> np.ndarray:
        """(H, W, 3) uint8 -> (H*s, W*s, 3) uint8."""
        return self._run(rgb[None])[0]

    def rgb_batch(self, images: list[np.ndarray]) -> list[np.ndarray]:
        """Like `rgb` for many images, batched by size (see module doc)."""
        s = self.scale
        groups: dict[tuple[int, int], list[int]] = {}
        for i, im in enumerate(images):
            key = (-(-im.shape[0] // BUCKET) * BUCKET, -(-im.shape[1] // BUCKET) * BUCKET)
            groups.setdefault(key, []).append(i)
        out: list[np.ndarray | None] = [None] * len(images)
        self.last_tiled = []
        for (gh, gw), members in sorted(groups.items()):
            per_call = max(1, min(BATCH_MAX, BATCH_PIXELS // (gh * gw)))
            for start in range(0, len(members), per_call):
                chunk = members[start : start + per_call]
                batch = np.stack([np.pad(images[i], ((0, gh - images[i].shape[0]), (0, gw - images[i].shape[1]), (0, 0)), mode="edge") for i in chunk])
                tiled = self.tiled
                result = self._run(batch)
                if self.tiled != tiled:  # the whole call is listed: halving may have tiled any of them
                    self.last_tiled.extend(chunk)
                for k, i in enumerate(chunk):
                    h, w = images[i].shape[:2]
                    out[i] = result[k, : h * s, : w * s]
        return out

    @staticmethod
    def canvas_input(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Steps 1 and the padding: what the model sees for one sprite."""
        return np.pad(fill_transparent(rgb, mask), ((PAD, PAD), (PAD, PAD), (0, 0)), mode="edge")

    def crop(self, upscaled: np.ndarray) -> np.ndarray:
        s = self.scale
        return upscaled[PAD * s : -PAD * s, PAD * s : -PAD * s]

    def canvas(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """(H, W, 3) art with an opacity mask -> (H*s, W*s, 3): steps 1 and 2."""
        return self.crop(self.rgb(self.canvas_input(rgb, mask)))

    def sprite(self, indices: np.ndarray, palette_rgb: np.ndarray, canvas: np.ndarray | None = None) -> np.ndarray:
        """Palette-index sprite -> (H*s, W*s, 4) uint8 RGBA. `canvas` is the
        model's output for it if it was already computed (`rgb_batch`)."""
        mask = indices != 0
        if not mask.any():
            h, w = indices.shape
            return np.zeros((h * self.scale, w * self.scale, 4), np.uint8)
        if canvas is None:
            canvas = self.canvas(palette_rgb[indices], mask)
        return np.dstack([canvas, upscale_mask(mask, self.scale)])

    @property
    def megapixels_per_second(self) -> float:
        return (self.input_pixels / 1e6) / self.seconds if self.seconds else 0.0


def nearest_sprite(indices: np.ndarray, palette_rgb: np.ndarray, scale: int = 4) -> np.ndarray:
    """The reference: the original pixels, scaled without filtering."""
    rgba = np.dstack([palette_rgb[indices], np.where(indices != 0, 255, 0).astype(np.uint8)])
    return rgba.repeat(scale, axis=0).repeat(scale, axis=1)
