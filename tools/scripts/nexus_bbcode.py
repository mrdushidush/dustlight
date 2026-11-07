"""The mod page's description as BBCode, ready to paste into Nexus Mods (PLAN.md R9).

Takes the filled page from `release.ps1 -DocsOnly` (reports/player-docs/nexus.md), keeps the
part between its two `---` lines, and puts the real zip sizes from release.json in place of
the estimates made before the zips existed.

  python -I tools/scripts/nexus_bbcode.py --page <nexus.md> --release <release.json> --out <file>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"[b]\1[/b]", text)
    text = re.sub(r"`([^`]+)`", r"[font=Courier New]\1[/font]", text)

    def link(m: re.Match[str]) -> str:
        url = m.group(0).rstrip(".,)")
        return f"[url={url}]{url}[/url]" + m.group(0)[len(url):]

    return re.sub(r"https?://[^\s\]]+", link, text)


def to_bbcode(lines: list[str]) -> str:
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush_para() -> None:
        if para:
            out.extend([inline(" ".join(para)), ""])
            para.clear()

    def flush_list() -> None:
        if items:
            out.append("[list]")
            out.extend(f"[*]{inline(i)}" for i in items)
            out.extend(["[/list]", ""])
            items.clear()

    for line in lines:
        s = line.rstrip()
        if s.startswith("## "):
            flush_para()
            flush_list()
            out.extend([f"[size=5][b]{s[3:]}[/b][/size]", ""])
        elif re.fullmatch(r"\*\*[^*]+\*\*", s):
            # A label alone on its line stays on its own line.
            flush_para()
            flush_list()
            out.extend([inline(s), ""])
        elif s.startswith("- "):
            flush_para()
            items.append(s[2:])
        elif s.startswith("  ") and items:
            items[-1] += " " + s.strip()
        elif not s:
            flush_para()
            flush_list()
        else:
            flush_list()
            para.append(s.strip())
    flush_para()
    flush_list()
    return "\n".join(out).strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    text = Path(args.page).read_text(encoding="utf-8")
    parts = re.split(r"^---\s*$", text, flags=re.M)
    if len(parts) < 3:
        raise SystemExit("expected the page between two --- lines")
    body = parts[1]

    release = json.loads(Path(args.release).read_text(encoding="utf-8-sig"))
    for tier in release["tiers"]:
        size = sum(f["bytes"] for f in release["files"] if f"-art-{tier}x" in f["file"])
        body, n = re.subn(rf"(\*\*{tier}x\*\* \()about [\d.]+ GB\)", rf"\g<1>{size / 1e9:.1f} GB)", body)
        if n != 1:
            raise SystemExit(f"no size to replace for {tier}x")

    bbcode = to_bbcode(body.strip("\n").splitlines())
    left = re.findall(r"\{[a-z_0-9]+\}", bbcode)
    if left or "(to be published)" in bbcode:
        raise SystemExit(f"unfilled text in the page: {left}")
    Path(args.out).write_text(bbcode, encoding="utf-8", newline="\n")
    print(f"{args.out}: {len(bbcode)} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
