"""Voice track V0.4 (PLAN.md §6.2): what there is to voice, and a spot check.

Counts from lines.sqlite (V0.1 lines, V0.2 labels, V0.3 speakers): entries,
words and hours of speech (at 150 words a minute) per label, per source and
per town; the town of a line is the town of the maps its speaker stands on
(data\\city.txt: each area's entrances name its maps by maps.txt lookup
name; random-encounter maps and the rest count as "elsewhere").

The spot check: 50 dialogue entries drawn with a fixed seed, stratified so
every label shows up (18 replies, 12 options, 10 floats, 6 message-box lines,
4 other or unlabelled), each with its label(s), speaker and the entries
around it. The author marks each label right or wrong on the page and copies the
result back. The page shows game text: local only, never published.

Output: <data_root>/reports/voice-v0/index.html and counts.json.
"""

from __future__ import annotations

import html
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict

from .config import Paths
from .dat import GameFiles
from .maps import read_maps_txt

WPM = 150
SAMPLE = (("reply", 18), ("option", 12), ("float", 10), ("display", 6), ("other", 4))
SEED = 20261104


def towns(files: GameFiles) -> dict[str, str]:
    """map file name (no extension) -> area name, from city.txt entrances."""
    by_lookup = {v["lookup_name"].lower(): k for k, v in read_maps_txt(files).items()}
    out: dict[str, str] = {}
    area = ""
    for line in files.read("data\\city.txt").decode("cp1252", "replace").splitlines():
        s = line.split(";")[0].strip()
        m = re.match(r"area_name\s*=\s*(.+)", s, re.I)
        if m:
            area = m.group(1).strip()
            continue
        m = re.match(r"entrance_\d+\s*=\s*([^,]*),[^,]*,[^,]*,([^,]+),", s, re.I)
        if m and area:
            name = by_lookup.get(m.group(2).strip().lower())
            if name:
                out.setdefault(name, area)
    return out


def _hours(words: int) -> float:
    return round(words / WPM / 60, 1)


def build(paths: Paths, files: GameFiles) -> dict:
    db = sqlite3.connect(paths.data_root / "voice" / "lines.sqlite")
    for table in ("lines", "labels", "speakers"):
        if not db.execute("select count(*) from sqlite_master where name=?", (table,)).fetchone()[0]:
            raise SystemExit(f"no table {table}: run voice-lines, voice-scan and voice-speakers first")

    # Per source and per label (an entry used two ways counts under both labels).
    sources = {s: {"entries": n, "words": w or 0, "hours": _hours(w or 0), "voiced": v}
               for s, n, w, v in db.execute("select source, count(*), sum(words), sum(audio <> '') from lines group by source")}
    labels = {}
    for label, n, w in db.execute(
        "select b.label, count(*), sum(l.words) from (select distinct file, num, label from labels) b "
        "join lines l on l.file=b.file and l.num=b.num where l.source='dialog' group by b.label"
    ):
        labels[label] = {"entries": n, "words": w or 0, "hours": _hours(w or 0)}
    unlabelled = db.execute(
        "select count(*), sum(words) from lines l where source='dialog' and not exists (select 1 from labels b where b.file=l.file and b.num=l.num)"
    ).fetchone()
    labels["unlabelled"] = {"entries": unlabelled[0], "words": unlabelled[1] or 0, "hours": _hours(unlabelled[1] or 0)}
    # What gets a generated voice: NPC replies and floats not voiced already, holodisks.
    todo = db.execute(
        "select count(*), sum(l.words) from lines l where l.source='dialog' and l.audio='' and exists "
        "(select 1 from labels b where b.file=l.file and b.num=l.num and (b.label like 'reply%' or b.label like 'float%'))"
    ).fetchone()
    to_voice = {
        "dialogue": {"entries": todo[0], "words": todo[1] or 0, "hours": _hours(todo[1] or 0)},
        "holodisks": {k: sources.get("holodisk", {}).get(k, 0) for k in ("entries", "words", "hours")},
        "combat taunts (optional)": {k: sources.get("combatai", {}).get(k, 0) for k in ("entries", "words", "hours")},
    }

    # Per town: reply and float words by the town of the speaker's maps.
    town_of = towns(files)
    script_town: dict[str, str] = {}
    places: dict[str, Counter] = defaultdict(Counter)
    for script, place, count in db.execute("select script, place, count from speakers where source='map'"):
        places[script][town_of.get(place, "elsewhere")] += count
    for script, c in places.items():
        script_town[script] = c.most_common(1)[0][0]
    per_town: dict[str, Counter] = defaultdict(Counter)
    seen = set()
    for file, num, label, script, words in db.execute(
        "select b.file, b.num, b.label, b.script, l.words from labels b join lines l on l.file=b.file and l.num=b.num "
        "where l.source='dialog' and l.audio=''"
    ):
        base = label.split("+")[0]
        if base not in ("reply", "float") or (file, num, base) in seen:
            continue
        seen.add((file, num, base))
        town = script_town.get(script, "no map speaker")
        per_town[town][base] += words or 0
    town_rows = sorted(
        ({"town": t, "reply_words": c["reply"], "float_words": c["float"], "hours": _hours(c["reply"] + c["float"])} for t, c in per_town.items()),
        key=lambda r: -(r["reply_words"] + r["float_words"]),
    )

    sample = _sample(db)
    db.close()
    counts = {"sources": sources, "labels": labels, "to_voice": to_voice, "towns": town_rows, "wpm": WPM}
    out = paths.data_root / "reports" / "voice-v0"
    out.mkdir(parents=True, exist_ok=True)
    (out / "counts.json").write_text(json.dumps(counts, indent=1), encoding="utf-8")
    (out / "index.html").write_text(_page(counts, sample), encoding="utf-8")
    return {"counts": counts, "page": out / "index.html", "sample": len(sample)}


