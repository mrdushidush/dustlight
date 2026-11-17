"""The eye, E1: the quality contest. Each local vision model answers the answer key (eye_key.py), and is
scored against what it could not see: The author's votes and marks, and the planted defects.

Rules from E0 and claudette (memory: feedback-local-model-runs): parallel 1, the author's saved per-model
config, exact model keys checked after load, thinking off, MTP draft 2 on the MTP builds, no runs
16:00-19:00 May-September (room heat) unless waived. Each answer is appended to <runs>/<model>/r<run>/answers.jsonl as
it comes, so a stopped run resumes where it was (answers with an HTTP error or timeout are asked again).

Scores (score_run): agreement with the author's decided votes (both orders), order consistency and position
bias, "same" on identical pairs, clean picked over planted, planted defects found (box on the spot) per
kind and level, clean crops flagged, the author's marks found. Boxes are 0-1000 of the image the model saw;
Qwen gives x first, Gemma y first: each run is scored in the order that finds more planted defects.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import eye
from .eye_key import CROP_H, CROP_W, LOOSE_KINDS, ROUND_TITLES, VARIANTS

CONFIG_DIR = Path.home() / ".lmstudio" / ".internal" / "user-concrete-model-default-config"
QUIET_HOURS = (16, 19)  # the author: no benches 16:00-19:00 (room heat) in summer, unless he waives it
QUIET_MONTHS = range(5, 10)  # May-September only (the author, 2026-11-12)


@dataclass(frozen=True)
class Contestant:
    key: str  # exact LM Studio model key
    split: int  # defect crops cut split x split: Gemma 4 looks at ~280 tokens per image, a shrunken copy
    e0_seconds: float  # E0: seconds per defect check at the crop size it will get (estimates only)
    note: str = ""


# the author's list for E1 (11-12). The dense 27Bs are dropped; gemma-4-31b-qat and qwen3.8-27b 3.84bpw only
# after the author opens their config in the LM Studio UI once.
CONTESTANTS = [
    Contestant("qwen3.6-35b-a3b-mtp@iq3_s", 1, 8.0, "the champion, vision file copied in (E0)"),
    Contestant("qwen3.6-35b-a3b-mtp@iq4_xs", 1, 9.5, "20% experts on CPU"),
    Contestant("qwen3.6-35b-a3b@q3_k_xl", 1, 20.9, "spills"),
    Contestant("qwen3.6-35b-a3b@iq4_xs", 1, 26.3, "spills"),
    Contestant("qwen/qwen3.6-35b-a3b", 1, 24.0, "spills (Q4_K_M)"),
    Contestant("qwen3.5-9b", 1, 8.2),
    Contestant("qwen3.5-4b", 1, 3.9),
    Contestant("google/gemma-4-26b-a4b-qat", 2, 1.3),
    Contestant("google/gemma-4-e4b", 2, 1.1),
    Contestant("google/gemma-4-e2b", 2, 1.5, "E0 4.1 s on whole crops"),
]
BY_KEY = {c.key: c for c in CONTESTANTS}


def safe_name(model: str) -> str:
    return model.replace("/", "_").replace("@", "_").replace(":", "_")


# --- LM Studio facts for the plan ----------------------------------------------------------------


def lms_models() -> dict[str, dict]:
    try:
        return {m["modelKey"]: m for m in json.loads(eye.lms("ls", "--json", timeout=120))}
    except Exception:  # noqa: BLE001 - a plan without LM Studio still prints
        return {}


def saved_config(model: str, listing: dict[str, dict]) -> tuple[Path | None, dict]:
    """the author's saved per-model load config (what `lms load` uses): file and {field: value}."""
    info = listing.get(model, {})
    candidates = []
    if info.get("path"):
        candidates.append(CONFIG_DIR / (info["path"] + ".json"))
    candidates.append(CONFIG_DIR / (model.split("@")[0] + ".json"))
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            fields = {}
            for f in data.get("load", {}).get("fields", []):
                value = f["value"]
                if isinstance(value, dict) and "checked" in value:
                    value = value["value"] if value["checked"] else "off"
                fields[f["key"].replace("llm.load.llama.", "").replace("llm.load.", "")] = value
            return path, fields
    return None, {}


def config_line(fields: dict) -> str:
    if not fields:
        return "NO SAVED CONFIG (the author opens it in the LM Studio UI once)"
    short = {
        "contextLength": "ctx", "offloadRatio": "gpu", "numCpuExpertLayersRatio": "cpu experts",
        "kCacheQuantizationType": "K", "vCacheQuantizationType": "V", "tryMmap": "mmap",
        "numParallelSessions": "parallel", "flashAttention": "flash",
    }
    return ", ".join(f"{short.get(k, k)} {v}" for k, v in fields.items() if k != "keepModelInMemory")


