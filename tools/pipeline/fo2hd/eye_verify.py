"""The eye, step 5 of "improving detection" (the author, 2026-11-12): a zoomed yes/no check on each reported box.

A whole-crop "list every defect" question makes the champion box ~4 spots on every crop (E1 run 5: clean crops
100% flagged). Asked about one spot at a time, zoomed in, with a plain yes/no, vision models are far more
careful. Each box from an earlier answer (a model's or eye_math's) gets one call: a window around it, cut from the
original and the HD crop alike, blown up with hard pixel edges to about the size the model takes whole.

Answers are appended to a jsonl as they come (resumable: a key already answered without error is skipped).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from . import eye

VERIFY_VERSION = "v1"

VERIFY_SYSTEM = (
    "You check one spot of an HD remaster of Fallout 2, a 1998 isometric role-playing game. You get the same small "
    "area twice, zoomed in with hard pixel edges: image 1 is the original game (low resolution, 256-colour palette, "
    "dithered), image 2 is the HD remaster. The remaster is meant to look like the original, only sharper, with "
    "smoother gradients, new light and soft shadows: those are intended, never a defect. People, animals and "
    "floating text may stand in different places in the two images: that is not a defect either. "
    "Look at the centre of image 2. Is there a real defect there that the original does not have: a thin straight "
    "line, gap or step where pieces meet (seam), something missing or black (hole), a piece still blocky or "
    "pixelated next to sharp art (lowres), dark or bright outlines or halos (outline), stray dots (specks), detail "
    "melted into blobs (smear), texture the original lacks such as cracks, rings, fur or noise (invented), a doubled "
    "or floating shadow (shadow), clearly wrong colour or brightness (color)? Answer yes only if you are sure."
)

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "why": {"type": "string", "maxLength": 200},
        "defect": {"type": "string", "enum": ["yes", "no"]},
        "kind": {"type": "string", "enum": eye.DEFECT_KINDS},
    },
    "required": ["why", "defect", "kind"],
}

VIEW_W, VIEW_H = 880, 600  # what the model sees of each image (Qwen takes this whole, ~560 tokens)


def window(box: list[float], size: tuple[int, int], min_w: int = 220, grow: float = 1.6) -> tuple[int, int, int, int]:
    """A window in the crop around box, with the view's aspect, at least min_w wide and grow x the box."""
    w, h = size
    bw, bh = box[2] - box[0], box[3] - box[1]
    ww = max(min_w, bw * grow, bh * grow * VIEW_W / VIEW_H)
    ww = min(ww, w, h * VIEW_W / VIEW_H)
    wh = ww * VIEW_H / VIEW_W
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    x0 = min(max(0.0, cx - ww / 2), w - ww)
    y0 = min(max(0.0, cy - wh / 2), h - wh)
    return round(x0), round(y0), round(x0 + ww), round(y0 + wh)


def zoomed(img: Image.Image, win: tuple[int, int, int, int]) -> Image.Image:
    part = img.crop(win)
    scale = max(1, min(VIEW_W // part.width, VIEW_H // part.height))  # whole factor: every source pixel the same size
    part = part.resize((part.width * scale, part.height * scale), Image.NEAREST)
    if part.width < VIEW_W * 0.75:  # small windows: finish with a smooth step up to the view size
        part = part.resize((VIEW_W, round(VIEW_W * part.height / part.width)), Image.NEAREST)
    return part


def ask(orig: Image.Image, hd: Image.Image, box: list[float], extra: dict | None = None) -> tuple[eye.Answer, list[int]]:
    win = window(box, orig.size)
    parts = ["Image 1, the original, zoomed in:", zoomed(orig, win), "Image 2, the HD remaster, the same area:",
             zoomed(hd, win), "Is there a real defect at the centre of image 2?"]
    return eye.chat(parts, VERIFY_SYSTEM, VERIFY_SCHEMA, max_tokens=250, temperature=0.0, extra=extra), list(win)


def box_key(item_id: str, box: list[float]) -> str:
    return item_id + "#" + hashlib.sha1(json.dumps([round(v) for v in box]).encode()).hexdigest()[:10]


def read(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["key"]] = row
    return done


def run(jobs: list[tuple[str, str, str, list[float]]], out: Path, extra: dict | None = None, log=print) -> int:
    """jobs: (item id, orig path, hd path, box in crop px). Appends one row per box to out. Returns calls made."""
    done = read(out)
    todo = [j for j in jobs if box_key(j[0], j[3]) not in done or done[box_key(j[0], j[3])].get("error")]
    out.parent.mkdir(parents=True, exist_ok=True)
    images: dict[str, Image.Image] = {}
    made = 0
    import time

    started = time.perf_counter()
    with open(out, "a", encoding="utf-8") as f:
        for n, (item_id, orig_path, hd_path, box) in enumerate(todo, 1):
            if (orig_path not in images or hd_path not in images) and len(images) > 40:
                images.clear()  # before loading the pair, so the pair itself is never dropped
            for p in (orig_path, hd_path):
                if p not in images:
                    images[p] = Image.open(p).convert("RGB")
            a, win = ask(images[orig_path], images[hd_path], box, extra)
            f.write(json.dumps({"key": box_key(item_id, box), "id": item_id, "box": [round(v) for v in box], "window": win,
                                "data": a.data, "s": round(a.seconds, 2), "error": a.error, "v": VERIFY_VERSION}) + "\n")
            f.flush()
            made += 1
            if n % 25 == 0 or n == len(todo):
                spent = time.perf_counter() - started
                log(f"verify: {n}/{len(todo)} in {spent / 60:.1f} min, ~{spent / n * (len(todo) - n) / 60:.0f} min left", flush=True)
    return made
