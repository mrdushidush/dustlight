"""Phase 7 report (PLAN.md P7a.7): one local HTML page, built from whatever
exists so far, so it can be regenerated as the batch and the tour progress.

Sections:
  batch     progress of the art batch (jobs.sqlite / jobs_pack.sqlite against
            the bulk manifests) and the tail of <data_root>/bulk/p7_batch.log
  maps      the map scan (fo2hd maps): maps, elevations, roof tiles, tour views
  critters  walk cycles of the review set (manifest "review_first"), original
            vs HD side by side at 3x (the tier the author plays at), as animated
            WebP, plus the P1.8 shimmer metric (1.0 = as calm as the original)
  tour      every view of a tour run (tools/scripts/tour.ps1): checks, holes,
            stills HD vs original, and the change against an earlier run (the
            original-art stills' change next to it: if they changed too, the
            scene differs; a still from before the capture setup isn't
            compared, the map self-test numbers are shown instead);
            failed views keep counting as failed, and those investigated carry
            the finding from <data_root>/reports/tour/explained.json
            ({"<view>": "what it is"}, written by hand after a diagnosis)
  ui        the tour run's UI walk (P7b): replay self-tests, HD UI layer log
            lines, coverage per window, stills with the HD UI vs original art

Writes <data_root>/reports/phase7/index.html. Game-derived images: keep it
local, never commit or publish it.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import anim
from . import frm as frm_mod
from . import hdassets
from .config import Paths
from .dat import GameFiles
from .report5 import BAR_ROWS

TIER = 3  # the author plays at 3440x1440, scale 3
GALLERY_ROTATION = 2  # facing south-east, toward the camera
DIFF_SCALE = 8  # stills are compared at 1/8 size: grain and single animated pixels average out
DIFF_LEVEL = 20  # luma steps (0..255) that count as a change
DIFF_FLAG = 0.02  # share of the map area changed that flags a view


def _manifest_progress(db: Path, stage: str, manifest: Path) -> tuple[int, int]:
    if not manifest.exists():
        return 0, 0
    paths = [a["path"] for a in json.loads(manifest.read_text(encoding="utf-8"))["assets"]]
    if not db.exists():
        return 0, len(paths)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        done = {row[0] for row in con.execute("SELECT item FROM jobs WHERE stage=?", (stage,))}
    finally:
        con.close()
    return sum(p in done for p in paths), len(paths)


def _walk_webp(files: GameFiles, paths: Paths, game_path: str, out: Path) -> dict | None:
    """Original (nearest) | HD, both at TIER x, one walk cycle in place."""
    master = hdassets.asset_dir(paths, game_path)
    meta_path = master / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    frm = frm_mod.decode(files.read(game_path))
    palette = frm_mod.load_palette(files.read("color.pal"))
    rotation = GALLERY_ROTATION
    entry = meta["rotations"][rotation]
    if "same_as" in entry:
        rotation = entry["same_as"]
        entry = meta["rotations"][rotation]
    tops, size = anim._layout(frm, rotation)
    original, hd = [], []
    for f, top, fmeta in zip(frm.frames[rotation], tops, entry["frames"]):
        rgba1 = np.dstack([palette[f.pixels], np.where(f.pixels != 0, 255, 0)]).astype(np.float32) / 255.0
        original.append(anim._canvas(rgba1.repeat(anim.S, 0).repeat(anim.S, 1), top, size))
        hd.append(anim._canvas(np.asarray(Image.open(master / f"{fmeta['name']}.albedo.png").convert("RGBA")).astype(np.float32) / 255.0, top, size))

    flows_fwd = [anim._flow(original[t], original[(t + 1) % len(original)]) for t in range(len(original))]
    flicker_orig = anim.flicker(original, flows_fwd)
    flicker_hd = anim.flicker(hd, flows_fwd)

    w, h = size[0] * TIER, size[1] * TIER
    frames = []
    for o, d in zip(original, hd):
        # Original: nearest to TIER x; HD: the TIER x tier (area-downscaled master).
        o3 = cv2.resize(anim._over(o), (w, h), interpolation=cv2.INTER_NEAREST)
        d3 = cv2.resize(anim._over(d), (w, h), interpolation=cv2.INTER_AREA)
        gap = np.tile(anim.BACKGROUND, (h, 12, 1))
        frames.append(Image.fromarray((np.clip(np.hstack([o3, gap, d3]), 0, 1) * 255).astype(np.uint8)))
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = int(1000 / max(frm.fps or 10, 1))
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=duration, loop=0, lossless=True)
    return {"frames": len(frames), "shimmer": flicker_hd / flicker_orig if flicker_orig > 0 else None}


def _map_area(path: Path) -> np.ndarray:
    im = np.asarray(Image.open(path).convert("L"))
    return im[: im.shape[0] - BAR_ROWS]


def hd_still_after_setup(run_dir: Path, name: str) -> bool | None:
    """tour.ps1's rule for a view's HD still: a back-buffer dump after the first
    map self-test (which runs only once the capture is set up: map, elevation,
    time of day, camera) and before Ctrl+F8. The result's
    `hd_still_after_setup`; runs from before that field: the view's debug log."""
    result = run_dir / f"{name}.json"
    if result.exists():
        r = json.loads(result.read_text(encoding="utf-8-sig"))
        if "hd_still_after_setup" in r:
            return bool(r["hd_still_after_setup"])
    log = run_dir / f"{name}.debug.log"
    if not log.exists():
        return None
    state, last, before, seen = 0, {}, -1, False
    for line in log.read_text(encoding="latin-1").splitlines():
        if "HD: " not in line:
            continue
        if state == 0 and not seen and re.search(r"map self-test '[^']+' 1x: ", line):
            seen, before = True, last.get(0, -1)
        m = re.search(r"present self-test 'auto-(\d+)': (PASS|FAIL)", line)
        if m:
            last[state] = max(last.get(state, -1), int(m.group(1)))
        if "HD: injecting key 357" in line:
            state = 1
    return seen and 0 in last and last[0] > before