# --- running -------------------------------------------------------------------------------------


def select(items: list[dict], subset: str) -> list[dict]:
    chosen = [i for i in items if subset == "full" or i["core"]]
    # a stopped run is still a fair sample: the order is random but fixed
    return sorted(chosen, key=lambda i: hashlib.sha1(i["id"].encode()).hexdigest())


def fingerprint(item: dict, opts: dict) -> str:
    blob = json.dumps({"prompt": eye.PROMPT_VERSION, "task": item["task"], "sha1": item["sha1"], **opts}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def item_opts(item: dict, split: int, vote_ref: bool, temperature: float, zoom: int = 1) -> dict:
    if item["task"] == "defects":
        opts = {"split": split, "temperature": temperature}
        if zoom > 1:  # only then, so the fingerprints of earlier runs stay valid
            opts["zoom"] = zoom
        return opts
    return {"ref": bool(vote_ref and item["images"].get("ref")), "temperature": temperature}


_SIZES: dict[str, tuple[int, int]] = {}


def call_seconds_estimate(item: dict, c: Contestant) -> float:
    """Rough: E0's seconds per defect check, scaled by the pixels sent (Qwen pays per pixel, Gemma per image)."""
    if item["task"] == "defects":
        return c.e0_seconds * c.split * c.split
    sizes = {}
    for role in ("a", "b", "ref"):
        p = item["images"].get(role)
        if p:
            if p not in _SIZES:
                _SIZES[p] = Image.open(p).size
            sizes[role] = _SIZES[p]
    images = len(sizes)
    if c.split > 1:  # Gemma: a fixed budget per image
        return c.e0_seconds * images / 2 * 1.2
    pixels = sum(w * h for w, h in sizes.values())
    return c.e0_seconds * (0.3 + 0.7 * pixels / (2 * CROP_W * CROP_H))


def _answer_record(a: eye.Answer, tile: list[int] | None) -> dict:
    return {
        "tile": tile, "data": a.data, "text": a.text[:300] if a.data is None else "", "s": round(a.seconds, 2),
        "pt": a.prompt_tokens, "ct": a.completion_tokens, "error": a.error,
    }


def ask_item(item: dict, opts: dict, extra: dict) -> list[dict]:
    images = item["images"]
    if item["task"] == "defects":
        orig = Image.open(images["orig"]).convert("RGB")
        hd = Image.open(images["hd"]).convert("RGB")
        split = opts["split"]
        zoom = opts.get("zoom", 1)
        tiles = [(0, 0, hd.width, hd.height)] if split == 1 else eye.crop_boxes(hd.width, hd.height, split, split, 64)
        calls = []
        for box in tiles:
            o_tile, h_tile = orig.crop(box), hd.crop(box)
            if zoom > 1:  # hard pixel edges: a 1 px seam becomes zoom px wide, inside fewer of the model's image tokens
                size = (o_tile.width * zoom, o_tile.height * zoom)
                o_tile, h_tile = o_tile.resize(size, Image.NEAREST), h_tile.resize(size, Image.NEAREST)
            # boxes come back 0-1000 of the image seen, so the tile maps them back whatever the zoom
            a = eye.ask_defects(o_tile, h_tile, extra=extra, max_tokens=900, temperature=opts["temperature"])
            calls.append(_answer_record(a, list(box)))
        return calls
    a_img = Image.open(images["a"]).convert("RGB")
    b_img = a_img if images["b"] == images["a"] else Image.open(images["b"]).convert("RGB")
    ref = Image.open(images["ref"]).convert("RGB") if opts.get("ref") else None
    answer = eye.ask_vote(a_img, b_img, ref, extra=extra, max_tokens=400, temperature=opts["temperature"])
    return [_answer_record(answer, None)]


def fake_answer(item: dict, kind: str, rng: random.Random) -> list[dict]:
    """fake:oracle answers from the truth, fake:random guesses: checks the scorer without a GPU."""
    if item["task"] == "vote":
        better = item["truth"]["better"] if kind == "oracle" else rng.choice(["A", "B", "same"])
        return [{"tile": None, "data": {"why": "", "better": better}, "text": "", "s": 0, "pt": 0, "ct": 0, "error": None}]
    w, h = item["size"]
    defects = []
    if kind == "oracle":
        for box in item["truth"]["boxes"]:
            defects.append({"kind": item["truth"].get("kind", "other"), "severity": 2, "note": "",
                            "box": [round(box[0] * 1000 / w), round(box[1] * 1000 / h), round(box[2] * 1000 / w), round(box[3] * 1000 / h)]})
    elif rng.random() < 0.5:
        x, y = rng.randint(0, 800), rng.randint(0, 800)
        defects.append({"kind": rng.choice(eye.DEFECT_KINDS), "severity": 1, "note": "", "box": [x, y, x + 150, y + 150]})
    return [{"tile": [0, 0, w, h], "data": {"defects": defects}, "text": "", "s": 0, "pt": 0, "ct": 0, "error": None}]


def read_answers(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["id"]] = row  # the last answer for an id wins
    return done


def in_quiet_hours() -> bool:
    now = time.localtime()
    return now.tm_mon in QUIET_MONTHS and QUIET_HOURS[0] <= now.tm_hour < QUIET_HOURS[1]


def load_contestant(model: str) -> dict:
    """Loads a model the contest's way: exact key from `lms ls`, the author's saved config, weights checked after load,
    one warm-up call. Returns what the session should record. Raises RuntimeError if anything is off."""
    listing = lms_models()
    info = listing.get(model)
    if info is None:
        raise RuntimeError(f"{model}: not in `lms ls` (exact keys only; lms load would fuzzy-match)")
    cfg_path, cfg = saved_config(model, listing)
    if not cfg:
        raise RuntimeError(f"{model}: no saved LM Studio config; the author opens the model in the UI once first")
    session: dict = {"config": {"file": str(cfg_path), "fields": cfg}, "vram_before_mb": eye.vram_used_mb()}
    session["load_seconds"] = round(eye.load(model, extra_args=eye.LOAD_ARGS.get(model, ())), 1)
    session["vram_mb"] = eye.vram_used_mb()
    ps = eye.loaded()
    session["loaded"] = ps
    want = info.get("path", model).replace("\\", "/").lower()
    if not ps or want not in json.dumps(ps).replace("\\\\", "/").lower():
        eye.unload_all()
        raise RuntimeError(f"{model}: lms ps does not show {want} after load: {json.dumps(ps)[:300]}")
    eye.ask_vote(Image.new("RGB", (64, 64)), Image.new("RGB", (64, 64)), extra=eye.THINK_OFF, max_tokens=200)  # warm-up
    return session


def run_model(model: str, items: list[dict], run_dir: Path, *, split: int, vote_ref: bool = True,
              temperature: float = 0.0, seed: int | None = None, any_hour: bool = False, zoom: int = 1, log=print) -> dict:
    """Answers every item not yet answered (same fingerprint, no error). Returns the run's meta."""
    run_dir.mkdir(parents=True, exist_ok=True)
    answers_path = run_dir / "answers.jsonl"
    done = read_answers(answers_path)
    todo = []
    for item in items:
        fp = fingerprint(item, item_opts(item, split, vote_ref, temperature, zoom))
        row = done.get(item["id"])
        if row and row["fp"] == fp and not any(c.get("error") for c in row["calls"]):
            continue
        todo.append((item, fp))
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"model": model, "sessions": []}
    meta.update({"model": model, "split": split, "vote_ref": vote_ref, "temperature": temperature, "seed": seed,
                 "prompt": eye.PROMPT_VERSION, "items": len(items), "zoom": zoom})
    if not todo:
        log(f"{model}: all {len(items)} items answered already")
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return meta
    if not any_hour and in_quiet_hours():
        log(f"{model}: not started, {QUIET_HOURS[0]}:00-{QUIET_HOURS[1]}:00 is no-bench time (--any-hour if the author waives it)")
        meta["stopped"] = "quiet hours"
        return meta

    fake = model.split(":", 1)[1] if model.startswith("fake:") else None
    session: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "todo": len(todo)}
    extra = dict(eye.THINK_OFF)
    if seed is not None:
        extra["seed"] = seed
    if not fake:
        session.update(load_contestant(model))
    rng = random.Random(seed or 0)
    started = time.perf_counter()
    errors_in_row = 0
    answered = 0
    stop = None
    with open(answers_path, "a", encoding="utf-8") as out:
        for n, (item, fp) in enumerate(todo, 1):
            if not any_hour and in_quiet_hours():
                stop = "quiet hours"
                break
            opts = item_opts(item, split, vote_ref, temperature, zoom)
            calls = fake_answer(item, fake, rng) if fake else ask_item(item, opts, extra)
            out.write(json.dumps({"id": item["id"], "fp": fp, "calls": calls, "t": round(time.time())}) + "\n")
            out.flush()
            answered += 1
            errors_in_row = errors_in_row + 1 if any(c.get("error") for c in calls) else 0
            if errors_in_row >= 5:
                stop = "5 errors in a row: " + str(calls[-1].get("error"))[:200]
                break
            if n % 10 == 0 or n == len(todo):
                spent = time.perf_counter() - started
                left = spent / n * (len(todo) - n)
                log(f"{model}: {n}/{len(todo)} in {spent / 60:.1f} min, ~{left / 60:.0f} min left", flush=True)
    session.update({"ended": time.strftime("%Y-%m-%d %H:%M:%S"), "answered": answered,
                    "seconds": round(time.perf_counter() - started, 1), "stopped": stop})
    if not fake:
        session["vram_peak_mb"] = eye.vram_used_mb()
        eye.unload_all()
    meta["sessions"].append(session)
    meta["stopped"] = stop
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    if stop:
        log(f"{model}: stopped ({stop}); run again to resume")
    return meta


