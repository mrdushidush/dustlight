"""Voice track V0.1 (PLAN.md §6.2): every line the game can show, in one database.

Reads, as the game sees them (archive precedence, English):
  text\\english\\dialog\\*.msg   dialogue: NPC replies, player options, floats and
                               descriptions mixed (the script scan, V0.2, tells
                               which is which)
  text\\english\\game\\combatai.msg   combat taunts
  text\\english\\game\\pipboy.msg     Pip-Boy labels (holodisk titles among them)
  data\\holodisk.txt                the holodisk index: each disk's pages are
                                   pipboy.msg entries text..text+500

.msg parsing mirrors CE's message.cc exactly: fields are {...}; anything
outside braces (comments) is skipped; newlines inside a field are dropped
(not turned into spaces); a later entry with the same number replaces the
earlier one. Text is cp1252.

Each line gets cleanup notes (stage directions like *sigh*, ALL-CAPS words,
digits, very long lines), recorded for the TTS step, never applied to the
game. Output: <data_root>/voice/lines.sqlite (rebuilt on each run).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .config import Paths
from .dat import GameFiles

DIALOG = "text\\english\\dialog\\"
GAME = "text\\english\\game\\"
STAGE = re.compile(r"\*[^*]{1,40}\*|\[[^\]]{1,40}\]")
CAPS = re.compile(r"\b[A-Z]{3,}\b")
DIGITS = re.compile(r"\d")
WORD = re.compile(r"[A-Za-z0-9']+")


def parse_msg(data: bytes) -> list[tuple[int, str, str]]:
    """(number, audio, text) per entry, as CE's messageListLoadFile reads them."""
    text = data.decode("cp1252", "replace")
    fields: list[str] = []
    i, n = 0, len(text)
    while True:
        start = text.find("{", i)
        if start < 0:
            break
        end = text.find("}", start + 1)
        if end < 0:
            break  # CE: "EOF reached" inside a field; the entries before it count
        fields.append(text[start + 1 : end].replace("\r", "").replace("\n", ""))
        i = end + 1
    entries: dict[int, tuple[str, str]] = {}
    order: list[int] = []
    for k in range(0, len(fields) - 2, 3):
        num_s, audio, body = fields[k : k + 3]
        try:
            num = int(num_s.strip())
        except ValueError:
            break  # CE stops at the first bad number
        if num not in entries:
            order.append(num)
        entries[num] = (audio.strip(), body)
    return [(num, *entries[num]) for num in order]


def notes(text: str) -> list[str]:
    out = []
    if STAGE.search(text):
        out.append("stage")
    if CAPS.search(text):
        out.append("caps")
    if DIGITS.search(text):
        out.append("digits")
    if len(WORD.findall(text)) > 60:
        out.append("long")
    if "%" in text:
        out.append("format")
    return out


def parse_holodisk_index(data: bytes) -> list[tuple[int, int, int]]:
    """(gvar, title msg, first text msg) per holodisk from data\\holodisk.txt
    (pipboy.cc holodiskInit: "gvar, title, text"; a disk's pages are the
    pipboy.msg entries text..text+500)."""
    out = []
    for line in data.decode("cp1252", "replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split(",")]
        if len(parts) >= 3 and all(x.lstrip("-").isdigit() for x in parts[:3]):
            out.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return out


def build(paths: Paths, files: GameFiles, report=print) -> dict:
    out_dir = paths.data_root / "voice"
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "lines.sqlite"
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.execute("create table lines (source text, file text, num integer, audio text, text text, words integer, notes text, primary key (file, num))")
    listing = files.list()
    stats: dict = {}

    def add(source: str, file: str, entries: list[tuple[int, str, str]]):
        rows = []
        for num, audio, body in entries:
            rows.append((source, file, num, audio, body, len(WORD.findall(body)), ",".join(notes(body))))
        db.executemany("insert into lines values (?, ?, ?, ?, ?, ?, ?)", rows)
        s = stats.setdefault(source, {"files": 0, "entries": 0, "words": 0, "voiced": 0, "empty": 0})
        s["files"] += 1
        s["entries"] += len(rows)
        s["words"] += sum(r[5] for r in rows)
        s["voiced"] += sum(1 for r in rows if r[3])
        s["empty"] += sum(1 for r in rows if not r[4].strip())

    for path in sorted(p for p in listing if p.lower().startswith(DIALOG) and p.lower().endswith(".msg")):
        add("dialog", Path(path.replace("\\", "/")).name.lower(), parse_msg(files.read(path)))
    if GAME + "combatai.msg" in listing:
        add("combatai", "combatai.msg", parse_msg(files.read(GAME + "combatai.msg")))
    if GAME + "pipboy.msg" in listing:
        pip = parse_msg(files.read(GAME + "pipboy.msg"))
        disks = parse_holodisk_index(files.read("data\\holodisk.txt")) if "data\\holodisk.txt" in listing else []
        ranges = [(text, text + 500) for _, _, text in disks]
        holo = [e for e in pip if any(a <= e[0] < b for a, b in ranges)]
        # Holodisks and the rest of pipboy.msg (labels, titles) share the file: two sources.
        add("holodisk", "pipboy.msg", holo)
        add("pipboy", "pipboy.msg#labels", [e for e in pip if e not in holo])
        stats["holodisk"]["disks"] = len(disks)
    db.commit()
    db.close()
    stats["db"] = str(db_path)
    return stats