def _selftest_numbers(hd_last: str | None) -> tuple[str, str] | None:
    """(PSNR, brightness) of a view's last map self-test 'hd' line."""
    m = re.search(r"PSNR ([\d.]+) dB.*?brightness ([+-]?[\d.]+)%", hd_last or "")
    return (m.group(1), m.group(2)) if m else None


def still_change(a: Path, b: Path) -> float | None:
    """Share of the map area that differs between two stills (at 1/DIFF_SCALE)."""
    if not a.exists() or not b.exists():
        return None
    x, y = _map_area(a), _map_area(b)
    if x.shape != y.shape:
        return 1.0
    size = (x.shape[1] // DIFF_SCALE, x.shape[0] // DIFF_SCALE)
    x = cv2.resize(x, size, interpolation=cv2.INTER_AREA).astype(np.int16)
    y = cv2.resize(y, size, interpolation=cv2.INTER_AREA).astype(np.int16)
    return float((np.abs(x - y) > DIFF_LEVEL).mean())


def _thumb(src: Path, dst: Path, width: int = 860, crop: tuple[int, int, int, int] | None = None) -> bool:
    """`crop`: (left, top, right, bottom) of the still; default: all but the
    interface bar."""
    if not src.exists():
        return False
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return True
    im = Image.open(src).convert("RGB")
    im = im.crop(crop or (0, 0, im.width, im.height - BAR_ROWS))
    im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, quality=88)
    return True


COVERAGE_LINE = re.compile(r"tick (\d+) window (\d+) at (-?\d+),(-?\d+) size (\d+)x(\d+):(.*?)\| live ops (\d+)")
TEXT_OPS = re.compile(r"\| text ops (\d+)")  # P7b.5: text draws that passed the 1x check
COVERAGE_KINDS = ["art", "art-loose", "text", "window-copy", "buffer-copy", "fill", "map", "unknown", "overwritten"]
TEXT_STYLE_PASSES = ("-smooth", "-sharp")
STILL_NAME = re.compile(r"ui-hd(-[a-z]+)?-(\d+)\.png$")


