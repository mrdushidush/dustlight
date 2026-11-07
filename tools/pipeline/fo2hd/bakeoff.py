"""Upscaler bake-off (PLAN.md P1.6): every candidate model on representative
sample assets per category -> a local HTML contact sheet plus metrics.

Metrics, per model and category (opaque pixels only):
  fidelity   PSNR (dB) between the output box-filtered back to 1x and the
             original. High = keeps the original's colors and shapes.
  sharpness  mean gradient magnitude of the output relative to a plain bicubic
             4x upscale. >1 = more detail than bicubic.
  speed      input megapixels per second on this GPU.

The report contains game-derived images: it stays under <data_root>/reports
and is never committed or published.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import frm as frm_mod
from .dat import GameFiles
from .upscale import Upscaler, fill_transparent, nearest_sprite

# How many assets per category go on the sheet.
PER_CATEGORY = {"floor": 8, "roof": 4, "wall": 6, "scenery": 6, "critter": 6, "item": 3}
WALK_ROTATION = 2  # south-east, facing the camera


@dataclass
class Item:
    category: str
    label: str
    path: str
    rotation: int
    frame: int
    indices: np.ndarray = field(repr=False)


def select_items(manifest: dict, files: GameFiles) -> list[Item]:
    items: list[Item] = []
    for category, count in PER_CATEGORY.items():
        assets = sorted((a for a in manifest["assets"] if a["category"] == category and not a["walk_cycle"]), key=lambda a: a["path"])
        if category in ("scenery", "wall"):
            # The biggest pieces show the most about a model.
            assets = sorted(assets, key=lambda a: -a["pixels"])[: count * 3]
            assets = sorted(assets, key=lambda a: a["path"])
        if category == "critter":
            walk = next(a for a in manifest["assets"] if a["walk_cycle"])
            frm = frm_mod.decode(files.read(walk["path"]))
            for f in (0, 2, 4):
                items.append(Item(category, f"{Path(walk['path']).name} walk dir {WALK_ROTATION} frame {f}", walk["path"], WALK_ROTATION, f, frm.frame(f, WALK_ROTATION).pixels))
            count -= 3
        step = max(1, len(assets) // count) if assets else 1
        for asset in assets[::step][:count]:
            frm = frm_mod.decode(files.read(asset["path"]))
            rotation, frame = asset["frames_used"][0] if asset["frames_used"] else (0, 0)
            items.append(Item(category, f"{Path(asset['path']).name} dir {rotation} frame {frame}", asset["path"], rotation, frame, frm.frame(frame, rotation).pixels))
    return items


def _luma(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def _gradient(gray: np.ndarray, mask: np.ndarray) -> float:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    return float(mag[mask].mean()) if mask.any() else 0.0


def metrics(item: Item, palette: np.ndarray, up: np.ndarray) -> dict:
    mask = item.indices != 0
    original = palette[item.indices]
    h, w = item.indices.shape
    down = cv2.resize(up[..., :3], (w, h), interpolation=cv2.INTER_AREA)
    err = (down.astype(np.float32) - original.astype(np.float32))[mask]
    mse = float((err**2).mean()) if err.size else 0.0
    psnr = 99.0 if mse == 0 else 10 * np.log10(255.0**2 / mse)

    big_mask = up[..., 3] > 128
    bicubic = cv2.resize(fill_transparent(original, mask), (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
    sharp_ref = _gradient(_luma(bicubic), big_mask)
    sharpness = _gradient(_luma(up[..., :3]), big_mask) / sharp_ref if sharp_ref else 1.0
    return {"fidelity": round(float(psnr), 2), "sharpness": round(sharpness, 3)}


def run(manifest: dict, files: GameFiles, model_paths: list[Path], out_dir: Path, report=print) -> dict:
    palette = frm_mod.load_palette(files.read("color.pal"))
    items = select_items(manifest, files)
    img_dir = out_dir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)

    def save(rgba: np.ndarray, name: str) -> str:
        Image.fromarray(rgba, "RGBA").save(img_dir / name)
        return f"img/{name}"

    columns = ["nearest"] + [p.stem for p in model_paths]
    cells: dict[tuple[int, str], str] = {}
    results: dict[str, dict] = {"items": [], "models": {}}
    for i, item in enumerate(items):
        cells[(i, "nearest")] = save(nearest_sprite(item.indices, palette), f"{i:03d}_nearest.png")

    for model_path in model_paths:
        report(f"  {model_path.stem}")
        upscaler = Upscaler(model_path)
        per_category: dict[str, list[dict]] = {}
        for i, item in enumerate(items):
            up = upscaler.sprite(item.indices, palette)
            cells[(i, model_path.stem)] = save(up, f"{i:03d}_{model_path.stem}.png")
            m = metrics(item, palette, up)
            per_category.setdefault(item.category, []).append(m)
        summary = {
            category: {
                "fidelity": round(float(np.mean([m["fidelity"] for m in ms])), 2),
                "sharpness": round(float(np.mean([m["sharpness"] for m in ms])), 3),
            }
            for category, ms in per_category.items()
        }
        results["models"][model_path.stem] = {
            "architecture": type(upscaler.descriptor.model).__name__,
            "half": upscaler.half,
            "mpx_per_s": round(upscaler.megapixels_per_second, 3),
            "categories": summary,
        }
        del upscaler
        import torch

        torch.cuda.empty_cache()

    results["items"] = [{"category": it.category, "label": it.label, "path": it.path, "w": int(it.indices.shape[1]), "h": int(it.indices.shape[0])} for it in items]
    (out_dir / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (out_dir / "index.html").write_text(render_html(items, columns, cells, results), encoding="utf-8")
    return results


def render_html(items: list[Item], columns: list[str], cells: dict, results: dict) -> str:
    out = [
        "<!doctype html><html><head><meta charset='utf-8'><title>Upscaler bake-off</title><style>",
        "body{background:#1d1d1b;color:#ddd;font:14px/1.4 system-ui,sans-serif;margin:16px}",
        "h1,h2{font-weight:600}table{border-collapse:collapse;margin:8px 0 24px}",
        "td,th{border:1px solid #444;padding:4px 8px;text-align:right}th{background:#2a2a28}",
        "td:first-child,th:first-child{text-align:left}.best{color:#9f9;font-weight:600}",
        ".row{display:flex;gap:12px;overflow-x:auto;padding:8px 0;border-bottom:1px solid #333}",
        ".cell{flex:none;text-align:center}.cell div{font-size:12px;color:#aaa;margin-bottom:4px}",
        ".cell img{background:#55524b;max-height:320px;image-rendering:auto;cursor:zoom-in}",
        ".cell img.zoom{max-height:none;cursor:zoom-out}.cell.nearest img{image-rendering:pixelated}",
        ".bg-check .cell img{background:repeating-conic-gradient(#666 0 25%,#444 0 50%) 0 0/16px 16px}",
        "</style></head><body>",
        "<h1>Upscaler bake-off (PLAN.md P1.6)</h1>",
        "<p>Each row: one sample sprite at 4x. <b>nearest</b> is the original pixels. Click an image to see it at full size. ",
        "<label><input type='checkbox' onchange=\"document.body.classList.toggle('bg-check',this.checked)\"> checkerboard background</label></p>",
        "<h2>Metrics</h2><p>fidelity = PSNR of the output scaled back to 1x vs. the original (higher keeps the original's look); ",
        "sharpness = detail relative to bicubic (higher = crisper); speed = input Mpx/s on the RTX 5060 Ti.</p>",
    ]
    categories = sorted({it.category for it in items}, key=list(PER_CATEGORY).index)
    models = results["models"]
    out.append("<table><tr><th>model</th><th>speed</th>" + "".join(f"<th>{c} fid / sharp</th>" for c in categories) + "</tr>")
    for name, m in models.items():
        row = f"<tr><td>{html.escape(name)}</td><td>{m['mpx_per_s']:.2f}</td>"
        for c in categories:
            s = m["categories"].get(c)
            row += f"<td>{s['fidelity']:.1f} / {s['sharpness']:.2f}</td>" if s else "<td></td>"
        out.append(row + "</tr>")
    out.append("</table>")

    for category in categories:
        out.append(f"<h2>{category}</h2>")
        for i, item in enumerate(items):
            if item.category != category:
                continue
            out.append(f"<div><b>{html.escape(item.label)}</b> <span style='color:#888'>{item.indices.shape[1]}x{item.indices.shape[0]}</span></div><div class='row'>")
            for column in columns:
                cls = "cell nearest" if column == "nearest" else "cell"
                out.append(f"<div class='{cls}'><div>{html.escape(column)}</div><img loading='lazy' src='{cells[(i, column)]}' onclick=\"this.classList.toggle('zoom')\"></div>")
            out.append("</div>")
    out.append("</body></html>")
    return "\n".join(out)
