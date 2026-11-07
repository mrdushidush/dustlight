"""Map objects (voice track V0.3): what every map file places, read offline.

After the tile grids (maps.parse, `objects_offset`) a map file holds, as
map.cc mapLoad reads it (big-endian int32 throughout):

  scripts  scripts.cc scriptLoadAll: for each of the 5 script types (system,
           spatial, timed, item, critter) a count, then ceil(count / 16)
           extents of 16 records plus the extent's length and a next field.
           A record (scriptRead): sid, next; spatial (sid >> 24 == 1) adds
           built tile + radius, timed (== 2) adds time; then 14 fields, the
           second of which is the scripts.lst index. All 16 slots are read
           even when the extent holds fewer scripts.
  objects  object.cc objectLoadAllInternal: a total count, then per elevation
           a count and the objects. An object (objectRead): 18 fields (id,
           tile, x, y, sx, sy, frame, rotation, fid, flags, elevation, pid,
           cid, light distance, light intensity, outline, sid, script index),
           then proto.cc objectDataRead: inventory length, capacity, pointer;
           a critter adds 11 fields (reaction, 7 combat fields, hp,
           radiation, poison), anything else a flags field plus, by its
           proto: weapon 2, ammo/misc/key 1, door 1, stairs 2, elevator 2,
           ladder 2 (1 in version 19 maps), exit grid 4. Then the inventory:
           per item a quantity and a nested object (object.cc
           _obj_load_obj).

A map object's script is the map script with its sid (the loader sets
scriptIndex from it). Protos give what the object is (protos.Protos).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import maps
from .dat import GameFiles

I32 = struct.Struct(">i")
OBJECT_HEAD = struct.Struct(">18i")
SCRIPT_TAIL = struct.Struct(">14i")
EXTENT = 16
SCRIPT_TYPES = 5
SPATIAL, TIMED = 1, 2

OBJ_ITEM, OBJ_CRITTER, OBJ_SCENERY, OBJ_WALL, OBJ_TILE, OBJ_MISC = range(6)
PROTO_DIRS = ("items", "critters", "scenery", "walls", "tiles", "misc")
ITEM_WEAPON, ITEM_AMMO, ITEM_MISC, ITEM_KEY = 3, 4, 5, 6
SCENERY_DOOR, SCENERY_STAIRS, SCENERY_ELEVATOR, SCENERY_LADDER_UP, SCENERY_LADDER_DOWN = range(5)
EXIT_GRID_PIDS = range((OBJ_MISC << 24) | 16, ((OBJ_MISC << 24) | 23) + 1)

# proto_types.h KillType, BodyType
KILL_TYPES = (
    "man", "woman", "child", "super mutant", "ghoul", "brahmin", "radscorpion", "rat", "floater",
    "centaur", "robot", "dog", "mantis", "deathclaw", "plant", "gecko", "alien", "giant ant", "big bad boss",
)
BODY_TYPES = ("biped", "quadruped", "robotic")
STAT_AGE, STAT_GENDER = 33, 34


class Protos:
    """Proto files by pid (proto\\<type>\\<type>.lst line = pid index - 1), cached."""

    def __init__(self, files: GameFiles):
        self.files = files
        self._lst: dict[int, list[str]] = {}
        self._raw: dict[int, bytes | None] = {}
        self._names: dict[str, dict[int, str]] = {}

    def _list(self, kind: int) -> list[str]:
        if kind not in self._lst:
            d = PROTO_DIRS[kind]
            text = self.files.read(f"proto\\{d}\\{d}.lst").decode("cp1252", "replace")
            self._lst[kind] = [line.split(";")[0].strip().lower() for line in text.splitlines()]
        return self._lst[kind]

    def pids(self, kind: int) -> list[int]:
        """Every pid the type's .lst names."""
        return [(kind << 24) | (i + 1) for i, name in enumerate(self._list(kind)) if name]

    def path(self, pid: int) -> str | None:
        kind, index = (pid >> 24) & 0xFF, pid & 0xFFFFFF
        if kind >= len(PROTO_DIRS) or index < 1:
            return None
        lst = self._list(kind)
        if index > len(lst) or not lst[index - 1]:
            return None
        return f"proto\\{PROTO_DIRS[kind]}\\{lst[index - 1]}"

    def raw(self, pid: int) -> bytes | None:
        if pid not in self._raw:
            p = self.path(pid)
            self._raw[pid] = self.files.read(p) if p and self.files.exists(p) else None
        return self._raw[pid]

    def subtype(self, pid: int) -> int | None:
        """Item type or scenery type (the int32 after pid, message, fid, light x2, flags x2, sid)."""
        b = self.raw(pid)
        return I32.unpack_from(b, 32)[0] if b and len(b) >= 36 else None

    def message_id(self, pid: int) -> int | None:
        b = self.raw(pid)
        return I32.unpack_from(b, 4)[0] if b else None

    def critter(self, pid: int) -> dict | None:
        """critter.cc protoCritterDataRead fields a voice needs."""
        b = self.raw(pid)
        if not b or (pid >> 24) != OBJ_CRITTER or len(b) < 412:
            return None
        v = struct.unpack_from(">12i", b, 0)
        base = struct.unpack_from(">35i", b, 48)
        body, _exp, kill = struct.unpack_from(">3i", b, 400)
        return {
            "message_id": v[1],
            "fid": v[2],
            "sid": v[7],
            "head_fid": v[8],
            "gender": "female" if base[STAT_GENDER] == 1 else "male",
            "age": base[STAT_AGE],
            "body_type": BODY_TYPES[body] if 0 <= body < len(BODY_TYPES) else str(body),
            "kill_type": KILL_TYPES[kill] if 0 <= kill < len(KILL_TYPES) else str(kill),
        }

    def name(self, pid: int) -> str:
        """The proto's name: pro_<type>.msg entry at its message id."""
        kind = (pid >> 24) & 0xFF
        msg = {OBJ_ITEM: "pro_item", OBJ_CRITTER: "pro_crit", OBJ_SCENERY: "pro_scen", OBJ_WALL: "pro_wall", OBJ_MISC: "pro_misc"}.get(kind)
        mid = self.message_id(pid)
        if msg is None or mid is None:
            return ""
        if msg not in self._names:
            from .voice_lines import parse_msg

            path = f"text\\english\\game\\{msg}.msg"
            self._names[msg] = {n: t for n, _a, t in parse_msg(self.files.read(path))} if self.files.exists(path) else {}
        return self._names[msg].get(mid, "")


