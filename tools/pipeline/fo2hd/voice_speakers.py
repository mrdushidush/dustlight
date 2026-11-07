"""Voice track V0.3 (PLAN.md §6.2): who says each line.

A dialogue line is said by whoever runs the script that uses it (the V0.2
labels name that script). A script runs on:

  map      an object a map file places with that script (map_objects): the
           critters, and scenery or items such as computers and terminals
  proto    any critter made from a proto whose own script it is (protos
           carry the scripts.lst index in their sid)
  spawn    a critter a script creates at runtime: create_object_sid(pid,
           tile, elevation, sid) with a constant pid and script (sid is the
           scripts.lst index + 1, interpreter_extra.cc opCreateObject)
  encounter  a random-encounter critter (data\\worldmap.txt `pid:` with
           `Script:`, the scripts.lst index + 1)
  mapscript  a map's own script (map header) or a spatial script: no body,
           narration at most

For critters the protos give gender (base stat 34), kill type (man, woman,
child, super mutant, ghoul, robot, ...), body type, the critter art and the
talking head. The name is CE's critterGetName: scrname.msg entry
101 + script index, else the proto's name.

Output: table `speakers` in <data_root>/voice/lines.sqlite (one row per
script, source, map and proto, with the instance count), and a coverage
summary of the labelled dialogue lines by what speaks them. `fo2hd
voice-scan` must run first (its labels say which script uses a line).
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from . import map_objects
from .config import Paths
from .dat import GameFiles
from .voice_lines import parse_msg
from .voice_scripts import PUSH_INT, decode

OP_CREATE_OBJECT = 0x80B7
# Coverage classes, best first.
CLASSES = ("one critter voice", "several critter voices", "object (computer, terminal, ...)", "map or spatial script only", "no speaker found",
           "script not in scripts.lst (never runs)")
KIND_NAMES = {map_objects.OBJ_CRITTER: "critter", map_objects.OBJ_SCENERY: "scenery", map_objects.OBJ_ITEM: "item",
              map_objects.OBJ_WALL: "wall", map_objects.OBJ_MISC: "misc"}


def _lst(files: GameFiles, path: str) -> list[str]:
    text = files.read(path).decode("cp1252", "replace")
    return [line.split(";")[0].split(",")[0].strip().lower() for line in text.splitlines()]


def spawns(code: list) -> list[tuple[int, int]]:
    """(critter pid, scripts.lst index) of each create_object_sid with constants.
    The sid is the last push; the pid is the nearest earlier push in the
    critter pid range (tile and elevation are expressions between them)."""
    out = []
    for k, (op, _) in enumerate(code):
        if op != OP_CREATE_OBJECT or k < 2 or code[k - 1][0] != PUSH_INT:
            continue
        sid = code[k - 1][1]
        if sid <= 0:
            continue
        for i in range(k - 2, max(-1, k - 40), -1):
            o, v = code[i]
            if o == PUSH_INT and (v >> 24) == map_objects.OBJ_CRITTER:
                out.append((v, sid - 1))
                break
    return out


def build(paths: Paths, files: GameFiles, report=print) -> dict:
    db_path = paths.data_root / "voice" / "lines.sqlite"
    db = sqlite3.connect(db_path)
    if not db.execute("select count(*) from sqlite_master where name='labels'").fetchone()[0]:
        raise SystemExit(f"no labels in {db_path}: run `fo2hd voice-scan` first")
    scripts = _lst(files, "scripts\\scripts.lst")
    script_name = lambda i: scripts[i].removesuffix(".int") if 0 <= i < len(scripts) else f"#{i}"  # noqa: E731
    scrname = {n: t for n, _a, t in parse_msg(files.read("text\\english\\game\\scrname.msg"))}
    critter_art = _lst(files, "art\\critters\\critters.lst")
    heads = _lst(files, "art\\heads\\heads.lst")
    protos = map_objects.Protos(files)

    # (script index, source, map, pid) -> instance count
    owners: Counter = Counter()
    stats: Counter = Counter()
    listing = files.list()
    for path in sorted(p for p in listing if p.startswith("maps\\") and p.endswith(".map")):
        stem = path.split("\\")[-1][:-4]
        m = map_objects.read(files.read(path), protos)
        stats["maps"] += 1
        owned = set()
        for obj in map_objects.walk(m["objects"]):
            if obj.script_index >= 0:
                owners[(obj.script_index, "map", stem, obj.pid)] += 1
                owned.add(obj.sid)
        if m["header_script"] > 0:
            owners[(m["header_script"] - 1, "mapscript", stem, -1)] += 1
        for sid, index in m["scripts"].items():
            if sid not in owned and (sid >> 24) == map_objects.SPATIAL:
                owners[(index, "mapscript", stem, -1)] += 1

    for pid in protos.pids(map_objects.OBJ_CRITTER):
        c = protos.critter(pid)
        if c and c["sid"] != -1:
            owners[(c["sid"] & 0xFFFFFF, "proto", "", pid)] += 1

    for path in sorted(p for p in listing if p.lower().startswith("scripts\\") and p.lower().endswith(".int")):
        try:
            code = decode(files.read(path))
        except (ValueError, IndexError):
            continue
        for pid, index in spawns(code):
            owners[(index, "spawn", Path(path.replace("\\", "/")).stem.lower(), pid)] += 1

    # Random encounters: data\worldmap.txt `type_NN=... pid:<pid> ... Script:<N>`
    # gives the critter scripts.lst index N - 1 (worldmap.cc).
    section = ""
    for line in files.read("data\\worldmap.txt").decode("cp1252", "replace").splitlines():
        s = line.split(";")[0].strip()
        if s.startswith("["):
            section = s.strip("[]").strip().lower()
            continue
        pid, script = re.search(r"\bpid:\s*(\d+)", s, re.I), re.search(r"\bscript:\s*(\d+)", s, re.I)
        if pid and script and int(script.group(1)) > 0:
            owners[(int(script.group(1)) - 1, "encounter", section, int(pid.group(1)))] += 1

    rows = []
    for (index, source, where, pid), count in sorted(owners.items()):
        kind = KIND_NAMES.get((pid >> 24) & 0xFF, "") if pid != -1 else "none"
        c = protos.critter(pid) if kind == "critter" else None
        name = scrname.get(101 + index, "") or (protos.name(pid) if pid != -1 else "")
        art = head = ""
        if c:
            fid_index = c["fid"] & 0xFFF
            art = critter_art[fid_index] if fid_index < len(critter_art) else ""
            if c["head_fid"] != -1:
                h = c["head_fid"] & 0xFFF
                head = heads[h] if h < len(heads) else ""
        rows.append((
            script_name(index), source, where, kind, pid, protos.name(pid) if pid != -1 else "", name,
            c["gender"] if c else "", c["kill_type"] if c else "", c["body_type"] if c else "", art, head, count,
        ))
    db.execute("drop table if exists speakers")
    db.execute(
        "create table speakers (script text, source text, place text, kind text, pid integer, proto text, name text,"
        " gender text, kill_type text, body_type text, art text, head text, count integer)"
    )
    db.executemany("insert into speakers values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()

    # Coverage: each labelled dialogue entry by what runs the script using it.
    voices: dict[str, set] = defaultdict(set)
    kinds: dict[str, set] = defaultdict(set)
    for script, source, kind, gender, kill in db.execute("select script, source, kind, gender, kill_type from speakers"):
        kinds[script].add(kind if source != "mapscript" else "mapscript")
        if kind == "critter":
            voices[script].add((gender, kill))

    def speaker_class(script: str) -> str:
        if len(voices.get(script, ())) == 1:
            return CLASSES[0]
        if voices.get(script):
            return CLASSES[1]
        k = kinds.get(script, set())
        if k & {"scenery", "item"}:
            return CLASSES[2]
        if k:
            return CLASSES[3]
        return CLASSES[4] if script in listed else CLASSES[5]

    listed = {s.removesuffix(".int") for s in scripts}

    # An entry can be used by several scripts (copies of a script among them,
    # which never run): it counts with the best speaker any of them has.
    best: dict[tuple, tuple[int, int]] = {}
    for file, num, label, script, n_words in db.execute(
        "select b.file, b.num, b.label, b.script, l.words from labels b join lines l on l.file=b.file and l.num=b.num where l.source='dialog'"
    ):
        base = label.split("+")[0]
        if base not in ("reply", "float"):
            continue
        rank = CLASSES.index(speaker_class(script))
        key = (file, num, base)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, n_words or 0)
    coverage: dict[str, Counter] = {}
    words: dict[str, Counter] = {}
    for (_file, _num, base), (rank, n_words) in best.items():
        coverage.setdefault(base, Counter())[CLASSES[rank]] += 1
        words.setdefault(base, Counter())[CLASSES[rank]] += n_words
    by_kill = Counter()
    for (kill,) in db.execute("select kill_type from speakers where kind='critter'"):
        by_kill[kill] += 1
    db.close()
    stats["owner rows"] = len(rows)
    for source in ("map", "proto", "spawn", "encounter", "mapscript"):
        stats[f"{source} rows"] = sum(1 for r in rows if r[1] == source)
    return {"stats": dict(stats), "coverage": coverage, "words": words, "kill_types": dict(by_kill.most_common()), "db": db_path}
