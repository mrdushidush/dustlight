"""Release upscaler bake-off (PLAN.md R1).

The public release can't use 4x-UltraSharpV2 (CC-BY-NC-SA-4.0), so the art
is made again with a permissively licensed model. Unlike the P1.6 bake-off
(raw model output on single sprites), every candidate here goes through the
production path (`hdassets.build`: tile context for floors and roofs, alpha,
all maps), so what the page shows is what the packs would hold:

  assets   the sample set (the four benchmark scenes' art) plus talking
           heads, interface panels and more critters, built per model into
           its own staging root <data_root>/r1/<model>/ (resumable)
  scenes   the benchmark captures composited per model (`compose`, HD art
           with the original light), reduced to 3x, the scale the author plays at
  flicker  the tribal walk cycle's shimmer per model (`anim`, 1.0 = as
           stable as the original pixel art)
  metrics  fidelity (PSNR of the 4x albedo box-filtered back to 1x against
           the original) and sharpness (gradient vs bicubic), per category

The page is a flip viewer: one model at a time on every panel, switched with
the number keys, so differences show as change in place. It holds game-
derived images: it stays under <data_root>/reports and is never published.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import anim, capture as capture_mod, frm as frm_mod, hdassets
from .bakeoff import _gradient, _luma
from .compose import Compositor
from .config import Paths
from .dat import GameFiles
from .jobs import JobDB
from .upscale import fill_transparent

SCENES = ["arroyo_day", "temple_outdoor", "temple_indoor", "newreno_night"]
VIEW_SCALE = 3  # the page shows scenes at the scale the author plays at (3440x1440, 3x)

# Extra art beyond the sample set, by game path; missing ones are skipped.
EXTRA_UI = [
    "art\\heads\\elderbn.frm",  # Arroyo's Elder
    "art\\heads\\haku2bn.frm",  # Hakunin
    "art\\heads\\mrcusbn.frm",  # Marcus
    "art\\intrface\\iface.frm",  # interface bar
    "art\\intrface\\pip.frm",  # Pip-Boy
    "art\\intrface\\edtredt.frm",  # character screen
    "art\\intrface\\alltlk.frm",  # dialogue window
    "art\\intrface\\hr_mainmenu.frm",  # main menu
]
EXTRA_CRITTERS = ["mamtntaa", "maroboaa", "nmgangaa", "hfjmpsaa", "mabrom", "masrat", "mafeye", "naghul"]
# Sheet panels per category from the sample set (biggest first for walls/scenery).
SHEET_PER_CATEGORY = {"floor": 4, "roof": 2, "wall": 3, "scenery": 3, "item": 2, "misc": 1}

# OpenModelDB licenses that allow a free public release of the art.
PERMISSIVE = {"CC0-1.0", "CC-BY-4.0", "MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "WTFPL", "Unlicense"}


def _assets_by_path(manifest_path: Path) -> dict[str, dict]:
    if not manifest_path.exists():
        return {}
    return {a["path"]: a for a in json.loads(manifest_path.read_text(encoding="utf-8"))["assets"]}


def build_manifest(data_root: Path) -> dict:
    """The sample set plus EXTRA_UI and EXTRA_CRITTERS (one standing animation each)."""
    sample = json.loads((data_root / "sample" / "manifest.json").read_text(encoding="utf-8"))
    assets = list(sample["assets"])
    have = {a["path"] for a in assets}
    ui = _assets_by_path(data_root / "bulk" / "manifest_ui.json")
    critters = _assets_by_path(data_root / "bulk" / "manifest_critters.json")
    for path in EXTRA_UI:
        if path in ui and path not in have:
            assets.append(ui[path])
            have.add(path)
    for prefix in EXTRA_CRITTERS:
        hit = sorted(p for p in critters if Path(p.lower()).name.startswith(prefix) and p.lower().endswith("aa.frm"))
        if hit and hit[0] not in have:
            assets.append(critters[hit[0]])
            have.add(hit[0])
    return {"assets": assets, "walk_cycle": sample.get("walk_cycle")}


def stage_paths(paths: Paths, model_path: Path) -> Paths:
    """A data root per model, sharing the main root's captures (a junction)."""
    root = paths.data_root / "r1" / model_path.stem
    root.mkdir(parents=True, exist_ok=True)
    link = root / "captures"
    if not link.exists():
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(paths.captures)], check=True, capture_output=True)
    return dataclasses.replace(paths, data_root=root)


