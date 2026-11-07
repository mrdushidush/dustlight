"""Read-only access to Fallout 2 archives and the game's virtual file system.

`Dat2` reads one DAT2 archive (as src/dfile.cc dbaseOpen does). `GameFiles`
stacks archives and loose folders in the same order as CE's gameDbInit, so the
pipeline always sees the file the game sees:

    master_patches > critter_patches > patchNNN.dat (highest N first)
    > ce.dat > f2_res.dat > critter.dat > master.dat

Paths are case-insensitive with backslashes, as in the game ("art\\critters\\x.frm").
sfall mods (mods_order.txt) are not stacked; vanilla installs have none.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path


def normalize(path: str) -> str:
    return path.replace("/", "\\").lower().lstrip("\\")


@dataclass(frozen=True)
class DatEntry:
    path: str  # as stored in the archive
    compressed: bool
    size: int  # uncompressed
    packed_size: int
    offset: int  # relative to the archive's data start


class Dat2:
    """One DAT2 archive: a footer-indexed table of zlib-compressed entries."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, DatEntry] = {}
        with open(self.path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(file_size - 8)
            tree_size, data_size = struct.unpack("<II", f.read(8))
            self._data_offset = file_size - data_size
            f.seek(file_size - tree_size - 8)
            table = f.read(tree_size)

        (count,) = struct.unpack_from("<I", table, 0)
        pos = 4
        for _ in range(count):
            (name_len,) = struct.unpack_from("<I", table, pos)
            pos += 4
            name = table[pos : pos + name_len].decode("cp1252")
            pos += name_len
            compressed = table[pos] != 0
            size, packed, offset = struct.unpack_from("<III", table, pos + 1)
            pos += 13
            self.entries[normalize(name)] = DatEntry(name, compressed, size, packed, offset)

    def read(self, path: str) -> bytes:
        entry = self.entries[normalize(path)]
        with open(self.path, "rb") as f:
            f.seek(self._data_offset + entry.offset)
            data = f.read(entry.packed_size if entry.compressed else entry.size)
        if entry.compressed:
            data = zlib.decompress(data)
        if len(data) != entry.size:
            raise ValueError(f"{self.path.name}:{entry.path}: size {len(data)} != {entry.size}")
        return data

    def __contains__(self, path: str) -> bool:
        return normalize(path) in self.entries


class ZipArchive:
    """CE also accepts ZIP-format .dat files (ce.dat may be one)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path)
        self.entries = {normalize(i.filename): i for i in self._zip.infolist() if not i.is_dir()}

    def read(self, path: str) -> bytes:
        return self._zip.read(self.entries[normalize(path)])

    def __contains__(self, path: str) -> bool:
        return normalize(path) in self.entries


class Folder:
    """Loose files under a directory (the game's data\\ patches folder)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries = {}
        if self.path.is_dir():
            for file in self.path.rglob("*"):
                if file.is_file():
                    self.entries[normalize(str(file.relative_to(self.path)))] = file

    def read(self, path: str) -> bytes:
        return self.entries[normalize(path)].read_bytes()

    def __contains__(self, path: str) -> bool:
        return normalize(path) in self.entries


def open_archive(path: Path):
    if path.is_dir():
        return Folder(path)
    with open(path, "rb") as f:
        if f.read(4) == b"PK\x03\x04":
            return ZipArchive(path)
    return Dat2(path)


def _read_cfg_system(cfg: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not cfg.exists():
        return values
    section = ""
    for line in cfg.read_text(encoding="cp1252", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
        elif section == "system" and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    return values


class GameFiles:
    """The game's file system as CE assembles it from a game folder."""

    def __init__(self, game_dir: Path):
        self.game_dir = Path(game_dir)
        system = _read_cfg_system(self.game_dir / "fallout2.cfg")

        def path(key: str, default: str) -> Path:
            return self.game_dir / (system.get(key) or default)

        # Highest priority first.
        layers: list[Path] = []
        for key, default in (("master_patches", "data"), ("critter_patches", "data")):
            p = path(key, default)
            if p.is_dir() and p not in layers:
                layers.append(p)
        patches = []
        for index in range(1000):
            p = self.game_dir / f"patch{index:03d}.dat"
            if not p.exists():
                break
            patches.append(p)
        layers.extend(reversed(patches))
        for p in (self.game_dir / "ce.dat", path("f2_res_dat", "f2_res.dat"), path("critter_dat", "critter.dat"), path("master_dat", "master.dat")):
            if p.exists():
                layers.append(p)

        self.layers = [open_archive(p) for p in layers]

    def find(self, path: str):
        for layer in self.layers:
            if path in layer:
                return layer
        return None

    def read(self, path: str) -> bytes:
        layer = self.find(path)
        if layer is None:
            raise FileNotFoundError(path)
        return layer.read(path)

    def exists(self, path: str) -> bool:
        return self.find(path) is not None

    def list(self) -> dict[str, object]:
        """Every visible path -> the layer that provides it."""
        result: dict[str, object] = {}
        for layer in reversed(self.layers):
            for key in layer.entries:
                result[key] = layer
        return result
