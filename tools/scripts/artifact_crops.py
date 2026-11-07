"""Zoomed crops for looking at the art up close: the original on the left, HD on the
right, the same part of the screen, every pixel doubled and stored without loss.

  artifact_crops.py scan  --tour <tour run> --media <media run> --scan <scan.json> --out <folder>
      Scores parts of every map's stills and writes numbered contact sheets to pick from.
  artifact_crops.py build --picks <picks.json> --out <folder> [--game <folder> --hd <hd\\art>]
      Writes one PNG per pick, crops.json, and index.html, a page for marking what bothers you.

A pick is {"run": folder, "view": name, "x": .., "y": ..} for a part of a still, or
{"art": "heads/elderbn", "x": .., "y": ..} for art the game doesn't light (faces, interface);
x and y are 1x art pixels there, drawn the way tier 3 draws them.

Only reads the stills, the masters and the game files. It doesn't use the GPU.
"""

from __future__ import annotations

import argparse
import ctypes
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BOX_W, BOX_H = 440, 300
ZOOM = 2
GAP = 10
HEADER = 34
MAP_BOTTOM = 1140  # the interface bar starts here on a 1440 px screen at scale 3
TIER = 3


def low_priority() -> None:
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    except (AttributeError, OSError):
        pass


def luma(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)


def stills(run: Path) -> list[Path]:
    return sorted(p for p in run.glob("*-hd.png") if "-fail-" not in p.name and not p.name.startswith("ui-"))


def map_of(view: str) -> str:
    return re.split(r"_e\d+_|_night_|_indoor_|_fire_", view)[0]