def _albedo(paths: Paths, game_path: str, rotation: int, frame: int) -> np.ndarray | None:
    hd_dir = hdassets.asset_dir(paths, game_path)
    meta_path = hd_dir / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rot = meta["rotations"][rotation]
    if "same_as" in rot:
        rotation = rot["same_as"]
    png = hd_dir / f"r{rotation}_f{frame:03d}.albedo.png"
    return np.asarray(Image.open(png).convert("RGBA")) if png.exists() else None


def _metrics(indices: np.ndarray, palette: np.ndarray, up: np.ndarray) -> dict:
    mask = indices != 0
    original = palette[indices]
    h, w = indices.shape
    down = cv2.resize(up[..., :3], (w, h), interpolation=cv2.INTER_AREA)
    err = (down.astype(np.float32) - original.astype(np.float32))[mask]
    mse = float((err**2).mean()) if err.size else 0.0
    psnr = 99.0 if mse == 0 else 10 * np.log10(255.0**2 / mse)
    big_mask = up[..., 3] > 128
    bicubic = cv2.resize(fill_transparent(original, mask), (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
    ref = _gradient(_luma(bicubic), big_mask)
    return {"fidelity": float(psnr), "sharpness": _gradient(_luma(up[..., :3]), big_mask) / ref if ref else 1.0}


def _on_background(rgba: np.ndarray, bg=(58, 56, 52)) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    return (rgba[..., :3].astype(np.float32) * a + np.array(bg, np.float32) * (1 - a)).round().astype(np.uint8)


def sheet_items(manifest: dict) -> list[dict]:
    """Which (asset, rotation, frame) the sheet shows: a few per sample category,
    every extra asset, the walk cycle facing the camera."""
    items = []
    sample_assets = [a for a in manifest["assets"] if a.get("used_by")]
    for category, count in SHEET_PER_CATEGORY.items():
        assets = [a for a in sample_assets if a["category"] == category]
        assets = sorted(assets, key=lambda a: -a["pixels"])[: count * 4]
        assets = sorted(assets, key=lambda a: a["path"])
        step = max(1, len(assets) // count) if assets else 1
        for a in assets[::step][:count]:
            rotation, frame = a["frames_used"][0] if a.get("frames_used") else (0, 0)
            items.append({"category": category, "path": a["path"], "rotation": rotation, "frame": frame})
    items.append({"category": "critter", "path": anim.WALK, "rotation": 2, "frame": 2})
    for a in manifest["assets"]:
        if a.get("used_by"):
            continue
        frame = 4 if "heads" in a["path"] and a["frames"] > 4 else 0
        rotation = 2 if a["category"] == "critter" else 0
        items.append({"category": a["category"], "path": a["path"], "rotation": rotation, "frame": frame})
    return items


CATEGORIES = ("floor", "roof", "wall", "scenery", "critter", "item", "misc", "interface")


def read_mix(path: Path) -> dict[str, Path]:
    """A .mix file: JSON {category: model file (relative to the .mix)}, plus
    "default" for categories it doesn't name: one candidate made of several
    models, as the release batch would run it (--category-model)."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    resolve = lambda f: Path(f) if Path(f).is_absolute() else (path.parent / f).resolve()
    default = resolve(spec["default"]) if "default" in spec else None
    return {c: resolve(spec[c]) if c in spec else default for c in CATEGORIES}


def run(paths: Paths, files: GameFiles, model_paths: list[Path], licenses: dict[str, str], governor=None, report=print,
        out_name: str = "release-bakeoff") -> dict:
    out = paths.reports / out_name
    img = out / "img"
    img.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(paths.data_root)
    palette = frm_mod.load_palette(files.read("color.pal"))
    items = sheet_items(manifest)
    report(f"release bake-off: {len(model_paths)} models, {len(manifest['assets'])} assets, {len(items)} sheet panels, {len(SCENES)} scenes")

    results: dict = {"models": {}, "items": [], "scenes": SCENES}
    # The reference column: nearest 4x of the original, shown at 3x like the rest.
    for i, it in enumerate(items):
        frm = frm_mod.decode(files.read(it["path"]))
        idx = frm.frame(it["frame"], it["rotation"]).pixels
        it["w"], it["h"] = int(idx.shape[1]), int(idx.shape[0])
        rgba = np.dstack([palette[idx], np.where(idx != 0, 255, 0)]).astype(np.uint8)
        near = rgba.repeat(VIEW_SCALE, 0).repeat(VIEW_SCALE, 1)
        Image.fromarray(_on_background(near)).save(img / f"a{i:03d}_original.png")
    for s in SCENES:
        orig = np.asarray(Image.open(paths.captures / f"{s}.png").convert("RGB"))
        Image.fromarray(orig.repeat(VIEW_SCALE, 0).repeat(VIEW_SCALE, 1)).save(img / f"s_{s}_original.png")
    results["items"] = items

    for model_path in model_paths:
        name = model_path.stem
        report(f"  {name}")
        sp = stage_paths(paths, model_path)
        jobs = JobDB(sp.data_root / "jobs.sqlite")
        model_for = read_mix(model_path) if model_path.suffix == ".mix" else {c: model_path for c in CATEGORIES}
        t0 = time.perf_counter()
        stats = hdassets.build(manifest, files, sp, jobs, model_for, report=report, governor=governor)
        jobs.close()
        build_s = time.perf_counter() - t0
        entry: dict = {
            "license": licenses.get(name, "?"),
            "permissive": all(part in PERMISSIVE for part in licenses.get(name, "?").split(" + ")),
            "model_seconds": round(stats["seconds"], 1),
            "model_mpx": round(stats["model_input_px"] / 1e6, 2),
            "built_now": stats["assets"],
            "build_wall_s": round(build_s, 1),
        }
        if stats["seconds"] > 0:
            entry["mpx_per_s"] = round(stats["model_input_px"] / 1e6 / stats["seconds"], 3)
        prev = sp.data_root / "r1_entry.json"
        if stats["assets"] == 0 and prev.exists():  # all resumed: keep the measured speed
            old = json.loads(prev.read_text(encoding="utf-8"))
            for k in ("model_seconds", "model_mpx", "mpx_per_s"):
                if k in old:
                    entry[k] = old[k]

        per_cat: dict[str, list[dict]] = {}
        for i, it in enumerate(items):
            up = _albedo(sp, it["path"], it["rotation"], it["frame"])
            if up is None:
                continue
            frm = frm_mod.decode(files.read(it["path"]))
            idx = frm.frame(it["frame"], it["rotation"]).pixels
            per_cat.setdefault(it["category"], []).append(_metrics(idx, palette, up))
            view = cv2.resize(up, (it["w"] * VIEW_SCALE, it["h"] * VIEW_SCALE), interpolation=cv2.INTER_AREA)
            Image.fromarray(_on_background(view)).save(img / f"a{i:03d}_{name}.png")
        entry["categories"] = {c: {"fidelity": round(float(np.mean([m["fidelity"] for m in ms])), 2),
                                   "sharpness": round(float(np.mean([m["sharpness"] for m in ms])), 3)} for c, ms in per_cat.items()}

        for s in SCENES:
            cap = capture_mod.load(paths.captures / f"{s}.json")
            comp = Compositor(cap, files, sp)
            images = comp.render()
            hd = images["hd_orig"]
            h, w = hd.shape[:2]
            view = cv2.resize(hd, (w * VIEW_SCALE // 4, h * VIEW_SCALE // 4), interpolation=cv2.INTER_AREA)
            Image.fromarray(view).save(img / f"s_{s}_{name}.png")

        try:
            a = anim.run(files, sp, sp.data_root / "anim", report=lambda *_: None)
            entry["flicker"] = a["mean"]["hd"]
        except SystemExit as e:  # no walk cycle built
            entry["flicker"] = None
            report(f"    flicker: {e}")
        (sp.data_root / "r1_entry.json").write_text(json.dumps(entry, indent=1), encoding="utf-8")
        results["models"][name] = entry
        report(f"    {entry.get('mpx_per_s', '?')} Mpx/s, flicker {entry['flicker']}, " +
               " ".join(f"{c}={v['fidelity']:.1f}/{v['sharpness']:.2f}" for c, v in entry["categories"].items()))
        import torch

        torch.cuda.empty_cache()

    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (out / "index.html").write_text(render_html(results), encoding="utf-8")
    return results


def render_html(results: dict) -> str:
    models = list(results["models"])
    columns = ["original"] + models
    keys = "1234567890qwertyuiop"
    esc = html.escape

    rows = []
    cats = sorted({c for m in results["models"].values() for c in m["categories"]})
    for i, name in enumerate(models):
        m = results["models"][name]
        lic = esc(m["license"]) + ("" if m["permissive"] else " <b class=bad>(not for release)</b>")
        cells = "".join(
            f"<td>{m['categories'][c]['fidelity']:.1f} / {m['categories'][c]['sharpness']:.2f}</td>" if c in m["categories"] else "<td>-</td>" for c in cats
        )
        fl = f"{m['flicker']:.2f}" if m.get("flicker") is not None else "-"
        speed = f"{m['mpx_per_s']:.2f}" if m.get("mpx_per_s") else "-"
        rows.append(f"<tr><td><kbd>{keys[i + 1]}</kbd></td><td>{esc(name)}</td><td>{lic}</td><td>{speed}</td><td>{fl}</td>{cells}</tr>")
    table = (
        "<table><tr><th>key</th><th>model</th><th>license</th><th>Mpx/s</th><th>flicker</th>"
        + "".join(f"<th>{esc(c)}<br><small>fidelity dB / sharpness</small></th>" for c in cats)
        + "</tr>" + "".join(rows) + "</table>"
    )

    def panel(prefix: str, title: str) -> str:
        return f'<section><h3>{esc(title)}</h3><div class=view><img data-prefix="{prefix}" src="img/{prefix}_original.png"></div></section>'

    scenes = "".join(panel(f"s_{s}", s.replace("_", " ")) for s in results["scenes"])
    assets = "".join(panel(f"a{i:03d}", f"{it['category']}: {Path(it['path']).name} (dir {it['rotation']}, frame {it['frame']})") for i, it in enumerate(results["items"]))
    buttons = "".join(f'<button data-col="{esc(c)}">{keys[k]} {esc(c)}</button>' for k, c in enumerate(columns))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Release upscaler bake-off</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:#1d1c1a; color:#e6e1d6; font:14px/1.4 system-ui, sans-serif; margin:0; }}
header {{ position:sticky; top:0; background:#26241f; padding:8px 16px; z-index:2; border-bottom:1px solid #444; }}
button {{ background:#3a372f; color:#e6e1d6; border:1px solid #555; padding:4px 8px; margin:2px; cursor:pointer; }}
button.on {{ background:#7a6a2a; border-color:#c9a94a; }}
main {{ padding:0 16px 40px; }}
table {{ border-collapse:collapse; margin:12px 0; }} td, th {{ border:1px solid #444; padding:3px 8px; text-align:left; }}
.bad {{ color:#e07a5f; }}
.view {{ overflow:auto; max-height:85vh; border:1px solid #444; }}
.view img {{ display:block; image-rendering:auto; }}
#now {{ font-weight:bold; color:#f2cc60; margin-left:8px; }}
</style></head><body>
<header><div>{buttons}<span id=now>original</span></div>
<small>Number keys switch every panel to one model; the scroll position stays, so changes show in place. Images at 3x (the author's scale): scenes are HD art with the original light, assets on a neutral background.</small></header>
<main>
<h2>Release upscaler bake-off (R1)</h2>
<p>Every candidate went through the production path (tile context, alpha, all maps), so this is what the packs would hold. Flicker: walk-cycle shimmer, 1.0 = as stable as the original (lower is calmer). Fidelity: PSNR of the 4x art reduced back to 1x against the original (higher keeps the original's colors and shapes). Sharpness: detail relative to a plain bicubic upscale.</p>
{table}
<h2>Scenes</h2>{scenes}
<h2>Assets</h2>{assets}
</main>
<script>
const cols = {json.dumps(columns)};
const keys = {json.dumps(keys)};
function show(c) {{
  document.querySelectorAll('img[data-prefix]').forEach(i => i.src = 'img/' + i.dataset.prefix + '_' + c + '.png');
  document.querySelectorAll('button[data-col]').forEach(b => b.classList.toggle('on', b.dataset.col === c));
  document.getElementById('now').textContent = c;
}}
document.querySelectorAll('button[data-col]').forEach(b => b.onclick = () => show(b.dataset.col));
document.addEventListener('keydown', e => {{ const k = keys.indexOf(e.key); if (k >= 0 && k < cols.length) show(cols[k]); }});
show('original');
</script></body></html>"""