def _sample(db: sqlite3.Connection) -> list[dict]:
    rng = random.Random(SEED)
    picked: list[dict] = []
    taken = set()
    for label, n in SAMPLE:
        if label == "other":
            rows = db.execute(
                "select l.file, l.num from lines l where l.source='dialog' and l.words > 0 and ("
                "not exists (select 1 from labels b where b.file=l.file and b.num=l.num) or "
                "exists (select 1 from labels b where b.file=l.file and b.num=l.num and b.label like 'other%'))"
            ).fetchall()
        else:
            rows = db.execute(
                "select distinct l.file, l.num from lines l join labels b on b.file=l.file and b.num=l.num "
                "where l.source='dialog' and l.words > 0 and b.label like ?", (label + "%",)
            ).fetchall()
        rows = [r for r in sorted(rows) if r not in taken]
        for file, num in rng.sample(rows, min(n, len(rows))):
            taken.add((file, num))
            picked.append({"file": file, "num": num, "drawn_as": label})
    for item in picked:
        file, num = item["file"], item["num"]
        item["text"] = db.execute("select text from lines where file=? and num=?", (file, num)).fetchone()[0]
        item["labels"] = sorted({r[0] for r in db.execute("select label from labels where file=? and num=?", (file, num))}) or ["(none)"]
        scripts = sorted({r[0] for r in db.execute("select script from labels where file=? and num=?", (file, num))})
        item["scripts"] = scripts
        speakers = []
        for script in scripts:
            for name, gender, kill, kind, source in db.execute(
                "select distinct name, gender, kill_type, kind, source from speakers where script=? and source <> 'mapscript'", (script,)
            ):
                speakers.append(f"{name or '?'} ({kill or kind}{', ' + gender if gender and kill not in ('man', 'woman') else ''})")
        item["speakers"] = sorted(set(speakers))[:4]
        item["before"] = [r[0] for r in db.execute("select text from lines where file=? and num<? order by num desc limit 2", (file, num))][::-1]
        item["after"] = [r[0] for r in db.execute("select text from lines where file=? and num>? order by num limit 1", (file, num))]
    return picked


