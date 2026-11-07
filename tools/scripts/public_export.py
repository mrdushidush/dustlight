"""Builds the public copy of this repository, locally (PLAN.md R9). Pushes nothing.

A fresh clone in --out, reset to the upstream commit this project forked from, with
everything since as ONE commit, so the private history (this PC's paths, the mail address
in 126 commits) isn't in it. The working repository isn't touched. On top of that, unless
told otherwise:
  --keep-name       keep the owner's first name (default: "the author" in our own files)
  --keep-internal   keep the planning and status documents and report notes
  --keep-upstream-ci  keep upstream's GitHub workflows (default: only hd-ci, this project
                    is Windows-only and they'd fire on the first push)
Afterwards it runs scrub_check.py --history inside the copy and fails if anything is found.

  tools/pipeline/.venv/Scripts/python.exe -I tools/scripts/public_export.py --out <new dir>
      --author-name mrdushidush --author-email <id>+mrdushidush@users.noreply.github.com
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

UPSTREAM_BASE = "ebb9c716"  # fallout2-ce/fallout2-ce at the fork (PLAN.md)

# Planning and status: written to one reader, full of day-to-day detail.
INTERNAL = ["PROJECT_BRIEF.md", "docs/STATUS.md", "docs/phase1", "docs/phase3", "docs/phase4", "docs/phase5", "docs/phase6"]
UPSTREAM_CI = ["ci-build.yml", "ci-build-web.yml", "ci-delete-pr-preview.yml", "codeql.yml", "auto-format.yml",
               "release.yml", "reusable-build-ce-dat.yml"]
# Our own files; upstream's and third-party text isn't ours to change.
OURS = ("src/hd/", "tools/", "docs/", "PLAN.md", "NOTICE.md", "README.md", "CMakeLists.txt", ".gitignore",
        "hd.local.ini.example", "src/object.cc", ".github/workflows/hd-ci.yml")
# What may keep naming the old title: the decision record and the check against it.
TITLE_STAYS = ("docs/decisions/0007-name-and-license.md", "tools/pipeline/fo2hd/release.py", "PLAN.md", "tools/scripts/public_export.py")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def rewrite(path: str, text: str, keep_name: bool) -> str:
    if path not in TITLE_STAYS:
        text = text.replace("Fallout HD-2D", "Dustlight")
    if keep_name or path == "tools/scripts/public_export.py":
        return text
    text = text.replace("Modified by David (GitHub `mrdushidush`)", "Modified by `mrdushidush` (GitHub)")
    # The sentence-start form first. "David R. Tribble" wrote fpattern; he stays.
    text = re.sub(r"(^|[.!?:]\s+|\*\*)David(?! R\. Tribble)", lambda m: m.group(1) + "The author", text, flags=re.M)
    return re.sub(r"\bDavid(?! R\. Tribble)", "the author", text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="a folder that doesn't exist yet")
    ap.add_argument("--author-name", required=True)
    ap.add_argument("--author-email", required=True, help="a GitHub noreply address keeps the real one private")
    ap.add_argument("--trailer", action="append", default=[], help="a line for the end of the commit message")
    ap.add_argument("--keep-name", action="store_true")
    ap.add_argument("--keep-internal", action="store_true")
    ap.add_argument("--keep-upstream-ci", action="store_true")
    args = ap.parse_args()

    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    out = Path(args.out)
    if out.exists():
        print(f"{out} exists: choose a new folder (nothing is overwritten)")
        return 2
    head = git(repo, "rev-parse", "HEAD").strip()
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--no-tags", str(repo), str(out)], check=True)
    git(out, "remote", "remove", "origin")
    git(out, "checkout", "-q", "-B", "public", UPSTREAM_BASE)
    git(out, "read-tree", "-u", "--reset", head)

    drop = []
    if not args.keep_internal:
        drop += INTERNAL
    if not args.keep_upstream_ci:
        drop += [f".github/workflows/{f}" for f in UPSTREAM_CI]
    for path in drop:
        # -f: read-tree just staged them, and git rm refuses staged changes without it.
        git(out, "rm", "-r", "-q", "-f", "--ignore-unmatch", path)

    changed = 0
    for path in git(out, "ls-files", "-z").split("\0"):
        if not path or not path.startswith(OURS):
            continue
        file = out / path
        data = file.read_bytes()
        if b"\0" in data[:8192]:
            continue
        before = data.decode("utf-8", "surrogateescape")
        after = rewrite(path, before, args.keep_name)
        if after != before:
            file.write_bytes(after.encode("utf-8", "surrogateescape"))
            changed += 1
    git(out, "add", "-A")

    message = out / ".git" / "EXPORT_MSG"
    message.write_text(
        "Dustlight: HD art and new lighting for Fallout 2 CE\n\n"
        "The whole project as one commit on top of the Fallout 2 CE commit it forked from.\n"
        "See README.md, NOTICE.md and docs/decisions/.\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n" + "".join(t + "\n" for t in args.trailer),
        encoding="utf-8", newline="\n")
    git(out, "-c", f"user.name={args.author_name}", "-c", f"user.email={args.author_email}", "commit", "-q", "-F", str(message))

    # Nothing of the private history stays reachable or on disk.
    git(out, "branch", "-q", "-M", "public", "main")
    for ref in git(out, "for-each-ref", "--format=%(refname)").split():
        if ref != "refs/heads/main":
            git(out, "update-ref", "-d", ref)
    git(out, "reflog", "expire", "--expire=now", "--all")
    git(out, "gc", "-q", "--prune=now")
    message.unlink(missing_ok=True)

    print(f"{out}: {len(git(out, 'ls-files').splitlines())} files, {changed} rewritten, "
          f"{git(out, 'rev-list', '--count', f'{UPSTREAM_BASE}..HEAD').strip()} commit(s) on upstream {UPSTREAM_BASE}")
    print("authors above upstream:", sorted(set(git(out, "log", "--format=%an <%ae>", f"{UPSTREAM_BASE}..HEAD").splitlines())))
    if not args.keep_name:
        left = git(out, "grep", "-n", "-E", r"\bDavid\b", "--", *OURS) if subprocess.run(
            ["git", "grep", "-q", "-E", r"\bDavid\b", "--", *OURS], cwd=out).returncode == 0 else ""
        left = [l for l in left.splitlines() if "Tribble" not in l and "public_export.py" not in l]
        print("first name left in our files:", left[:10] or "none")
    scrub = out / "tools" / "scripts" / "scrub_check.py"
    code = subprocess.run([sys.executable, "-I", str(scrub), "--history"], cwd=out).returncode
    print("scrub check:", "clean" if code == 0 else "FINDINGS")
    return code


if __name__ == "__main__":
    sys.exit(main())