@dataclass
class MapObject:
    pid: int
    fid: int
    tile: int
    elevation: int
    sid: int
    script_index: int  # scripts.lst index, -1 = none
    owner_pid: int = -1  # the container or critter holding it (inventory)
    inventory: list = field(default_factory=list)


def read_scripts(data: bytes, offset: int) -> tuple[dict[int, int], int]:
    """sid -> scripts.lst index for every map script, and the offset after them."""
    scripts: dict[int, int] = {}
    for _type in range(SCRIPT_TYPES):
        (count,) = I32.unpack_from(data, offset)
        offset += 4
        if count < 0:
            raise ValueError(f"script count {count}")
        for extent in range((count + EXTENT - 1) // EXTENT):
            used = min(EXTENT, count - extent * EXTENT)
            for slot in range(EXTENT):
                sid = I32.unpack_from(data, offset)[0]
                offset += 8
                kind = sid >> 24
                offset += 8 if kind == SPATIAL else 4 if kind == TIMED else 0
                tail = SCRIPT_TAIL.unpack_from(data, offset)
                offset += SCRIPT_TAIL.size
                if slot < used:
                    scripts[sid] = tail[1]
            offset += 8  # extent length, next
    return scripts, offset


def _object(data: bytes, offset: int, protos: Protos, version: int, owner_pid: int) -> tuple[MapObject, int]:
    f = OBJECT_HEAD.unpack_from(data, offset)
    offset += OBJECT_HEAD.size
    pid = f[11]
    obj = MapObject(pid=pid, fid=f[8], tile=f[1], elevation=f[10], sid=f[16], script_index=f[17], owner_pid=owner_pid)
    length, _capacity, _pointer = struct.unpack_from(">3i", data, offset)
    offset += 12
    kind = (pid >> 24) & 0xFF
    if kind == OBJ_CRITTER:
        offset += 11 * 4
    else:
        offset += 4
        if kind == OBJ_ITEM:
            offset += {ITEM_WEAPON: 8, ITEM_AMMO: 4, ITEM_MISC: 4, ITEM_KEY: 4}.get(protos.subtype(pid), 0)
        elif kind == OBJ_SCENERY:
            ladder = 4 if version == 19 else 8
            offset += {SCENERY_DOOR: 4, SCENERY_STAIRS: 8, SCENERY_ELEVATOR: 8, SCENERY_LADDER_UP: ladder, SCENERY_LADDER_DOWN: ladder}.get(protos.subtype(pid), 0)
        elif kind == OBJ_MISC and pid in EXIT_GRID_PIDS:
            offset += 16
    if length < 0:
        raise ValueError(f"inventory length {length}")
    for _ in range(length):
        offset += 4  # quantity
        item, offset = _object(data, offset, protos, version, pid)
        obj.inventory.append(item)
    return obj, offset


def read(data: bytes, protos: Protos) -> dict:
    """Header, map scripts and every object (inventories nested) of one map file."""
    m = maps.parse(data)
    scripts, offset = read_scripts(data, m["objects_offset"])
    (total,) = I32.unpack_from(data, offset)
    offset += 4
    objects: list[MapObject] = []
    for _elevation in range(3):
        (count,) = I32.unpack_from(data, offset)
        offset += 4
        for _ in range(count):
            obj, offset = _object(data, offset, protos, m["version"], -1)
            objects.append(obj)
    # The loader takes the script index from the object's map script.
    for obj in walk(objects):
        obj.script_index = scripts.get(obj.sid, -1) if obj.sid != -1 else -1
    return {
        "version": m["version"],
        "header_script": m["script"],
        "scripts": scripts,
        "objects": objects,
        "total": total,
        "end": offset,
        "size": len(data),
    }


def walk(objects: list[MapObject]):
    """Every object, inventories included."""
    for obj in objects:
        yield obj
        yield from walk(obj.inventory)