def _page(counts: dict, sample: list[dict]) -> str:
    e = html.escape

    def table(rows, cols):
        head = "".join(f"<th>{e(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{e(str(r[c]))}</td>" for c in cols) + "</tr>" for r in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    to_voice = [{"what": k, **v} for k, v in counts["to_voice"].items()]
    labels = [{"label": k, **v} for k, v in sorted(counts["labels"].items(), key=lambda kv: -kv[1]["words"])]
    cards = []
    for i, s in enumerate(sample, 1):
        ctx_before = "".join(f"<p class=ctx>{e(t)}</p>" for t in s["before"])
        ctx_after = "".join(f"<p class=ctx>{e(t)}</p>" for t in s["after"])
        speakers = ", ".join(e(x) for x in s["speakers"]) or "none found"
        options_only = all(label.startswith("option") for label in s["labels"])
        who = f"you, to: {speakers}" if options_only else f"said by: {speakers}"
        cards.append(f"""
<section class=card id=c{i} data-id="{i}">
  <div class=meta><b>{i}.</b> {e(s['file'])} #{s['num']} · label: <b>{e(', '.join(s['labels']))}</b> · {who}</div>
  {ctx_before}<p class=line>{e(s['text'])}</p>{ctx_after}
  <div class=ask>
    <label><input type=radio name=l{i} value=ok> label right</label>
    <label><input type=radio name=l{i} value=wrong> label wrong</label>
    <label><input type=radio name=l{i} value=unsure> can't tell</label>
    <input class=note name=n{i} placeholder="note (optional)">
  </div>
</section>""")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Voice line check</title>
<style>
:root {{ --bg:#f6f5f1; --fg:#1d1c1a; --muted:#6b675f; --card:#fff; --line:#ddd8cc; --accent:#3d6b4f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#171716; --fg:#e8e5dd; --muted:#9c978c; --card:#22211f; --line:#3a3833; --accent:#7fb38f; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif; margin:0 auto; max-width:900px; padding:24px 16px 80px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:28px 0 8px; }}
p.lead {{ color:var(--muted); margin:0 0 16px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; margin-bottom:8px; }}
th,td {{ border-bottom:1px solid var(--line); padding:4px 8px; text-align:left; }} td:not(:first-child),th:not(:first-child) {{ text-align:right; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin:10px 0; }}
.meta {{ color:var(--muted); font-size:13px; margin-bottom:6px; }} .meta b {{ color:var(--fg); }}
.ctx {{ color:var(--muted); font-size:13px; margin:2px 0; }} .line {{ font-size:16px; margin:6px 0; border-left:3px solid var(--accent); padding-left:10px; }}
.ask {{ display:flex; flex-wrap:wrap; gap:6px 16px; align-items:center; margin-top:8px; font-size:14px; }}
.note {{ flex:1; min-width:160px; background:transparent; color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:3px 6px; }}
#bar {{ position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:10px 16px; display:flex; gap:12px; align-items:center; justify-content:center; }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:6px; padding:7px 14px; font-size:14px; cursor:pointer; }}
textarea {{ width:100%; height:120px; margin-top:8px; display:none; background:var(--card); color:var(--fg); border:1px solid var(--line); }}
</style></head><body>
<h1>Voice line check (V0)</h1>
<p class=lead>What the game has to voice, and 50 lines to check. Each line shows the label the script scan gave it: <b>reply</b> = the NPC says it in a conversation, <b>option</b> = a choice you pick (stays silent), <b>float</b> = words over someone's head, <b>display</b> = the message box, <b>other</b> = used some other way. Grey lines are the entries around it in the same file.</p>
<h2>To voice</h2>{table(to_voice, ["what", "entries", "words", "hours"])}
<h2>Dialogue by label</h2>{table(labels, ["label", "entries", "words", "hours"])}
<h2>Replies and floats by town (words)</h2>{table(counts["towns"], ["town", "reply_words", "float_words", "hours"])}
<p class=lead>Hours at {counts["wpm"]} words a minute. Lines that already have speech in the game (the talking heads) are left out of "to voice".</p>
<h2>50 lines to check</h2>
<p class=lead>For each: is the <b>label</b> right? The speaker is shown for information.</p>
{''.join(cards)}
<textarea id=out readonly></textarea>
<div id=bar><span id=done>0 / {len(sample)} marked</span><button id=copy>Copy results</button></div>
<script>
const N = {len(sample)};
const key = 'voice-v0-check';
function collect() {{
  const r = {{}};
  for (let i = 1; i <= N; i++) {{
    const v = document.querySelector(`input[name=l${{i}}]:checked`);
    const n = document.querySelector(`input[name=n${{i}}]`).value.trim();
    if (v || n) r[i] = [v ? v.value : '', n];
  }}
  return r;
}}
function update() {{
  const r = collect();
  document.getElementById('done').textContent = Object.values(r).filter(x => x[0]).length + ' / ' + N + ' marked';
  try {{ localStorage.setItem(key, JSON.stringify(r)); }} catch (e) {{}}
}}
try {{
  const saved = JSON.parse(localStorage.getItem(key) || '{{}}');
  for (const [i, [v, n]] of Object.entries(saved)) {{
    if (v) {{ const el = document.querySelector(`input[name=l${{i}}][value=${{v}}]`); if (el) el.checked = true; }}
    const t = document.querySelector(`input[name=n${{i}}]`); if (t) t.value = n || '';
  }}
}} catch (e) {{}}
document.addEventListener('input', update);
update();
document.getElementById('copy').onclick = async () => {{
  const r = collect();
  const ok = Object.values(r).filter(x => x[0] === 'ok').length;
  const lines = ['voice V0 check: ' + ok + ' right of ' + Object.values(r).filter(x => x[0]).length + ' marked'];
  for (const [i, [v, n]] of Object.entries(r)) if (v !== 'ok' || n) lines.push(i + ': ' + (v || '-') + (n ? ' (' + n + ')' : ''));
  const text = lines.join('\\n');
  const out = document.getElementById('out');
  out.value = text; out.style.display = 'block';
  try {{ await navigator.clipboard.writeText(text); document.getElementById('copy').textContent = 'Copied'; }} catch (e) {{ out.select(); }}
}};
</script></body></html>"""
