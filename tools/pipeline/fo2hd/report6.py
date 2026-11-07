"""Phase 6 report (PLAN.md P6.7): one local HTML page.

Collects the captures of tools/scripts/phase6_stills.ps1 (stills in
<data_root>/reports/phase6/img/<scene>-<state>.png, frame sequences in
<data_root>/reports/phase6/seq/<scene>-<state>/NNN.png + times.txt, debug logs
in <data_root>/reports/present/still6-<scene>.debug.log), the map test
summaries (<data_root>/reports/map/summary-<label>.json for LABELS) and the
benchmarks (present_bench.ps1). Frame sequences become animated WebP (1:1
crops of the most animated area); stills become JPEG. Writes
<data_root>/reports/phase6/index.html. Game-derived images: keep it local,
never commit or publish it.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Paths
from .report5 import BAR_ROWS, _gpu_times, _light_passes, _load_bench, _load_json, _luma, _vram, _web

CYCLE_SCENES = {
    "slime_caves": "Klamath's toxic caves: glowing goo (slime colors 229–232)",
    "vault_computers": "Vault City's vault: computer screens (monitor colors 233–237) and their red lights (fire colors 238–247)",
    "vault_alarm": "Vault City's vault: the small red alarm lights (color 254, pulsing 30 times a second)",
    "coast_water": "The coast: surf along the beach (shoreline colors 248–253)",
    "sf_docks": "San Francisco docks: water under the piers (shoreline)",
    "reno_fire": "New Reno at night: burning barrels (fire colors)",
    "den_fire": "The Den at night",
}
POST_SCENES = ["temple_indoor", "temple_outdoor", "arroyo_day", "arroyo_night", "den_night", "newreno_night", "slime_caves", "vault_computers"]
SCENE_TITLES = {
    "temple_indoor": "Temple of Trials, inside",
    "temple_outdoor": "Temple of Trials, outside, 12:00",
    "arroyo_day": "Arroyo village, 13:00",
    "arroyo_night": "Arroyo village, 23:00",
    "den_night": "The Den, 23:00",
    "newreno_night": "New Reno, 23:00",
    "slime_caves": "Klamath toxic caves",
    "vault_computers": "Vault City, the vault",
}
POST_EFFECTS = {
    "bloom": "Glow (bloom) around fire, monitors, neon and glowing goo",
    "grade": "Color grading",
    "vignette": "Vignette: slightly darker toward the map's corners",
    "grain": "Film grain (moves; a still shows one frame of it)",
}
LABELS = {
    "p6_new": "New light + post effects, HD art, default window (2×): no holes; brightness drift against the original",
    "p6_new_3440": "The same at 3440×1440, scale 3 (HD checks at 3×)",
    "p6_orig_exact": "Original light, post effects off, every frame forced to the original art: the Phase 4 original-equivalent check",
    "p6_cycle_orig": "Animated-color scenes, original light: HD cycle colors against the game's live colors",
    "p6_cycle_new": "Animated-color scenes, new light + post effects: no holes",
}
WEBP_BOX = (520, 360)  # 1:1 crop of the frame sequences


def _crop(box: list[int]) -> tuple[int, int, int, int]:
    """A notes box (x, y, width, height, as in the Phase 5 notes) as PIL's
    (left, top, right, bottom)."""
    x, y, w, h = box
    return x, y, x + w, y + h


def _times(seq: Path) -> list[int]:
    f = seq / "times.txt"
    if not f.exists():
        return []
    return [int(v) for v in f.read_text().strip().split(",") if v]


def _frames(seq: Path) -> list[Path]:
    return sorted(seq.glob("[0-9][0-9][0-9].png"))


def _moves(frames: list[Path]) -> bool:
    """Whether anything changes over the first 30 frames (the original game's
    recording: no grain, so any change is animation)."""
    first = np.asarray(Image.open(frames[0]).convert("RGB"))
    return any(not np.array_equal(first, np.asarray(Image.open(f).convert("RGB"))) for f in frames[1:30])


def _busiest_box(frames: list[Path], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The crop where the frames change most (summed absolute frame
    differences over the first 30 frames)."""
    stack = np.stack([np.asarray(Image.open(f).convert("L"), dtype=np.float32) for f in frames[:30]])
    change = np.abs(np.diff(stack, axis=0)).sum(axis=0)
    w, h = size
    ii = np.pad(change.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    best, bx, by = -1.0, 0, 0
    for y in range(0, change.shape[0] - h + 1, 8):
        for x in range(0, change.shape[1] - w + 1, 8):
            s = ii[y + h, x + w] - ii[y, x + w] - ii[y + h, x] + ii[y, x]
            if s > best:
                best, bx, by = s, x, y
    return bx, by, bx + w, by + h


def _webp(seq: Path, out: Path, box: tuple[int, int, int, int] | None) -> str | None:
    """Animated WebP of a frame sequence (1:1 crop), with the recorded frame
    times; returns its path relative to the page."""
    frames = _frames(seq)
    if not frames:
        return None
    times = _times(seq)
    if len(times) != len(frames):
        times = [i * 33 for i in range(len(frames))]
    durations = [max(16, b - a) for a, b in zip(times, times[1:])] + [33]
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.stat().st_mtime < frames[-1].stat().st_mtime:
        images = [Image.open(f).convert("RGB") for f in frames]
        if box:
            images = [im.crop(box) for im in images]
        images[0].save(out, save_all=True, append_images=images[1:], duration=durations, loop=0, quality=88, method=4)
    return f"{out.parent.name}/{out.name}"


def _palette_line(log: Path) -> str | None:
    if not log.exists():
        return None
    m = re.search(r"HD: palette: (.*)", log.read_text(encoding="latin-1"))
    return m.group(1).strip() if m else None


def _post_gpu(logs: list[Path]) -> tuple[float, float, int] | None:
    found = []
    for log in logs:
        if not log.exists():
            continue
        m = re.search(r"HD: post GPU ms per draw: mean ([\d.]+), max ([\d.]+) over (\d+) draws", log.read_text(encoding="latin-1"))
        if m:
            found.append((float(m.group(1)), float(m.group(2)), int(m.group(3))))
    if not found:
        return None
    return sum(f[0] for f in found) / len(found), max(f[1] for f in found), sum(f[2] for f in found)


def build(paths: Paths, notes: dict) -> Path:
    reports = paths.reports
    out = reports / "phase6"
    img = out / "img"
    seqs = out / "seq"
    anim = out / "anim"
    img.mkdir(parents=True, exist_ok=True)
    present = reports / "present"

    summaries = {label: _load_json(reports / "map" / f"summary-{label}.json") for label in LABELS}
    benches = {name: _load_bench(present / f"bench-{name}.csv") for name in ("walk", "stress")}
    gpu = {name: _gpu_times(present, name) for name in ("walk", "stress")}
    passes = {name: _light_passes(sorted(present.glob(f"bench-{name}-hd-*.debug.log"))) for name in ("walk", "stress")}
    post_gpu = _post_gpu(sorted(present.glob("bench-*-hd-*.debug.log")) + [present / f"still6-{s}.debug.log" for s in POST_SCENES])
    vram = _vram(present)

    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Phase 6 report</title><style>",
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
        ".pair{display:flex;flex-wrap:wrap;gap:12px}.pair figure{margin:0;max-width:100%}.pair img{display:block;max-width:100%}",
        "figcaption{color:var(--dim);font-size:13px;margin-top:4px}",
        "</style></head><body>",
        "<h1>Phase 6 — effects</h1>",
        f"<p class='dim'>{e(notes.get('generated', ''))}</p>",
    ]

    def slider(a: str | None, b: str | None, ta: str, tb: str) -> str:
        if not a or not b:
            return "<p class='dim'>(image missing)</p>"
        return (
            "<div class='cmp'>"
            f"<img src='{a}' alt='{e(ta)}'><img class='top' src='{b}' alt='{e(tb)}'><div class='bar'></div>"
            f"<div class='tags'><span>{e(ta)}</span><span>{e(tb)}</span></div>"
            "<input type='range' min='0' max='100' value='50' oninput=\"this.parentNode.style.setProperty('--pos',this.value+'%')\"></div>"
        )

    def figure(src: str | None, caption: str) -> str:
        if not src:
            return f"<figure><p class='dim'>(missing: {e(caption)})</p></figure>"
        return f"<figure><img src='{src}' alt='{e(caption)}' loading='lazy'><figcaption>{e(caption)}</figcaption></figure>"

    if notes.get("decisions"):
        parts.append("<h2>What I need from you</h2><div class='box'><ul>")
        parts += [f"<li>{d}</li>" for d in notes["decisions"]]
        parts.append("</ul></div>")
    if notes.get("summary"):
        parts.append("<h2>What changed</h2><div class='box'>")
        parts += [f"<p>{p}</p>" for p in notes["summary"]]
        parts.append("</div>")

    # Animated colors.
    parts.append("<h2>Animated colors in HD (palette cycling)</h2>")
    parts.append(f"<p>{notes.get('cycle_intro', '')}</p>")
    for scene, title in CYCLE_SCENES.items():
        orig_seq = seqs / f"{scene}-orig"
        hd_seq = seqs / f"{scene}-hd"
        if not _frames(orig_seq) or not _frames(hd_seq):
            continue
        parts.append(f"<h3>{e(title)}</h3>")
        if notes.get("cycle", {}).get(scene):
            parts.append(f"<p>{notes['cycle'][scene]}</p>")
        if not _moves(_frames(orig_seq)):
            parts.append("<p class='dim'>(Nothing animates in the original game's recording of this view, so there is nothing to compare.)</p>")
            continue
        box = _busiest_box(_frames(orig_seq), WEBP_BOX)
        a = _webp(orig_seq, anim / f"{scene}-orig.webp", box)
        b = _webp(hd_seq, anim / f"{scene}-hd.webp", box)
        parts.append("<div class='pair'>" + figure(a, "original game (1:1 crop of the 3440×1440 frame)") + figure(b, "HD (same crop, a different moment)") + "</div>")

    # Scenes: everything on vs the original, and vs post effects off.
    parts.append("<h2>Scenes</h2>")
    parts.append(f"<p>{notes.get('scenes_intro', '')}</p>")
    lumas = {}
    for scene in POST_SCENES:
        p6 = _web(img, f"{scene}-p6")
        if not p6:
            continue
        orig = _web(img, f"{scene}-orig")
        off = _web(img, f"{scene}-post-off")
        lumas[scene] = {state: _luma(img / f"{scene}-{state}.png") for state in ("p6", "post-off", "agx", "orig")}
        parts.append(f"<h3>{e(SCENE_TITLES.get(scene, scene))}</h3>")
        if notes.get("scenes", {}).get(scene):
            parts.append(f"<p>{notes['scenes'][scene]}</p>")
        parts.append(slider(orig, p6, "original game", "Phase 6"))
        parts.append(slider(off, p6, "post effects off (Phase 5 look)", "Phase 6"))

    # One effect off at a time, 1:1 close-ups.
    parts.append("<h2>Each post effect on its own</h2>")
    for item in notes.get("effects", []):
        # Default pair: the effect off vs everything on; "a"/"b" name other states.
        fx, scene, box = item["fx"], item["scene"], _crop(item["box"])
        sa, sb = item.get("a", f"no-{fx}"), item.get("b", "p6")
        a = _web(img, f"{scene}-{sa}", box, suffix=f"-crop-{fx}")
        b = _web(img, f"{scene}-{sb}", box, suffix=f"-crop-{fx}")
        parts.append(f"<h3>{e(item.get('title', POST_EFFECTS.get(fx, fx)))} — {e(SCENE_TITLES.get(scene, scene))}</h3><p>{item.get('text', '')}</p>")
        parts.append(slider(a, b, item.get("a_label", f"no {fx}"), item.get("b_label", "on")))

    # Tone map.
    parts.append("<h2>Tone map: P5's shoulder vs AgX</h2>")
    parts.append(f"<p>{notes.get('agx', '')}</p>")
    for item in notes.get("agx_shots", []):
        scene, box = item["scene"], _crop(item["box"]) if item.get("box") else None
        a = _web(img, f"{scene}-p6", box, suffix="-agxcmp" if box else "")
        b = _web(img, f"{scene}-agx", box, suffix="-crop" if box else "")
        parts.append(f"<h3>{e(SCENE_TITLES.get(scene, scene))}</h3>" + slider(a, b, "shoulder (default)", "AgX"))

    # Fades.
    fade = seqs / "arroyo_day-fade"
    if _frames(fade):
        parts.append("<h2>Fades stay HD</h2>")
        parts.append(f"<p>{notes.get('fade', '')}</p>")
        im = Image.open(_frames(fade)[0])
        w, h = im.size
        src = _webp(fade, anim / "arroyo_day-fade.webp", (0, 0, w, h))
        parts.append("<div class='pair'>" + figure(src, "the game's fades to white and back, to black and back (HD the whole time)") + "</div>")
        line = _palette_line(present / "still6-arroyo_day.debug.log")
        if line:
            parts.append(f"<p class='dim'>Counter at exit: {e(line)}</p>")

    # Brightness.
    parts.append("<h2>Brightness</h2><div class='box'><table><tr><th>scene</th><th class='num'>original</th><th class='num'>post off</th><th class='num'>Phase 6</th><th class='num'>vs post off</th><th class='num'>vs original</th><th class='num'>AgX vs Phase 6</th></tr>")
    for scene, row in lumas.items():
        o, off, p6, agx = row["orig"], row["post-off"], row["p6"], row["agx"]

        def pct(a, b):
            return f"{(a / b - 1) * 100:+.1f}%" if a and b else "–"

        parts.append(f"<tr><td>{e(SCENE_TITLES.get(scene, scene))}</td><td class='num'>{o or 0:.3f}</td><td class='num'>{off or 0:.3f}</td><td class='num'>{p6 or 0:.3f}</td>"
                     f"<td class='num'>{pct(p6, off)}</td><td class='num'>{pct(p6, o)}</td><td class='num'>{pct(agx, p6)}</td></tr>")
    parts.append("</table><p class='dim'>Mean brightness of the map area (gamma-2.2 luma of the linear mean) of the stills above. AgX column: the AgX tone map against the default shoulder, both with post effects on.</p></div>")

    # Checks.
    parts.append("<h2>Checks</h2><div class='box'><table><tr><th>run</th><th class='num'>checks</th><th class='num'>failed</th><th>what it shows</th></tr>")
    for label, text in LABELS.items():
        s = summaries[label]
        if not s:
            parts.append(f"<tr><td>{label}</td><td colspan='3' class='dim'>not run</td></tr>")
            continue
        cls = "ok" if s.get("failed", 1) == 0 else "bad"
        parts.append(f"<tr><td>{label}</td><td class='num'>{s.get('checks')}</td><td class='num {cls}'>{s.get('failed')}</td><td>{e(text)}</td></tr>")
    parts.append("</table>")
    for note in notes.get("checks", []):
        parts.append(f"<p>{note}</p>")
    parts.append("</div>")

    # Cost.
    parts.append("<h2>Cost at 3440×1440</h2><div class='box'><table><tr><th>scenario</th><th>variant</th><th class='num'>CPU ms / frame</th><th class='num'>fps</th><th class='num'>map GPU ms / redraw (max)</th></tr>")
    for name, rows in benches.items():
        for variant, (ms, fps) in rows.items():
            g = gpu[name].get(variant)
            parts.append(f"<tr><td>{name}</td><td>{e(variant)}</td><td class='num'>{ms:.2f}</td><td class='num'>{fps:.0f}</td><td class='num'>{f'{g[0]:.2f} ({g[1]:.1f})' if g else '–'}</td></tr>")
    parts.append("</table>")
    for name, found in passes.items():
        if found:
            per, draws = found
            parts.append(f"<p class='dim'>{name}: lighting GPU per pass over {draws} draws: " + ", ".join(f"{k} {v[0]:.2f} ms" for k, v in per.items()) + "</p>")
    if post_gpu:
        parts.append(f"<p class='dim'>Post effects GPU per draw: mean {post_gpu[0]:.3f} ms, max {post_gpu[1]:.2f} ms over {post_gpu[2]} draws.</p>")
    if vram:
        parts.append(f"<p class='dim'>VRAM: {e(vram)}</p>")
    for note in notes.get("cost", []):
        parts.append(f"<p>{note}</p>")
    parts.append("</div>")

    for key, title in (("settings", "Settings"), ("provisional", "Provisional calls"), ("gaps", "Known gaps")):
        if notes.get(key):
            parts.append(f"<h2>{title}</h2><div class='box'><ul>")
            parts += [f"<li>{item}</li>" for item in notes[key]]
            parts.append("</ul></div>")

    parts.append("</body></html>")
    page = out / "index.html"
    page.write_text("\n".join(parts), encoding="utf-8")
    return page
