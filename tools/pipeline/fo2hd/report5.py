"""Phase 5 report (PLAN.md P5.10): one local HTML page.

Collects the stills (phase5_stills.ps1 -> <data_root>/reports/phase5/img/
<scene>-<state>.png and the per-scene debug logs in <data_root>/reports/
present/still5-<scene>.debug.log), the map test summaries (map_test.ps1 ->
<data_root>/reports/map/summary-<label>.json for the labels in LABELS) and the
benchmarks (present_bench.ps1 -> <data_root>/reports/present/bench-*.csv and
the per-run debug logs). Page images are JPEG copies in img/web/ (the PNG
stills are ~7 MB each); close-ups are cut at 1:1. Writes
<data_root>/reports/phase5/index.html. Game-derived images: keep it local,
never commit or publish it.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Paths

SCENES = ["temple_indoor", "temple_outdoor", "arroyo_day", "arroyo_night", "den_night", "newreno_night"]
SCENE_TITLES = {
    "temple_indoor": "Temple of Trials, inside",
    "temple_outdoor": "Temple of Trials, outside, 12:00",
    "arroyo_day": "Arroyo village, 13:00",
    "arroyo_night": "Arroyo village, 23:00",
    "den_night": "The Den, 23:00",
    "newreno_night": "New Reno, 23:00",
}
EFFECTS = {
    "normals": "Surface shape: light follows the shape of walls, objects and the HD art's relief",
    "colors": "Light colors taken from the art around each light (neon, fire, lamps)",
    "shadows": "Shadows from lamps and fires",
    "pool": "Bright pools near strong lights",
    "tonemap": "Soft roll-off of very bright light",
    "sun": "Daylight from the top left, with shadows",
    "ao": "Contact shadows at wall bases and under objects",
    "night": "Darker, bluer nights",
    "gi": "Bounce light: lit ground and walls light their surroundings",
}
LABELS = {
    "p5_new": "New light, HD art, default window (2×): no holes; brightness drift against the original reported",
    "p5_new_3440": "New light at 3440×1440, scale 3 (HD checks at 3×)",
    "p5_orig_exact": "Original light with every frame forced to the original art: the Phase 4 original-equivalent check still passes",
    "p5_seams_orig": "Original light, HD art, after the floor-seam re-bake (P5.0): seam metric",
}
VARIANT_TITLES = {
    "d3d11": "D3D11 presenter, CPU map (Phase 2)",
    "map": "GPU map, original art (Phase 3)",
    "hdorig": "HD art, original light (Phase 4)",
    "hd": "HD art, new light (Phase 5)",
}
BAR_ROWS = 306  # interface bar at scale 3, below the map


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
        m = re.search(r"map GPU time per redraw: mean ([\d.]+) ms, max ([\d.]+) ms", log.read_text(encoding="latin-1"))
        if m:
            found.setdefault(variant, []).append((float(m.group(1)), float(m.group(2))))
    return {k: (sum(a for a, _ in v) / len(v), max(b for _, b in v)) for k, v in found.items()}


def _light_passes(logs: list[Path]) -> tuple[dict[str, tuple[float, float]], int] | None:
    """Per-pass lighting GPU ms (mean of the runs' means, max of maxes) from
    'HD: lighting GPU ms per pass' lines."""
    means: dict[str, list[float]] = {}
    maxes: dict[str, float] = {}
    draws = 0
    for log in logs:
        m = re.search(r"HD: lighting GPU ms per pass over (\d+) draws, mean \(max\): (.*)", log.read_text(encoding="latin-1"))
        if not m:
            continue
        draws += int(m.group(1))
        for name, mean, mx in re.findall(r"([a-z/]+) ([\d.]+) \(([\d.]+)\)", m.group(2)):
            means.setdefault(name, []).append(float(mean))
            maxes[name] = max(maxes.get(name, 0.0), float(mx))
    if not means:
        return None
    return {k: (sum(v) / len(v), maxes[k]) for k, v in means.items()}, draws


def _vram(present_dir: Path) -> str | None:
    for log in sorted(present_dir.glob("bench-*-hd-*.debug.log")):
        m = re.search(r"HD: VRAM: (.*)", log.read_text(encoding="latin-1"))
        if m:
            return m.group(1).strip()
    return None


def _luma(path: Path) -> float | None:
    """Mean brightness of the map area: gamma-2.2 luma of the linear mean."""
    if not path.exists():
        return None
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0
    lin = a[: a.shape[0] - BAR_ROWS] ** 2.2
    return float((0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).mean() ** (1 / 2.2))


def _lights(log: Path) -> list[dict]:
    """The lights the game described for a scene ('HD: light ...' lines)."""
    if not log.exists():
        return []
    out = []
    pattern = r"HD: light (\d+) at tile (\d+): (\w+), z (\d+), color ([\d.]+) ([\d.]+) ([\d.]+) \((\d+) px\), radius (\d+), intensity ([\d.]+)"
    for m in re.finditer(pattern, log.read_text(encoding="latin-1")):
        out.append({"id": int(m.group(1)), "kind": m.group(3), "z": int(m.group(4)), "color": tuple(float(m.group(k)) for k in (5, 6, 7)),
                    "pixels": int(m.group(8)), "radius": int(m.group(9)), "intensity": float(m.group(10))})
    return out


def _hd_numbers(line: str) -> dict:
    out = {}
    for key, pattern in [
        ("holes", r"(\d+) holes"),
        ("psnr", r"PSNR ([\d.]+) dB"),
        ("p99", r"p99 (\d+)"),
        ("brightness", r"brightness ([+-][\d.]+)%"),
        ("mode", r"; (new|original) light"),
        ("result", r"hd: (PASS|FAIL)"),
    ]:
        m = re.search(pattern, line)
        if m:
            out[key] = m.group(1)
    return out


def _web(img: Path, name: str, box: tuple[int, int, int, int] | None = None, suffix: str = "") -> str | None:
    """JPEG copy of img/<name>.png (optionally a 1:1 crop) for the page; returns
    its path relative to the page."""
    src = img / f"{name}.png"
    if not src.exists():
        return None
    web = img / "web"
    web.mkdir(exist_ok=True)
    dst = web / f"{name}{suffix}.jpg"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        im = Image.open(src).convert("RGB")
        im = im.crop(box) if box else im.crop((0, 0, im.width, im.height - BAR_ROWS))
        im.save(dst, quality=92)
    return f"img/web/{dst.name}"


def build(paths: Paths, notes: dict) -> Path:
    reports = paths.reports
    out = reports / "phase5"
    img = out / "img"
    img.mkdir(parents=True, exist_ok=True)
    present = reports / "present"

    summaries = {label: _load_json(reports / "map" / f"summary-{label}.json") for label in LABELS}
    benches = {name: _load_bench(present / f"bench-{name}.csv") for name in ("walk", "stress")}
    gpu = {name: _gpu_times(present, name) for name in ("walk", "stress")}
    passes = {name: _light_passes(sorted(present.glob(f"bench-{name}-hd-*.debug.log"))) for name in ("walk", "stress")}
    still_passes = _light_passes([present / f"still5-{s}.debug.log" for s in SCENES if (present / f"still5-{s}.debug.log").exists()])
    vram = _vram(present)

    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Phase 5 report</title><style>",
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
        ".sw{display:inline-block;width:14px;height:14px;border-radius:3px;vertical-align:-2px;margin-right:6px;border:1px solid #0006}",
        "figcaption{color:var(--dim);font-size:13px;margin-top:4px}",
        "</style></head><body>",
        "<h1>Dustlight — Phase 5: new lighting</h1>",
        f"<p class='dim'>Generated by <code>fo2hd report5</code> from the automated runs. {e(notes.get('generated', ''))}</p>",
        "<div class='box'><b>What I need from you</b><ul>",
    ]
    for item in notes.get("decisions", []):
        parts.append(f"<li>{item}</li>")
    parts.append("</ul></div>")

    parts.append("<h2>What Phase 5 built</h2>")
    for line in notes.get("summary", []):
        parts.append(f"<p>{line}</p>")

    def slider(a: str | None, b: str | None, ta: str, tb: str) -> str:
        if a is None or b is None:
            return "<p class='dim'>(image missing)</p>"
        return (f"<div class='cmp'><img src='{a}' alt='{e(ta)}'><img class='top' src='{b}' alt='{e(tb)}'><div class='bar'></div>"
                f"<div class='tags'><span>{e(ta)}</span><span>{e(tb)}</span></div>"
                "<input type='range' min='0' max='100' value='50' oninput=\"this.parentNode.style.setProperty('--pos',this.value+'%')\"></div>")

    # Scenes.
    brightness = {s: {st: _luma(img / f"{s}-{st}.png") for st in ("orig", "p4", "p5")} for s in SCENES}
    parts.append("<h2>What it looks like</h2>")
    parts.append("<p>Back-buffer captures at your native 3440×1440 (scale 3), the map area only. Each scene was captured in one run: the new light first, "
                 "then Ctrl+F7 (the original light on the HD art: what Phase 4 showed you), then Ctrl+F8 (the original art: the original game). "
                 "Critters and fires keep animating between the captures. Drag across an image to compare.</p>")
    scene_notes = notes.get("scenes", {})
    for s in SCENES:
        p5 = _web(img, f"{s}-p5")
        if p5 is None:
            continue
        parts.append(f"<h3>{e(SCENE_TITLES[s])}</h3>")
        if s in scene_notes:
            parts.append(f"<p>{scene_notes[s]}</p>")
        parts.append(slider(p5, _web(img, f"{s}-p4"), "new light", "original light (Phase 4)"))
        parts.append("<div style='height:10px'></div>")
        parts.append(slider(p5, _web(img, f"{s}-orig"), "new light", "original game"))
        b = brightness[s]
        if b["p5"] and b["p4"] and b["orig"]:
            parts.append(f"<p class='dim'>Mean brightness of the map area: new light {b['p5']:.3f}, Phase 4 {b['p4']:.3f} ({100 * (b['p5'] / b['p4'] - 1):+.1f}%), "
                         f"original game {b['orig']:.3f} ({100 * (b['p5'] / b['orig'] - 1):+.1f}%).</p>")

    # One effect off.
    effect_spots = notes.get("effects", [])
    if effect_spots:
        parts.append("<h2>One effect at a time</h2>")
        parts.append("<p>The same run, with Ctrl+F6 switching off one effect at a time. Close-ups at 1:1 (no scaling); left: everything on, right: the one effect off.</p>")
        for spot in effect_spots:
            fx, s, box = spot["fx"], spot["scene"], tuple(spot["box"])
            x, y, w, h = box
            a = _web(img, f"{s}-p5", (x, y, x + w, y + h), f"-crop-{fx}")
            b = _web(img, f"{s}-no-{fx}", (x, y, x + w, y + h), "-crop")
            parts.append(f"<h3>{e(EFFECTS.get(fx, fx))} <span class='dim'>({e(fx)}, {e(SCENE_TITLES.get(s, s))})</span></h3>")
            parts.append(slider(a, b, "all effects", f"no {fx}"))
            if spot.get("text"):
                parts.append(f"<p class='dim'>{spot['text']}</p>")

    # Light colors.
    parts.append("<h2>Light sources and their colors</h2>")
    parts.append("<p>Every light the game knows near the view, as described by the new light (kind, mount height, color). A color comes from bright, "
                 "saturated art pixels around the light's column, mixed half and half with warm white; lights with no such pixels keep the warm default "
                 "(lamps) or orange (fires); critters' own lights are neutral.</p>")
    parts.append("<table><tr><th>scene</th><th class='num'>lights</th><th>kinds</th><th>colors found in the art</th></tr>")
    for s in SCENES:
        lights = _lights(present / f"still5-{s}.debug.log")
        if not lights:
            continue
        kinds: dict[str, int] = {}
        for light in lights:
            kinds[light["kind"]] = kinds.get(light["kind"], 0) + 1
        colored = [light for light in lights if light["pixels"] > 0]
        swatches = " ".join(
            f"<span class='sw' title='light {c['id']}: {c['pixels']} px' style='background:rgb({int(c['color'][0] * 255)},{int(c['color'][1] * 255)},{int(c['color'][2] * 255)})'></span>"
            for c in colored) or "<span class='dim'>none (defaults)</span>"
        parts.append(f"<tr><td>{e(SCENE_TITLES[s])}</td><td class='num'>{len(lights)}</td><td>{e(', '.join(f'{v} {k}' for k, v in sorted(kinds.items())))}</td><td>{swatches}</td></tr>")
    parts.append("</table>")

    # Checks.
    parts.append("<h2>Checks</h2>")
    parts.append("<p>The automated map runs of Phases 3–4 (every translucency mode, outline type, floating text, scrolling and moving critters forced on), "
                 "with the HD check every 1.5 s. With the new light only <b>holes</b> must be 0 (the colors change on purpose); the brightness drift against "
                 "the original is reported. With the original light the Phase 4 checks apply unchanged.</p>")
    for label, title in LABELS.items():
        s = summaries.get(label) or {}
        if not s:
            continue
        cls = "ok" if s.get("failed", 1) == 0 else "bad"
        parts.append(f"<h3>{e(title)}: <span class='{cls}'>{s.get('checks', 0)} checks, {s.get('failed', 0)} failed</span></h3>")
        parts.append(f"<p class='dim'>{e(' '.join(s.get('extra') or []))} · {e(str(s.get('when', '')))}</p>")
        parts.append("<table><tr><th>scene</th><th class='num'>checks</th><th class='num'>failed</th><th class='num'>HD checks</th><th class='num'>holes</th>"
                     "<th>light</th><th class='num'>PSNR vs original</th><th class='num'>p99</th><th class='num'>brightness vs original</th><th>seams (last)</th></tr>")
        for r in s.get("runs", []):
            nums = [_hd_numbers(x) for x in (r.get("hd") or [])]
            holes = max((int(n.get("holes", 0)) for n in nums), default=0)
            last = nums[-1] if nums else {}
            drifts = [float(n["brightness"]) for n in nums if "brightness" in n]
            drift = f"{min(drifts):+.1f} … {max(drifts):+.1f}%" if drifts else "—"
            seams = re.sub(r"^.*seams: floor steps ", "", (r.get("seams") or [""])[-1])
            rc = "ok" if r.get("failed", 1) == 0 else "bad"
            hc = "ok" if holes == 0 else "bad"
            parts.append(f"<tr><td>{e(r['name'])}</td><td class='num'>{r['checks']}</td><td class='num {rc}'>{r['failed']}</td><td class='num'>{r.get('hd_checks', 0)}</td>"
                         f"<td class='num {hc}'>{holes}</td><td>{e(last.get('mode', '—'))}</td><td class='num'>{last.get('psnr', '—')}</td><td class='num'>{last.get('p99', '—')}</td>"
                         f"<td class='num'>{drift}</td><td class='dim'>{e(seams)}</td></tr>")
        parts.append("</table>")
    for line in notes.get("checks_text", []):
        parts.append(f"<p>{line}</p>")

    # Cost.
    if any(benches.values()) or vram or still_passes:
        parts.append("<h2>Cost</h2>")
        parts.append("<p>At 3440×1440 scale 3, frames capped at 60 fps by the game. <b>CPU</b>: time per frame spent getting the frame to the screen "
                     "(including the draw list and the light inputs when the map changed). <b>GPU</b>: time per map redraw on the graphics card (GPU timers); "
                     "the map is redrawn only when something on it changes.</p>")
        parts.append("<table><tr><th>path</th><th class='num'>save walk: CPU/frame</th><th class='num'>fps</th><th class='num'>GPU/redraw (mean, max)</th>"
                     "<th class='num'>stress: CPU/frame</th><th class='num'>fps</th><th class='num'>GPU/redraw (mean, max)</th></tr>")
        for variant, title in VARIANT_TITLES.items():
            cells = []
            for scenario in ("walk", "stress"):
                v = benches[scenario].get(variant)
                g = gpu[scenario].get(variant)
                cells.append("—" if v is None else f"{v[0]:.2f} ms")
                cells.append("—" if v is None else f"{v[1]:.0f}")
                cells.append("—" if g is None else f"{g[0]:.2f} / {g[1]:.2f} ms")
            if all(c == "—" for c in cells):
                continue
            parts.append(f"<tr><td>{e(title)}</td>" + "".join(f"<td class='num'>{c}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
        rows = [("save walk", passes["walk"]), ("stress", passes["stress"]), ("the six scenes (stills runs)", still_passes)]
        rows = [(name, p) for name, p in rows if p]
        if rows:
            # The log's own "total" (sum of means; max of whole draws) is the last column.
            names = [n for n in rows[0][1][0] if n != "total"] + ["total"]
            parts.append("<h3>New light, GPU time per pass (ms per light draw: mean / max)</h3>")
            parts.append("<table><tr><th>run</th><th class='num'>draws</th>" + "".join(f"<th class='num'>{e(n)}</th>" for n in names) + "</tr>")
            for name, (p, draws) in rows:
                parts.append(f"<tr><td>{e(name)}</td><td class='num'>{draws}</td>"
                             + "".join(f"<td class='num'>{p[n][0]:.2f} / {p[n][1]:.2f}</td>" if n in p else "<td>—</td>" for n in names) + "</tr>")
            parts.append("</table>")
        if vram:
            parts.append(f"<p>Video memory with the new light on: {e(vram)}.</p>")
        for line in notes.get("cost_text", []):
            parts.append(f"<p>{line}</p>")

    if notes.get("settings"):
        parts.append("<h2>Settings</h2><p>All in <code>[hd_light]</code> of the game's config file; Ctrl+F7 switches new/original light while playing.</p>")
        parts.append("<table><tr><th>setting</th><th>default</th><th>what it does</th></tr>")
        for key, value, text in notes["settings"]:
            parts.append(f"<tr><td><code>{e(key)}</code></td><td><code>{e(value)}</code></td><td>{text}</td></tr>")
        parts.append("</table>")

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