def score_view(hd_path: Path) -> list[dict]:
    """The busiest usable part of a still, a middling one and the middle of the
    screen, where the player and whoever they're talking to usually stand."""
    view = hd_path.name[: -len("-hd.png")]
    hd = cv2.imread(str(hd_path), cv2.IMREAD_COLOR)
    orig = cv2.imread(str(hd_path.with_name(view + "-orig.png")), cv2.IMREAD_COLOR)
    if hd is None or orig is None or hd.shape != orig.shape:
        return []
    lh, lo = luma(hd), luma(orig)
    band = np.abs(cv2.GaussianBlur(lh, (0, 0), 1.2) - cv2.GaussianBlur(lh, (0, 0), 5.0))
    soft_h, soft_o = cv2.GaussianBlur(lh, (0, 0), 8.0), cv2.GaussianBlur(lo, (0, 0), 8.0)
    h, w = lh.shape
    boxes = []
    for y in range(0, min(h, MAP_BOTTOM) - BOX_H + 1, 150):
        for x in range(0, w - BOX_W + 1, 220):
            a, b = soft_h[y : y + BOX_H, x : x + BOX_W], soft_o[y : y + BOX_H, x : x + BOX_W]
            if a.mean() < 24 or b.mean() < 14 or (lo[y : y + BOX_H, x : x + BOX_W] < 3).mean() > 0.25:
                continue
            if a.std() > 3 and b.std() > 3:
                if np.corrcoef(a[::8, ::8].ravel(), b[::8, ::8].ravel())[0, 1] < 0.45:
                    continue  # something moved between the two stills
            boxes.append({"x": x, "y": y, "score": float(band[y : y + BOX_H, x : x + BOX_W].mean())})
    if not boxes:
        return []
    boxes.sort(key=lambda b: -b["score"])
    out = [dict(boxes[0], kind="busy"), dict(boxes[len(boxes) // 2], kind="middling")]
    cx, cy = (w - BOX_W) // 2, (min(h, MAP_BOTTOM) - BOX_H) // 2
    out.append({"x": cx, "y": cy, "score": 0.0, "kind": "centre"})
    return [dict(c, view=view, run=str(hd_path.parent)) for c in out]


def font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def scan(args: argparse.Namespace) -> None:
    low_priority()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    per_map: dict[str, list[Path]] = {}
    for p in stills(Path(args.tour)):
        per_map.setdefault(map_of(p.name[: -len("-hd.png")]), []).append(p)
    chosen = [p for paths in per_map.values() for p in paths[:: max(1, len(paths) // 2)][:2]]
    chosen += stills(Path(args.media))
    print(f"{len(per_map)} maps, {len(chosen)} stills")
    with ThreadPoolExecutor(4) as pool:
        cands = [c for r in pool.map(score_view, chosen) for c in r]
    names = {m["file"].lower().removesuffix(".map"): m.get("lookup_name") or m["file"] for m in json.loads(Path(args.scan).read_text(encoding="utf-8"))["maps"]}
    for i, c in enumerate(cands):
        c["id"] = i
        c["place"] = names.get(map_of(c["view"]), c["view"])
    (out / "candidates.json").write_text(json.dumps(cands, indent=1), encoding="utf-8")
    print(f"{len(cands)} candidates")

    per_sheet, tw, th, cols = 70, 220, 150, 10
    small = font(13)
    for s in range(0, len(cands), per_sheet):
        part = cands[s : s + per_sheet]
        rows = (len(part) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (tw + 4), rows * (th + 20)), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        for i, c in enumerate(part):
            im = Image.open(Path(c["run"]) / f"{c['view']}-hd.png").convert("RGB").crop((c["x"], c["y"], c["x"] + BOX_W, c["y"] + BOX_H)).resize((tw, th), Image.BILINEAR)
            x, y = (i % cols) * (tw + 4), (i // cols) * (th + 20)
            sheet.paste(im, (x, y + 18))
            draw.text((x + 2, y + 1), f"{c['id']} {c['view'][:24]}", fill=(255, 230, 120), font=small)
        sheet.save(out / f"sheet_{s // per_sheet:02d}.jpg", quality=88)
    print("sheets:", (len(cands) + per_sheet - 1) // per_sheet)


def art_sides(pick: dict, game: Path, hd_root: Path) -> tuple[np.ndarray, np.ndarray]:
    """Art the game doesn't light, at the size tier 3 draws it (RGB)."""
    from fo2hd import frm as frm_mod
    from fo2hd.dat import GameFiles

    files = GameFiles(game)
    palette = frm_mod.load_palette(files.read("color.pal"))
    indices = frm_mod.decode(files.read(f"art/{pick['art']}.frm")).frame(0, 0).pixels
    x, y = pick["x"], pick["y"]
    w1, h1 = (BOX_W + TIER - 1) // TIER, (BOX_H + TIER - 1) // TIER
    part = indices[y : y + h1, x : x + w1]
    backdrop = np.array((30, 30, 30), np.uint8)
    left = np.where((part != 0)[..., None], palette[part], backdrop).repeat(TIER, 0).repeat(TIER, 1)[:BOX_H, :BOX_W]
    albedo = np.asarray(Image.open(hd_root / pick["art"] / "r0_f000.albedo.png").convert("RGBA"))
    h4, w4 = albedo.shape[:2]
    scaled = cv2.resize(albedo, (w4 * TIER // 4, h4 * TIER // 4), interpolation=cv2.INTER_AREA).astype(np.float32)
    a = scaled[..., 3:] / 255.0
    rgb = np.round(scaled[..., :3] * a + backdrop * (1 - a)).astype(np.uint8)
    right = rgb[y * TIER : y * TIER + BOX_H, x * TIER : x * TIER + BOX_W]

    def filled(side: np.ndarray) -> np.ndarray:  # art smaller than the box
        canvas = np.full((BOX_H, BOX_W, 3), backdrop, np.uint8)
        canvas[: side.shape[0], : side.shape[1]] = side
        return canvas

    return filled(left), filled(right)


def still_sides(pick: dict) -> tuple[np.ndarray, np.ndarray]:
    run, view, x, y = Path(pick["run"]), pick["view"], pick["x"], pick["y"]
    box = (x, y, x + BOX_W, y + BOX_H)
    return tuple(np.asarray(Image.open(run / f"{view}-{side}.png").convert("RGB").crop(box)) for side in ("orig", "hd"))


def build(args: argparse.Namespace) -> None:
    low_priority()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    picks = json.loads(Path(args.picks).read_text(encoding="utf-8"))
    big, small = font(20), font(15)
    records = []
    for n, pick in enumerate(picks, 1):
        if "art" in pick:
            left, right = art_sides(pick, Path(args.game), Path(args.hd))
            where = f"{pick['art']}, no light"
        else:
            left, right = still_sides(pick)
            where = f"{pick['view']}, screen {pick['x']},{pick['y']}"
        pw, ph = BOX_W * ZOOM, BOX_H * ZOOM
        sheet = Image.new("RGB", (pw * 2 + GAP, ph + HEADER), (32, 32, 32))
        for side, x in ((left, 0), (right, pw + GAP)):
            sheet.paste(Image.fromarray(side).resize((pw, ph), Image.NEAREST), (x, HEADER))
        draw = ImageDraw.Draw(sheet)
        draw.text((10, 5), f"#{n}  {pick['place']}", fill=(255, 255, 255), font=big)
        draw.text((pw + GAP + 10, 8), f"Dustlight  ({where})", fill=(200, 200, 200), font=small)
        name = f"{n:02d}_{re.sub(r'[^a-z0-9]+', '-', pick['place'].lower()).strip('-')}.png"
        sheet.save(out / name)
        records.append(dict(pick, n=n, file=name, where=where))
        print(name, flush=True)
    (out / "crops.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    (out / "index.html").write_text(page(records), encoding="utf-8")


def page(records: list[dict]) -> str:
    cards = "\n".join(
        f"<section data-n='{r['n']}'><div class='frame'><img loading='lazy' src='{html.escape(r['file'])}' "
        f"width='{BOX_W * ZOOM * 2 + GAP}' height='{BOX_H * ZOOM + HEADER}' "
        f"alt='Crop {r['n']}: {html.escape(r['place'], quote=True)}' draggable='false'><div class='layer'></div></div>"
        f"<div class='row'><label><input type='checkbox' class='fine'> Nothing bothers me here</label>"
        f"<ol class='notes'></ol></div></section>"
        for r in records
    )
    return PAGE.replace("{{COUNT}}", str(len(records))).replace("{{CARDS}}", cards)


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Artifact Marks</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:dark;--bg:#141414;--panel:#222;--text:#eee;--dim:#aaa;--mark:#ffd400}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);padding:10px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #444}
header b{font-size:16px}
header span{color:var(--dim)}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #777;background:#333;color:var(--text);cursor:pointer}
button.main{background:#ffd400;color:#111;border-color:#ffd400;font-weight:600}
main{padding:16px;max-width:1800px;margin:auto}
section{margin:0 0 28px}
.frame{position:relative;line-height:0;user-select:none;cursor:crosshair;touch-action:none}
.frame img{width:100%;height:auto;display:block;image-rendering:pixelated;min-height:120px;background:#222}
.layer{position:absolute;inset:0}
.box{position:absolute;border:2px solid var(--mark);box-shadow:0 0 0 1px #000;pointer-events:none}
.box i{position:absolute;left:-2px;top:-22px;background:var(--mark);color:#111;font:600 13px/20px system-ui;padding:0 6px;white-space:nowrap}
.row{background:var(--panel);padding:8px 12px;min-height:40px}
.row label{color:var(--dim);cursor:pointer}
.notes{margin:6px 0 0;padding-left:22px}
.notes:empty{display:none}
.notes li{margin:4px 0}
.notes input{width:min(640px,70%);font:inherit;padding:3px 7px;background:#111;color:var(--text);border:1px solid #666;border-radius:4px}
.notes button{padding:2px 9px;margin-left:6px}
#sent{color:#8fdc8f}
</style>
<header><b>Drag a box around anything that bothers you, then say why</b>
<span id="count"></span>
<button class="main" id="save">Save marks</button><span id="sent"></span></header>
<main>
{{CARDS}}
</main>
<script>
const KEY = 'artifact-marks-v1', TOTAL = {{COUNT}};
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) {}
const store = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
const entry = n => state[n] || (state[n] = {fine: false, marks: []});

function refreshCount() {
  const seen = Object.values(state).filter(s => s.fine || s.marks.length).length;
  const marks = Object.values(state).reduce((a, s) => a + s.marks.length, 0);
  document.getElementById('count').textContent = `${seen} of ${TOTAL} crops done, ${marks} marks`;
}

function render(sec) {
  const n = sec.dataset.n, e = entry(n), layer = sec.querySelector('.layer'), list = sec.querySelector('.notes');
  layer.innerHTML = ''; list.innerHTML = '';
  sec.querySelector('.fine').checked = e.fine;
  e.marks.forEach((m, i) => {
    const b = document.createElement('div');
    b.className = 'box';
    Object.assign(b.style, {left: m.x * 100 + '%', top: m.y * 100 + '%', width: m.w * 100 + '%', height: m.h * 100 + '%'});
    b.innerHTML = `<i>${i + 1}</i>`;
    layer.append(b);
    const li = document.createElement('li');
    const input = document.createElement('input');
    input.value = m.note || '';
    input.placeholder = 'What bothers you here?';
    input.setAttribute('aria-label', `Note for mark ${i + 1} on crop ${n}`);
    input.addEventListener('input', () => { m.note = input.value; store(); });
    const del = document.createElement('button');
    del.textContent = 'Remove';
    del.addEventListener('click', () => { e.marks.splice(i, 1); store(); render(sec); refreshCount(); });
    li.append(input, del);
    list.append(li);
  });
}

document.querySelectorAll('section').forEach(sec => {
  const frame = sec.querySelector('.frame'), layer = sec.querySelector('.layer');
  render(sec);
  sec.querySelector('.fine').addEventListener('change', ev => { entry(sec.dataset.n).fine = ev.target.checked; store(); refreshCount(); });
  let start = null, ghost = null;
  const pos = ev => {
    const r = frame.getBoundingClientRect();
    return {x: Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height))};
  };
  const shape = (a, b) => ({x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y)});
  frame.addEventListener('pointerdown', ev => {
    start = pos(ev);
    frame.setPointerCapture(ev.pointerId);
    ghost = document.createElement('div');
    ghost.className = 'box';
    layer.append(ghost);
  });
  frame.addEventListener('pointermove', ev => {
    if (!start) return;
    const s = shape(start, pos(ev));
    Object.assign(ghost.style, {left: s.x * 100 + '%', top: s.y * 100 + '%', width: s.w * 100 + '%', height: s.h * 100 + '%'});
  });
  const finish = ev => {
    if (!start) return;
    const s = shape(start, pos(ev));
    start = null;
    ghost.remove();
    const px = frame.getBoundingClientRect();
    if (s.w * px.width < 6 || s.h * px.height < 6) return; // a click, not a box
    const e = entry(sec.dataset.n);
    e.marks.push(Object.assign(s, {note: ''}));
    e.fine = false;
    store(); render(sec); refreshCount();
    const inputs = sec.querySelectorAll('.notes input');
    inputs[inputs.length - 1].focus({preventScroll: true});
  };
  frame.addEventListener('pointerup', finish);
  frame.addEventListener('pointercancel', () => { start = null; if (ghost) ghost.remove(); });
});

document.getElementById('save').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(state, null, 1)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'artifact-marks.json';
  document.body.append(a); a.click(); a.remove();
  document.getElementById('sent').textContent = 'Saved to your Downloads folder as artifact-marks.json';
});
refreshCount();
</script>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    for name in ("tour", "media", "scan", "out"):
        s.add_argument("--" + name, required=True)
    b = sub.add_parser("build")
    b.add_argument("--picks", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--game")
    b.add_argument("--hd")
    args = parser.parse_args()
    {"scan": scan, "build": build}[args.command](args)


if __name__ == "__main__":
    main()
