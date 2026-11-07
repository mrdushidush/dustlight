"""Release packaging (PLAN.md R5).

From one data root's runtime packs (the release art, `<release_root>`) and
a built engine, makes what players download:

  <name>-<version>-engine.zip          the engine exe, ce.dat, the docs (small)
  <name>-<version>-art-<N>x[-partK].zip  the HD art for one scale tier, split
                                       into independent parts under the host's
                                       file size limit; each part extracts on
                                       its own into the game folder
  SHA256SUMS.txt, release.json         checksums and what went in

Everything extracts into the game folder: the exe and docs at the top, the
art under hd\\ (the engine's default `[hd] hd_art_dir`). Each art zip also
holds hd\\pack_<N>x.json, the marker the engine's first start reads to pick
the scale (src/hd/first_run.cc).

Checks before anything is written:
  - no shipped file is byte-identical to any file of the game (every entry of
    every archive and every loose file of the pristine install): the release
    holds derived art only, never the original's files;
  - the exe imports only Windows system DLLs (static C runtime, no debug or
    redistributable DLLs a player might not have);
  - every pack has its tier's file, every font its tier's atlas;
  - every pack and the fonts came from a model the credits file names, and the
    credits name no model nothing came from;
  - no filled doc has a placeholder left, and neither the exe nor a doc holds a
    path of this PC or the old working title.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .dat import GameFiles

# Measured on the P7 packs: deflate level 6 keeps 58.5% of BC7 data (the
# transparent blocks compress). Parts are filled to this many raw bytes so
# the compressed zip stays under the limit with a margin.
DEFLATE_RATIO = 0.62
COMPRESS_LEVEL = 6

# What a doc placeholder looks like; other braces in a doc are left alone.
PLACEHOLDER = re.compile(r"\{(?:name|version|exe|model_[a-z]+|size_\dx|known_issues|source_url)\}")
# What must not be in a shipped file: this PC's paths and the working title ("HD-2D" is a
# Square Enix trademark). Case-sensitive: an exe's random bytes must not match by chance.
LEAKS = re.compile(rb"[A-Za-z]:[\\/](?:dev|Games|Users|fo2hd)[\\/][^\0\r\n\"']{0,60}|HD-2D|Fallout HD|fallout2-hd")

# DLLs every Windows 10/11 has. Anything else in the exe's import table fails
# the check (vcruntime140.dll, msvcp140.dll, api-ms-win-crt-*, debug DLLs).
SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "gdi32.dll", "shell32.dll", "ole32.dll", "oleaut32.dll", "advapi32.dll",
    "winmm.dll", "imm32.dll", "version.dll", "setupapi.dll", "cfgmgr32.dll", "hid.dll", "shlwapi.dll",
    "comdlg32.dll", "comctl32.dll", "ws2_32.dll", "dinput8.dll", "dsound.dll", "dxgi.dll", "d3d11.dll",
    "d3dcompiler_47.dll", "xinput1_4.dll", "uxtheme.dll", "dwmapi.dll", "psapi.dll", "dbghelp.dll", "bcrypt.dll",
    "crypt32.dll", "secur32.dll", "ntdll.dll", "powrprof.dll", "winhttp.dll", "rpcrt4.dll", "msimg32.dll",
}


@dataclass
class Member:
    arcname: str
    source: Path | None  # None: `data` holds the bytes
    size: int
    data: bytes | None = None


def pe_imports(exe: Path) -> list[str]:
    """The DLL names in a PE32+ file's import table (and its delay-load table)."""
    b = exe.read_bytes()
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if b[pe : pe + 4] != b"PE\0\0":
        raise ValueError(f"{exe}: not a PE file")
    n_sections = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", b, opt)[0]
    dirs = opt + (112 if magic == 0x20B else 96)
    sections = []
    for i in range(n_sections):
        s = opt + opt_size + i * 40
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", b, s + 8)
        sections.append((va, max(vsize, rawsize), rawptr))

    def offset(rva: int) -> int:
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"rva {rva:#x} outside the sections")

    def cstr(off: int) -> str:
        end = b.index(b"\0", off)
        return b[off:end].decode("ascii", "replace")

    names = []
    imp_rva = struct.unpack_from("<I", b, dirs + 8)[0]
    if imp_rva:
        off = offset(imp_rva)
        while True:
            name_rva = struct.unpack_from("<I", b, off + 12)[0]
            if name_rva == 0:
                break
            names.append(cstr(offset(name_rva)))
            off += 20
    delay_rva = struct.unpack_from("<I", b, dirs + 13 * 8)[0]
    if delay_rva:
        off = offset(delay_rva)
        while True:
            name_rva = struct.unpack_from("<I", b, off + 4)[0]
            if name_rva == 0:
                break
            names.append(cstr(offset(name_rva)))
            off += 32
    return names


