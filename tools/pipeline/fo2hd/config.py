"""Machine paths from hd.local.ini (the same file tools/scripts/*.ps1 read)."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Paths:
    pristine: Path
    working_copy: Path
    data_root: Path

    @property
    def captures(self) -> Path:
        return self.data_root / "captures"

    @property
    def cache(self) -> Path:
        return self.data_root / "cache"

    @property
    def reports(self) -> Path:
        return self.data_root / "reports"


def ini_path(ini: Path | None = None) -> Path:
    # FO2HD_INI: another machine-paths file (PLAN.md R6, the release's clean-install copy;
    # P8, hd.fo1.ini for Fallout 1 through Et Tu). A relative path is the repo's.
    env = os.environ.get("FO2HD_INI")
    ini = Path(ini) if ini else (Path(env) if env else REPO_ROOT / "hd.local.ini")
    return ini if ini.is_absolute() else REPO_ROOT / ini


def read_ini(ini: Path | None = None) -> configparser.ConfigParser:
    ini = ini_path(ini)
    if not ini.exists():
        raise SystemExit(f"Missing {ini}. Copy hd.local.ini.example to hd.local.ini and set the paths.")
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    parser.read(ini, encoding="utf-8")
    return parser


def load_paths(ini: Path | None = None) -> Paths:
    section = read_ini(ini)["paths"]
    return Paths(
        pristine=Path(section["pristine"]),
        working_copy=Path(section["working_copy"]),
        data_root=Path(section["data_root"]),
    )
