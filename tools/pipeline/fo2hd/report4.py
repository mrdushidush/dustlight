"""Phase 4 report (PLAN.md P4.6): one local HTML page.

Collects the HD map test summaries (tools/scripts/map_test.ps1 ->
<data_root>/reports/map/summary-<label>.json for the labels in LABELS), the
benchmarks (present_bench.ps1 -> <data_root>/reports/present/bench-*.csv and
the per-run debug logs), the pack statistics (<data_root>/hdpack/last_pack.json)
and the stills (phase4_stills.ps1 -> <data_root>/reports/phase4/img/), and
cuts close-ups from the stills. Writes <data_root>/reports/phase4/index.html.
Game-derived images: keep it local, never commit or publish it.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

from PIL import Image

from .config import Paths

LABELS = {
    "hd_orig": "HD color pass with every frame forced to the original art (original-equivalent check)",
    "hd_on": "HD art on",
    "hd_mix50": "HD art on, half of the art files forced to the original art (mixing check)",
    "walk_hd": "Your save game with the working copy's new defaults (HD on): inventory, character screen, automap, options and skilldex over the map",
    "hd_3440": "HD art on at 3440×1440, scale 3 (HD checks at 3×)",
    "hd_budget": "HD art on at 3440×1440 with a 1-page (4 MB) pool: over-budget fallback check",
    "hd_budget2": "HD art on at 3440×1440 with a 2-page (8 MB) pool",
}
SCENES = ["temple_indoor", "temple_outdoor", "arroyo_day", "newreno_night"]
SCENE_TITLES = {
    "temple_indoor": "Temple of Trials, inside",
    "temple_outdoor": "Temple of Trials, outside",
    "arroyo_day": "Arroyo village, 13:00",
    "newreno_night": "New Reno, 23:00",
}
VARIANT_TITLES = {
    "d3d11": "D3D11 presenter, CPU map (Phase 2)",
    "map": "GPU map, original art (Phase 3)",
    "hd": "GPU map, HD art (Phase 4)",
}


def _load_json(path: Path) -> dict:
    # PowerShell writes UTF-8 with a BOM.
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def _load_bench(path: Path) -> dict[str, tuple[float, float]]:
    if not path.exists():
        return {}
    rows: dict[str, list[tuple[float, float]]] = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row["Variant"], []).append((float(row["MsPerFrame"]), float(row["Fps"])))
    return {k: (sum(a for a, _ in v) / len(v), sum(b for _, b in v) / len(v)) for k, v in rows.items()}


def _gpu_times(present_dir: Path, scenario: str) -> dict[str, tuple[float, float]]:
    """Mean/max map GPU time per redraw from the benchmark debug logs, by variant."""
    found: dict[str, list[tuple[float, float]]] = {}
    for log in present_dir.glob(f"bench-{scenario}-*-*.debug.log"):
        variant = log.name.split("-")[2]
        text = log.read_text(encoding="latin-1")
        m = re.search(r"map GPU time per redraw: mean ([\d.]+) ms, max ([\d.]+) ms", text)
        if m:
            found.setdefault(variant, []).append((float(m.group(1)), float(m.group(2))))
    return {k: (sum(a for a, _ in v) / len(v), max(b for _, b in v)) for k, v in found.items()}


def _vram(present_dir: Path) -> str | None:
    for log in sorted(present_dir.glob("bench-*-hd-*.debug.log")):
        m = re.search(r"HD: VRAM: (.*)", log.read_text(encoding="latin-1"))
        if m:
            return m.group(1).strip()
    return None


def _hd_numbers(line: str) -> dict:
    out = {}
    for key, pattern in [
        ("holes", r"(\d+) holes"),
        ("min_alpha", r"min alpha ([\d.]+)"),
        ("hd", r"HD art on (\d+) of (\d+) frames"),
        ("psnr", r"PSNR ([\d.]+) dB"),
        ("p99", r"p99 (\d+)"),
        ("result", r"hd: (PASS|FAIL)"),
    ]:
        m = re.search(pattern, line)
        if m:
            out[key] = m.groups() if len(m.groups()) > 1 else m.group(1)
    return out


def _crops(img_dir: Path) -> list[tuple[str, str, str]]:
    """Native-resolution close-ups (HD | original) cut from the stills."""
    spots = {
        "temple_indoor": [(1900, 160, 700, 420, "Stone walls, an arch and the flagstone floor inside the temple.")],
        "temple_outdoor": [(1150, 300, 700, 420, "The temple entrance: stairs, a fire bowl and the player. The flames are the original's palette-cycling pixels (until P6).")],
        "arroyo_day": [(1380, 150, 700, 420, "Arroyo: a tent and villagers on sand. The sand loses the original's dither; look for faint straight lines at some floor-tile edges (see Seams).")],
        "newreno_night": [(2400, 320, 700, 420, "New Reno at night: walls, graffiti and neon. Colors and light are the original's; the cycling neon stays original pixels.")],
    }
    out = []
    for scene, boxes in spots.items():
        hd_path = img_dir / f"{scene}-hd.png"
        orig_path = img_dir / f"{scene}-orig.png"
        mix_path = img_dir / f"{scene}-mix.png"
        if not (hd_path.exists() and orig_path.exists()):
            continue
        for i, (x, y, w, h, caption) in enumerate(boxes):
            for kind, src in (("hd", hd_path), ("orig", orig_path), ("mix", mix_path)):
                if src.exists():
                    Image.open(src).crop((x, y, x + w, y + h)).save(img_dir / f"crop-{scene}-{i}-{kind}.png")
            out.append((scene, f"crop-{scene}-{i}", caption))
    return out


def build(paths: Paths, notes: dict) -> Path:
    reports = paths.reports
    out = reports / "phase4"
    img = out / "img"
    img.mkdir(parents=True, exist_ok=True)
    present = reports / "present"

    summaries = {label: _load_json(reports / "map" / f"summary-{label}.json") for label in LABELS}
    pack = _load_json(paths.data_root / "hdpack" / "last_pack.json")
    benches = {name: _load_bench(present / f"bench-{name}.csv") for name in ("walk", "stress")}
    gpu = {name: _gpu_times(present, name) for name in ("walk", "stress")}
    vram = _vram(present)
    crops = _crops(img)

    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Phase 4 report</title><style>",
        ":root{--bg:#1b1a17;--panel:#25231f;--ink:#e4dfd4;--dim:#a39d90;--accent:#d8a14a;--ok:#8fd18a;--bad:#e27d6a}",
        "body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1400px;padding:16px}",
        "h1{font-size:26px;margin:8px 0}h2{font-size:20px;margin:32px 0 8px;color:var(--accent)}h3{font-size:16px;margin:18px 0 6px}",
        ".box{background:var(--panel);border-radius:8px;padding:12px 16px;margin:12px 0}",
        "table{border-collapse:collapse;margin:8px 0;display:block;overflow-x:auto}td,th{border-bottom:1px solid #3a372f;padding:4px 10px;text-align:left;vertical-align:top}",
        "th{color:var(--dim);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}",
        ".ok{color:var(--ok)}.bad{color:var(--bad)}.dim{color:var(--dim)}",
        ".cmp{position:relative;display:inline-block;max-width:100%;--pos:50%;user-select:none}",
        ".cmp img{display:block;max-width:100%}",
        ".cmp img.top{position:absolute;inset:0;clip-path:inset(0 0 0 var(--pos))}",
        ".cmp .bar{position:absolute;top:0;bottom:0;left:var(--pos);width:2px;background:var(--accent);pointer-events:none}",
        ".cmp input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:ew-resize;margin:0}",
        ".tags{position:absolute;top:6px;left:8px;right:8px;display:flex;justify-content:space-between;pointer-events:none;font-size:12px}",
        ".tags span{background:#000a;padding:2px 6px;border-radius:4px}",
        ".pair{display:flex;gap:12px;flex-wrap:wrap}.pair figure{flex:1 1 560px;margin:0}.pair img{width:100%;image-rendering:pixelated}",
        "figcaption{color:var(--dim);font-size:13px;margin-top:4px}",
        "</style></head><body>",
        "<h1>Dustlight — Phase 4: HD art in the game</h1>",
        f"<p class='dim'>Generated by <code>fo2hd report4</code> from the automated test runs. {e(notes.get('generated', ''))}</p>",
        "<div class='box'><b>What I need from you</b><ul>",
    ]
    for item in notes.get("decisions", []):
        parts.append(f"<li>{item}</li>")
    parts.append("</ul></div>")

    parts.append("<h2>What Phase 4 built</h2>")
    for line in notes.get("summary", []):
        parts.append(f"<p>{line}</p>")

    def slider(a: str, b: str, ta: str, tb: str) -> str:
        return (f"<div class='cmp'><img src='img/{a}' alt='{e(ta)}'><img class='top' src='img/{b}' alt='{e(tb)}'><div class='bar'></div>"
                f"<div class='tags'><span>{e(ta)}</span><span>{e(tb)}</span></div>"
                "<input type='range' min='0' max='100' value='50' oninput=\"this.parentNode.style.setProperty('--pos',this.value+'%')\"></div>")

    if any((img / f"{s}-hd.png").exists() for s in SCENES):
        parts.append("<h2>What it looks like</h2>")
        parts.append("<p>Back-buffer captures at your native 3440×1440 (scale 3, 1146×480 game pixels). Each pair is the same moment of the same scene: "
                     "HD art on, then switched off with Ctrl+F8 (the original art drawn by the same renderer). Drag across an image to compare. "
                     "Lighting, colors and the palette effects are still the original game's; new lighting is Phase 5.</p>")
        for s in SCENES:
            if (img / f"{s}-hd.png").exists() and (img / f"{s}-orig.png").exists():
                parts.append(f"<h3>{e(SCENE_TITLES[s])}</h3>")
                parts.append(slider(f"{s}-hd.png", f"{s}-orig.png", "HD art", "original art"))

    if crops:
        parts.append("<h2>Close-ups at native resolution</h2>")
        parts.append("<p>1:1 pixels from the captures above (no scaling): HD on the left, original on the right; the third image, where present, "
                     "is the mixing run with half of the art files forced back to the original art.</p>")
        for scene, stem, caption in crops:
            parts.append("<div class='pair'>")
            for kind, title in (("hd", "HD"), ("orig", "original"), ("mix", "mixed (half the files original)")):
                if (img / f"{stem}-{kind}.png").exists():
                    parts.append(f"<figure><img src='img/{stem}-{kind}.png' alt='{e(title)}'><figcaption>{e(SCENE_TITLES[scene])}: {e(title)}</figcaption></figure>")
            parts.append(f"</div><p class='dim'>{e(caption)}</p>")

    parts.append("<h2>Checks</h2>")
    parts.append("<p>Same automated runs as Phase 3 (the four benchmark scenes with every translucency mode, outline type, floating text, scrolling and "
                 "moving critters forced on), plus a new check every 1.5 s on the HD pass: <b>holes</b> (pixels the original draws where the HD image "
                 "would let the black behind the map show through by at least one display step) must be 0. With the original art forced, the HD pass's colors "
                 "are also compared with the original's (PSNR, 99th-percentile error).</p>")
    for label, title in LABELS.items():
        s = summaries.get(label) or {}
        if not s:
            continue
        cls = "ok" if s.get("failed", 1) == 0 else "bad"
        parts.append(f"<h3>{e(title)}: <span class='{cls}'>{s.get('checks', 0)} checks, {s.get('failed', 0)} failed</span></h3>")
        parts.append(f"<p class='dim'>{e(' '.join(s.get('extra') or []))} · {e(str(s.get('when', '')))}</p>")
        parts.append("<table><tr><th>scene</th><th class='num'>checks</th><th class='num'>failed</th><th class='num'>HD checks</th>"
                     "<th class='num'>holes</th><th class='num'>min coverage</th><th class='num'>frames with HD art</th><th class='num'>PSNR vs original</th><th class='num'>p99</th><th>seams (last)</th></tr>")
        for r in s.get("runs", []):
            hd_lines = r.get("hd") or []
            nums = [_hd_numbers(x) for x in hd_lines]
            holes = max((int(n.get("holes", 0)) for n in nums), default=0)
            min_alpha = min((float(n["min_alpha"]) for n in nums if "min_alpha" in n), default=None)
            last = nums[-1] if nums else {}
            hd_frames = f"{last['hd'][0]} of {last['hd'][1]}" if "hd" in last else "—"
            seams = (r.get("seams") or [""])[-1]
            seams = re.sub(r"^.*seams: floor steps ", "", seams)
            rc = "ok" if r.get("failed", 1) == 0 else "bad"
            hc = "ok" if holes == 0 else "bad"
            parts.append(f"<tr><td>{e(r['name'])}</td><td class='num'>{r['checks']}</td><td class='num {rc}'>{r['failed']}</td><td class='num'>{r.get('hd_checks', 0)}</td>"
                         f"<td class='num {hc}'>{holes}</td><td class='num'>{'' if min_alpha is None else f'{min_alpha:.4f}'}</td><td class='num'>{hd_frames}</td>"
                         f"<td class='num'>{last.get('psnr', '—')}</td><td class='num'>{last.get('p99', '—')}</td><td class='dim'>{e(seams)}</td></tr>")
        parts.append("</table>")
    for line in notes.get("checks_text", []):
        parts.append(f"<p>{line}</p>")

    if pack:
        parts.append("<h2>The art</h2>")
        b = pack.get("bytes", {})
        rt = pack.get("roundtrip", {})
        parts.append(f"<p>{pack.get('packed', 0) + pack.get('skipped', 0):,} art files (all floors, roofs, walls, scenery, items and misc art, plus the "
                     f"sample critters) baked to 4× masters with 4x-UltraSharpV2, then packed into GPU-compressed (BC7) atlases at 2×, 3× and 4×: "
                     f"{int(b.get('2', 0)) / 1e6:.0f} MB, {int(b.get('3', 0)) / 1e6:.0f} MB and {int(b.get('4', 0)) / 1e6:.0f} MB on disk. "
                     f"The game loads one size: 3× at your resolution. Compression round trip at 3× on {rt.get('frames', 0)} random frames: "
                     f"mean {rt.get('psnr_mean', 0):.1f} dB, worst {rt.get('psnr_min', 0):.1f} dB.</p>")
        for line in notes.get("art_text", []):
            parts.append(f"<p>{line}</p>")

    if any(benches.values()) or vram:
        parts.append("<h2>Cost</h2>")
        parts.append("<p>At 3440×1440 scale 3, frames capped at 60 fps by the game. <b>CPU</b>: time per frame spent getting the frame to the screen "
                     "(including rebuilding the draw list when the map changed). <b>GPU</b>: time per map redraw on the graphics card, measured with GPU timers "
                     "(new in this phase); the map is redrawn only when something on it changes.</p>")
        parts.append("<table><tr><th>path</th><th class='num'>save walk: CPU/frame</th><th class='num'>fps</th><th class='num'>GPU/redraw (mean, max)</th>"
                     "<th class='num'>stress: CPU/frame</th><th class='num'>fps</th><th class='num'>GPU/redraw (mean, max)</th></tr>")
        for variant in ("d3d11", "map", "hd"):
            cells = []
            for scenario in ("walk", "stress"):
                v = benches[scenario].get(variant)
                g = gpu[scenario].get(variant)
                cells.append("—" if v is None else f"{v[0]:.2f} ms")
                cells.append("—" if v is None else f"{v[1]:.0f}")
                cells.append("—" if g is None else f"{g[0]:.2f} / {g[1]:.2f} ms")
            if all(c == "—" for c in cells):
                continue
            parts.append(f"<tr><td>{e(VARIANT_TITLES[variant])}</td>" + "".join(f"<td class='num'>{c}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
        if vram:
            parts.append(f"<p>Video memory with HD art on: {e(vram)}.</p>")
        for line in notes.get("cost_text", []):
            parts.append(f"<p>{line}</p>")

    parts.append("<h2>Provisional calls (yours to overrule)</h2><ul>")
    for item in notes.get("provisional", []):
        parts.append(f"<li>{item}</li>")
    parts.append("</ul><h2>Known gaps</h2><ul>")
    for item in notes.get("gaps", []):
        parts.append(f"<li>{item}</li>")
    parts.append("</ul><h2>What happens next</h2><ul>")
    for item in notes.get("next", []):
        parts.append(f"<li>{item}</li>")
    parts.append("</ul></body></html>")
    (out / "index.html").write_text("\n".join(parts), encoding="utf-8")
    return out / "index.html"
