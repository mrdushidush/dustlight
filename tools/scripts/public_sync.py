"""Brings the public copy (PLAN.md R9) up to one private commit, locally. Pushes and commits nothing.

public_export.py made the public repository once (upstream base + one commit); each release since adds
commits there. This writes into the public copy's working tree exactly what public_export.py would ship
from <commit>: its files without the internal documents and upstream's CI workflows, our own text files
with the name rewrite. Files the commit doesn't have are deleted. Then `git add -A` and a summary; the
commit (with its message and author) stays a separate, reviewed step.

  tools/pipeline/.venv/Scripts/python.exe -I tools/scripts/public_sync.py --public <public copy> --commit <sha>

The private history never reaches the public copy: the files come from `git archive` of the one commit.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_export import INTERNAL, OURS, UPSTREAM_CI, git, rewrite  # noqa: E402


def crlf_paths(public: Path) -> set[str]:
    """The public copy's text files stored with CRLF (upstream's .ini files and a few of ours): they stay so."""
    out = set()
    for path in git(public, "ls-files", "-z").split("\0"):
        if path and (public / path).is_file():
            data = (public / path).read_bytes()
            if b"\0" not in data[:8192] and b"\r\n" in data:
                out.add(path)
    return out


def exported(repo: Path, commit: str, keep_name: bool, keep_crlf: set[str]) -> dict[str, bytes]:
    tar = subprocess.run(["git", "archive", "--format=tar", commit], cwd=repo, check=True, capture_output=True).stdout
    drop = [p.rstrip("/") for p in INTERNAL] + [f".github/workflows/{f}" for f in UPSTREAM_CI]
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for member in archive:
            if not member.isfile():
                continue
            path = member.name
            if any(path == d or path.startswith(d + "/") for d in drop):
                continue
            data = archive.extractfile(member).read()
            if path.startswith(OURS) and b"\0" not in data[:8192]:
                data = rewrite(path, data.decode("utf-8", "surrogateescape"), keep_name).encode("utf-8", "surrogateescape")
                # Our text files are LF in the public copy; a few came into the private one with CRLF.
                if b"\r\n" in data and path not in keep_crlf:
                    data = data.replace(b"\r\n", b"\n")
            files[path] = data
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", required=True, help="the public copy (a clone of the public repository)")
    ap.add_argument("--commit", required=True, help="the private commit to mirror")
    ap.add_argument("--keep-name", action="store_true")
    ap.add_argument("--merging", action="store_true",
                    help="the public copy is in a merge of upstream (git merge --no-commit): its result becomes the commit's export")
    ap.add_argument("--take", action="append", default=[], metavar="PATH",
                    help="this file as --take-from has it (e.g. a document whose older text named this PC's folders)")
    ap.add_argument("--take-from", default="", metavar="COMMIT")
    args = ap.parse_args()
    if bool(args.take) != bool(args.take_from):
        print("--take and --take-from go together")
        return 2

    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    public = Path(args.public)
    in_merge = (public / ".git" / "MERGE_HEAD").exists()
    if args.merging != in_merge:
        print(f"{public}: {'no merge in progress' if args.merging else 'a merge is in progress (pass --merging)'}")
        return 2
    if not args.merging and git(public, "status", "--porcelain").strip():
        print(f"{public} has uncommitted changes: commit or reset them first")
        return 2
    keep_crlf = crlf_paths(public)
    files = exported(repo, args.commit, args.keep_name, keep_crlf)
    if args.take:
        later = exported(repo, args.take_from, args.keep_name, keep_crlf)
        for path in args.take:
            if path not in later:
                print(f"--take {path}: not in {args.take_from}")
                return 2
            files[path] = later[path]
    tracked = {p for p in git(public, "ls-files", "-z").split("\0") if p}
    written = removed = 0
    for path, data in files.items():
        target = public / path
        if not target.exists() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
    for path in sorted(tracked - files.keys()):
        (public / path).unlink(missing_ok=True)
        removed += 1
    git(public, "add", "-A")
    print(f"{args.commit}: {len(files)} files exported, {written} written, {removed} removed")
    print(git(public, "diff", "--cached", "--stat") or "no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
