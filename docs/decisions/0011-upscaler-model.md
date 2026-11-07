# ADR-0011: 4x-UltraSharpV2 as the upscaler for all art categories

**Status:** Accepted, 2026-10-31 (provisional pick by Claude overnight; confirmed by the author
at the Phase 1 go/no-go).

## Context
P1.6 ran seven 4× models from OpenModelDB on representative sprites of every category
(floors, roofs, walls, scenery, critters incl. the tribal walk cycle, items), with the alpha
handling from `tools/pipeline/fo2hd/upscale.py` (nearest-color fill of transparent pixels,
edge padding, separately upscaled mask). Contact sheet and metrics:
`<data_root>/reports/bakeoff/index.html` (`fo2hd bakeoff`).

Findings (visual review; metrics in the report):
- **Nomos2-hq-mosr** turns Fallout's dithering into colored noise. It "wins" the automatic
  fidelity/sharpness metrics *because* it keeps the dither — the metrics alone can't pick.
- **Fatality-MK2** invents swirls and patterns on floors and walls.
- **FSDedither-Riven** keeps more texture, with some noise and invented wisps.
- **NXbrz** is faithful but soft and blobby.
- **PBRify-RPLKSRd-V3** is clean and a little soft; fastest of the good ones.
- **Textures-GTAV-rgt-s-dither** is clean and crisp, slightly "carved" on floors; close second.
- **UltraSharpV2** (DAT2 architecture) removes the dither cleanly, keeps colors and shapes,
  and invents the least. Best on critters, walls and scenery; good on floors and roofs.

## Decision
Use **4x-UltraSharpV2** (Kim2091, CC-BY-NC-SA-4.0) for every category in Phase 1. The model
is a per-category setting, so floors/roofs can switch to Textures-GTAV-rgt-s-dither
(CC-BY-4.0) later without touching anything else.

## Consequences
- Non-commercial license: fine for a personal project; recorded in `docs/THIRD_PARTY.md`.
  A public release would need a permissively licensed model (GTAV-dither or PBRify are CC).
- Animation stability is judged separately (P1.8); a different critter model may follow.
- Silhouettes keep the original's pixel staircase at 4×; the mask path gets extra smoothing
  (P1.7/P1.9), independent of the model.
