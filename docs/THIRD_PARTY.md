# Third-party code and models

Everything Dustlight uses that we didn't write, with its license (ADR-0007).
Upstream CE's own dependencies (SDL2, zlib, fpattern, lodepng, stb_vorbis) are covered
by upstream's notices and not repeated here.

## Engine (vendored in `third_party/`)

| Library | Version | License | Used for |
|---|---|---|---|
| Dear ImGui (core + dx11/sdl2 backends, unmodified) | 1.93.0 | MIT | developer overlay (ADR-0008), `[hd_debug] overlay=1` / Ctrl+F11 |

## Python pipeline (`tools/pipeline`, installed by uv, never vendored)

| Package | License | Used for |
|---|---|---|
| numpy | BSD-3-Clause | arrays |
| Pillow | MIT-CMU (HPND) | PNG I/O |
| PyTorch, torchvision | BSD-3-Clause | GPU inference |
| spandrel | MIT | loading upscaler architectures |
| opencv-python-headless | Apache-2.0 | image filters, distance transforms |
| imageio | BSD-2-Clause | GIF/video output |

## ML models (downloaded to `<data_root>/models`, never committed)

Listed with SHA-256 in `<data_root>/models/candidates.json` (from OpenModelDB).

| Model | Author | License | Status |
|---|---|---|---|
| 4x-UltraSharpV2 | Kim2091 | CC-BY-NC-SA-4.0 | **in use, in the release too**, with the author's permission (free mod, credit; ADR-0011, ADR-0015) |
| 4x-Textures-GTAV-rgt-s-dither | Helaman (Phhofm) | CC-BY-4.0 | bake-off runner-up |
| 4x-PBRify-RPLKSRd-V3 | Kim2091 | CC0-1.0 | bake-off |
| 4x-FSDedither-Riven | jacob | GPL-3.0-only | bake-off |
| 4x-Fatality-MK2 | Twittman | CC-BY-NC-SA-4.0 | bake-off |
| 4x-NXbrz | archerpolation | CC-BY-NC-SA-4.0 | bake-off |
| 4x-Nomos2-hq-mosr | Helaman (Phhofm) | CC-BY-4.0 | bake-off |

Non-commercial licenses are acceptable while the project is personal (PLAN.md §3.7);
any public release needs a review of this table.
