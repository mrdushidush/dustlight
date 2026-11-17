"""The eye: local vision models in LM Studio look at game stills.

Plan: <data_root>/reports/art-lab/eye/PLAN.md (2026-11-12). Step E0 (this file so far):

- an LM Studio client: OpenAI-compatible /v1/chat/completions, images sent as data: URLs
  (the wire format claudette's image_describe proved), answers as fixed JSON forms;
- 1:1 crops of tour stills (vision models shrink big images, and a 1 px seam vanishes);
- the two questions: what is wrong in this HD crop next to its original, and a blind A/B vote;
- model load/unload through the `lms` CLI, and a speed bench (`fo2hd eye-bench`).

Nothing here imports torch: the eye shares the one GPU with the game and the upscaler by taking
turns (load, judge, unload), not by sharing it.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

LMS_URL = "http://127.0.0.1:1234/v1"
MODEL_ID = "eye"  # every model is loaded under this identifier, so the API name never changes

DEFECT_KINDS = ["seam", "hole", "lowres", "outline", "specks", "smear", "invented", "shadow", "color", "other"]

# Changes with the question wording: answers given under another version are asked again (E1 contest).
# e1: seam names thin straight lines too, smear is soft blur only (E0: Qwen called a planted dark line "smear").
PROMPT_VERSION = "e1"

DEFECT_SYSTEM = (
    "You check an HD remaster of Fallout 2, a 1998 isometric role-playing game, for rendering defects. "
    "You get two crops of the same place on screen. Image 1 is the original game: low resolution, "
    "256-colour palette, dithered. Image 2 is the HD remaster: the same art upscaled, with new light and "
    "soft shadows. The remaster is meant to look like the original, only sharper and with more depth. "
    "Sharper detail, different light, soft shadows and smoother gradients are intended: never report those. "
    "Report only real defects in image 2: "
    "seam = a thin straight line (dark or light), a gap or a step, often where floor tiles, walls or roofs "
    "meet, that the original does not have; "
    "hole = something missing or black/empty where the original has content; "
    "lowres = a piece still blocky or pixelated next to sharp HD art; "
    "outline = dark or bright outlines or halos around objects or tiles; "
    "specks = stray dots or specks; "
    "smear = detail melted or blurred into soft blobs, or text that is no longer readable; "
    "invented = texture that is not in the original (cracks, rings, fur, noise, patterns); "
    "shadow = a doubled, floating or clearly wrong shadow; "
    "color = colours or brightness clearly unlike the original; "
    "other = anything else that looks broken. "
    "Boxes are [x0, y0, x1, y1] in image 2, scaled 0-1000. "
    "If image 2 has no real defect, return an empty list. Do not guess."
)

VOTE_SYSTEM = (
    "You judge versions of an HD remaster of Fallout 2, a 1998 isometric role-playing game. "
    "The goal of the remaster: same mood as the original, more depth. It must stay faithful to the original "
    "art and must never look like generic AI upscaling: no smeared faces, no invented texture, no lines where "
    "pieces meet, no plastic look. You get two versions, A and B, of the same crop. "
    "Pick the one that looks better by that goal, or 'same' if you cannot tell them apart or neither is better."
)

DEFECT_SCHEMA = {
    "type": "object",
    "properties": {
        "defects": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": DEFECT_KINDS},
                    "box": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
                    "severity": {"type": "integer", "enum": [1, 2, 3]},
                    "note": {"type": "string", "maxLength": 200},
                },
                "required": ["kind", "box", "severity", "note"],
            },
        }
    },
    "required": ["defects"],
}

VOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "why": {"type": "string", "maxLength": 400},  # gemma-4-e4b ran on to the token cap without it (E0)
        "better": {"type": "string", "enum": ["A", "B", "same"]},
    },
    "required": ["why", "better"],
}


@dataclass
class Answer:
    data: dict | None  # the parsed JSON form; None if the reply wasn't valid JSON
    text: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    error: str | None = None


def png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def crop_boxes(width: int, height: int, cols: int = 4, rows: int = 2, overlap: int = 64) -> list[tuple[int, int, int, int]]:
    """cols x rows boxes covering the image, each grown by overlap/2 into its neighbours."""
    boxes = []
    for r in range(rows):
        for c in range(cols):
            x0 = max(0, round(c * width / cols) - overlap // 2)
            x1 = min(width, round((c + 1) * width / cols) + overlap // 2)
            y0 = max(0, round(r * height / rows) - overlap // 2)
            y1 = min(height, round((r + 1) * height / rows) + overlap // 2)
            boxes.append((x0, y0, x1, y1))
    return boxes


def _parse_json(text: str) -> dict | None:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def chat(
    parts: list[str | Image.Image],
    system: str,
    schema: dict | None = None,
    model: str = MODEL_ID,
    max_tokens: int = 800,
    temperature: float = 0.0,
    extra: dict | None = None,
    timeout: float = 600.0,
) -> Answer:
    """One request. `parts` mixes text and images in the order the model sees them."""
    content = []
    for part in parts:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
        else:
            content.append({"type": "image_url", "image_url": {"url": png_data_url(part)}})
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": schema}}
    if extra:
        body.update(extra)
    request = urllib.request.Request(
        LMS_URL + "/chat/completions", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        detail = exc.read().decode("utf-8", "replace")[:400] if isinstance(exc, urllib.error.HTTPError) else ""
        return Answer(None, "", "", 0, 0, time.perf_counter() - started, f"{exc} {detail}".strip())
    seconds = time.perf_counter() - started
    message = reply.get("choices", [{}])[0].get("message", {})
    text = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    usage = reply.get("usage", {})
    return Answer(
        _parse_json(text), text, reasoning, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), seconds
    )


def ask_defects(original: Image.Image, hd: Image.Image, **kwargs) -> Answer:
    parts = ["Image 1, the original:", original, "Image 2, the HD remaster:", hd, "List the real defects in image 2."]
    return chat(parts, DEFECT_SYSTEM, DEFECT_SCHEMA, **kwargs)


def ask_vote(a: Image.Image, b: Image.Image, original: Image.Image | None = None, **kwargs) -> Answer:
    parts: list[str | Image.Image] = []
    if original is not None:
        parts += ["For reference, the original game:", original]
    parts += ["Version A:", a, "Version B:", b, "Which version looks better: A, B or same?"]
    return chat(parts, VOTE_SYSTEM, VOTE_SCHEMA, **kwargs)


# --- models -------------------------------------------------------------------------------------


def _lms_exe() -> str:
    found = shutil.which("lms")
    if found:
        return found
    fallback = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    if fallback.exists():
        return str(fallback)
    raise SystemExit("lms (LM Studio's CLI) not found")


def lms(*args: str, timeout: float = 900.0) -> str:
    result = subprocess.run(
        [_lms_exe(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"lms {' '.join(args)} failed ({result.returncode}): {(result.stderr or result.stdout)[-600:]}")
    return result.stdout


def unload_all() -> None:
    lms("unload", "--all")


def load(model: str, context: int | None = None, extra_args: tuple[str, ...] = (), timeout: float = 600.0) -> float:
    """Unloads everything, loads `model` under MODEL_ID; returns the seconds it took.

    Everything not given here comes from the author's saved per-model config in LM Studio (KV cache
    q8_0, no mmap, his CPU-expert split for models that spill, context). Parallel is always 1:
    more slots split the context and were ~4x slower in E0 (claudette's lesson too). A model
    without a saved config silently gets 4 slots, so `--parallel 1` is always passed.
    """
    unload_all()
    args = ["load", model, "--parallel", "1", "--identifier", MODEL_ID, "-y", *extra_args]
    if context:
        args[2:2] = ["-c", str(context)]
    started = time.perf_counter()
    lms(*args, timeout=timeout)
    return time.perf_counter() - started


def loaded() -> list[dict]:
    """`lms ps --json`: what is loaded, for checking the right weights came up (lms load fuzzy-matches)."""
    try:
        return json.loads(lms("ps", "--json", timeout=60))
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return []


def map_area(img: Image.Image, scale: int = 3) -> Image.Image:
    """The map part of a 1440-tall tour still: the interface bar (100 px at 1x) is its own question."""
    w, h = img.size
    return img.crop((0, 0, w, h - 100 * scale))


def vram_used_mb() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


# --- E0 speed bench -----------------------------------------------------------------------------

THINK_OFF = {"reasoning_effort": "none"}  # LM Studio: the one switch that stopped Qwen3.6's thinking (E0 probe)

# Load flags per model (claudette docs/hardware.md: the champion's MTP flags buy its speed; draft 2 = peak).
_MTP = ("--speculative-draft-mtp", "--speculative-draft-max-tokens", "2")
LOAD_ARGS = {
    "qwen3.6-35b-a3b-mtp@iq3_s": _MTP,
    "qwen3.6-35b-a3b-mtp@iq4_xs": _MTP,
}


def plant(img: Image.Image, kind: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """A copy of `img` with one obvious defect; returns it and the defect's box in pixels."""
    out = img.convert("RGB").copy()
    w, h = out.size
    if kind == "hole":
        box = (int(w * 0.55), int(h * 0.35), int(w * 0.55) + 70, int(h * 0.35) + 45)
        out.paste((0, 0, 0), box)
    elif kind == "seam":
        y = int(h * 0.6)
        box = (int(w * 0.15), y, int(w * 0.75), y + 3)
        out.paste((12, 10, 8), box)
    else:
        raise ValueError(kind)
    return out, box


def box_hits(found: list, planted: tuple[int, int, int, int], size: tuple[int, int], margin: int = 40) -> str | None:
    """Does a reported box (0-1000 scale) overlap the planted one? Tries x-first and Gemma's y-first order."""
    w, h = size
    px0, py0, px1, py1 = planted[0] - margin, planted[1] - margin, planted[2] + margin, planted[3] + margin
    for order in ("xy", "yx"):
        for item in found:
            box = item.get("box") if isinstance(item, dict) else None
            if not isinstance(box, list) or len(box) != 4:
                continue
            a, b, c, d = box if order == "xy" else (box[1], box[0], box[3], box[2])
            x0, x1 = sorted((a * w / 1000, c * w / 1000))
            y0, y1 = sorted((b * h / 1000, d * h / 1000))
            if x0 <= px1 and x1 >= px0 and y0 <= py1 and y1 >= py0:
                return order
    return None


def bench(
    model: str, stills: list[tuple[Path, Path]], out_dir: Path, crops_per_still: int = 2, cols: int = 4, rows: int = 2
) -> dict:
    """Speed and a plumbing sanity check for one model. stills: (hd, orig) pairs of one tour view each.

    cols x rows sets the crop size: 4 x 2 of a 3438x1140 map is ~890x600, which Qwen3.5/3.6 take at
    full size (~560 image tokens) but Gemma 4 squeezes into ~280 tokens (E0); 8 x 4 gives Gemma 1:1.
    """
    import random

    rng = random.Random(1112)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"model": model, "crop_grid": [cols, rows], "calls": []}

    pairs = []  # (name, orig crop, hd crop)
    for hd_path, orig_path in stills:
        hd, orig = (map_area(Image.open(p).convert("RGB")) for p in (hd_path, orig_path))
        boxes = crop_boxes(*hd.size, cols=cols, rows=rows)
        for index in rng.sample(range(len(boxes)), crops_per_still):
            pairs.append((f"{hd_path.stem[:-3]}#{index}", orig.crop(boxes[index]), hd.crop(boxes[index])))

    def record(kind: str, name: str, answer: Answer, **more) -> None:
        result["calls"].append({
            "kind": kind, "crop": name, "seconds": round(answer.seconds, 2), "prompt_tokens": answer.prompt_tokens,
            "completion_tokens": answer.completion_tokens, "reasoning_chars": len(answer.reasoning),
            "error": answer.error, "answer": answer.data, **more,
        })

    unload_all()
    before = vram_used_mb()
    result["load_seconds"] = round(load(model, extra_args=LOAD_ARGS.get(model, ())), 1)
    result["vram_mb"] = [before, vram_used_mb()]
    result["loaded"] = loaded()
    ask_vote(pairs[0][2], pairs[0][2], extra=THINK_OFF, max_tokens=300)  # warm-up

    for name, orig_c, hd_c in pairs:
        record("defects", name, ask_defects(orig_c, hd_c, extra=THINK_OFF, max_tokens=900))
        hd_first = rng.random() < 0.5
        a, b = (hd_c, orig_c) if hd_first else (orig_c, hd_c)
        record("vote_hd_vs_orig", name, ask_vote(a, b, extra=THINK_OFF, max_tokens=400), hd_is="A" if hd_first else "B")
    name, orig_c, hd_c = pairs[0]
    for _ in range(2):
        record("vote_identical", name, ask_vote(hd_c, hd_c, extra=THINK_OFF, max_tokens=400), expect="same")
    for kind in ("hole", "seam"):
        for name, orig_c, hd_c in pairs[:3]:
            planted, box = plant(hd_c, kind)
            answer = ask_defects(orig_c, planted, extra=THINK_OFF, max_tokens=900)
            hit = box_hits((answer.data or {}).get("defects", []), box, planted.size)
            record("planted_" + kind, name, answer, hit=hit)
            if name == pairs[0][0]:
                planted.save(out_dir / f"planted_{kind}.png")
    result["vram_mb_peak"] = vram_used_mb()
    unload_all()
    (out_dir / "bench.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def bench_summary(result: dict) -> str:
    calls = result["calls"]

    def of(kind: str) -> list[dict]:
        return [c for c in calls if c["kind"] == kind]

    def mean_seconds(kind: str) -> float:
        rows = [c["seconds"] for c in of(kind) if not c["error"]]
        return sum(rows) / len(rows) if rows else float("nan")

    defects = of("defects")
    flagged = sum(1 for c in defects if c["answer"] and c["answer"].get("defects"))
    votes = [c for c in of("vote_hd_vs_orig") if c["answer"]]
    hd_wins = sum(1 for c in votes if c["answer"].get("better") == c["hd_is"])
    same = sum(1 for c in votes if c["answer"].get("better") == "same")
    ident = [c for c in of("vote_identical") if c["answer"]]
    info = (result.get("loaded") or [{}])[0]
    weights = info.get("path") or info.get("modelKey") or "?"
    slots = info.get("maxParallelSessions", info.get("parallel", info.get("numParallelSessions", "?")))
    every = [c["seconds"] for c in calls if c["kind"] in ("defects", "planted_hole", "planted_seam") and not c["error"]]
    per_hour = 3600 * len(every) / sum(every) if every else 0
    lines = [
        f"{result['model']}: load {result['load_seconds']} s, VRAM {result['vram_mb'][0]} -> {result['vram_mb'][1]} MB "
        f"(peak {result.get('vram_mb_peak')}), weights {weights}, ctx {info.get('contextLength', '?')}, parallel {slots}",
        f"  defect check {mean_seconds('defects'):.1f} s (~{per_hour:.0f} checks/h); vote {mean_seconds('vote_hd_vs_orig'):.1f} s; "
        f"bad JSON {sum(1 for c in calls if c['answer'] is None)} of {len(calls)}",
        f"  clean crops flagged: {flagged} of {len(defects)}; HD vs original: HD {hd_wins}, original {len(votes) - hd_wins - same}, same {same}; "
        f"identical pair said same: {sum(1 for c in ident if c['answer'].get('better') == 'same')} of {len(ident)}",
    ]
    for kind in ("hole", "seam"):
        rows = of("planted_" + kind)
        right_kind = sum(
            1 for c in rows if c.get("hit") and any(d.get("kind") == kind for d in (c["answer"] or {}).get("defects", []))
        )
        lines.append(f"  planted {kind}: a box on the spot {sum(1 for c in rows if c.get('hit'))} of {len(rows)}, "
                     f"named '{kind}' {right_kind} of {len(rows)}")
    return "\n".join(lines)