# --- scoring -------------------------------------------------------------------------------------


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def boxes_px(calls: list[dict], order: str, size: tuple[int, int]) -> list[tuple[list[float], str]]:
    """Every reported box in crop pixels, with its kind. Values above 1000 are taken as pixels of the image seen."""
    out = []
    for call in calls:
        data = call.get("data") or {}
        tile = call.get("tile") or [0, 0, size[0], size[1]]
        tw, th = tile[2] - tile[0], tile[3] - tile[1]
        for d in data.get("defects", []) if isinstance(data.get("defects"), list) else []:
            box = d.get("box") if isinstance(d, dict) else None
            if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box):
                continue
            a, b, c, e = box if order == "xy" else (box[1], box[0], box[3], box[2])
            scale_x, scale_y = (1, 1) if max(box) > 1000 else (tw / 1000, th / 1000)
            x0, x1 = sorted((tile[0] + a * scale_x, tile[0] + c * scale_x))
            y0, y1 = sorted((tile[1] + b * scale_y, tile[1] + e * scale_y))
            out.append(([x0, y0, x1, y1], str(d.get("kind", "other"))))
    return out


def hits(reported: list[float], truth: list[int], size: tuple[int, int], margin: int = 12) -> bool:
    """The model points at the defect: its box's centre lies on the true box (grown by margin).

    E1 run 1: with "the boxes touch", the champion's boxes (about 4 per crop) laid on another crop's plant
    "found" 60% of them by luck; with the centre rule luck drops to 11-22% and its real finds stay 53-83%.
    """
    cx, cy = (reported[0] + reported[2]) / 2, (reported[1] + reported[3]) / 2
    return truth[0] - margin <= cx <= truth[2] + margin and truth[1] - margin <= cy <= truth[3] + margin