def _coverage_rows(path: Path) -> list[dict]:
    """ui_coverage.txt: per window (id, place and size), the tick where
    replayable art explained the most of it."""
    best: dict[tuple, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = COVERAGE_LINE.match(line)
        if not m:
            continue
        shares = {k: float(v) for k, v in re.findall(r"([a-z-]+) ([\d.]+)%", m.group(7))}
        row = {"tick": int(m.group(1)), "window": int(m.group(2)), "x": int(m.group(3)), "y": int(m.group(4)),
               "w": int(m.group(5)), "h": int(m.group(6)), "ops": int(m.group(8)), "shares": shares,
               "text_ops": int(t.group(1)) if (t := TEXT_OPS.search(line)) else 0,
               "frms": line.split("| frms", 1)[1].strip() if "| frms" in line else ""}
        key = (row["window"], row["x"], row["y"], row["w"], row["h"])
        if key not in best or shares.get("art", 0.0) > best[key]["shares"].get("art", 0.0):
            best[key] = row
    return sorted(best.values(), key=lambda r: (r["tick"], r["window"]))


def _bench_section(bench: list[dict], e) -> list[str]:
    """P7a.6: the combat benchmark's scripted play runs (fps per 120 frames)."""
    if not bench:
        return []
    parts = ["<h2>Combat benchmark (P7a.6)</h2>",
             "<p>arcaves.map (where combat dipped to 48–49 fps in the author's QA session), 60 s of the real game loop at 3440×1440 "
             "with the HD art and HD UI: standing still, and entering combat and ending turns over and over; “-orig” is the combat run with CE's own renderer "
             "(no GPU map, no HD UI) through the same presenter. fps per 120 frames (about 2 s) and each window's longest frame, the first two windows (loading) left out.</p>"]
    for r in sorted(bench, key=lambda r: r["name"]):
        # The first two windows are the map loading (and an uncapped black screen): warm-up.
        fps = [f for f in (r.get("fps") or [])[2:] if f > 0]
        if not fps:
            parts.append(f"<div class='box'><b>{e(r['name'])}</b>: <span class='bad'>no fps lines</span></div>")
            continue
        ordered = sorted(fps)
        low = sum(1 for f in fps if f < 57)
        parts.append(f"<div class='box'><b>{e(r['name'])}</b>: {len(fps)} windows, median {ordered[len(ordered) // 2]:.1f} fps, "
                     f"lowest {ordered[0]:.1f}, <span class='{'bad' if low else 'ok'}'>{low} below 57</span>"
                     f"<br><span class='dim'>{e(' '.join(f'{f:.0f}' for f in fps))}</span>")
        longest = (r.get("longest_ms") or [])[2:]
        if longest:
            parts.append(f"<br><span class='dim'>longest frame per window (ms): {e(' '.join(f'{m:.0f}' for m in longest))}</span>")
        for line in (r.get("gpu") or [])[:4]:
            parts.append(f"<br><span class='dim'>{e(line)}</span>")
        parts.append("</div>")
    return parts


def _missing_section(run_dir: Path, e) -> list[str]:
    """P7a.3: FRMs the tour's games drew without an HD pack (<view>.art_missing.txt,
    the game's hd_art_missing.txt of that run), by folder, with the views they were in."""
    views_of: dict[str, list[str]] = {}
    for path in sorted(run_dir.glob("*.art_missing.txt")):
        view = path.name[: -len(".art_missing.txt")]
        for name in path.read_text(encoding="utf-8", errors="replace").split():
            views_of.setdefault(name.replace("/", "\\").lower(), []).append(view)
    parts = ["<h2>Art without packs (P7a.3)</h2>"]
    if not views_of:
        parts.append("<p class='ok'>Every FRM the tour's games drew had an HD pack (or no view has reported yet).</p>")
        return parts
    folders: dict[str, list[str]] = {}
    for name in views_of:
        folders.setdefault(name.rpartition("\\")[0], []).append(name)
    parts.append(f"<p>{len(views_of):,} FRMs drawn without a pack in {len({v for vs in views_of.values() for v in vs})} views "
                 "(drawn at 1× through the GPU upscale instead). Critters stay on this list until the batch has packed them.</p>")
    parts.append("<table><tr><th>Folder</th><th class='num'>FRMs</th><th>Most seen (views)</th></tr>")
    for folder, names in sorted(folders.items(), key=lambda kv: -len(kv[1])):
        top = sorted(names, key=lambda n: -len(views_of[n]))[:12]
        parts.append(f"<tr><td>{e(folder)}</td><td class='num'>{len(names):,}</td>"
                     f"<td class='dim'>{e(', '.join(f'{n.rpartition(chr(92))[2]} ({len(views_of[n])})' for n in top))}</td></tr>")
    parts.append("</table>")
    return parts


def _ui_section(run_dir: Path, out: Path, e, extra: bool = False) -> list[str]:
    """The UI walk of a run. extra: a ui_test.ps1 run shown next to the tour's own walk
    (its own heading, thumbnails prefixed with the run name so they don't collide)."""
    walk_path = run_dir / "ui_walk.json"
    if not walk_path.exists():
        return []
    walk = json.loads(walk_path.read_text(encoding="utf-8-sig"))
    if extra:
        names = ", ".join(n for n, p in walk.items() if isinstance(p, dict))
        parts = [f"<h2>UI walk {e(run_dir.name)}</h2>",
                 f"<p>A walk of its own (<code>ui_test.ps1</code>), passes {e(names)}. Hover a picture to see the original.</p>"]
    else:
        parts = ["<h2>UI walk (P7b)</h2>",
                 "<p>Save slot 1 loaded (only loaded), then inventory, character screen, automap, options, Skilldex, Pip-Boy, help, the save and load screens and the preferences; "
                 "in runs of their own a dialogue with the nearest critter that has one, and the world map. "
                 "Each once with the HD UI and once with the original art. Hover a picture to see the original.</p>"]
    prefix = f"{run_dir.name}-" if extra else ""
    for name, p in walk.items():
        if not isinstance(p, dict):
            continue
        replay = "no replay self-tests" if not p.get("replay_runs") else (
            f"<span class='{'bad' if p.get('replay_failed') else 'ok'}'>{p['replay_runs']} replay self-tests, {p.get('replay_failed', 0)} failed</span>")
        hd = "" if not p.get("hd_runs") else (
            f", <span class='{'bad' if p.get('hd_failed') else 'ok'}'>{p['hd_runs']} HD self-tests, {p.get('hd_failed', 0)} failed</span>")
        parts.append(f"<div class='box'><b>{e(name)}</b>: {p.get('coverage_ticks', 0)} coverage samples, {replay}{hd}, {len(p.get('stills', []))} stills")
        for line in (p.get("replay_first_fail") or [])[:1]:
            parts.append(f"<br><span class='bad'>{e(line)}</span>")
        for line in (p.get("hd_lines") or [])[-2:]:
            parts.append(f"<br><span class='{'bad' if ': FAIL' in line else 'dim'}'>{e(line)}</span>")
        for line in (p.get("ui_layer") or [])[:6]:
            parts.append(f"<br><span class='dim'>{e(line)}</span>")
        for line in (p.get("ui_text") or [])[:4]:  # P7b.5: atlas load, the 1x check counts
            parts.append(f"<br><span class='dim'>{e(line)}</span>")
        parts.append("</div>")

    for coverage in sorted(run_dir.glob("ui_coverage*.txt")):
        rows = _coverage_rows(coverage)
        walk_name = coverage.stem.partition("-")[2] or "hd"
        parts.append(f"<h3>Coverage per window ({e(walk_name)})</h3><p class='dim'>Share of each window's pixels by what wrote them last, "
                     "at the moment replayable art explained the most of it. “art” is what the HD UI can draw today.</p>")
        parts.append("<table><tr><th>Window</th><th>Size</th>" + "".join(f"<th class='num'>{k}</th>" for k in COVERAGE_KINDS)
                     + "<th class='num'>ops</th><th class='num'>text ops</th><th>Most blitted art</th></tr>")
        for r in rows:
            cells = "".join(f"<td class='num'>{r['shares'].get(k, 0.0):.1f}%</td>" for k in COVERAGE_KINDS)
            parts.append(f"<tr><td>{r['window']} at {r['x']},{r['y']}</td><td>{r['w']}×{r['h']}</td>{cells}"
                         f"<td class='num'>{r['ops']}</td><td class='num'>{r['text_ops']}</td><td class='dim'>{e(r['frms'][:160])}</td></tr>")
        parts.append("</table>")

    # ui-hd-NNN.png pairs with ui-orig-NNN.png, ui-hd-talk-NNN.png with ui-orig-talk-NNN.png.
    hd_stills = sorted(p for p in run_dir.glob("ui-hd*.png") if STILL_NAME.match(p.name))
    if hd_stills:
        parts.append("<h3>Stills</h3><p class='dim'>The middle 1920 pixels (where the 640-wide screens sit at 3×), "
                     "half size; click for the full frame.</p><div class='grid'>")
        for hd in hd_stills:
            m = STILL_NAME.match(hd.name)
            walk_suffix, number = m.group(1) or "", m.group(2)
            # The text-style passes (P7b.5) walk the same screens as "hd".
            orig = run_dir / f"ui-orig{'' if walk_suffix in TEXT_STYLE_PASSES else walk_suffix}-{number}.png"
            with Image.open(hd) as im:
                width, height = im.size
            left = max(0, (width - 1920) // 2)
            crop = (left, 0, min(width, left + 1920), height)
            stem = f"{walk_suffix.lstrip('-') or 'screens'}-{number}"
            th, to = out / "ui" / f"{prefix}hd-{stem}.jpg", out / "ui" / f"{prefix}orig-{stem}.jpg"
            has_hd, has_orig = _thumb(hd, th, 960, crop), _thumb(orig, to, 960, crop)
            img = f"<a class='flip' href='../tour/{e(run_dir.name)}/{e(hd.name)}'><img src='ui/{th.name}' loading='lazy'>"
            img += f"<img class='o' src='ui/{to.name}' loading='lazy'></a>" if has_orig and has_hd else "</a>"
            parts.append(f"<figure style='width:960px'>{img}<figcaption>{e(stem)}{'' if has_orig else ' (no original to compare)'}</figcaption></figure>")
        parts.append("</div>")
    return parts


def build(paths: Paths, files: GameFiles, tour_run: str | None = None, baseline: str | None = None, gallery: bool = True,
          ui_runs: list[str] | tuple[str, ...] = ()) -> Path:
    reports = paths.reports
    out = reports / "phase7"
    out.mkdir(parents=True, exist_ok=True)
    data = paths.data_root
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Phase 7 progress</title><style>",
        ":root{--bg:#1b1a17;--panel:#25231f;--ink:#e4dfd4;--dim:#a39d90;--accent:#d8a14a;--ok:#8fd18a;--bad:#e27d6a}",
        "body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1400px;padding:16px}",
        "h1{font-size:26px;margin:8px 0}h2{font-size:20px;margin:32px 0 8px;color:var(--accent)}h3{font-size:16px;margin:18px 0 6px}",
        ".box{background:var(--panel);border-radius:8px;padding:12px 16px;margin:12px 0}",
        "table{border-collapse:collapse;margin:8px 0;display:block;overflow-x:auto}td,th{border-bottom:1px solid #3a372f;padding:4px 10px;text-align:left;vertical-align:top}",
        "th{color:var(--dim);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}",
        ".ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--accent)}.dim{color:var(--dim)}",
        ".grid{display:flex;flex-wrap:wrap;gap:14px}.grid figure{margin:0;background:var(--panel);padding:8px;border-radius:8px}",
        "figure img{display:block;max-width:100%;image-rendering:auto}figcaption{color:var(--dim);font-size:13px;margin-top:4px}",
        ".flip{position:relative;display:block}.flip img.o{position:absolute;inset:0;opacity:0}.flip:hover img.o{opacity:1}",
        "pre{white-space:pre-wrap;font-size:12px;color:var(--dim)}",
        "</style></head><body>",
        "<h1>Dustlight — Phase 7 progress</h1>",
        "<p class='dim'>Generated by <code>fo2hd report7</code>; rerun it any time, every section shows what exists so far.</p>",
    ]

    # Batch.
    bulk = data / "bulk"
    parts.append("<h2>Art batch</h2><table><tr><th>Step</th><th class='num'>Done</th><th class='num'>Of</th></tr>")
    for label, db, stage, manifest in (
        ("Roofs (map art manifest, re-bake)", "jobs.sqlite", "hdassets", "manifest_tiles_walls_scenery_items_misc.json"),
        ("UI art masters", "jobs.sqlite", "hdassets", "manifest_ui.json"),
        ("UI packs (what the game loads)", "jobs_pack.sqlite", "pack", "manifest_ui.json"),
        ("Critter masters", "jobs.sqlite", "hdassets", "manifest_critters.json"),
        ("Critter packs (what the game loads)", "jobs_pack.sqlite", "pack", "manifest_critters.json"),
    ):
        done, total = _manifest_progress(data / db, stage, bulk / manifest)
        parts.append(f"<tr><td>{e(label)}</td><td class='num'>{done:,}</td><td class='num'>{total:,}</td></tr>")
    parts.append("</table>")
    log = bulk / "p7_batch.log"
    if log.exists():
        tail = log.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-8:]
        parts.append(f"<pre>{e(chr(10).join(tail))}</pre>")

    # Map scan.
    scan_path = data / "maps" / "scan.json"
    if scan_path.exists():
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        roofs = json.loads((data / "maps" / "roof_tiles.json").read_text(encoding="utf-8"))
        tour = json.loads((data / "maps" / "tour.json").read_text(encoding="utf-8"))
        elevations = sum(len(m["elevations"]) for m in scan["maps"])
        parts.append("<h2>Map scan</h2>")
        parts.append(f"<p>{len(scan['maps'])} map files ({sum(m['listed'] for m in scan['maps'])} listed in maps.txt), {elevations} elevations, "
                     f"{len(scan['tiles']):,} different floor/roof tiles in use, {len(roofs):,} of them drawn mostly as roofs (until now only 29 were treated as roofs). "
                     f"The capture tour has {len(tour)} views: the entrance and up to two of the busiest spots of every elevation.</p>")

    # Critter gallery.
    manifest_path = bulk / "manifest_critters.json"
    review = json.loads(manifest_path.read_text(encoding="utf-8")).get("review_first", []) if manifest_path.exists() else []
    walks = [p for p in review if p.endswith("ab.frm")]
    if gallery and walks:
        parts.append("<h2>Critters</h2><p>Walk cycles at your 3× scale: the original on the left, HD on the right. "
                     "“Shimmer” is the flicker the motion doesn't explain, relative to the original (1.0 = as calm as the original pixel art).</p><div class='grid'>")
        shown = 0
        for game_path in walks:
            name = game_path.split("\\")[-1][:-4]
            result = _walk_webp(files, paths, game_path, out / "critters" / f"{name}.webp")
            if result is None:
                continue
            shown += 1
            shimmer = f", shimmer {result['shimmer']:.2f}" if result["shimmer"] else ""
            parts.append(f"<figure><img src='critters/{name}.webp' alt='{e(name)}'><figcaption>{e(name)}{shimmer}</figcaption></figure>")
        parts.append("</div>")
        if shown < len(walks):
            parts.append(f"<p class='dim'>{len(walks) - shown} more appear when the batch has built them.</p>")

    # Tour.
    tour_root = reports / "tour"
    # Default: the latest tour run (ui_test.ps1's ui-* runs only walk the UI).
    runs = sorted(d.name for d in tour_root.iterdir() if d.is_dir() and not d.name.startswith("ui-")) if tour_root.exists() else []
    run = tour_run or (runs[-1] if runs else None)
    if run and (tour_root / run).exists():
        run_dir = tour_root / run
        base_dir = tour_root / baseline if baseline else None
        parts += _ui_section(run_dir, out, e)
        for ui_run in ui_runs:
            parts += _ui_section(tour_root / ui_run, out, e, extra=True)
        results = [json.loads(p.read_text(encoding="utf-8-sig")) for p in sorted(run_dir.glob("*.json")) if p.name != "ui_walk.json"]
        bench = [r for r in results if r["name"].startswith("bench-")]
        results = [r for r in results if not r["name"].startswith("bench-")]
        parts += _bench_section(bench, e)
        checks = sum(r["checks"] for r in results)
        failed = [r for r in results if r["failed"]]
        empty = [r for r in results if r["checks"] == 0]
        explained_path = tour_root / "explained.json"
        explained = json.loads(explained_path.read_text(encoding="utf-8")) if explained_path.exists() else {}
        open_fails = [r for r in failed if r["name"] not in explained]
        parts.append(f"<h2>Capture tour ({e(run)})</h2>")
        parts.append(f"<p>{len(results)} views, {checks:,} checks, <span class='{'bad' if open_fails else ('warn' if failed else 'ok')}'>{len(failed)} views with failed checks"
                     f"{f' ({len(failed) - len(open_fails)} investigated and explained below the picture)' if len(open_fails) < len(failed) else ''}</span>, "
                     f"{len(empty)} without checks. Hover a picture to see the original game.</p>")
        rows = []
        counts = {"hd": 0, "scene": 0, "base_early": 0, "early": 0}
        # Open failures first, then the explained ones, then the rest by name.
        for r in sorted(results, key=lambda r: (not (r["failed"] and r["name"] not in explained), not r["failed"], r["name"])):
            hd = run_dir / f"{r['name']}-hd.png"
            orig = run_dir / f"{r['name']}-orig.png"
            th, to = out / "tour" / f"{r['name']}-hd.jpg", out / "tour" / f"{r['name']}-orig.jpg"
            has_hd, has_orig = _thumb(hd, th), _thumb(orig, to)
            # A still from before the capture setup (tour.ps1 retries those since
            # 2026-11-03) shows another moment, often another spot: not compared.
            early = hd_still_after_setup(run_dir, r["name"]) is False
            base_early = base_dir is not None and hd_still_after_setup(base_dir, r["name"]) is False
            change = still_change(hd, base_dir / hd.name) if base_dir and not early and not base_early else None
            orig_change = still_change(orig, base_dir / orig.name) if change is not None else None
            flag = change is not None and change > DIFF_FLAG
            # The original-art still changed too: critters walked, the scene isn't the same.
            scene = flag and orig_change is not None and orig_change > DIFF_FLAG / 4
            counts["hd"] += flag and not scene
            counts["scene"] += scene
            counts["base_early"] += base_early and not early
            counts["early"] += early
            note = explained.get(r["name"]) if r["failed"] else None
            status = ("warn" if note else "bad") if r["failed"] or (flag and not scene) else ("warn" if scene or early else "ok")
            caption = f"{r['name']}: {r['checks']} checks, {r['failed']} failed"
            if early:
                caption += "; HD still from before the capture setup"
            elif base_early:
                caption += f", not compared: the {baseline} HD still is from before the capture setup"
                now, then = _selftest_numbers(r.get("hd_last")), None
                base_result = base_dir / f"{r['name']}.json"
                if base_result.exists():
                    then = _selftest_numbers(json.loads(base_result.read_text(encoding="utf-8-sig")).get("hd_last"))
                if now and then:
                    caption += (f" (map self-test as in {baseline}: PSNR {now[0]} dB, brightness {now[1]}%)" if now == then else
                                f" (map self-test: PSNR {then[0]} -> {now[0]} dB, brightness {then[1]} -> {now[1]}%)")
            elif change is not None:
                caption += f", {100 * change:.1f}% changed since {baseline}"
                if scene:
                    caption += f" (original art {100 * orig_change:.1f}%: the scene itself differs)"
            if r.get("art_missing"):
                caption += f"; art without packs: {r['art_missing']}"
            img = ""
            if has_hd:
                img = f"<a class='flip' href='../tour/{e(run)}/{e(hd.name)}'><img src='tour/{th.name}' loading='lazy'>"
                img += f"<img class='o' src='tour/{to.name}' loading='lazy'></a>" if has_orig else "</a>"
            fails = "".join(f"<br><span class='bad'>{e(f)}</span>" for f in r.get("fails", [])[:3])
            if note:
                fails = f"<br><span class='warn'>Investigated: {e(note)}</span>" + "".join(
                    f"<br><span class='dim'>{e(f)}</span>" for f in r.get("fails", [])[:1])
            rows.append(f"<figure style='width:430px'>{img}<figcaption class='{status}'>{e(caption)}{fails}</figcaption></figure>")
        if base_dir:
            parts.append(f"<p>Against {e(baseline)} (map area at 1/{DIFF_SCALE} size, cells more than {DIFF_LEVEL} luma steps apart, flagged above "
                         f"{100 * DIFF_FLAG:.0f}%): <span class='{'bad' if counts['hd'] else 'ok'}'>{counts['hd']} views changed with the original-art still the same</span>, "
                         f"{counts['scene']} changed where the original-art still changed too (the scene differs: critters walked), "
                         f"{counts['base_early']} not compared (the {e(baseline)} HD still is from before the capture setup; their map self-test numbers are shown), "
                         f"{counts['early']} with this run's HD still from before the setup.</p>")
        parts.append("<div class='grid'>" + "".join(rows) + "</div>")
        parts += _missing_section(run_dir, e)

    parts.append("</body></html>")
    page = out / "index.html"
    page.write_text("\n".join(parts), encoding="utf-8")
    return page
