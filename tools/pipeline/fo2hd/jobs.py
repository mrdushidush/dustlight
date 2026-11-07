"""Resumable batch bookkeeping (PLAN.md section 3.5).

A stage processes items (game paths, asset keys) one at a time. Each finished
item is recorded with a fingerprint of its inputs; a rerun skips items whose
fingerprint is unchanged and whose outputs still exist, so a batch killed at
any point resumes where it stopped. Outputs are written to a temporary name and
renamed only when complete, so a kill never leaves a half-written file that
looks finished.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

COMMIT_SECONDS = 60.0


def fingerprint(*parts: bytes | str | int) -> str:
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, int):
            part = str(part)
        if isinstance(part, str):
            part = part.encode("utf-8")
        h.update(len(part).to_bytes(8, "little"))
        h.update(part)
    return h.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class JobDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " stage TEXT NOT NULL, item TEXT NOT NULL, fingerprint TEXT NOT NULL,"
            " output TEXT, finished REAL NOT NULL, PRIMARY KEY (stage, item))"
        )
        self._db.commit()
        self._pending = 0
        self._committed = time.monotonic()

    def is_done(self, stage: str, item: str, fp: str) -> bool:
        row = self._db.execute("SELECT fingerprint, output FROM jobs WHERE stage=? AND item=?", (stage, item)).fetchone()
        if row is None or row[0] != fp:
            return False
        return row[1] is None or Path(row[1]).exists()

    def mark_done(self, stage: str, item: str, fp: str, output: Path | None) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?)",
            (stage, item, fp, str(output) if output else None, time.time()),
        )
        self._pending += 1
        # Every 200 items or once a minute: a hard kill (p7_batch.ps1 -Stop)
        # loses at most that much of slow items like big UI frames.
        if self._pending >= 200 or time.monotonic() - self._committed >= COMMIT_SECONDS:
            self.commit()

    def forget(self, stage: str, item: str) -> None:
        """The item counts as not done (it is built again)."""
        self._db.execute("DELETE FROM jobs WHERE stage=? AND item=?", (stage, item))
        self._pending += 1

    def count(self, stage: str) -> int:
        return self._db.execute("SELECT COUNT(*) FROM jobs WHERE stage=?", (stage,)).fetchone()[0]

    def commit(self) -> None:
        self._db.commit()
        self._pending = 0
        self._committed = time.monotonic()

    def close(self) -> None:
        self.commit()
        self._db.close()
