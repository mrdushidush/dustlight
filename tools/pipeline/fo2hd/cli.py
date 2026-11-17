"""fo2hd command line. Run `uv run fo2hd --help` from tools/pipeline."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from . import capture as capture_mod
from .config import load_paths
from .dat import GameFiles


def _paths(args):
    paths = load_paths()
    if args.data_root:
        paths = dataclasses.replace(paths, data_root=Path(args.data_root))
    return paths


def _default_model(args, paths, category: str | None = None) -> Path:
    """The model for a category: --category-model category=file, else
    --default-model (the release art, PLAN.md R2), else hdassets.DEFAULT_MODEL
    in <data_root>/models. Fonts count as "interface"."""
    for spec in getattr(args, "category_model", None) or []:
        cat, _, file = spec.partition("=")
        if category is not None and cat == category:
            return Path(file)
    if getattr(args, "default_model", None):
        return Path(args.default_model)
    from . import hdassets

    return paths.data_root / "models" / hdassets.DEFAULT_MODEL


def _game_dir(args) -> Path:
    return Path(args.game) if args.game else load_paths().working_copy


def _game_files(args) -> GameFiles:
    return GameFiles(_game_dir(args))


def cmd_replay(args) -> int:
    from .replay import replay

    paths = _paths(args)
    files = _game_files(args)
    targets = [Path(p) for p in args.captures] or sorted(paths.captures.glob("*.json"))
    if not targets:
        print(f"no captures in {paths.captures}")
        return 1

    failed = 0
    for target in targets:
        cap = capture_mod.load(target)
        result = replay(cap, files)
        diff = result.diff_pixels
        status = "PASS" if diff == 0 else "FAIL"
        counts = {}
        for d in cap.draws:
            counts[d["op"]] = counts.get(d["op"], 0) + 1
        ops = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if k != "pass")
        print(f"{status}  {target.name}: {diff} px differ of {result.captured.size} ({ops})")
        if diff != 0:
            failed += 1
            out = target.with_name(target.stem + ".replay-diff.png")
            rgb = cap.screen_rgb()
            side = np.concatenate([rgb[result.captured], rgb[result.replayed], np.where(result.diff_mask[..., None], [255, 0, 255], rgb[result.captured] // 3).astype(np.uint8)], axis=1)
            Image.fromarray(side).save(out)
            ys, xs = np.nonzero(result.diff_mask)
            print(f"      first differences at (x, y): {list(zip(xs[:5].tolist(), ys[:5].tolist()))}; see {out}")
    return 1 if failed else 0


def cmd_dat_list(args) -> int:
    files = _game_files(args)
    if args.layers:
        for p in files.layer_paths:
            print(p)
        return 0
    for path, layer in sorted(files.list().items()):
        print(f"{path}\t{layer.path}" if args.where else path)
    return 0


def cmd_art_diff(args) -> int:
    from . import artdiff
    from .config import ini_path, read_ini

    ini = read_ini()
    fo1 = ini["fo1"] if ini.has_section("fo1") else {}
    base = args.base or fo1.get("base_ini")
    if not base:
        raise SystemExit("art-diff: --base <ini or game folder> (or [fo1] base_ini in the ini)")
    base_path = Path(base)
    base_game = base_path if base_path.is_dir() else load_paths(ini_path(base_path)).working_copy
    base_art = Path(args.base_art or fo1.get("base_art") or base_game / "hd")
    if not (base_art / "art").is_dir():
        raise SystemExit(f"art-diff: no packs under {base_art}\\art")
    game = _game_files(args)
    print(f"game: {game.game_dir}\nbase: {base_game}\nbase art: {base_art}")
    result = artdiff.diff(game, GameFiles(base_game), base_art)
    out = Path(args.out) if args.out else _paths(args).data_root / "artdiff"
    manifest = artdiff.write(result, out)
    artdiff.report(result, manifest)
    print(f"-> {out}")
    return 0


def cmd_art_skeletons(args) -> int:
    import json

    from . import artdiff

    root = _paths(args).data_root
    manifest_path = Path(args.manifest) if args.manifest else root / "artdiff" / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else root / "skeleton"
    folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    written = artdiff.write_skeletons(manifest, _game_files(args), out, folders)
    print(f"art-skeletons: {written} written for {', '.join(folders)} -> {out}")
    return 0


def cmd_art_twins(args) -> int:
    import json

    from . import artdiff
    from .config import read_ini

    root = _paths(args).data_root
    ini = read_ini()
    base_art = Path(args.base_art or (ini["fo1"].get("base_art") if ini.has_section("fo1") else ""))
    twins = json.loads((Path(args.twins) if args.twins else root / "artdiff" / "twins.json").read_text(encoding="utf-8"))
    out = Path(args.out)
    done, skipped = artdiff.copy_twins(twins, base_art, out)
    print(f"art-twins: {done} packs copied from {base_art}, {skipped} already there -> {out}")
    return 0


def cmd_dat_extract(args) -> int:
    files = _game_files(args)
    data = files.read(args.path)
    Path(args.out).write_bytes(data)
    print(f"{args.path}: {len(data)} bytes -> {args.out}")
    return 0


def cmd_extract(args) -> int:
    from .jobs import JobDB
    from .stages import extract

    paths = _paths(args)
    jobs = JobDB(paths.data_root / "jobs.sqlite")
    written, skipped = extract(_game_files(args), paths, args.patterns, jobs)
    jobs.close()
    print(f"extract: {written} written, {skipped} already done -> {paths.data_root / 'extract'}")
    return 0


def cmd_decode(args) -> int:
    from .jobs import JobDB
    from .stages import decode

    paths = _paths(args)
    jobs = JobDB(paths.data_root / "jobs.sqlite")
    decoded, skipped = decode(_game_files(args), paths, args.patterns, jobs)
    jobs.close()
    print(f"decode: {decoded} decoded, {skipped} already done -> {paths.data_root / 'decoded'}")
    return 0


def cmd_sample(args) -> int:
    from . import sample
    from .jobs import JobDB
    from .stages import decode

    paths = _paths(args)
    files = _game_files(args)
    captures = [Path(p) for p in args.captures] or sorted(paths.captures.glob("*.json"))
    manifest = sample.build(captures, files)
    out = paths.data_root / "sample" / "manifest.json"
    sample.write(manifest, out)
    print(f"sample set from {len(captures)} captures -> {out}")
    for category, c in manifest["categories"].items():
        print(f"  {category:8} {c['files']:4} files {c['frames']:6} frames {c['pixels'] / 1e6:7.2f} Mpx")
    jobs = JobDB(paths.data_root / "jobs.sqlite")
    decoded, skipped = decode(files, paths, [a["path"] for a in manifest["assets"]], jobs)
    jobs.close()
    print(f"decode: {decoded} decoded, {skipped} already done")
    return 0


def cmd_manifest(args) -> int:
    import json

    from . import sample

    paths = _paths(args)
    known_path = paths.data_root / "sample" / "manifest.json"
    known = json.loads(known_path.read_text(encoding="utf-8")) if known_path.exists() else None
    roofs_path = paths.data_root / "maps" / "roof_tiles.json"
    roofs = json.loads(roofs_path.read_text(encoding="utf-8")) if roofs_path.exists() else None
    folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    manifest = sample.bulk(_game_files(args), folders, known, roofs)
    out = Path(args.out) if args.out else paths.data_root / "bulk" / f"manifest_{'_'.join(folders)}.json"
    sample.write(manifest, out)
    print(f"bulk manifest for {', '.join(folders)} -> {out}")
    for category, c in manifest["categories"].items():
        if c["files"]:
            print(f"  {category:8} {c['files']:5} files {c['frames']:7} frames {c['pixels'] / 1e6:7.2f} Mpx")
    return 0


def cmd_bakeoff(args) -> int:
    import json

    from . import bakeoff

    paths = _paths(args)
    manifest = json.loads((paths.data_root / "sample" / "manifest.json").read_text(encoding="utf-8"))
    models_dir = paths.data_root / "models"
    model_paths = [Path(p) for p in args.models] or sorted(p for p in models_dir.iterdir() if p.suffix in (".pth", ".safetensors", ".pt"))
    out = paths.reports / "bakeoff"
    print(f"bake-off: {len(model_paths)} models -> {out}")
    results = bakeoff.run(manifest, _game_files(args), model_paths, out)
    for name, m in results["models"].items():
        cats = "  ".join(f"{c}={s['fidelity']:.1f}/{s['sharpness']:.2f}" for c, s in m["categories"].items())
        print(f"  {name:32} {m['mpx_per_s']:6.2f} Mpx/s  {cats}")
    print(f"open {out / 'index.html'}")
    return 0


def cmd_release_bakeoff(args) -> int:
    import json

    from . import release_bakeoff

    paths = _paths(args)
    models_dir = paths.data_root / "models"
    licenses = {}
    for cand in (models_dir / "candidates.json", models_dir / "r1" / "candidates.json", models_dir / "r1b" / "candidates.json", models_dir / "mix" / "candidates.json"):
        if cand.exists():
            for c in json.loads(cand.read_text(encoding="utf-8")):
                licenses[c["name"]] = c["license"]
    model_paths = [Path(p) for p in args.models]
    results = release_bakeoff.run(paths, _game_files(args), model_paths, licenses, governor=_governor(args, paths), out_name=args.out_name)
    print(f"open {paths.reports / args.out_name / 'index.html'} ({len(results['models'])} models)")
    return 0


def cmd_release(args) -> int:
    from . import release

    paths = _paths(args)
    docs = []
    for spec in args.doc or []:
        src, _, arc = spec.partition("=")
        docs.append((Path(src), arc or Path(src).name))
    fields = {}
    for spec in args.field or []:
        key, _, value = spec.partition("=")
        # @file: the value is that file's text (long fields like the known issues)
        fields[key] = Path(value[1:]).read_text(encoding="utf-8").strip() if value.startswith("@") else value
    if not args.art_only and not (args.exe and args.ce_dat):
        raise SystemExit("release: --exe and --ce-dat are required (or --art-only)")
    art_files = []
    for spec in args.art_file or []:
        src, _, arc = spec.partition("=")
        art_files.append((Path(src), arc or Path(src).name))
    info = release.build(
        name=args.name,
        version=args.version,
        exe=Path(args.exe) if args.exe else None,
        exe_name=args.exe_name or (Path(args.exe).name if args.exe else ""),
        ce_dat=Path(args.ce_dat) if args.ce_dat else None,
        docs=docs,
        hdpack=Path(args.hdpack) if args.hdpack else paths.data_root / "hdpack",
        pristine=Path(args.pristine) if args.pristine else paths.pristine,
        art_only=args.art_only,
        art_prefix=args.art_prefix,
        art_label=args.art_label,
        art_files=art_files,
        out=Path(args.out),
        tiers=[] if args.tiers.strip().lower() == "none" else [int(t) for t in args.tiers.split(",")],
        max_part_bytes=int(float(args.max_part_gb) * 1e9),
        credits=release.load_credits(Path(args.art_credits)),
        fields=fields,
        page=Path(args.page) if args.page else None,
        docs_only=args.docs_only,
    )
    total = sum(f["bytes"] for f in info["files"])
    print(f"release {info['name']} {info['version']}: {len(info['files'])} zips, {total / 1e9:.2f} GB, {info['seconds']} s -> {args.out}")
    return 1 if info["over_limit"] else 0


def cmd_release_media(args) -> int:
    from . import release_media

    out = Path(args.out) if args.out else _paths(args).data_root / "reports" / "release-media"
    page = release_media.build(Path(args.run), Path(args.views), out, args.name)
    print(f"open {page}")
    return 0


def cmd_voice_lines(args) -> int:
    from . import voice_lines

    stats = voice_lines.build(_paths(args), _game_files(args))
    for source, st in stats.items():
        if isinstance(st, dict):
            print(f"  {source:9} {st['files']:4} files {st['entries']:6} entries {st['words']:7} words, {st['voiced']:5} with audio, {st['empty']:4} empty")
    print(f"-> {stats['db']}")
    return 0


def cmd_voice_scan(args) -> int:
    from . import voice_scripts

    r = voice_scripts.scan(_paths(args), _game_files(args))
    total, words = r["total"]
    print(f"dialogue entries labelled: {r['labelled'][0]} of {total} ({100 * r['labelled'][0] / total:.1f}%), {r['labelled'][1]} of {words} words")
    for label, (n, w) in r["per_label"].items():
        print(f"  {label:8} {n:6} entries {w:7} words")
    unresolved = {k: v for k, v in r["stats"].items() if k.startswith("unresolved")}
    print(f"  uses not resolved: {sum(unresolved.values())} ({', '.join(f'{k[11:]} {v}' for k, v in unresolved.items())})")
    return 0


def cmd_voice_speakers(args) -> int:
    from . import voice_speakers

    r = voice_speakers.build(_paths(args), _game_files(args))
    st = r["stats"]
    print(f"{st['maps']} maps; speaker rows: {st['owner rows']} (map {st['map rows']}, proto {st['proto rows']}, spawn {st['spawn rows']}, encounter {st['encounter rows']}, map/spatial script {st['mapscript rows']})")
    for label, counts in r["coverage"].items():
        total, words = sum(counts.values()), sum(r["words"][label].values())
        print(f"  {label}: {total} entries, {words} words")
        for cls, n in counts.most_common():
            w = r["words"][label][cls]
            print(f"    {cls:40} {n:6} entries ({100 * n / total:5.1f}%) {w:7} words ({100 * w / max(words, 1):5.1f}%)")
    print("  critter rows by kill type: " + ", ".join(f"{k} {n}" for k, n in r["kill_types"].items()))
    print(f"-> {r['db']} (table speakers)")
    return 0


def cmd_voice_report(args) -> int:
    from . import voice_report

    r = voice_report.build(_paths(args), _game_files(args))
    for what, v in r["counts"]["to_voice"].items():
        print(f"  to voice, {what:26} {v['entries']:6} entries {v['words']:7} words {v['hours']:5} h")
    for t in r["counts"]["towns"][:8]:
        print(f"  {t['town']:22} replies {t['reply_words']:6} words, floats {t['float_words']:6} words ({t['hours']} h)")
    print(f"-> {r['page']} ({r['sample']} lines to check; game text: local only)")
    return 0


def cmd_hdassets(args) -> int:
    import json
    import time

    from . import hdassets, sample
    from .jobs import JobDB

    paths = _paths(args)
    manifest_path = Path(args.manifest) if args.manifest else paths.data_root / "sample" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    models_dir = paths.data_root / "models"
    model_for = {c: _default_model(args, paths, c) for c in sample.CATEGORIES}
    for override in args.model or []:
        category, _, name = override.partition("=")
        model_for[category] = models_dir / name
    jobs = JobDB(paths.data_root / "jobs.sqlite")
    start = time.perf_counter()
    stats = hdassets.build(manifest, _game_files(args), paths, jobs, model_for, governor=_governor(args, paths))
    jobs.close()
    wall = time.perf_counter() - start
    stats["wall_seconds"] = round(wall, 1)
    (paths.data_root / "hd").mkdir(parents=True, exist_ok=True)
    (paths.data_root / "hd" / "last_run.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"hd assets: {stats['assets']} built, {stats['skipped']} already done, {stats['frames']} frames in {wall:.0f}s "
          f"(model {stats['seconds']:.0f}s for {stats['model_input_px'] / 1e6:.2f} Mpx incl. padding)")
    return 0


def cmd_pack(args) -> int:
    import json
    import random
    import time

    from . import pack
    from .jobs import JobDB

    paths = _paths(args)
    manifests = [Path(m) for m in args.manifest] if args.manifest else [paths.data_root / "sample" / "manifest.json"]
    assets: dict[str, dict] = {}
    for m in manifests:
        for asset in json.loads(m.read_text(encoding="utf-8"))["assets"]:
            assets.setdefault(asset["path"], asset)
    files = _game_files(args)
    # Own job DB: packing usually runs while hdassets is still writing jobs.sqlite.
    jobs = JobDB(paths.data_root / "jobs_pack.sqlite")
    start = time.perf_counter()
    stats = pack.build({"assets": list(assets.values())}, files, paths, jobs, governor=_governor(args, paths))
    jobs.close()
    stats["wall_seconds"] = round(time.perf_counter() - start, 1)
    if args.check:
        packed = [p for p in assets if (pack.pack_dir(paths, p) / "pack.json").exists()]
        random.Random(1).shuffle(packed)
        stats["roundtrip"] = pack.roundtrip_check(paths, packed[: args.check])
    out = paths.data_root / "hdpack" / "last_pack.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(json.dumps(stats, indent=1))
    return 0


def cmd_compose(args) -> int:
    import json
    import time

    from .compose import Compositor, original_equivalent

    paths = _paths(args)
    files = _game_files(args)
    targets = [Path(p) for p in args.captures] or sorted(paths.captures.glob("*.json"))
    out_root = paths.reports / "compose"
    summary = {}
    for target in targets:
        cap = capture_mod.load(target)
        out_dir = out_root / target.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        comp = Compositor(cap, files, paths)
        images = comp.render()
        seconds = time.perf_counter() - start
        for name, image in images.items():
            Image.fromarray(image).save(out_dir / f"{name}.png")
        equiv = original_equivalent(cap, files, paths)
        Image.fromarray(equiv.pop("image")).save(out_dir / "orig_equiv.png")
        info = {
            "seconds": round(seconds, 1),
            "hd_assets_used": len(comp.assets.hd_used),
            "fallback_assets": sorted(comp.assets.fallbacks),
            "original_equivalent": equiv,
            "lighting": comp.stats,
        }
        (out_dir / "info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        summary[target.stem] = info
        print(f"{target.stem}: {seconds:.1f}s, {info['hd_assets_used']} HD assets, {len(info['fallback_assets'])} fallbacks, "
              f"original-equivalent mean |diff| {equiv['mean_abs']} (p99 {equiv['p99_abs']}, PSNR {equiv['psnr']} dB), lights {comp.stats['lights']}")
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"-> {out_root}")
    return 0


def cmd_anim(args) -> int:
    from . import anim

    paths = _paths(args)
    out = paths.reports / "anim"
    results = anim.run(_game_files(args), paths, out)
    print("flicker relative to the original (lower = calmer):")
    for name, value in results["mean"].items():
        per = " ".join(f"{r[name]:.2f}" for r in results["rotations"].values())
        print(f"  {name:11} {value:.2f}   per direction: {per}")
    print(f"-> {out}")
    return 0


def cmd_report(args) -> int:
    import json

    from . import hdassets, report

    paths = _paths(args)
    files = _game_files(args)
    manifest = json.loads((paths.data_root / "sample" / "manifest.json").read_text(encoding="utf-8"))
    checks = hdassets.map_checks(manifest, paths, files)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else {}
    notes.setdefault("checks", {})
    notes["checks"]["P1.7 floor normals vs straight up (mean / worst asset)"] = (
        f"{checks['floor_normal_mean_angle_from_up_deg']} deg / {checks['floor_normal_max_mean_angle_deg']} deg (detail relief only)")
    notes["checks"]["P1.7 wall normals |z| (0 = vertical wall)"] = f"{checks['wall_normal_mean_abs_z']}"
    notes["checks"]["P1.7 cycle/emissive masks outside palette ranges 229-254"] = f"{checks['cycle_mask_pixels_outside_ranges']} px (of {checks['emissive_pixels']} emissive px)"
    out = report.build(paths, files, notes)
    print(f"-> {out}")
    return 0


def cmd_report3(args) -> int:
    import json

    from . import report3

    paths = _paths(args)
    files = _game_files(args)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else {}
    out = report3.build(paths, files, notes)
    print(f"-> {out}")
    return 0


def cmd_report4(args) -> int:
    import json

    from . import report4

    paths = _paths(args)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else {}
    out = report4.build(paths, notes)
    print(f"-> {out}")
    return 0


def cmd_report5(args) -> int:
    import json

    from . import report5

    paths = _paths(args)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else {}
    out = report5.build(paths, notes)
    print(f"-> {out}")
    return 0


def cmd_report6(args) -> int:
    import json

    from . import report6

    paths = _paths(args)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else {}
    out = report6.build(paths, notes)
    print(f"-> {out}")
    return 0


def _governor(args, paths):
    """--adaptive: share the PC with its user (fo2hd.governor)."""
    if not getattr(args, "adaptive", False):
        return None
    from . import governor

    governor.lower_priority()
    governor.cap_vram()
    return governor.Governor(paths.data_root, report=lambda line: print(line, flush=True))


def cmd_retry_tiled(args) -> int:
    """Frames the batch had to upscale in tiles (they didn't fit the 8 GB VRAM
    cap, <data_root>/bulk/tiled.jsonl) built again in one piece with the cap
    at governor.RETRY_VRAM_CAP_GB, only while the PC is locked, then repacked.
    The list moves into bulk/retry_tiled.json first, so a killed run resumes;
    frames that still don't fit are tiled again and logged afresh."""
    import json
    import time

    from . import frm as frm_mod
    from . import governor, hdassets, pack
    from .jobs import JobDB
    from .upscale import BATCH_PIXELS, BUCKET, PAD

    paths = _paths(args)
    bulk = paths.data_root / "bulk"
    log = paths.data_root / hdassets.TILED_LOG
    files = _game_files(args)

    if args.log_larger_than:
        # Backfill: frames built before tiling was logged. Those whose padded
        # model input is larger than this went through the model alone.
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        logged = 0
        for asset in manifest["assets"]:
            frm = frm_mod.decode(files.read(asset["path"]))
            for rotation in range(frm_mod.ROTATIONS):
                if frm.data_offsets[rotation] in frm.data_offsets[:rotation]:
                    continue  # the same frames as an earlier rotation
                for index, frame in enumerate(frm.frames[rotation]):
                    h = -(-(frame.height + 2 * PAD) // BUCKET) * BUCKET
                    w = -(-(frame.width + 2 * PAD) // BUCKET) * BUCKET
                    if w * h > args.log_larger_than:
                        hdassets.log_tiled(paths, asset["path"], f"r{rotation}_f{index:03d}", (h, w), _default_model(args, paths, asset["category"]).name, reason="size")
                        logged += 1
        print(f"logged {logged} frames larger than {args.log_larger_than} px (padded) from {args.manifest}")
        return 0

    retry_file = bulk / "retry_tiled.json"
    retry = json.loads(retry_file.read_text(encoding="utf-8")) if retry_file.exists() else {"assets": [], "frames": []}
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []
    known = {a["path"] for a in retry["assets"]}
    new = sorted({e["path"] for e in entries} - known)
    if new:
        # The assets as the batch's manifests list them (category etc.).
        by_path = {}
        for m in sorted(bulk.glob("manifest_*.json")):
            for asset in json.loads(m.read_text(encoding="utf-8"))["assets"]:
                by_path.setdefault(asset["path"], asset)
        missing = [p for p in new if p not in by_path]
        if missing:
            print(f"not in any bulk manifest, skipped: {', '.join(missing[:10])}")
        added = [by_path[p] for p in new if p in by_path]
        # Their jobs are dropped once, when they join the list (not again on a resume).
        for db_name, stage in (("jobs.sqlite", "hdassets"), ("jobs_pack.sqlite", "pack")):
            jobs = JobDB(paths.data_root / db_name)
            for asset in added:
                jobs.forget(stage, asset["path"])
            jobs.commit()
            jobs.close()
        retry["assets"].extend(added)
    retry["frames"].extend(entries)
    retry_file.write_text(json.dumps(retry, indent=1), encoding="utf-8")
    if log.exists():
        log.unlink()  # its entries are in retry_tiled.json now
    if not retry["assets"]:
        print("nothing to retry")
        return 0

    governor.lower_priority()
    governor.cap_vram(governor.RETRY_VRAM_CAP_GB)
    gov = governor.Governor(paths.data_root, report=lambda line: print(line, flush=True), locked_only=True)
    print(f"retrying {len(retry['assets'])} assets ({len(retry['frames'])} logged frames) with {governor.RETRY_VRAM_CAP_GB:.0f} GB VRAM, "
          "only while the PC is locked", flush=True)
    models_dir = paths.data_root / "models"
    model_for = {asset["category"]: _default_model(args, paths, asset["category"]) for asset in retry["assets"]}
    manifest = {"assets": retry["assets"]}
    jobs = JobDB(paths.data_root / "jobs.sqlite")
    stats = hdassets.build(manifest, files, paths, jobs, model_for, governor=gov)
    jobs.close()
    jobs = JobDB(paths.data_root / "jobs_pack.sqlite")
    packed = pack.build(manifest, files, paths, jobs, governor=gov)
    jobs.close()
    done = bulk / f"retry_tiled.done-{time.strftime('%Y%m%d-%H%M%S')}.json"
    retry_file.replace(done)
    still = sum(1 for _ in log.open(encoding="utf-8")) if log.exists() else 0
    print(f"retry done: {stats['assets']} assets rebuilt, {packed.get('packed', 0)} packed; {still} frames still needed tiles "
          f"(logged again in {hdassets.TILED_LOG.as_posix()}); list kept as {done.name}")
    return 0


def cmd_report7(args) -> int:
    from . import report7

    page = report7.build(_paths(args), _game_files(args), tour_run=args.tour_run, baseline=args.baseline, gallery=not args.no_gallery,
                         ui_runs=args.ui_run)
    print(f"open {page}")
    return 0


def cmd_fonts(args) -> int:
    import torch

    from . import fonts, governor, hdassets

    # The upscaler runs on the CPU (the GPU belongs to the batch): idle
    # priority and a few threads, so the PC stays usable.
    governor.lower_priority(governor.IDLE_PRIORITY_CLASS)
    torch.set_num_threads(4)
    paths = _paths(args)
    model = None if args.no_model else _default_model(args, paths, "interface")
    index = fonts.build_atlases(paths, _game_files(args), model)
    print(f"-> {index}")
    return 0


def cmd_fonts_review(args) -> int:
    from . import fonts, hdassets

    paths = _paths(args)
    model = None if args.no_model else _default_model(args, paths, "interface")
    page = fonts.review(paths, _game_files(args), model)
    print(f"open {page}")
    return 0


def cmd_maps(args) -> int:
    import json

    from . import maps

    paths = _paths(args)
    result = maps.scan(_game_files(args), listed_only=args.listed)
    out = paths.data_root / "maps"
    out.mkdir(parents=True, exist_ok=True)
    (out / "scan.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    scenes = maps.tour(result)
    (out / "tour.json").write_text(json.dumps(scenes, indent=1), encoding="utf-8")
    roofs = maps.roof_tiles(result)
    (out / "roof_tiles.json").write_text(json.dumps(roofs, indent=1), encoding="utf-8")
    both = [p for p, u in result["tiles"].items() if u["roof"] and u["floor"]]
    elevations = sum(len(m["elevations"]) for m in result["maps"])
    print(f"maps: {len(result['maps'])} files ({sum(m['listed'] for m in result['maps'])} in maps.txt), {elevations} elevations")
    print(f"tiles used: {len(result['tiles'])}; as roofs (mostly): {len(roofs)}; used both ways: {len(both)}")
    print(f"tour: {len(scenes)} views -> {out / 'tour.json'}")
    return 0


def cmd_eye_bench(args) -> int:
    import subprocess
    import time

    from . import eye

    paths = _paths(args)
    run = Path(args.run) if Path(args.run).is_dir() else paths.reports / "tour" / args.run
    stills = [(run / f"{v}-hd.png", run / f"{v}-orig.png") for v in args.views.split(",")]
    missing = [str(p) for pair in stills for p in pair if not p.exists()]
    if missing:
        raise SystemExit("missing stills: " + ", ".join(missing))
    out = Path(args.out) if args.out else paths.reports / "eye" / ("bench-" + time.strftime("%Y%m%d-%H%M"))
    failed = 0
    for model in args.models:
        safe = model.replace("/", "_").replace("@", "_")
        try:
            result = eye.bench(model, stills, out / safe, cols=args.cols, rows=args.rows)
            print(eye.bench_summary(result), flush=True)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failed += 1
            print(f"{model}: FAILED: {exc}", flush=True)
            eye.unload_all()
    print(f"-> {out}")
    return 1 if failed else 0


def _eye_dirs(args):
    """The eye's folders. They live in the release data root (look pages, tour/p093-wide, artifact-crops)."""
    paths = _paths(args)
    lab = Path(args.lab) if args.lab else paths.reports / "art-lab"
    key = Path(args.key) if args.key else paths.data_root / "art-lab-data" / "eye" / "key"
    return paths, lab, key


def cmd_eye_key(args) -> int:
    from . import eye_key

    paths, lab, key = _eye_dirs(args)
    run = Path(args.run) if Path(args.run).is_dir() else paths.reports / "tour" / args.run
    summary = eye_key.build(lab, paths.reports / "artifact-crops", run, key, seed=args.seed)
    for name, counts in summary.items():
        print(f"{name:20s} {counts['full']:4d} items, {counts['core']:4d} in the core set")
    print(f"total {sum(c['full'] for c in summary.values())}, core {sum(c['core'] for c in summary.values())} -> {key}")
    return 0


def cmd_eye_contest(args) -> int:
    import subprocess

    from . import eye, eye_contest, eye_key

    paths, lab, key = _eye_dirs(args)
    items = eye_contest.select(eye_key.load_key(key), args.subset)
    if args.task:
        items = [i for i in items if i["task"] == args.task]
    runs = lab / "eye" / "runs"
    models = args.models or [c.key for c in eye_contest.CONTESTANTS]
    if args.dry:
        listing = eye_contest.lms_models()
        total = 0.0
        print(f"{args.subset} set: {len(items)} items "
              f"({sum(1 for i in items if i['task'] == 'vote')} votes, {sum(1 for i in items if i['task'] == 'defects')} defect checks)")
        for model in models:
            c = eye_contest.BY_KEY.get(model) or eye_contest.Contestant(model, args.split or 1, 8.0, "not in the E1 list")
            if args.split:
                c = eye_contest.Contestant(c.key, args.split, c.e0_seconds, c.note)
            calls = sum(c.split * c.split if i["task"] == "defects" else 1 for i in items)
            seconds = sum(eye_contest.call_seconds_estimate(i, c) for i in items) + 90  # + load
            total += seconds
            cfg_path, cfg = eye_contest.saved_config(model, listing)
            weights = listing.get(model, {}).get("path", "NOT IN lms ls")
            print(f"\n{model}  [{c.note}]\n  weights {weights}\n  config  {eye_contest.config_line(cfg)}"
                  f"\n  flags   --parallel 1{' ' + ' '.join(eye.LOAD_ARGS.get(model, ())) if model in eye.LOAD_ARGS else ''}"
                  f"\n  {calls} calls (defect crops cut {c.split}x{c.split}), ~{seconds / 60:.0f} min (rough, from E0 speeds)")
        print(f"\nall: ~{total / 3600:.1f} h")
        return 0
    failed = 0
    for model in models:
        c = eye_contest.BY_KEY.get(model)
        split = args.split or (c.split if c else 1)
        run_dir = runs / eye_contest.safe_name(model) / f"r{args.run}"
        try:
            eye_contest.run_model(model, items, run_dir, split=split, vote_ref=not args.no_ref,
                                  temperature=args.temperature, seed=args.run if args.temperature > 0 else None,
                                  any_hour=args.any_hour)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failed += 1
            print(f"{model}: FAILED: {exc}", flush=True)
            if not model.startswith("fake:"):
                eye.unload_all()
    return cmd_eye_score(args) or (1 if failed else 0)


def cmd_eye_patrol(args) -> int:
    import subprocess
    from collections import Counter

    from . import eye, eye_contest, eye_patrol

    paths, lab, _ = _eye_dirs(args)
    run = Path(args.run) if Path(args.run).is_dir() else paths.reports / "tour" / args.run
    name = run.name + (f"-{args.pair}" if args.pair else "")  # pair crops never mix with still crops
    items = eye_patrol.build_items(run, paths.data_root / "art-lab-data" / "eye" / "patrol" / name, pair=args.pair)
    if args.views:
        wanted = set(args.views.split(","))
        items = [i for i in items if i["view"] in wanted]
    out = lab / "eye" / "patrol" / name / eye_contest.safe_name(args.model)
    if not args.sheets_only:
        try:
            eye_contest.run_model(args.model, items, out, split=1, vote_ref=False, temperature=0.0, any_hour=args.any_hour)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"{args.model}: FAILED: {exc}", flush=True)
            if not args.model.startswith("fake:"):
                eye.unload_all()
            return 1
    answers = eye_contest.read_answers(out / "answers.jsonl")
    found = eye_patrol.flags(items, answers)
    every = len(found)
    found = eye_patrol.drop_skipped(items, found)
    written = eye_patrol.sheets(items, found, out / "sheets", min_severity=args.min_severity)
    flagged = len({f["crop"] for f in found})
    print(f"{len(answers)}/{len(items)} crops answered, {flagged} with flags, {len(found)} flags "
          f"({every - len(found)} dropped on characters, exit grids, markers, text); "
          f"by severity {dict(sorted(Counter(f['severity'] for f in found).items(), key=str))}; "
          f"by kind {dict(Counter(f['kind'] for f in found).most_common())}")
    print(f"-> {len(written)} sheets (severity >= {args.min_severity}) in {out / 'sheets'}")
    return 0


def cmd_eye_score(args) -> int:
    import json

    from . import eye_contest, eye_key

    paths, lab, key = _eye_dirs(args)
    items = eye_key.load_key(key)
    scores = eye_contest.score_all(lab / "eye" / "runs", items)
    out = lab / "eye"
    (out / "scorecard.json").write_text(json.dumps(scores, indent=1), encoding="utf-8")
    eye_contest.page(scores, items, out / "scorecard.html")
    for s in scores:
        d = s["author"]
        print(f"{s['model']:32s} {s['run']}: The author {d['score']} (n {d['n']}), planted "
              f"{s['planted_found_l1']['rate']}/{s['planted_found_l2']['rate']}, clean flagged {s['clean_flagged']['rate']}, "
              f"marks {s['marks_found']['rate']}, {s['seconds_per_call']} s/call")
    print(f"-> {out / 'scorecard.html'}")
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run

    return 0 if run(_paths(args), _game_dir(args)) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fo2hd", description=__doc__)
    parser.add_argument("--game", help="game folder (default: working_copy from hd.local.ini)")
    parser.add_argument("--data-root", help="output root (default: data_root from hd.local.ini)")
    parser.add_argument("--category-model", action="append", help="category=model file (critter, interface, floor, roof, wall, scenery, item, misc; fonts use interface's); overrides --default-model")
    parser.add_argument("--default-model", help="model file for every category without its own --model (default: <data_root>/models/4x-UltraSharpV2)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replay", help="re-rasterize captures from the FRMs and diff against the captured PNG")
    p.add_argument("captures", nargs="*", help="capture .json files (default: all in <data_root>/captures)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("dat-list", help="list every file the game can see, after archive precedence")
    p.add_argument("--layers", action="store_true", help="only the archives and folders, highest priority first")
    p.add_argument("--where", action="store_true", help="each file's archive or folder too (tab-separated)")
    p.set_defaults(func=cmd_dat_list)

    p = sub.add_parser("art-diff", help="P8.1: every art file a game on Fallout 2's files (Et Tu) draws, against the base game's packs -> build manifest")
    p.add_argument("--base", help="the base game: its machine-paths ini (working_copy) or game folder (default: [fo1] base_ini)")
    p.add_argument("--base-art", help="the base game's pack folder (default: [fo1] base_art, else <base game>\\hd)")
    p.add_argument("--out", help="output folder (default: <data_root>/artdiff)")
    p.set_defaults(func=cmd_art_diff)

    p = sub.add_parser("art-skeletons", help="P8.4: pack.json (layout + frame hashes, no atlases) for build-manifest files without any pack, for art_x16.py --source")
    p.add_argument("--manifest", help="art-diff's build manifest (default: <data_root>/artdiff/build_manifest.json)")
    p.add_argument("--folders", default="critters", help="comma-separated art folders (default: critters)")
    p.add_argument("--out", help="output folder (default: <data_root>/skeleton); packs go to <out>/art/<folder>/<name>")
    p.set_defaults(func=cmd_art_skeletons)

    p = sub.add_parser("art-twins", help="P8.4: copy each twin's base pack under its own name (atlases hard-linked)")
    p.add_argument("--out", required=True, help="the game's own art folder (packs go to <out>/art/...)")
    p.add_argument("--twins", help="art-diff's twins.json (default: <data_root>/artdiff/twins.json)")
    p.add_argument("--base-art", help="the base game's pack folder (default: [fo1] base_art)")
    p.set_defaults(func=cmd_art_twins)

    p = sub.add_parser("dat-extract", help="extract one file as the game sees it")
    p.add_argument("path")
    p.add_argument("out")
    p.set_defaults(func=cmd_dat_extract)

    p = sub.add_parser("extract", help="extract game files (resumable) to <data_root>/extract")
    p.add_argument("patterns", nargs="*", help="game-path wildcards, e.g. art/critters/* (default: everything)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("decode", help="decode FRMs (resumable) to <data_root>/decoded")
    p.add_argument("patterns", nargs="*", help="game-path wildcards (default: every FRM)")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("sample", help="P1.5: build the sample set from captures and decode it")
    p.add_argument("captures", nargs="*", help="capture .json files (default: all in <data_root>/captures)")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("bakeoff", help="P1.6: run candidate upscalers on sample assets -> HTML contact sheet")
    p.add_argument("models", nargs="*", help="model files (default: all in <data_root>/models)")
    p.set_defaults(func=cmd_bakeoff)

    p = sub.add_parser("release-bakeoff", help="R1: permissive upscalers through the production path -> local flip-viewer page")
    p.add_argument("models", nargs="+", help="model files (.pth/.safetensors, .chain, or .mix: a model per category), in the order of the page's keys")
    p.add_argument("--out-name", default="release-bakeoff", help="page folder under <data_root>/reports")
    p.add_argument("--adaptive", action="store_true", help="like hdassets --adaptive")
    p.set_defaults(func=cmd_release_bakeoff)

    p = sub.add_parser("release", help="R5: engine zip + per-tier art zips for players, with the release checks")
    p.add_argument("--name", required=True, help="release name (file names, pack markers)")
    p.add_argument("--version", required=True)
    p.add_argument("--exe", help="built engine exe (required unless --art-only)")
    p.add_argument("--exe-name", help="file name of the exe in the zip (default: as built)")
    p.add_argument("--ce-dat", help="built ce.dat (out/dist/ce.dat; required unless --art-only)")
    p.add_argument("--doc", action="append", help="source=name in the zip, e.g. docs/player/README.md=Relit-README.md")
    p.add_argument("--hdpack", help="runtime packs (default: <data_root>/hdpack)")
    p.add_argument("--out", required=True)
    p.add_argument("--tiers", default="2,3,4", help="art packs to zip, or none: an engine-only update (the art of an earlier release)")
    p.add_argument("--max-part-gb", default="4.0", help="largest zip (Nexus took 5 GB per file at last report)")
    p.add_argument("--art-credits", required=True, help="docs/player/art_credits_*.json: the models that made the art; the release refuses art from any other")
    p.add_argument("--field", action="append", help="key=value (or key=@file) for {key} in the docs, e.g. known_issues=@docs/player/known_issues.md")
    p.add_argument("--page", help="store page text (docs/player/nexus.md): filled and written next to the zips, in no zip")
    p.add_argument("--docs-only", action="store_true", help="only write the filled docs to --out, to read them before a build")
    p.add_argument("--art-only", action="store_true", help="P8.7: only the art zips of --hdpack (no engine zip, no docs), e.g. Fallout 1's art")
    p.add_argument("--art-prefix", default="hd", help="the art folder the zips unpack into (hd-fo1 for Fallout 1's art)")
    p.add_argument("--art-label", default="art", help="zip name part: <name>-<version>-<label>-<N>x.zip (art-fo1)")
    p.add_argument("--art-file", action="append", help="source=path under --art-prefix, in every tier's zip (painted_shadows.json)")
    p.add_argument("--pristine", help="game folder whose files nothing shipped may equal (default: [paths] pristine; Et Tu's folder for Fallout 1)")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("release-media", help="R7: side by sides and whole screens per place from a tour run -> local page to choose from")
    p.add_argument("--run", required=True, help="tour run folder of the views below (<data_root>/reports/tour/<run>)")
    p.add_argument("--views", required=True, help="tools/scripts/media_views.json")
    p.add_argument("--out", help="default: <data_root>/reports/release-media")
    p.add_argument("--name", default="Dustlight", help="the label over the HD side")
    p.set_defaults(func=cmd_release_media)

    p = sub.add_parser("voice-lines", help="V0.1: every dialogue, combat taunt and holodisk line -> <data_root>/voice/lines.sqlite")
    p.set_defaults(func=cmd_voice_lines)

    p = sub.add_parser("voice-scan", help="V0.2: label each dialogue line reply/option/float/display from the compiled scripts")
    p.set_defaults(func=cmd_voice_scan)

    p = sub.add_parser("voice-speakers", help="V0.3: who runs each script (map objects, protos, spawns) -> speakers table, coverage of the labelled lines")
    p.set_defaults(func=cmd_voice_speakers)

    p = sub.add_parser("voice-report", help="V0.4: counts (entries, words, hours per label and town) + the 50-line spot-check page (local only)")
    p.set_defaults(func=cmd_voice_report)

    p = sub.add_parser("manifest", help="P4: manifest of every FRM in some art folders, for a bulk hdassets run")
    p.add_argument("folders", help="comma-separated art folders, e.g. tiles,walls,scenery,items,misc")
    p.add_argument("--out", help="output file (default: <data_root>/bulk/manifest_<folders>.json)")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("hdassets", help="P1.7: albedo/normal/height/emissive/cycle maps for the sample set (or --manifest)")
    p.add_argument("--model", action="append", help="category=model file, e.g. floor=4x-Textures-GTAV-rgt-s-dither.pth")
    p.add_argument("--manifest", help="asset manifest (default: <data_root>/sample/manifest.json)")
    p.add_argument("--adaptive", action="store_true", help="low priority; half the GPU while the PC is in use, full while locked, paused while the game runs")
    p.set_defaults(func=cmd_hdassets)

    p = sub.add_parser("pack", help="P4.1: HD masters -> BC7 runtime packs (2x/3x/4x) in <data_root>/hdpack")
    p.add_argument("--manifest", action="append", help="asset manifest(s) (default: the sample set)")
    p.add_argument("--check", type=int, default=0, help="BC7 round-trip check on N random packed assets")
    p.add_argument("--adaptive", action="store_true", help="like hdassets --adaptive (texconv compresses on the GPU)")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("retry-tiled", help="P7: rebuild the frames that needed tiles (bulk/tiled.jsonl) in one piece with 15 GB VRAM, only while the PC is locked")
    p.add_argument("--log-larger-than", type=int, default=0, help="only log (backfill) the --manifest's frames whose padded model input is larger than this many px")
    p.add_argument("--manifest", help="with --log-larger-than: the asset manifest to scan")
    p.set_defaults(func=cmd_retry_tiled)

    p = sub.add_parser("compose", help="P1.9: render captures at 4x: original, HD with original light, HD lit")
    p.add_argument("captures", nargs="*", help="capture .json files (default: all in <data_root>/captures)")
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("anim", help="P1.8: walk-cycle stability GIFs and flicker metric")
    p.set_defaults(func=cmd_anim)

    p = sub.add_parser("report", help="P1.10: build the local go/no-go report page")
    p.add_argument("--notes", help="JSON with decisions, failures, next steps, extra checks")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("report3", help="P3.5: build the local Phase 3 report page")
    p.add_argument("--notes", help="JSON with decisions, summary, gaps, next steps")
    p.set_defaults(func=cmd_report3)

    p = sub.add_parser("report4", help="P4.6: build the local Phase 4 report page")
    p.add_argument("--notes", help="JSON with decisions, summary, provisional calls, gaps, next steps")
    p.set_defaults(func=cmd_report4)

    p = sub.add_parser("report5", help="P5.10: build the local Phase 5 report page")
    p.add_argument("--notes", help="JSON with decisions, summary, effect close-ups, settings, provisional calls, gaps, next steps")
    p.set_defaults(func=cmd_report5)

    p = sub.add_parser("report6", help="P6.7: build the local Phase 6 report page (animations, post effects, fades)")
    p.add_argument("--notes", help="JSON with decisions, summary, per-scene and per-effect texts, settings, provisional calls, gaps")
    p.set_defaults(func=cmd_report6)

    p = sub.add_parser("report7", help="P7: progress page (batch, map scan, critter walk cycles, capture tour)")
    p.add_argument("--tour-run", help="tour run folder name under <data_root>/reports/tour (default: the latest)")
    p.add_argument("--baseline", help="earlier tour run to diff the stills against")
    p.add_argument("--no-gallery", action="store_true", help="skip the critter walk cycles")
    p.add_argument("--ui-run", action="append", default=[], help="also show the UI walk of this run (ui_test.ps1), e.g. ui-snap2; repeatable")
    p.set_defaults(func=cmd_report7)

    p = sub.add_parser("maps", help="P7: scan every map file -> tile usage (floors/roofs) and the capture tour")
    p.add_argument("--listed", action="store_true", help="only the maps data\\maps.txt names (Fallout Et Tu: its own 86, not Fallout 2's leftovers)")
    p.set_defaults(func=cmd_maps)

    p = sub.add_parser("fonts-review", help="P7b.5: the game's AAF fonts at 3x today vs HD glyph candidates (local page)")
    p.add_argument("--no-model", action="store_true", help="skip the upscaler candidate (it runs on the CPU)")
    p.set_defaults(func=cmd_fonts_review)

    p = sub.add_parser("fonts", help="P7b.5: HD glyph atlases (upscaler, smooth, sharp at 2x/3x/4x) -> <data_root>/hdpack/fonts")
    p.add_argument("--no-model", action="store_true", help="skip the upscaler style (it runs on the CPU)")
    p.set_defaults(func=cmd_fonts)

    p = sub.add_parser("eye-bench", help="the eye, E0: speed + plumbing check of local vision models in LM Studio (parallel 1, the author's saved configs)")
    p.add_argument("models", nargs="+", help="exact LM Studio model keys (lms load fuzzy-matches a missing key: use `lms ls`)")
    p.add_argument("--run", default="p093-wide", help="tour run folder, or its name under <data_root>/reports/tour (-hd/-orig stills; p093-wide lives in the release data root)")
    p.add_argument("--views", default="klamall_e0_0,arcaves_e2_1,navarro_e0_1", help="comma-separated view names")
    p.add_argument("--cols", type=int, default=4, help="crop grid columns over the map (4 x 2 ~ 890x600 px: Qwen size; 8 x 4 for Gemma)")
    p.add_argument("--rows", type=int, default=2, help="crop grid rows")
    p.add_argument("--out", help="output folder (default <data_root>/reports/eye/bench-<time>)")
    p.set_defaults(func=cmd_eye_bench)

    def eye_dirs(p):
        p.add_argument("--lab", help="art-lab reports folder (default <data_root>/reports/art-lab; use the release data root)")
        p.add_argument("--key", help="answer key folder (default <data_root>/art-lab-data/eye/key)")

    p = sub.add_parser("eye-key", help="the eye, E1: build the answer key (the author's votes + marks, planted defects); CPU only")
    eye_dirs(p)
    p.add_argument("--run", default="p093-wide", help="tour run with clean -hd/-orig stills for the planted defects")
    p.add_argument("--seed", type=int, default=1112)
    p.set_defaults(func=cmd_eye_key)

    p = sub.add_parser("eye-contest", help="the eye, E1: models answer the key (GPU: The author approves configs and start), then eye-score")
    eye_dirs(p)
    p.add_argument("models", nargs="*", help="exact LM Studio keys (default: the E1 list); fake:oracle / fake:random check the scorer")
    p.add_argument("--subset", choices=["core", "full"], default="core", help="core ~270 items (all models), full ~720 (the best few)")
    p.add_argument("--run", type=int, default=1, help="run number: repeats size each model's error bar")
    p.add_argument("--temperature", type=float, default=0.0, help="0 for run 1; repeats use >0 with seed = run")
    p.add_argument("--split", type=int, help="cut defect crops split x split (default per model: Gemma 2)")
    p.add_argument("--no-ref", action="store_true", help="votes without the original as reference (as the author voted)")
    p.add_argument("--task", choices=["vote", "defects"], help="only this part of the key")
    p.add_argument("--any-hour", action="store_true", help="run 16:00-19:00 in May-September too (only when the author waives it)")
    p.add_argument("--dry", action="store_true", help="print models, the author's saved configs, calls and a time estimate; no GPU")
    p.set_defaults(func=cmd_eye_contest)

    p = sub.add_parser("eye-patrol", help="the eye, E2 trial: one model checks every 880x600 map crop of a tour run; review sheets")
    eye_dirs(p)
    p.add_argument("model", nargs="?", default="qwen3.6-35b-a3b-mtp@iq3_s", help="exact LM Studio key (default: the champion)")
    p.add_argument("--run", default="p093-wide", help="tour run with -hd/-orig stills")
    p.add_argument("--pair", help="crop a map self-test's same-frame pair instead of the stills, e.g. auto-000 "
                                  "(<view>-fail-auto-000-hd.png / -orig.png: no critter moves, no grain, no UI)")
    p.add_argument("--views", help="comma-separated view names (default: all)")
    p.add_argument("--min-severity", type=int, default=1, help="flags drawn on the review sheets from this severity up")
    p.add_argument("--sheets-only", action="store_true", help="no model: redraw the sheets from the answers so far")
    p.add_argument("--any-hour", action="store_true", help="run 16:00-19:00 in May-September too (only when the author waives it)")
    p.set_defaults(func=cmd_eye_patrol)

    p = sub.add_parser("eye-score", help="the eye, E1: score every contest run -> art-lab/eye/scorecard.html")
    eye_dirs(p)
    p.set_defaults(func=cmd_eye_score)

    p = sub.add_parser("selftest", help="P1.4 acceptance: compare reader/decoder with ce-dat-tool and ce-frm2png")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
