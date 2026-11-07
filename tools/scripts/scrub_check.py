"""Before the repository goes public (PLAN.md R9): what in it shouldn't.

Scans every tracked file (and, with --history, every blob ever committed on
top of the upstream base) for:
  - game data: Fallout file types outside upstream's own files/ and tests
  - big binaries (over --max-mb)
  - local paths and personal details: <drive>:\\Games, data roots, user folders, mail
  - things that look like credentials (API keys, tokens, private keys)

Prints findings grouped by kind; exit 1 if any. Run from the repo root:
  tools/pipeline/.venv/Scripts/python.exe -I tools/scripts/scrub_check.py [--history]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict

UPSTREAM_BASE = "ebb9c716"  # fallout2-ce/fallout2-ce at the fork (PLAN.md)
GAME_TYPES = re.compile(r"\.(frm|fr[0-5]|pal|msg|dat|acm|map|pro|int|lst|mve|lip|gam|sav|aaf|fon|rix)$", re.I)
UPSTREAM_DATA = ("files/", "sfall_testing/", "os/")
TEXT_PATTERNS = {
    "local path": re.compile(r"[A-Za-z]:\\\\?(Games|fo2hd-data|fo2hd-release|Users\\\\?david)\b", re.I),
    "mail address": re.compile(r"[\w.+-]+@(gmail|outlook|hotmail|yahoo|proton)\.\w+", re.I),
    "credential": re.compile(r"(sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})"),
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def scan_blob(path: str, data: bytes, findings: dict, max_bytes: int) -> None:
    if GAME_TYPES.search(path) and not path.startswith(UPSTREAM_DATA):
        findings["game data type"].append(path)
    if len(data) > max_bytes:
        findings["big file"].append(f"{path} ({len(data) / 1e6:.1f} MB)")
    if b"\0" in data[:8192]:
        return  # binary: no text checks
    text = data.decode("utf-8", "replace")
    for kind, pattern in TEXT_PATTERNS.items():
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            findings[kind].append(f"{path}:{line}: {m.group(0)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true", help="also every blob committed since the upstream base")
    ap.add_argument("--max-mb", type=float, default=5.0)
    ap.add_argument("--limit", type=int, default=15, help="findings shown per kind")
    args = ap.parse_args()
    findings: dict[str, list[str]] = defaultdict(list)
    max_bytes = int(args.max_mb * 1e6)

    for path in git("ls-files", "-z").split("\0"):
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        scan_blob(path, data, findings, max_bytes)

    if args.history:
        seen = set()
        for line in git("rev-list", "--objects", f"{UPSTREAM_BASE}..HEAD").splitlines():
            sha, _, path = line.partition(" ")
            if not path or sha in seen:
                continue
            seen.add(sha)
            if git("cat-file", "-t", sha).strip() != "blob":
                continue
            data = subprocess.run(["git", "cat-file", "blob", sha], check=True, capture_output=True).stdout
            scan_blob(f"(history) {path}", data, findings, max_bytes)

    authors = sorted(set(git("log", f"{UPSTREAM_BASE}..HEAD", "--format=%an <%ae>").splitlines()))
    print("commit authors since the fork (public with the history):")
    for a in authors:
        print(f"  {a}")
    total = 0
    for kind, items in sorted(findings.items()):
        uniq = sorted(set(items))
        total += len(uniq)
        print(f"\n{kind}: {len(uniq)}")
        for item in uniq[: args.limit]:
            print(f"  {item}")
        if len(uniq) > args.limit:
            print(f"  ... {len(uniq) - args.limit} more")
    print(f"\n{total} findings")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
