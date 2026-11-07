"""Python compositor at 4x (PLAN.md P1.9).

Replays a capture's draw list at 4x with HD assets (falling back to the
original sprite, nearest-upscaled, where the sample set has none), building a
G-buffer (albedo, normal, reference normal, height, ground line, emissive),
then lights it:

  original   the captured 1x frame, nearest 4x (the reference)
  hd_orig    HD art x the original's own per-pixel light (from the 1x replay)
  hd_lit     HD art x new lighting:
               ambient (tinted, darker at night)
             + each light: the original's light amount at that ground point,
               shared among the lights that reach it, then shaped by the
               surface normal, tinted with a color found near the light, and
               shadowed by a 2.5D heightfield march; sums above 1 are kept
               (HDR) and rolled off by a soft shoulder
             + emissive glow (palette-cycle colors) with a little bloom
  orig_equiv original art (nearest) x original light as a continuous multiply:
               must match `original` up to palette quantization (the
               "original-equivalent" invariant of PLAN.md section 3.4)

With every new effect at neutral (white lights, no normal shaping, no shadows)
hd_lit reduces to hd_orig by construction: ambient + (T - ambient) = T.

Screen/world conventions: see fo2hd.hdassets. 1x screen pixels are the unit;
the ground is foreshortened 2:1, so ground v = 2 * screen y.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

from . import frm as frm_mod
from . import hdassets
from .capture import Capture
from .config import Paths
from .dat import GameFiles, normalize
from .replay import Rasterizer, intersect
from .sample import category_of

S = 4
COS30 = 0.8660254

# How the surface normal shapes each light, per category (0 = original look):
#   detail  surface relief from the detail normals; averages out to the
#           original brightness
#   shape   large-scale orientation of the surface toward the light (faces
#           turned to a lamp get brighter, faces turned away darker)
NORMAL_DETAIL = {"floor": 0.8, "roof": 0.0, "wall": 0.6, "scenery": 0.6, "critter": 0.5, "item": 0.5, "misc": 0.5}
NORMAL_SHAPE = {"floor": 0.3, "roof": 0.0, "wall": 0.45, "scenery": 0.3, "critter": 0.2, "item": 0.2, "misc": 0.2}
# Light above ambient is boosted near strong sources: E * (1 + POOL_GAIN * E).
POOL_GAIN = 0.6
SHADOW_STRENGTH = 0.5
SHADOW_RANGE = 64.0  # 1x px along the ground, about two hexes
CATEGORY_CODES = {name: i for i, name in enumerate(["none", "floor", "roof", "wall", "scenery", "critter", "item", "misc"])}
HARD_EDGED = {"floor", "roof", "wall"}  # these tile against neighbors; soft alpha would show seams
TRANSLUCENT_TINT = {
    "trans_red": (1.0, 0.35, 0.3),
    "trans_wall": (0.75, 0.85, 1.0),
    "trans_glass": (0.6, 0.95, 1.0),
    "trans_steam": (1.0, 1.0, 1.0),
    "trans_energy": (0.9, 1.0, 0.4),
}


@dataclass
class Sprite:
    albedo: np.ndarray  # (h, w, 3) float32 0..1, sRGB-encoded like the palette
    alpha: np.ndarray  # (h, w) float32
    hard: np.ndarray  # (h, w) bool, the original pixel mask at 4x
    normal: np.ndarray  # (h, w, 3)
    normal_ref: np.ndarray  # (h, w, 3) heuristic normal without detail
    height: np.ndarray  # (h, w) 1x px above the ground line
    emissive: np.ndarray  # (h, w) 0..1
    fallback: bool


class AssetStore:
    def __init__(self, paths: Paths, files: GameFiles, palette: np.ndarray, use_hd: bool = True):
        self.paths = paths
        self.files = files
        self.palette = palette
        self.use_hd = use_hd
        self._frms: dict[str, frm_mod.Frm] = {}
        self._sprites: dict[tuple, Sprite] = {}
        self._metas: dict[str, dict | None] = {}
        self.fallbacks: set[str] = set()
        self.hd_used: set[str] = set()

    def frm(self, game_path: str) -> frm_mod.Frm:
        if game_path not in self._frms:
            self._frms[game_path] = frm_mod.decode(self.files.read(game_path))
        return self._frms[game_path]

    def _meta(self, game_path: str) -> dict | None:
        if game_path not in self._metas:
            meta_path = hdassets.asset_dir(self.paths, game_path) / "meta.json"
            self._metas[game_path] = json.loads(meta_path.read_text(encoding="utf-8")) if self.use_hd and meta_path.exists() else None
        return self._metas[game_path]

    def get(self, game_path: str, category: str, rotation: int, frame: int) -> Sprite:
        key = (game_path, category, rotation, frame)
        if key in self._sprites:
            return self._sprites[key]
        indices = self.frm(game_path).frame(frame, rotation).pixels
        hard = (indices != 0).repeat(S, 0).repeat(S, 1)
        meta = self._meta(game_path)
        if meta is not None:
            r = rotation
            while "same_as" in meta["rotations"][r]:
                r = meta["rotations"][r]["same_as"]
            base = hdassets.asset_dir(self.paths, game_path) / f"r{r}_f{frame:03d}"
            rgba = np.asarray(Image.open(f"{base}.albedo.png").convert("RGBA"), dtype=np.float32) / 255.0
            normal = hdassets.decode_normals(np.asarray(Image.open(f"{base}.normal.png").convert("RGB")))
            height = cv2.resize(hdassets.height_map(category, indices != 0), (hard.shape[1], hard.shape[0]), interpolation=cv2.INTER_LINEAR)
            emissive = np.asarray(Image.open(f"{base}.emissive.png"), dtype=np.float32) / 255.0
            albedo, alpha, fallback = rgba[..., :3], rgba[..., 3], False
            self.hd_used.add(game_path)
        else:
            mask1 = indices != 0
            albedo = (self.palette[indices].astype(np.float32) / 255.0).repeat(S, 0).repeat(S, 1)
            alpha = hard.astype(np.float32)
            height = cv2.resize(hdassets.height_map(category, mask1), (hard.shape[1], hard.shape[0]), interpolation=cv2.INTER_LINEAR)
            emissive = hdassets.emissive_mask(indices).astype(np.float32).repeat(S, 0).repeat(S, 1)
            normal = None
            fallback = True
            self.fallbacks.add(game_path)
        normal_ref = hdassets.base_normals(category, alpha > 0.5)
        if normal is None:
            normal = normal_ref
        sprite = Sprite(albedo, alpha, hard, normal, normal_ref, height, emissive, fallback)
        self._sprites[key] = sprite
        return sprite


@dataclass
class GBuffer:
    h: int
    w: int
    color: np.ndarray = field(init=False)
    normal: np.ndarray = field(init=False)
    normal_ref: np.ndarray = field(init=False)
    height: np.ndarray = field(init=False)
    ground: np.ndarray = field(init=False)  # screen y (4x px) of the ground point under each pixel
    emissive: np.ndarray = field(init=False)
    category: np.ndarray = field(init=False)
    unlit: np.ndarray = field(init=False)  # interface/text/outline/stencil: copied as-is

    def __post_init__(self):
        h, w = self.h, self.w
        self.color = np.zeros((h, w, 3), np.float32)
        self.normal = np.zeros((h, w, 3), np.float32)
        self.normal[..., 2] = 1.0
        self.normal_ref = self.normal.copy()
        self.height = np.zeros((h, w), np.float32)
        self.ground = np.tile(np.arange(h, dtype=np.float32)[:, None], (1, w))
        self.emissive = np.zeros((h, w), np.float32)
        self.category = np.zeros((h, w), np.int8)
        self.unlit = np.zeros((h, w), bool)


class Compositor:
    def __init__(self, capture: Capture, files: GameFiles, paths: Paths, use_hd: bool = True):
        self.capture = capture
        self.files = files
        self.paths = paths
        self.palette = capture.screen_rgb()
        # Fallback sprites use the palette the game actually showed (incl. the
        # cycle phase at capture time); HD assets use cycle phase 0.
        self.assets = AssetStore(paths, files, self.palette, use_hd=use_hd)
        self.H, self.W = capture.height * S, capture.width * S

        replay = Rasterizer(capture, files)
        self.indices1 = replay.run()
        self.light1 = replay.light.astype(np.float32) / 65536.0
        self.owner1 = replay.owner
        self.original_rgb1 = self.palette[self.indices1]

    # --- G-buffer ---------------------------------------------------------

    def _egg_alpha(self, egg_fid: int, egg_rect, region) -> np.ndarray:
        """Opacity (4x, over `region` = 4x rect) left by the egg cut-out."""
        out = np.ones((region[3] - region[1] + 1, region[2] - region[0] + 1), np.float32)
        if egg_fid not in self.capture.art:
            return out  # captures before the egg's art was always listed
        egg = self.assets.frm(normalize(self.capture.art[egg_fid])).frame(0, 0).pixels.astype(np.float32)
        a1 = np.where(egg == 0, 1.0, egg / 128.0).astype(np.float32)
        a4 = cv2.resize(a1, (a1.shape[1] * S, a1.shape[0] * S), interpolation=cv2.INTER_LINEAR)
        ex, ey = egg_rect[0] * S, egg_rect[1] * S
        cut = intersect(region, (ex, ey, ex + a4.shape[1] - 1, ey + a4.shape[0] - 1))
        if cut is not None:
            out[cut[1] - region[1] : cut[3] - region[1] + 1, cut[0] - region[0] : cut[2] - region[0] + 1] = a4[
                cut[1] - ey : cut[3] - ey + 1, cut[0] - ex : cut[2] - ex + 1
            ]
        return out

    def _place(self, g: GBuffer, sprite: Sprite, x1: int, y1: int, clip1, category: str, ground_offset: float = 0.0, egg=None, tint=None, opacity: float = 1.0):
        sh, sw = sprite.alpha.shape
        x4, y4 = x1 * S, y1 * S
        clip4 = (clip1[0] * S, clip1[1] * S, clip1[2] * S + S - 1, clip1[3] * S + S - 1)
        region = intersect((x4, y4, x4 + sw - 1, y4 + sh - 1), clip4)
        region = intersect(region, (0, 0, self.W - 1, self.H - 1)) if region else None
        if region is None:
            return
        l, t, r, b = region
        sy, sx = slice(t - y4, b - y4 + 1), slice(l - x4, r - x4 + 1)
        dy, dx = slice(t, b + 1), slice(l, r + 1)

        alpha = (sprite.hard[sy, sx].astype(np.float32) if category in HARD_EDGED else sprite.alpha[sy, sx]) * opacity
        if egg is not None:
            alpha = alpha * self._egg_alpha(egg[0], egg[1], region)
        albedo = sprite.albedo[sy, sx]
        if tint is not None:
            albedo = albedo * np.asarray(tint, np.float32)
        a = alpha[..., None]
        g.color[dy, dx] = albedo * a + g.color[dy, dx] * (1 - a)
        top = alpha > 0.5
        for target, source in ((g.normal, sprite.normal), (g.normal_ref, sprite.normal_ref)):
            target[dy, dx][top] = source[sy, sx][top]
        height = sprite.height[sy, sx] + ground_offset
        g.height[dy, dx][top] = height[top]
        rows = np.arange(t, b + 1, dtype=np.float32)[:, None] + np.zeros((1, r - l + 1), np.float32)
        g.ground[dy, dx][top] = (rows + height * S)[top]
        g.emissive[dy, dx] = sprite.emissive[sy, sx] * alpha + g.emissive[dy, dx] * (1 - alpha)
        g.category[dy, dx][top] = CATEGORY_CODES[category]
        g.unlit[dy, dx][top] = False

    def build(self) -> GBuffer:
        g = GBuffer(self.H, self.W)
        cap = self.capture
        for index, d in enumerate(cap.draws):
            op = d["op"]
            if op in ("pass", "fill", "text", "outline"):
                continue  # copied from the 1x frame afterwards
            category = category_of(d)
            if category is None:
                continue  # interface art (cursors)
            game_path = normalize(cap.art[d["fid"]])
            if op == "floor":
                sprite = self.assets.get(game_path, "floor", 0, 0)
                self._place(g, sprite, d["x"], d["y"], d["clip"], "floor")
            elif op == "roof":
                sprite = self.assets.get(game_path, "roof", 0, 0)
                self._place(g, sprite, d["x"], d["y"], d["clip"], "roof", ground_offset=96.0, egg=(cap.meta["egg"]["fid"], d["egg_rect"]))
            elif op == "object":
                sprite = self.assets.get(game_path, category, d["rotation"], d["frame"])
                egg = (d["egg_fid"], d["egg_rect"]) if "egg_rect" in d else None
                mode = d["mode"]
                tint = TRANSLUCENT_TINT.get(mode)
                self._place(g, sprite, d["x"], d["y"], d["clip"], category, egg=egg, tint=tint, opacity=0.5 if tint else 1.0)

        # Text, outlines, stencil fills and cursors: the original pixels, unlit.
        copy_ops = {i for i, d in enumerate(cap.draws) if d["op"] in ("text", "outline") or (d["op"] == "object" and d["mode"] == "interface")}
        stencil = False
        for i, d in enumerate(cap.draws):
            if d["op"] == "pass":
                stencil = d["pass"] == "stencil"
            elif d["op"] == "fill" and stencil:
                copy_ops.add(i)
        if copy_ops:
            mask1 = np.isin(self.owner1, list(copy_ops))
            mask4 = mask1.repeat(S, 0).repeat(S, 1)
            rgb4 = (self.original_rgb1.astype(np.float32) / 255.0).repeat(S, 0).repeat(S, 1)
            g.color[mask4] = rgb4[mask4]
            g.unlit |= mask4
        return g

    # --- lighting -----------------------------------------------------------

    def light_sources(self) -> list[dict]:
        cap = self.capture
        drawn = {d["id"]: d for d in cap.draws if d["op"] == "object"}
        sources = []
        for light in cap.lights:
            cx, cy = light["screen"][0] + 16, light["screen"][1] + 8
            path = normalize(cap.art.get(light["fid"], "")) if light["fid"] in cap.art else ""
            kind = "critter" if (light["pid"] >> 24) == 1 else ("fire" if any(k in path for k in ("fire", "barrel", "torch", "firpit")) else "lamp")
            height = {"critter": 30.0, "fire": 22.0, "lamp": 42.0}[kind]
            sources.append({**light, "x": float(cx), "y": float(cy), "z": height, "kind": kind, "drawn": light["id"] in drawn})
        return sources

    def light_color(self, source: dict) -> np.ndarray:
        """Average color of bright, saturated pixels around the light (1x frame)."""
        if source["kind"] == "critter":
            return np.ones(3, np.float32)
        rgb = self.original_rgb1.astype(np.float32) / 255.0
        h, w = rgb.shape[:2]
        x, y = int(source["x"]), int(source["y"] - source["z"])
        r = 40
        patch = rgb[max(0, y - r) : min(h, y + r), max(0, x - r) : min(w, x + r)].reshape(-1, 3)
        fallback = np.array([1.0, 0.86, 0.68] if source["kind"] == "fire" else [1.0, 0.94, 0.84], np.float32)
        if patch.size == 0:
            return fallback
        luma = patch @ np.array([0.299, 0.587, 0.114], np.float32)
        sat = patch.max(axis=1) - patch.min(axis=1)
        pick = (luma > 0.45) & (sat > 0.2)
        if pick.sum() < 6:
            return fallback
        color = (patch[pick] * luma[pick, None]).sum(0) / luma[pick].sum()
        color = color / max(color.max(), 1e-3)
        return (0.5 * color + 0.5 * fallback).astype(np.float32)

    def _hex_field(self, margin: int = 96) -> tuple[np.ndarray, int]:
        """Unclamped per-hex light painted on a 1x canvas (nearest hex), smoothed."""
        cap = self.capture
        h, w = cap.height + 2 * margin + 96, cap.width + 2 * margin
        centers = cap.hex_screen + np.array([16, 8]) + margin
        inside = (centers[:, 0] >= 0) & (centers[:, 0] < w) & (centers[:, 1] >= 0) & (centers[:, 1] < h)
        seeds = np.ones((h, w), np.uint8)
        idx = np.flatnonzero(inside)
        seeds[centers[idx, 1], centers[idx, 0]] = 0
        _, labels = cv2.distanceTransformWithLabels(seeds, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        lut = np.zeros(labels.max() + 1, np.float32)
        lut[labels[centers[idx, 1], centers[idx, 0]]] = cap.hex_light[idx] / 65536.0
        field = cv2.GaussianBlur(lut[labels], (0, 0), 7.0)
        return field, margin

    def light(self, g: GBuffer, neutral: bool = False) -> tuple[np.ndarray, dict]:
        cap = self.capture
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        ambient = cap.meta["ambient"] / 65536.0
        hour = cap.meta["time_of_day"]
        night = (hour < 600 or hour >= 1900) and ambient < 0.9
        ambient_color = np.array([0.82, 0.88, 1.0], np.float32) * 0.92 if night and not neutral else np.ones(3, np.float32)

        # Ground positions of every 4x pixel, in 1x canvas coordinates.
        field, margin = self._hex_field()
        xs4 = (np.arange(self.W, dtype=np.float32) + 0.5) / S
        gx = np.broadcast_to(xs4[None, :], (self.H, self.W))
        gy = (g.ground + 0.5) / S
        T = cv2.remap(field, (gx + margin).astype(np.float32), (gy + margin).astype(np.float32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        E = np.maximum(T - ambient, 0.0)  # light above ambient, may exceed 1 (HDR)
        if not neutral:
            E = E * (1.0 + POOL_GAIN * E)
        # Roofs only ever see ambient light in the original (tileRenderRoof).
        E[g.category == CATEGORY_CODES["roof"]] = 0.0

        sources = self.light_sources()
        heightfield = self._heightfield(g)
        self._place_lights_on_tall_objects(sources, heightfield)
        t_dev = lambda a: torch.from_numpy(np.ascontiguousarray(a)).to(dev)
        gx_t, gy_t, gz_t = t_dev(gx), t_dev(gy), t_dev(g.height)
        n_t, nref_t = t_dev(g.normal), t_dev(g.normal_ref)
        detail = np.zeros(len(CATEGORY_CODES), np.float32)
        shape_k = np.zeros(len(CATEGORY_CODES), np.float32)
        for name, code in CATEGORY_CODES.items():
            detail[code] = 0.0 if neutral else NORMAL_DETAIL.get(name, 0.0)
            shape_k[code] = 0.0 if neutral else NORMAL_SHAPE.get(name, 0.0)
        cat_t = t_dev(g.category).long()
        detail_t, shape_t = t_dev(detail)[cat_t], t_dev(shape_k)[cat_t]

        # Unblocked analytic falloff per light (light.cc), used only to share
        # the measured field among the lights that reach a point.
        weights, directional = [], []
        for s in sources:
            I = min(s["intensity"], 65536) / 65536.0
            D = min(s["distance"], 8)
            step = (I - 655 / 65536.0) / (D + 1)
            dx = (gx_t - s["x"]) / 32.0
            dy = (gy_t - s["y"]) / 13.856
            k = torch.sqrt(dx * dx + dy * dy)
            f = torch.clamp(I - torch.clamp(k - 0.5, min=0.0) * step, min=0.0)
            weights.append(f)

            Lu = s["x"] - gx_t
            Lv = 2.0 * (s["y"] - gy_t)
            Lz = (s["z"] - gz_t) / COS30
            norm = torch.sqrt(Lu * Lu + Lv * Lv + Lz * Lz).clamp(min=1e-3)
            L = torch.stack([Lu / norm, Lv / norm, Lz / norm], dim=-1)
            # Half-Lambert ("wrap") keeps the dark side from going to zero.
            wrap = ((n_t * L).sum(-1) * 0.5 + 0.5).clamp(0.0, 1.0)
            wrap_ref = ((nref_t * L).sum(-1) * 0.5 + 0.5).clamp(0.0, 1.0)
            relief = ((wrap + 0.15) / (wrap_ref + 0.15)).clamp(0.6, 1.5)
            shape = (1.0 + detail_t * (relief - 1.0)) * (1.0 + shape_t * (2.0 * wrap_ref - 1.0))
            shadow = torch.ones_like(shape) if neutral else self._shadow(heightfield, s, gx_t, gy_t, gz_t, dev)
            color = torch.ones(3, device=dev) if neutral else t_dev(self.light_color(s))
            directional.append((shape * shadow)[..., None] * color)

        total = torch.zeros((self.H, self.W, 3), device=dev)
        if sources:
            W = torch.stack(weights)
            share = W / W.sum(0).clamp(min=1e-6)
            E_t = t_dev(E)
            for i in range(len(sources)):
                total += (E_t * share[i])[..., None] * directional[i]
        light = t_dev(ambient * ambient_color)[None, None, :] + total
        stats = {"ambient": ambient, "night": night, "lights": len(sources), "max_light": float(light.max())}
        return light.cpu().numpy(), stats

    def _heightfield(self, g: GBuffer) -> tuple[torch.Tensor, int]:
        """Max object height standing on each 1x ground point (2.5D occluders)."""
        margin = 8
        h, w = self.capture.height + 2 * margin + 96, self.capture.width + 2 * margin
        hf = np.zeros(h * w, np.float32)
        tall = g.height > 3.0
        ys, xs = np.nonzero(tall)
        gyi = np.clip(np.round(g.ground[ys, xs] / S).astype(np.int64) + margin, 0, h - 1)
        gxi = np.clip(xs // S + margin, 0, w - 1)
        np.maximum.at(hf, gyi * w + gxi, g.height[ys, xs])
        hf = cv2.dilate(hf.reshape(h, w), np.ones((2, 2), np.uint8))
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        return torch.from_numpy(hf).to(dev), margin

    def _place_lights_on_tall_objects(self, sources: list[dict], heightfield, radius: int = 16) -> None:
        """A light standing at a tall object (a street lamp's post) shines from
        near its top, not from the default height."""
        hf, margin = heightfield
        hf = hf.cpu().numpy()
        for s in sources:
            if s["kind"] != "lamp":
                continue
            x, y = int(s["x"]) + margin, int(s["y"]) + margin
            patch = hf[max(0, y - radius) : y + radius, max(0, x - radius) : x + radius]
            if patch.size:
                s["z"] = float(np.clip(0.85 * patch.max(), s["z"], 160.0))

    def _shadow(self, heightfield, s: dict, gx: torch.Tensor, gy: torch.Tensor, gz: torch.Tensor, dev: str, steps: int = 20) -> torch.Tensor:
        """Fine-scale shadows: march at most SHADOW_RANGE toward the light.

        Long-range blocking (light behind walls) is already in the original's
        per-hex light field, so marching all the way to the light would block
        those lights twice."""
        hf, margin = heightfield
        hh, ww = hf.shape
        occluded = torch.zeros_like(gx)
        dist = torch.sqrt((s["x"] - gx) ** 2 + (2 * (s["y"] - gy)) ** 2)
        # Stop short of the light itself: whatever it is mounted on doesn't block it.
        t_max = torch.minimum((SHADOW_RANGE / dist.clamp(min=1e-3)).clamp(max=1.0), (1.0 - 24.0 / dist.clamp(min=1e-3)).clamp(min=0.0))
        for i in range(1, steps):
            t = t_max * (i / steps)
            px = gx + (s["x"] - gx) * t
            py = gy + (s["y"] - gy) * t
            pz = gz + (s["z"] - gz) * t
            xi = (px + margin).round().long().clamp(0, ww - 1)
            yi = (py + margin).round().long().clamp(0, hh - 1)
            blocker = hf[yi, xi]
            near_self = (dist * t) < 4.0
            occluded = torch.maximum(occluded, ((blocker > pz + 2.0) & ~near_self).float())
        # Soften the edge a little; shadows never go fully black (bounce).
        occluded = torch.nn.functional.avg_pool2d(occluded[None, None], 9, stride=1, padding=4)[0, 0]
        return 1.0 - SHADOW_STRENGTH * occluded

    # --- outputs ----------------------------------------------------------

    def render(self) -> dict[str, np.ndarray]:
        g = self.build()
        light4_orig = self.light1.repeat(S, 0).repeat(S, 1)
        out = {"original": self.original_rgb1.repeat(S, 0).repeat(S, 1)}

        hd_orig = g.color * light4_orig[..., None]
        out["hd_orig"] = to8(np.where(g.unlit[..., None], g.color, hd_orig))

        light, self.stats = self.light(g)
        lit = g.color * light
        emissive = g.emissive[..., None]
        lit = lit * (1 - emissive) + g.color * emissive * 1.15
        glow = cv2.GaussianBlur(g.color * emissive, (0, 0), 10.0)
        lit = lit + 0.45 * glow
        lit = shoulder(lit)
        out["hd_lit"] = to8(np.where(g.unlit[..., None], g.color, lit))
        out["light"] = to8(shoulder(light) * 0.85)
        out["normals"] = to8(g.normal * 0.5 + 0.5)
        self.gbuffer = g
        return out


def shoulder(x: np.ndarray, knee: float = 0.8) -> np.ndarray:
    """Linear up to `knee`, then rolls off smoothly toward 1."""
    over = np.maximum(x - knee, 0.0)
    return np.where(x <= knee, x, knee + (1 - knee) * (1 - np.exp(-over / (1 - knee))))


def to8(x: np.ndarray) -> np.ndarray:
    return np.round(np.clip(x, 0, 1) * 255).astype(np.uint8)


def original_equivalent(capture: Capture, files: GameFiles, paths: Paths) -> dict:
    """Original art (no HD assets) x original light, continuous: vs the real frame."""
    comp = Compositor(capture, files, paths, use_hd=False)
    g = comp.build()
    light4 = comp.light1.repeat(S, 0).repeat(S, 1)
    equiv = to8(np.where(g.unlit[..., None], g.color, g.color * light4[..., None]))
    original = comp.original_rgb1.repeat(S, 0).repeat(S, 1)
    diff = np.abs(equiv.astype(np.int16) - original.astype(np.int16))
    mse = float((diff.astype(np.float32) ** 2).mean())
    lit = comp.light1 < 0.999
    return {
        "mean_abs": round(float(diff.mean()), 3),
        "p99_abs": int(np.percentile(diff, 99)),
        "max_abs": int(diff.max()),
        "psnr": round(10 * np.log10(255**2 / mse), 2) if mse else 99.0,
        "lit_fraction": round(float(lit.mean()), 3),
        "image": equiv,
    }