def _rate(k: float, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci": wilson(k, n)}


def score_run(answers: dict[str, dict], key: list[dict]) -> dict:
    by_id = {i["id"]: i for i in key}
    rows = [(by_id[a["id"]], a) for a in answers.values() if a["id"] in by_id]
    calls = [c for _, a in rows for c in a["calls"]]
    ok_calls = [c for c in calls if not c.get("error")]
    result: dict = {
        "answered": len(rows), "calls": len(calls), "errors": len(calls) - len(ok_calls),
        "bad_json": sum(1 for c in ok_calls if c.get("data") is None),
        "seconds_per_call": round(sum(c["s"] for c in ok_calls) / len(ok_calls), 2) if ok_calls else None,
        "prompt_tokens_per_call": round(sum(c["pt"] for c in ok_calls) / len(ok_calls)) if ok_calls else None,
    }

    # votes
    def better(a: dict) -> str | None:
        data = a["calls"][0].get("data") if a["calls"] else None
        value = data.get("better") if isinstance(data, dict) else None
        return value if value in ("A", "B", "same") else None

    votes = [(i, a, better(a)) for i, a in rows if i["task"] == "vote"]
    author = [(i, ans) for i, _, ans in votes if i["source"] == "author" and i["truth"]["author"] != "s"]

    def agreement(subset) -> dict:
        agree = sum(1 for i, ans in subset if ans == i["truth"]["better"])
        same = sum(1 for i, ans in subset if ans == "same")
        missing = sum(1 for _, ans in subset if ans is None)
        n = len(subset)
        decisive = n - same - missing
        return {"n": n, "agree": agree, "same": same, "no_answer": missing,
                "score": round((agree + 0.5 * same) / n, 3) if n else None,  # chance 0.5
                "decisive_agree": _rate(agree, decisive)}

    result["author"] = agreement(author)
    result["author_by_round"] = {r: agreement([(i, a) for i, a in author if i["round"] == r]) for r in ROUND_TITLES}
    diffs = sorted(i["diff"]["mean"] for i, _ in author if i["diff"]["mean"] is not None)
    median = diffs[len(diffs) // 2] if diffs else 0
    result["author_clear"] = agreement([(i, a) for i, a in author if (i["diff"]["mean"] or 0) >= median])
    result["author_clear_threshold"] = median
    ties = [(i, ans) for i, _, ans in votes if i["source"] == "author" and i["truth"]["author"] == "s"]
    result["author_ties_said_same"] = _rate(sum(1 for _, ans in ties if ans == "same"), len(ties))

    pairs: dict[str, dict] = {}
    for i, _, ans in votes:
        if i.get("order"):
            pairs.setdefault(i["pair"], {})[i["order"]] = ans
    both = [p for p in pairs.values() if "ab" in p and "ba" in p and p["ab"] and p["ba"]]
    # the author's votes are not perfect (he says he sometimes picked over an artifact, or saw what wasn't there):
    # where a model went against him in both orders, its reason is worth a look by eye
    against = []
    by_pair = {}
    for i, a, ans in votes:
        if i["source"] == "author" and i["truth"]["author"] != "s":
            by_pair.setdefault(i["pair"], {})[i["order"]] = (i, a, ans)
    for pair_id, sides in sorted(by_pair.items()):
        if "ab" in sides and "ba" in sides:
            (i_ab, a_ab, ans_ab), (_, a_ba, ans_ba) = sides["ab"], sides["ba"]
            if ans_ab in ("A", "B") and ans_ab != i_ab["truth"]["better"] and (ans_ab, ans_ba) in (("A", "B"), ("B", "A")):
                left, right = i_ab["versions"]
                author_pick = left if i_ab["truth"]["better"] == "A" else right
                why = [((x["calls"][0].get("data") or {}).get("why", "")) for x in (a_ab, a_ba)]
                against.append({"pair": pair_id, "round": i_ab["round"], "crop": i_ab["crop"], "author": author_pick,
                                "model": right if author_pick == left else left, "why": why,
                                "images": [i_ab["images"]["a"], i_ab["images"]["b"]]})
    result["against_author"] = against
    consistent = sum(1 for p in both if (p["ab"], p["ba"]) in (("A", "B"), ("B", "A"), ("same", "same")))
    result["order_consistent"] = _rate(consistent, len(both))
    decisive_all = [ans for i, _, ans in votes if ans in ("A", "B") and i["source"] != "identical"]
    result["first_shown_picked"] = _rate(sum(1 for a in decisive_all if a == "A"), len(decisive_all))  # 0.5 = no bias
    ident = [ans for i, _, ans in votes if i["source"] == "identical"]
    result["identical_said_same"] = _rate(sum(1 for a in ident if a == "same"), len(ident))
    pv = [(i, ans) for i, _, ans in votes if i["source"] == "planted"]
    result["clean_over_planted"] = _rate(sum(1 for i, a in pv if a == i["truth"]["better"]), len(pv))
    result["clean_over_planted_same"] = sum(1 for _, a in pv if a == "same")

    # defects
    planted = [(i, a) for i, a in rows if i["task"] == "defects" and i["source"] == "planted"]

    def found_in(order: str) -> int:
        return sum(1 for i, a in planted
                   if any(hits(b, i["truth"]["boxes"][0], tuple(i["size"])) for b, _ in boxes_px(a["calls"], order, tuple(i["size"]))))

    order = "xy" if found_in("xy") >= found_in("yx") else "yx"
    result["box_order"] = order
    per_variant: dict[str, dict] = {}
    found_rows = []
    for i, a in planted:
        size = tuple(i["size"])
        boxes = boxes_px(a["calls"], order, size)
        hit_kinds = [k for b, k in boxes if hits(b, i["truth"]["boxes"][0], size)]
        found = bool(hit_kinds)
        kind = i["truth"]["kind"]
        found_rows.append({"id": i["id"], "variant": i["variant"], "level": i["level"], "found": found,
                           "strict": kind in hit_kinds, "loose": any(k in LOOSE_KINDS[kind] for k in hit_kinds),
                           "extra": len(boxes) - len(hit_kinds), "flagged": bool(boxes)})
    for level in (1, 2):
        sub = [r for r in found_rows if r["level"] == level]
        result[f"planted_found_l{level}"] = _rate(sum(r["found"] for r in sub), len(sub))
    for variant, _ in VARIANTS:
        for level in (1, 2):
            sub = [r for r in found_rows if r["variant"] == variant and r["level"] == level]
            per_variant[f"{variant}-{level}"] = _rate(sum(r["found"] for r in sub), len(sub))
    result["planted_by_variant"] = per_variant
    # Box luck: a model that boxes half of every crop "finds" plants by chance. The same model's boxes from the
    # next planted crop (another place) laid on this crop's plant: what it would score by luck.
    ordered = sorted(planted, key=lambda ia: ia[0]["id"])
    luck = 0
    for k, (i, _) in enumerate(ordered):
        other = ordered[(k + 1) % len(ordered)][1] if len(ordered) > 1 else None
        size = tuple(i["size"])
        if other is not None and any(hits(b, i["truth"]["boxes"][0], size) for b, _ in boxes_px(other["calls"], order, size)):
            luck += 1
    result["planted_found_by_luck"] = _rate(luck, len(ordered))
    got = [r for r in found_rows if r["found"]]
    result["kind_strict"] = _rate(sum(r["strict"] for r in got), len(got))
    result["kind_loose"] = _rate(sum(r["loose"] for r in got), len(got))
    result["planted_missed"] = [r["id"] for r in found_rows if not r["found"]]

    clean = [(i, a) for i, a in rows if i["task"] == "defects" and i["source"] == "clean"]
    clean_boxes = [len(boxes_px(a["calls"], order, tuple(i["size"]))) for i, a in clean]
    result["clean_flagged"] = _rate(sum(1 for n in clean_boxes if n), len(clean_boxes))
    result["clean_boxes_per_crop"] = round(sum(clean_boxes) / len(clean_boxes), 2) if clean_boxes else None

    marks = [(i, a) for i, a in rows if i["task"] == "defects" and i["source"] == "marks"]
    mark_total = mark_found = crops_found = 0
    marked = [(i, a) for i, a in marks if i["truth"]["status"] == "marked"]
    for i, a in marked:
        size = tuple(i["size"])
        boxes = boxes_px(a["calls"], order, size)
        found_here = [any(hits(b, m, size) for b, _ in boxes) for m in i["truth"]["boxes"]]
        mark_total += len(found_here)
        mark_found += sum(found_here)
        crops_found += any(found_here)
    result["marks_found"] = _rate(mark_found, mark_total)
    luck = total = 0
    ordered = sorted(marked, key=lambda ia: ia[0]["id"])
    for k, (i, _) in enumerate(ordered):
        other = ordered[(k + 1) % len(ordered)][1] if len(ordered) > 1 else None
        size = tuple(i["size"])
        boxes = boxes_px(other["calls"], order, size) if other is not None else []
        for m in i["truth"]["boxes"]:
            total += 1
            luck += any(hits(b, m, size) for b, _ in boxes)
    result["marks_found_by_luck"] = _rate(luck, total)
    result["marked_crops_found"] = _rate(crops_found, len(marked))
    unmarked = [(i, a) for i, a in marks if i["truth"]["status"] == "unmarked"]
    result["outcomes"] = item_outcomes(answers, by_id, order)
    result["unmarked_flagged"] = _rate(sum(1 for i, a in unmarked if boxes_px(a["calls"], order, tuple(i["size"]))), len(unmarked))
    return result


def author_repeats(key: list[dict]) -> tuple[int, int]:
    """Pairs the author voted on more than once, and how many got the same answer every time: his own noise."""
    seen: dict[tuple, list] = {}
    for i in key:
        if i["task"] == "vote" and i["source"] == "author" and i["order"] == "ab":
            left, right = i["versions"]
            pick = {"l": left, "r": right, "s": "same"}[i["truth"]["author"]]
            seen.setdefault((i["round"], i["crop"], frozenset((left, right))), []).append(pick)
    reps = [v for v in seen.values() if len(v) > 1]
    return sum(1 for v in reps if len(set(v)) == 1), len(reps)


def item_outcomes(answers: dict[str, dict], by_id: dict[str, dict], order: str) -> dict[str, list]:
    """Per item what the model said, in a form two runs can be compared on: the vote, or (flagged, found)."""
    out: dict[str, list] = {}
    for a in answers.values():
        i = by_id.get(a["id"])
        if i is None:
            continue
        if i["task"] == "vote":
            data = a["calls"][0].get("data") if a["calls"] else None
            out[a["id"]] = ["vote", data.get("better") if isinstance(data, dict) else None]
        else:
            size = tuple(i["size"])
            boxes = boxes_px(a["calls"], order, size)
            found = any(hits(b, i["truth"]["boxes"][0], size) for b, _ in boxes) if i["source"] == "planted" else None
            out[a["id"]] = ["defects", bool(boxes), found]
    return out


def run_agreement(scores: list[dict]) -> list[dict]:
    """Repeat runs of one model: how often each later run gave run 1's answer, and how often all runs agree."""
    by_model: dict[str, list[dict]] = {}
    for sc in scores:
        if not sc["model"].startswith("fake:"):
            by_model.setdefault(sc["model"], []).append(sc)
    rows = []
    for model, runs in by_model.items():
        if len(runs) < 2:
            continue
        runs = sorted(runs, key=lambda r: r["run"])
        outs = [r["outcomes"] for r in runs]
        common = set(outs[0]).intersection(*outs[1:])

        def share(kind: str, pick) -> dict:
            ids = [i for i in common if outs[0][i][0] == kind and pick(outs[0][i]) is not None]
            same = sum(1 for i in ids if len({json.dumps(pick(o[i])) for o in outs}) == 1)
            return _rate(same, len(ids))

        rows.append({
            "model": model, "runs": [r["run"] for r in runs], "temperatures": [r.get("temperature") for r in runs],
            "votes_all_same": share("vote", lambda o: o[1]),
            "flags_all_same": share("defects", lambda o: o[1]),
            "planted_found_all_same": share("defects", lambda o: o[2]),
        })
    return rows


def score_all(runs_dir: Path, key: list[dict]) -> list[dict]:
    out = []
    for meta_path in sorted(runs_dir.glob("*/r*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        answers = read_answers(meta_path.parent / "answers.jsonl")
        if not answers:
            continue
        score = score_run(answers, key)
        score.update({"model": meta["model"], "run": meta_path.parent.name, "split": meta.get("split"),
                      "vote_ref": meta.get("vote_ref"), "temperature": meta.get("temperature"), "zoom": meta.get("zoom", 1),
                      "items": meta.get("items"), "prompt": meta.get("prompt"), "stopped": meta.get("stopped")})
        out.append(score)
    return out


# --- the page ------------------------------------------------------------------------------------


def _pct(r: dict | None, ci: bool = True) -> str:
    if not r or r.get("rate") is None:
        return "<td class=na>-</td>"
    lo_hi = f"<small>{round(r['ci'][0] * 100)}-{round(r['ci'][1] * 100)}</small>" if ci and r.get("ci") else ""
    return f"<td title='{r['k']:g} of {r['n']}'>{round(r['rate'] * 100)}% {lo_hi}</td>"


def _score_cell(a: dict | None) -> str:
    if not a or not a.get("n"):
        return "<td class=na>-</td>"
    title = f"{a['agree']} agree, {a['same']} same, {a['n'] - a['agree'] - a['same'] - a['no_answer']} against, of {a['n']}"
    return f"<td title='{title}'>{round(a['score'] * 100)}% <small>n={a['n']}</small></td>"


def _run_label(s: dict) -> str:
    """r1, plus what differs from the default: temperature, no reference image on votes."""
    label = s["run"]
    if s.get("temperature"):
        label += f" t{s['temperature']:g}"
    if s.get("vote_ref") is False:
        label += " no ref"
    if (s.get("zoom") or 1) > 1:
        label += f" {s['split']}x{s['split']} tiles zoom {s['zoom']}"
    return label


def _name(model: str) -> str:
    return "chance (random answers)" if model == "fake:random" else model


def page(scores: list[dict], key: list[dict], path: Path) -> None:
    summary: dict[str, dict] = {}
    for item in key:
        s = summary.setdefault(f"{item['task']} / {item['source']}", {"full": 0, "core": 0})
        s["full"] += 1
        s["core"] += int(item["core"])
    head = (
        "<tr><th>model</th><th>run</th><th>s / call</th><th>clean over planted</th><th>identical: same</th>"
        "<th>order kept</th><th>first shown picked</th><th>planted found, subtle</th><th>planted found, clear</th>"
        "<th>planted, by box luck</th><th>kind right</th><th>clean crops flagged</th><th>the author's marks found</th>"
        "<th>marks, by box luck</th><th>matches the author</th>"
        "<th>clear pairs</th><th>against the author</th><th>answers</th></tr>"
    )
    rows = []
    for s in scores:
        state = (" <b>stopped</b>" if s.get("stopped") else "") + (f" <b>{s['errors']} errors</b>" if s["errors"] else "")
        rows.append(
            f"<tr><td class=m>{html.escape(_name(s['model']))}</td><td>{_run_label(s)}</td><td>{s['seconds_per_call'] or '-'}</td>"
            + _pct(s["clean_over_planted"]) + _pct(s["identical_said_same"]) + _pct(s["order_consistent"])
            + _pct(s["first_shown_picked"]) + _pct(s["planted_found_l1"]) + _pct(s["planted_found_l2"])
            + _pct(s.get("planted_found_by_luck")) + _pct(s["kind_loose"]) + _pct(s["clean_flagged"])
            + _pct(s["marks_found"]) + _pct(s.get("marks_found_by_luck"))
            + _score_cell(s["author"]) + _score_cell(s["author_clear"]) + f"<td>{len(s.get('against_author', []))}</td>"
            + f"<td>{s['answered']}/{s['items']}{state}</td></tr>"
        )
    round_head = "<tr><th>model</th><th>run</th>" + "".join(f"<th title='{html.escape(t)}'>{r}</th>" for r, t in ROUND_TITLES.items()) + "</tr>"
    round_rows = [
        f"<tr><td class=m>{html.escape(_name(s['model']))}</td><td>{_run_label(s)}</td>" + "".join(_score_cell(s["author_by_round"].get(r)) for r in ROUND_TITLES) + "</tr>"
        for s in scores
    ]
    var_cols = [f"{v}-{lv}" for v, _ in VARIANTS for lv in (1, 2)]
    var_head = "<tr><th>model</th><th>run</th>" + "".join(f"<th>{c}</th>" for c in var_cols) + "</tr>"
    var_rows = [
        f"<tr><td class=m>{html.escape(_name(s['model']))}</td><td>{_run_label(s)}</td>" + "".join(_pct(s["planted_by_variant"].get(c), ci=False) for c in var_cols) + "</tr>"
        for s in scores
    ]
    same_twice, repeated = author_repeats(key)
    against_parts = []
    for s in scores:
        if s["model"].startswith("fake:") or not s.get("against_author"):
            continue
        lines = "".join(
            f"<li><b>{html.escape(x['crop'])}</b> (round {x['round']}): The author took <b>{html.escape(x['author'])}</b>, "
            f"the model <b>{html.escape(x['model'])}</b> both times. <small>{html.escape(x['why'][0][:300])}</small></li>"
            for x in s["against_author"]
        )
        against_parts.append(f"<h3>{html.escape(_name(s['model']))} {s['run']}: {len(s['against_author'])} pairs</h3><ol>{lines}</ol>")
    against_html = "".join(against_parts) or "<p>none yet</p>"
    agreement_rows = "".join(
        f"<tr><td class=m>{html.escape(r['model'])}</td><td>{', '.join(r['runs'])}</td>"
        f"<td>{', '.join(str(t) for t in r['temperatures'])}</td>" + _pct(r["votes_all_same"]) + _pct(r["flags_all_same"])
        + _pct(r["planted_found_all_same"]) + "</tr>"
        for r in run_agreement(scores)
    ) or "<tr><td colspan=6>no repeat runs yet</td></tr>"
    key_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v['full']}</td><td>{v['core']}</td></tr>" for k, v in summary.items())
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Eye scorecard</title><style>
:root{{--bg:#fafaf8;--fg:#1d1d1b;--dim:#6b6b66;--line:#ddd9d0;--head:#efece4}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--dim:#9a9a92;--line:#34332f;--head:#23221f}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px 20px 40px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 6px}} h3{{font-size:14px;margin:16px 0 4px}} li{{margin:3px 0}} p{{max-width:900px;color:var(--dim)}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right;white-space:nowrap}}
th{{background:var(--head);font-weight:600;position:sticky;top:0}} td.m,th:first-child{{text-align:left}}
small{{color:var(--dim)}} td.na{{color:var(--dim)}}
</style></head><body>
<h1>The eye: which local vision model to trust, for what</h1>
<p>E1 contest, built {time.strftime('%Y-%m-%d %H:%M')}. Each model answered the answer key blind: pairs and crops only,
no names. Percent with a 95% range under it; hover a cell for counts. Chance on votes is 50%.</p>
<h2>Headline</h2>
<p><b>matches the author</b>: his decided blind votes, each shown in his order and swapped; "same" counts half.
<b>clear pairs</b>: the half of those pairs that differ most. <b>order kept</b>: the answer flips when the pair is
swapped. <b>first shown picked</b>: 50% means no side bias. <b>planted found</b>: the centre of a reported box lies on the
planted defect (subtle and clear levels). <b>by box luck</b>: the same model's boxes from another crop laid on this one: what its box count alone would
score (a model that boxes everything "finds" a lot). <b>kind right</b>: of the found ones, a fitting name. <b>clean crops flagged</b>: untouched
crops with any report (an upper bound on false alarms: the shipped art has real defects too).
<b>the author's marks found</b>: his 37 marks on the artifact crops. The first columns have certain answers (planted,
identical, clean over planted); the author's votes do not: he says he sometimes picked over an artifact or saw what wasn't
there, and on the {repeated} pairs he happened to vote on twice he gave the same answer {same_twice} times
({round(100 * same_twice / max(1, repeated))}%). A model near that level matches him as well as he matches himself.
<b>against the author</b>: pairs where the model picked the other one in both orders (listed below, to check by eye).</p>
<div class=wrap><table>{head}{''.join(rows) or '<tr><td colspan=15>no runs yet</td></tr>'}</table></div>
<h2>Repeat runs: same answer every time</h2>
<p>The same questions asked again. Temperature 0 asks whether the same input gives the same answer; above 0 shows how
much the answers move when sampled (what a "2 of 3 times" rule sees).</p>
<div class=wrap><table><tr><th>model</th><th>runs</th><th>temperature</th><th>votes</th><th>crops flagged or not</th>
<th>planted found or not</th></tr>{agreement_rows}</table></div>
<h2>Matches the author, per vote round</h2>
<div class=wrap><table>{round_head}{''.join(round_rows)}</table></div>
<h2>Planted defects found, per kind (1 subtle, 2 clear)</h2>
<div class=wrap><table>{var_head}{''.join(var_rows)}</table></div>
<h2>Against the author: the model picked the other one in both orders</h2>
<p>Each is either the model's miss or a vote of the author's worth a second look (an artifact in his pick).</p>
{against_html}
<h2>The key</h2>
<div class=wrap><table><tr><th>part</th><th>items</th><th>core set</th></tr>{key_rows}</table></div>
</body></html>"""
    path.write_text(doc, encoding="utf-8")