def check_imports(exe: Path) -> list[str]:
    """Problems with the exe's imports (empty = fine)."""
    return [f"{exe.name} imports {dll}, which a player's PC may not have" for dll in pe_imports(exe) if dll.lower() not in SYSTEM_DLLS]


def game_hashes(game_dir: Path, report=print) -> set[bytes]:
    """SHA-1 of every file of the game: each archive's entries (shadowed ones
    too) and every loose file in the folder."""
    hashes: set[bytes] = set()
    files = GameFiles(game_dir)
    for layer in files.layers:
        entries = getattr(layer, "entries", {})
        for path in list(entries):
            try:
                hashes.add(hashlib.sha1(layer.read(path)).digest())
            except Exception as e:  # noqa: BLE001 - an unreadable entry can't be shipped by accident either
                report(f"  (unreadable archive entry {path}: {e})")
    for f in game_dir.rglob("*"):
        if f.is_file():
            hashes.add(hashlib.sha1(f.read_bytes()).digest())
    return hashes


def tier_members(hdpack: Path, tier: int) -> tuple[list[Member], list[str]]:
    """Every file one tier's art zip holds, and what is missing."""
    members: list[Member] = []
    missing: list[str] = []
    for pack_json in sorted((hdpack / "art").rglob("pack.json")):
        rel = pack_json.parent.relative_to(hdpack)
        meta = json.loads(pack_json.read_text(encoding="utf-8"))
        dds = pack_json.parent / f"albedo_{tier}x.dds"
        if tier not in meta.get("tiers", [tier]) or not dds.exists():
            missing.append(str(rel))
            continue
        members.append(Member(f"hd/{rel.as_posix()}/pack.json", pack_json, pack_json.stat().st_size))
        members.append(Member(f"hd/{rel.as_posix()}/{dds.name}", dds, dds.stat().st_size))
    fonts = hdpack / "fonts"
    if fonts.exists():
        index = fonts / "fonts.json"
        if index.exists():
            members.append(Member("hd/fonts/fonts.json", index, index.stat().st_size))
        for png in sorted(fonts.glob(f"*-{tier}x.png")):
            members.append(Member(f"hd/fonts/{png.name}", png, png.stat().st_size))
    return members, missing


def split(members: list[Member], max_bytes: int) -> list[list[Member]]:
    """Consecutive parts (by path) whose estimated zip size stays under
    max_bytes. A pack's pack.json and its DDS stay in the same part."""
    budget = max_bytes / DEFLATE_RATIO
    parts: list[list[Member]] = [[]]
    size = 0.0
    i = 0
    while i < len(members):
        group = [members[i]]
        # pack.json is always followed by its albedo file
        if members[i].arcname.endswith("/pack.json") and i + 1 < len(members):
            group.append(members[i + 1])
        g = sum(m.size for m in group)
        if parts[-1] and size + g > budget:
            parts.append([])
            size = 0.0
        parts[-1].extend(group)
        size += g
        i += len(group)
    return parts


