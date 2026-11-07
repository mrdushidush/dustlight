"""P1.4 acceptance: the Python reader/decoder against CE's own tools.

1. Every archive's file list equals `ce-dat-tool <dat> list`.
2. Sampled entries extract to the same bytes as `ce-dat-tool extract`.
3. Sampled FRM frames decode to the same palette indices as `ce-frm2png`.
4. A full extraction killed part-way resumes and ends complete and correct.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import frm as frm_mod
from .config import REPO_ROOT, Paths
from .dat import Dat2, GameFiles, ZipArchive, normalize

TOOLS = REPO_ROOT / "out" / "build" / "windows-x64" / "RelWithDebInfo"


def _run(*args) -> str:
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="cp1252")
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def check_lists(files: GameFiles, report) -> bool:
    ok = True
    for layer in files.layers:
        if not isinstance(layer, (Dat2, ZipArchive)):
            continue
        ours = sorted(e.path if isinstance(layer, Dat2) else e.filename.replace("/", "\\") for e in layer.entries.values())
        theirs = sorted(line for line in _run(TOOLS / "ce-dat-tool.exe", layer.path, "list").splitlines() if line)
        same = ours == theirs
        ok &= same
        report(f"  list {layer.path.name}: {len(ours)} entries, {'identical' if same else 'DIFFERENT'}")
        if not same:
            report(f"    only ours: {sorted(set(ours) - set(theirs))[:5]}, only ce-dat-tool: {sorted(set(theirs) - set(ours))[:5]}")
    return ok


def check_extract(files: GameFiles, report, per_archive: int = 40) -> bool:
    ok = True
    rng = random.Random(1)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for layer in files.layers:
            if not isinstance(layer, Dat2):
                continue
            sample = rng.sample(sorted(layer.entries.values(), key=lambda e: e.path), min(per_archive, len(layer.entries)))
            list_file = tmp / f"{layer.path.stem}.txt"
            list_file.write_text("\n".join(e.path for e in sample), encoding="cp1252")
            out = tmp / layer.path.stem
            _run(TOOLS / "ce-dat-tool.exe", layer.path, "extract", "--file-list", list_file, out)
            mismatches = 0
            for entry in sample:
                theirs = (out / entry.path.replace("\\", "/")).read_bytes()
                if theirs != layer.read(entry.path):
                    mismatches += 1
            ok &= mismatches == 0
            compressed = sum(e.compressed for e in sample)
            report(f"  extract {layer.path.name}: {len(sample)} sampled ({compressed} compressed), {mismatches} mismatches")
    return ok


def check_decode(files: GameFiles, report, count: int = 40) -> bool:
    rng = random.Random(2)
    visible = files.list()
    frms = sorted(p for p in visible if p.startswith("art\\") and p.endswith((".frm", ".fr0", ".fr3")))
    sample = rng.sample(frms, count)
    mismatches = 0
    frames_checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        palette = tmp / "color.pal"
        palette.write_bytes(files.read("color.pal"))
        for i, path in enumerate(sample):
            data = files.read(path)
            src = tmp / f"{i}{Path(path).suffix}"
            src.write_bytes(data)
            frm = frm_mod.decode(data)
            # First, middle and last frame of the first and last stored direction.
            rotations = sorted({0, max(range(6), key=lambda r: frm.data_offsets[r])})
            frames = sorted({0, frm.frame_count // 2, frm.frame_count - 1})
            for rotation in rotations:
                for f in frames:
                    out = tmp / f"{i}_{rotation}_{f}.png"
                    _run(TOOLS / "ce-frm2png.exe", src, out, "--from-frm", "--to-png", "--palette", palette, "--frame", f, "--direction", rotation)
                    theirs = np.asarray(Image.open(out))
                    ours = frm.frame(f, rotation).pixels
                    frames_checked += 1
                    if theirs.shape != ours.shape or not np.array_equal(theirs, ours):
                        mismatches += 1
                        report(f"    MISMATCH {path} frame {f} direction {rotation}")
    report(f"  decode: {count} FRMs, {frames_checked} frames, {mismatches} mismatches")
    return mismatches == 0


def check_resume(game_dir: Path, report, pattern: str = "art\\critters\\*") -> bool:
    """Kills an extraction part-way, reruns it and verifies every output."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmd = [sys.executable, "-m", "fo2hd.cli", "--game", str(game_dir), "--data-root", str(root), "extract", pattern]
        first = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=Path(__file__).parents[1])
        time.sleep(4)
        first.kill()
        first.wait()
        partial = sum(1 for _ in (root / "extract").rglob("*") if _.is_file()) if (root / "extract").exists() else 0
        second = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parents[1])
        if second.returncode != 0:
            report(f"  resume: rerun failed: {second.stderr[-400:]}")
            return False
        files = GameFiles(game_dir)
        expected = sorted(p for p in files.list() if p.startswith("art\\critters\\"))
        bad = 0
        for path in expected:
            out = root / "extract" / normalize(path).replace("\\", "/")
            if not out.exists() or out.read_bytes() != files.read(path):
                bad += 1
        leftovers = [p for p in (root / "extract").rglob("*.part")]
        report(f"  resume: killed after {partial} files, rerun said '{second.stdout.strip().splitlines()[-1]}', {len(expected)} expected, {bad} bad, {len(leftovers)} .part leftovers")
        return bad == 0 and not leftovers and partial > 0


def run(paths: Paths, game_dir: Path, report=print) -> bool:
    if not (TOOLS / "ce-dat-tool.exe").exists():
        report(f"missing {TOOLS}; run tools/scripts/build.ps1 first")
        return False
    files = GameFiles(game_dir)
    report(f"layers (highest first): {[getattr(l, 'path', None).name for l in files.layers]}")
    results = {
        "lists": check_lists(files, report),
        "extract": check_extract(files, report),
        "decode": check_decode(files, report),
        "resume": check_resume(game_dir, report),
    }
    for name, ok in results.items():
        report(f"{'PASS' if ok else 'FAIL'}  {name}")
    return all(results.values())
