"""Voice track V0.2 (PLAN.md §6.2): which message is said how, from the
compiled scripts.

Every scripts\\*.int is decoded (2-byte big-endian opcodes; pushes carry a
4-byte value: 0xC001 int, 0xA001 float, 0x9001 string) and each use of a
dialogue message is labelled:

  reply    gsay_reply(list, msg) and gsay_message(list, msg, reaction):
           the NPC's line in a conversation
  option   gsay_option(list, msg, proc, reaction), giq_option(iq, list, msg,
           proc, reaction): the player's choices (stay silent)
  float    message_str(list, msg) whose string goes straight to float_msg:
           words over a critter's head
  display  message_str -> display_msg: the message box ("You see ...")
  other    message_str used any other way (joined with other text, passed
           to a procedure, ...): treated as dynamic

The message number is found at a fixed push position before the opcode
(the arguments after it, procedure and reaction, are plain pushes in the
compiled scripts); a number from random(a, b) becomes the range a..b. A use
whose number isn't a constant is counted as "unresolved" for that script.
The list id N of a constant list is scripts.lst line N-1 (scripts.cc
scriptsGetMessageList); its file is dialog\\<script>.msg.

Labels go into lines.sqlite (table labels: file, num, label, script) next to
the line reader's table (V0.1); `fo2hd voice-lines` must run first.
"""

from __future__ import annotations

import sqlite3
import struct
from collections import Counter
from pathlib import Path

from .config import Paths
from .dat import GameFiles

PUSH_INT, PUSH_FLOAT, PUSH_STRING = 0xC001, 0xA001, 0x9001
OP_ADD = 0x8039
OP_RANDOM = 0x80B4
OP_DISPLAY_MSG = 0x80B8
OP_MESSAGE_STR = 0x8105
OP_FLOAT_MSG = 0x810A
OP_GSAY_REPLY, OP_GSAY_OPTION, OP_GSAY_MESSAGE, OP_GIQ_OPTION = 0x811E, 0x811F, 0x8120, 0x8121
# opcode -> (label, how many pushes after the msg argument, i.e. its position from the end)
MSG_OPS = {
    OP_GSAY_REPLY: ("reply", 0),
    OP_GSAY_MESSAGE: ("reply", 1),
    OP_GSAY_OPTION: ("option", 2),
    OP_GIQ_OPTION: ("option", 2),
    OP_MESSAGE_STR: ("message_str", 0),
}


def decode(b: bytes) -> list[tuple[int, int | None]]:
    """(opcode, immediate) for the code after the header tables."""
    n = struct.unpack_from(">I", b, 42)[0]
    ident = 46 + 24 * n
    pos = ident + 4 + struct.unpack_from(">I", b, ident)[0]
    size = struct.unpack_from(">I", b, pos)[0]
    pos += 4 + (0 if size == 0xFFFFFFFF else size)
    bodies = [struct.unpack_from(">6I", b, 46 + 24 * i)[4] for i in range(n)]
    pos = min([pos] + [o for o in bodies if o >= pos]) if bodies else pos
    out = []
    end = len(b) - 1
    while pos < end:
        op = struct.unpack_from(">H", b, pos)[0]
        pos += 2
        if op in (PUSH_INT, PUSH_FLOAT, PUSH_STRING) and pos + 4 <= len(b):
            out.append((op, struct.unpack_from(">i", b, pos)[0]))
            pos += 4
        else:
            out.append((op, None))
    return out


def number(code: list, i: int):
    """The constant number (int or (lo, hi) range) whose computation ends at
    code[i], and the index before it; (None, i) if it isn't one. Handles a
    push, random(a, b), and base + either (the scripts' `200 + random(0, 2)`)."""
    op, value = code[i]
    if op == PUSH_INT:
        return value, i - 1
    if op == OP_RANDOM and i >= 2 and code[i - 1][0] == PUSH_INT and code[i - 2][0] == PUSH_INT:
        return (code[i - 2][1], code[i - 1][1]), i - 3
    if op == OP_ADD and i >= 1:
        right, j = number(code, i - 1)
        if right is None or j < 0:
            return None, i
        left, j2 = number(code, j)
        if left is None:
            return None, i
        lo = (left[0] if isinstance(left, tuple) else left) + (right[0] if isinstance(right, tuple) else right)
        hi = (left[1] if isinstance(left, tuple) else left) + (right[1] if isinstance(right, tuple) else right)
        return ((lo, hi) if lo != hi else lo), j2
    return None, i