def _write_zip(path: str, members: list[tuple[str, str | None, bytes | None]]) -> tuple[str, int, str]:
    tmp = Path(path + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL, allowZip64=True) as z:
        for arcname, source, data in members:
            if data is not None:
                z.writestr(arcname, data)
            else:
                z.write(source, arcname)
    tmp.replace(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return path, Path(path).stat().st_size, h.hexdigest()


def fill(text: str, fields: dict[str, str]) -> str:
    """Replaces {key} for every key in `fields`; other braces stay as they are."""
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text


def check_leaks(label: str, data: bytes) -> list[str]:
    """Build paths and the old working title don't ship: not in the exe's strings
    (__FILE__, the pdb path), not in the docs."""
    hits = sorted({m.group(0).decode("latin1") for m in LEAKS.finditer(data)})
    return [f"{label} holds {hits[:5]}"] if hits else []


def unfilled(text: str) -> list[str]:
    """The doc placeholders still in a filled text (a field nobody provided)."""
    return sorted(set(PLACEHOLDER.findall(text)))


def load_credits(path: Path) -> dict:
    """The art's credits file (docs/player/art_credits_*.json): `art_models`, the
    model files pack.json may name; `credits`, who gets named in the docs."""
    info = json.loads(path.read_text(encoding="utf-8"))
    if not info.get("art_models") or not info.get("credits"):
        raise SystemExit(f"{path}: needs art_models and credits")
    for c in info["credits"]:
        missing = [k for k in ("name", "author", "license", "use") if not c.get(k)]
        if missing:
            raise SystemExit(f"{path}: {c.get('name', '?')} lacks {missing}")
    return info


def credit_fields(info: dict) -> dict[str, str]:
    """The doc placeholders for the models: one phrase, a bullet list, table rows, and the
    terms the art ships under. A credit's optional `note` (a permission, links) ends its
    bullet; the file's optional `art_terms` goes in THIRD_PARTY.md."""
    credits = info["credits"]

    def bullet(c: dict) -> str:
        text = f"- **{c['name']}** by {c['author']} ({c['license']}): {c['use']}."
        return f"{text} {c['note']}" if c.get("note") else text

    return {
        "model_short": " and ".join(f"{c['name']} by {c['author']} ({c['license']})" for c in credits),
        "model_credits": "\n".join(bullet(c) for c in credits),
        "model_rows": "\n".join(f"| {c['name']} | {c['author']} | {c['license']} | {c['use']} (not included; only its output) |" for c in credits),
        "art_terms": info.get("art_terms", ""),
    }


def pack_models(hdpack: Path) -> dict[str, int]:
    """How many runtime packs each model made (pack.json), plus the font atlases'
    model (fonts.json)."""
    counts: dict[str, int] = {}
    for pack_json in (hdpack / "art").rglob("pack.json"):
        model = json.loads(pack_json.read_text(encoding="utf-8")).get("model", "(none)")
        counts[model] = counts.get(model, 0) + 1
    fonts = hdpack / "fonts" / "fonts.json"
    if fonts.exists():
        model = json.loads(fonts.read_text(encoding="utf-8")).get("model", "(none)")
        counts[model] = counts.get(model, 0) + 1
    return counts


def check_models(counts: dict[str, int], art_models: list[str]) -> list[str]:
    """The credits have to name what made the art: stray art from another model
    could carry another license, and a credit for a model nobody used is false."""
    problems = []
    stray = {m: n for m, n in counts.items() if m not in art_models}
    if stray:
        problems.append(f"art from models the credits don't name: {stray}")
    unused = [m for m in art_models if m not in counts]
    if unused:
        problems.append(f"the credits name models no art came from: {unused}")
    return problems


def build(
    *,
    name: str,
    version: str,
    exe: Path,
    exe_name: str,
    ce_dat: Path,
    docs: list[tuple[Path, str]],
    hdpack: Path,
    pristine: Path,
    out: Path,
    tiers: list[int],
    max_part_bytes: int,
    credits: dict,
    fields: dict[str, str] | None = None,
    page: Path | None = None,
    docs_only: bool = False,
    report=print,
) -> dict:
    started = time.perf_counter()
    problems: list[str] = []
    if not docs_only:
        problems += check_imports(exe)

    report("release: collecting files")
    tier_members_all = {tier: tier_members(hdpack, tier) for tier in tiers}
    counts = pack_models(hdpack)
    report(f"  art by model: {counts}")
    if not docs_only:
        problems += check_models(counts, credits["art_models"])
    model = credits["credits"]
    # Placeholders in the docs ({name}, {model_short}, {size_3x}, ...).
    values = {"name": name, "version": version, "exe": exe_name, **credit_fields(credits)}
    for tier, (members, _) in tier_members_all.items():
        values[f"size_{tier}x"] = f"about {sum(m.size for m in members) * 0.585 / 1e9:.1f} GB"
    for t in (2, 3, 4):
        values.setdefault(f"size_{t}x", "not in this release")
    values.update(fields or {})

    def filled(src: Path) -> bytes:
        return fill(src.read_text(encoding="utf-8"), values).replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")

    # The store page isn't in any zip; it goes next to them, ready to paste.
    page_data = filled(page) if page else None
    if docs_only:
        # A preview of what players will read; the sizes count the packs built so far.
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for src, arc in docs:
            if src.suffix.lower() in (".md", ".txt"):
                target = out / arc.replace("/", "_")
                target.write_bytes(filled(src))
                written.append(target.name)
        if page_data is not None:
            (out / page.name).write_bytes(page_data)
            written.append(page.name)
        for f in written:
            left = unfilled((out / f).read_text(encoding="utf-8"))
            if left:
                raise SystemExit(f"release: {f}: placeholders left: {left}")
        report(f"release: docs only -> {out}: {written}")
        return {"name": name, "version": version, "files": [], "over_limit": [], "seconds": round(time.perf_counter() - started)}
    engine = [Member(exe_name, exe, exe.stat().st_size), Member("ce.dat", ce_dat, ce_dat.stat().st_size)]
    for src, arc in docs:
        if src.suffix.lower() in (".md", ".txt"):
            data = filled(src)
            engine.append(Member(arc, None, len(data), data))
        else:
            engine.append(Member(arc, src, src.stat().st_size))
    left = {m.arcname: unfilled(m.data.decode("utf-8")) for m in engine if m.data is not None}
    left = {arc: keys for arc, keys in left.items() if keys}
    if page_data is not None and unfilled(page_data.decode("utf-8")):
        left[page.name] = unfilled(page_data.decode("utf-8"))
    if left:
        problems.append(f"placeholders left: {left}")
    for m in engine:
        problems += check_leaks(m.arcname, m.data if m.data is not None else m.source.read_bytes())
    if page_data is not None:
        problems += check_leaks(page.name, page_data)
    tier_parts: dict[int, list[list[Member]]] = {}
    for tier in tiers:
        members, missing = tier_members_all[tier]
        if missing:
            problems.append(f"{tier}x: {len(missing)} packs without their {tier}x file (first: {missing[:3]})")
        marker = {
            "name": name, "version": version, "tier": tier, "model": model,
            "packs": sum(1 for m in members if m.arcname.endswith("pack.json")),
            "bytes": sum(m.size for m in members),
        }
        data = json.dumps(marker, indent=1).encode("utf-8")
        members.insert(0, Member(f"hd/pack_{tier}x.json", None, len(data), data))
        tier_parts[tier] = split(members, max_part_bytes)
        report(f"  {tier}x: {len(members)} files, {marker['bytes'] / 1e9:.2f} GB raw -> {len(tier_parts[tier])} part(s)")

    report("release: hashing the game's own files (nothing original may ship)")
    original = game_hashes(pristine, report)
    shipped = engine + [m for parts in tier_parts.values() for part in parts for m in part]
    hits = []
    for m in shipped:
        data = m.data if m.data is not None else m.source.read_bytes()
        if hashlib.sha1(data).digest() in original:
            hits.append(m.arcname)
    if hits:
        problems.append(f"{len(hits)} shipped files are identical to game files (first: {hits[:5]})")
    report(f"  {len(original)} game files hashed, {len(shipped)} shipped files checked, {len(hits)} identical")

    if problems:
        for p in problems:
            report(f"PROBLEM: {p}")
        raise SystemExit("release: checks failed, nothing written")

    out.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, list]] = []

    def entry(ms: list[Member]) -> list:
        return [(m.arcname, str(m.source) if m.source else None, m.data) for m in ms]

    base = f"{name}-{version}"
    jobs.append((str(out / f"{base}-engine.zip"), entry(engine)))
    for tier, parts in tier_parts.items():
        for k, part in enumerate(parts, 1):
            suffix = f"-part{k}of{len(parts)}" if len(parts) > 1 else ""
            jobs.append((str(out / f"{base}-art-{tier}x{suffix}.zip"), entry(part)))
    report(f"release: writing {len(jobs)} zips")
    results = []
    with ProcessPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        for path, size, digest in pool.map(_write_zip, [j[0] for j in jobs], [j[1] for j in jobs]):
            results.append({"file": Path(path).name, "bytes": size, "sha256": digest})
            report(f"  {Path(path).name}: {size / 1e9:.2f} GB")
    over = [r["file"] for r in results if r["bytes"] > max_part_bytes]
    sums = "".join(f"{r['sha256']}  {r['file']}\n" for r in results)
    (out / "SHA256SUMS.txt").write_text(sums, encoding="utf-8", newline="\n")
    if page_data is not None:
        (out / page.name).write_bytes(page_data)
    info = {
        "name": name, "version": version, "model": model, "tiers": tiers, "max_part_bytes": max_part_bytes,
        "files": results, "over_limit": over, "exe_imports": pe_imports(exe), "seconds": round(time.perf_counter() - started),
    }
    (out / "release.json").write_text(json.dumps(info, indent=1), encoding="utf-8", newline="\n")
    if over:
        report(f"WARNING: over the part limit: {over}")
    return info
