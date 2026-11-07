# fo2hd — offline asset pipeline

Python package for everything Dustlight does outside the engine
(PLAN.md §3.5). It reads game data from the working copy and writes only under
the data root from `hd.local.ini`; nothing it produces belongs in git.

Setup (once): `uv sync` in this folder (Python 3.12 venv in `.venv`).

| Command | What it does |
|---|---|
| `uv run fo2hd replay [capture.json ...]` | Re-rasterizes engine captures from the original FRMs and diffs them against the captured map window (PLAN.md P1.2). Must report 0 px. Writes `<stem>.replay-diff.png` on failure. |
| `uv run fo2hd extract [pattern ...]` | Game files as the game sees them -> `<data_root>/extract/` (resumable). |
| `uv run fo2hd decode [pattern ...]` | FRMs -> RGBA + palette-index PNG per frame + `meta.json` -> `<data_root>/decoded/` (resumable). |
| `uv run fo2hd selftest` | P1.4 acceptance: archive lists, extraction and FRM decoding vs CE's `ce-dat-tool`/`ce-frm2png`, plus a kill-and-resume extraction. Needs `build.ps1` output. |
| `uv run fo2hd sample` | P1.5: sample set (every FRM the captures draw + the walk cycle) → `<data_root>/sample/manifest.json`, decoded. |
| `uv run fo2hd bakeoff [model ...]` | P1.6: upscaler contact sheet + metrics → `<data_root>/reports/bakeoff/`. |
| `uv run fo2hd hdassets [--model category=file]` | P1.7: albedo/normal/height/emissive/cycle maps for the sample set → `<data_root>/hd/`. |
| `uv run fo2hd anim` | P1.8: walk-cycle GIFs and shimmer metric → `<data_root>/reports/anim/`. |
| `uv run fo2hd compose [capture ...]` | P1.9: 4× stills (original, HD with original light, HD lit) → `<data_root>/reports/compose/`. |
| `uv run fo2hd report --notes ../../docs/phase1/report-notes.json` | P1.10: the go/no-go page → `<data_root>/reports/phase1/index.html`. |
| `uv run fo2hd manifest <folders>` / `hdassets --manifest <m>` / `pack --manifest <m>` | P4/P7 bulk passes (every FRM of some art folders). `--adaptive` shares the PC with its user: low priority, half the GPU while the PC is in use, full while locked, paused while the game runs, VRAM capped (`governor`); `<data_root>/batch_mode.txt` = auto/full/shared/pause overrides. `tools/scripts/p7_batch.ps1` runs the P7 batch detached. |
| `uv run fo2hd maps` | P7: every map file → tile usage (floors/roofs; roofs feed `manifest`), the capture tour's views → `<data_root>/maps/`. |
| `uv run fo2hd report7 [--tour-run R] [--baseline R]` | P7 progress page: batch, map scan, critter walk cycles original vs HD, capture tour (`tools/scripts/tour.ps1`) with diffs against an earlier run → `<data_root>/reports/phase7/index.html`. |
| `uv run fo2hd dat-list` | Every file the game can see, after archive precedence. |
| `uv run fo2hd dat-extract <path> <out>` | One file as the game sees it. |

Modules: `dat` (DAT2/ZIP/folder layers in CE's order), `frm` (FRM decoding),
`capture` (engine capture files), `replay` (ports of CE's map draw routines),
`jobs` (SQLite job DB + atomic writes; what makes stages resumable),
`stages` (extract, decode), `upscale` (spandrel + sprite alpha handling),
`bakeoff`, `hdassets`, `anim`, `compose` (4× G-buffer + lighting), `report`,
`artstats` (size of all game art), `governor` (pacing long batches), `maps`
(map files: tile grids, tour views), `report3`…`report7`.

Patterns are game paths with wildcards; `/` and `\` both work
(`art/critters/hmwarr*`). Stage bookkeeping is in `<data_root>/jobs.sqlite`.

GPU: PyTorch 2.14 with CUDA 13 wheels (needed for the RTX 5060 Ti, sm_120).
