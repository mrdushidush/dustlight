"""Replace only the text documents in a built engine zip with freshly filled ones (PLAN.md R9).

For a late fix to a document, without building the version again: the exe and ce.dat stay byte
for byte what the tests ran, and the script stops if anything else would change. The real art
sizes go into the README in place of the estimates, and SHA256SUMS.txt, release.json and
nexus.md follow. The old files are kept in --keep. Run it only when no test reads the zip.

  python -I tools/scripts/release_docs_patch.py --dist <dist\\version> --docs <reports\\player-docs> --keep <new dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

# Member of the zip -> file in the docs folder.
DOCUMENTS = {
    "{name}-README.txt": "{name}-README.txt",
    "hd/docs/NOTICE.md": "hd_docs_NOTICE.md",
    "hd/docs/THIRD_PARTY.md": "hd_docs_THIRD_PARTY.md",
    "hd/docs/CHANGELOG.md": "hd_docs_CHANGELOG.md",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 22):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--keep", required=True, help="where the old files go; a folder that doesn't exist yet")
    args = ap.parse_args()
    dist, docs, keep = Path(args.dist), Path(args.docs), Path(args.keep)
    if keep.exists():
        print(f"{keep} exists: choose a new folder (nothing is overwritten)")
        return 2

    release_file = dist / "release.json"
    release = json.loads(release_file.read_text(encoding="utf-8-sig"))
    name = release["name"]
    engine = dist / f"{name}-{release['version']}-engine.zip"
    sizes = {t: sum(f["bytes"] for f in release["files"] if f"-art-{t}x" in f["file"]) for t in release["tiers"]}

    new_docs: dict[str, bytes] = {}
    readme = f"{name}-README.txt"
    for member, source in DOCUMENTS.items():
        member, source = member.format(name=name), source.format(name=name)
        data = (docs / source).read_bytes()
        if member == readme:
            text = data.decode("utf-8")
            for tier, size in sizes.items():
                text, n = re.subn(rf"(\*\*{tier}x\*\* \| )about [\d.]+ GB", rf"\g<1>about {size / 1e9:.1f} GB", text)
                if n != 1:
                    print(f"no size for {tier}x in the README")
                    return 1
            data = text.encode("utf-8")
        new_docs[member] = data

    tmp = engine.with_suffix(".new")
    with zipfile.ZipFile(engine) as old, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as new:
        for info in old.infolist():
            out = zipfile.ZipInfo(info.filename, info.date_time)
            out.compress_type = zipfile.ZIP_DEFLATED
            out.external_attr = info.external_attr
            new.writestr(out, new_docs.get(info.filename, old.read(info)))

    with zipfile.ZipFile(engine) as old, zipfile.ZipFile(tmp) as new:
        if old.namelist() != new.namelist() or new.testzip() is not None:
            tmp.unlink()
            print("the new zip's members differ from the old ones, or it is damaged")
            return 1
        changed = [n for n in old.namelist() if old.read(n) != new.read(n)]
        if not set(changed) <= set(new_docs):
            tmp.unlink()
            print(f"more than the documents changed: {changed}")
            return 1
        exe = next(n for n in new.namelist() if n.endswith(".exe"))
        print(f"{exe} sha256 {hashlib.sha256(new.read(exe)).hexdigest()[:16]}, unchanged")
    print("documents changed:", changed)

    keep.mkdir(parents=True)
    sums = dist / "SHA256SUMS.txt"
    page = dist / "nexus.md"
    for f in (engine, sums, release_file, page):
        if f.exists():
            shutil.copy2(f, keep / f.name)
    tmp.replace(engine)

    digest = sha256(engine)
    for f in release["files"]:
        if f["file"] == engine.name:
            f["bytes"] = engine.stat().st_size
            f["sha256"] = digest
    release_file.write_text(json.dumps(release, indent=1) + "\n", encoding="utf-8", newline="\n")
    lines = [f"{digest}  {engine.name}" if line.endswith(engine.name) else line
             for line in sums.read_text(encoding="utf-8").splitlines()]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if (docs / "nexus.md").exists():
        shutil.copy2(docs / "nexus.md", page)
    print(f"{engine.name}: {engine.stat().st_size} bytes, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