def msg_arg(code: list, k: int, after: int):
    """The msg argument of the call at code[k]: int, (lo, hi) range, or None;
    and the list id before it (int or None)."""
    i = k - 1
    for _ in range(after):  # the plain pushes after the msg argument
        if i < 0 or code[i][0] != PUSH_INT:
            return None, None
        i -= 1
    if i < 0:
        return None, None
    msg, j = number(code, i)
    if msg is None:
        return None, None
    lst = code[j][1] if j >= 0 and code[j][0] == PUSH_INT else None
    return msg, lst


CONSUMERS = {OP_GSAY_REPLY: "reply", OP_GSAY_MESSAGE: "reply", OP_GSAY_OPTION: "option", OP_GIQ_OPTION: "option",
             OP_FLOAT_MSG: "float", OP_DISPLAY_MSG: "display"}


def consumer(code: list, k: int) -> tuple[str, bool]:
    """What the string from message_str at code[k] is used for, and whether
    it is joined with other text on the way (a dynamic line). Plain pushes
    (the call's later arguments), other message_str calls and joins may come
    between; anything else first means "other"."""
    dynamic = False
    for i in range(k + 1, min(k + 16, len(code))):
        op = code[i][0]
        if op in CONSUMERS:
            return CONSUMERS[op], dynamic
        if op == OP_ADD:
            dynamic = True
            continue
        if op in (PUSH_INT, PUSH_STRING, OP_MESSAGE_STR):
            if op == PUSH_STRING:
                dynamic = True
            continue
        return "other", dynamic
    return "other", dynamic


def scan(paths: Paths, files: GameFiles, report=print) -> dict:
    db_path = paths.data_root / "voice" / "lines.sqlite"
    if not db_path.exists():
        raise SystemExit(f"no {db_path}: run `fo2hd voice-lines` first")
    lst = files.read("scripts\\scripts.lst").decode("cp1252", "replace").splitlines()
    names = [line.split(";")[0].strip().lower().removesuffix(".int") for line in lst]
    db = sqlite3.connect(db_path)
    db.execute("drop table if exists labels")
    db.execute("create table labels (file text, num integer, label text, script text)")
    stats: Counter = Counter()
    rows = set()
    listing = files.list()
    scripts = sorted(p for p in listing if p.lower().startswith("scripts\\") and p.lower().endswith(".int"))
    listed = set(names)
    for path in scripts:
        script = Path(path.replace("\\", "/")).stem.lower()
        if script not in listed:
            # Leftovers (copies, cut scripts) never run: nothing can load a
            # script scripts.lst doesn't name, and their list ids refer to
            # whatever scripts.lst they were compiled against, so they would
            # label other characters' lines (161 such files).
            stats["skipped, not in scripts.lst"] += 1
            continue
        try:
            code = decode(files.read(path))
        except (struct.error, ValueError):
            stats["undecodable scripts"] += 1
            continue
        for k, (op, _) in enumerate(code):
            if op not in MSG_OPS:
                continue
            label, after = MSG_OPS[op]
            msg, list_id = msg_arg(code, k, after)
            dynamic = False
            if label == "message_str":
                label, dynamic = consumer(code, k)
            elif k >= 1 and code[k - 1 - after][0] in (OP_MESSAGE_STR, OP_ADD, PUSH_STRING):
                # The text argument is a string (message_str, maybe joined):
                # that message_str call is labelled where it stands.
                stats[f"{label} via string"] += 1
                continue
            if msg is None or list_id is None:
                stats[f"unresolved {label}"] += 1
                continue
            if not (1 <= list_id <= len(names)):
                stats["bad list id"] += 1
                continue
            msg_file = names[list_id - 1] + ".msg"
            nums = range(msg[0], msg[1] + 1) if isinstance(msg, tuple) else [msg]
            tag = label + ("+dynamic" if dynamic else "")
            for num in nums:
                rows.add((msg_file, num, tag, script))
            stats[f"{tag}{' (range)' if isinstance(msg, tuple) else ''}"] += 1
    db.executemany("insert into labels values (?, ?, ?, ?)", sorted(rows))
    db.commit()
    stats["scripts"] = len(scripts)
    # Coverage of the dialogue lines: entries with at least one label.
    labelled = db.execute(
        "select count(*), sum(l.words) from lines l where l.source='dialog' and exists (select 1 from labels b where b.file=l.file and b.num=l.num)"
    ).fetchone()
    total = db.execute("select count(*), sum(words) from lines where source='dialog'").fetchone()
    per_label = {}
    for label in ("reply", "option", "float", "display", "other"):
        per_label[label] = db.execute(
            "select count(*), coalesce(sum(l.words), 0) from lines l where l.source='dialog' and exists "
            "(select 1 from labels b where b.file=l.file and b.num=l.num and b.label like ?)", (label + "%",)
        ).fetchone()
    db.close()
    return {"stats": dict(stats), "labelled": labelled, "total": total, "per_label": per_label}
