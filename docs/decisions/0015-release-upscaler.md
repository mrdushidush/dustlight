# ADR-0015: The release art's upscaler

**Status:** Accepted, 2026-11-05: option 2, the release ships UltraSharpV2 with its author's
permission; the author's pick for the fallback art (below) stays on disk, unused

**Amended 2026-11-06:** the author's review of the release candidate changed what the model is
used for. Characters and creatures use no model: they are the original pixels, enlarged with
Scale2x (`edge16` in each pack's record). Floors and roofs are 60% and walls, rocks and scenery
35% Scale2x, mixed with the model's output. Items, talking heads, the interface and the fonts
are the model's output alone.

## Context
The public release (PLAN.md §6.3a) ships HD art made by an upscaler. The art the author plays
with comes from 4x-UltraSharpV2 (ADR-0011), whose model card and OpenModelDB entry give
CC-BY-NC-SA-4.0 and say nothing about output images. Whether a model's license reaches the
images it makes is unsettled; read strictly, the HD art would have to be non-commercial
(the release is free: fine), credit the author (fine), and be shared under the same
license (ShareAlike: possible only for what we hold rights in, since the art is derived
from Fallout 2's own).

The R1 bake-off (`fo2hd release-bakeoff`, local page `<data_root>\reports\release-bakeoff`)
runs candidates through the production path on the sample set plus talking heads,
interface panels and more critters. First findings (visual, then metrics):
- **Map art** (floors, walls, scenery): 4x-PBRify-UpscalerV4 (CC0) and
  4x-Textures-GTAV-rgt-s-dither (CC-BY-4.0) come close to UltraSharpV2;
  4x-PBRify-RPLKSRd-V3 (CC0) is a little softer.
- **Critters:** every permissive model reads Fallout's dither as texture (a super mutant
  gets rocky, cracked skin), where UltraSharpV2 removes it. A 1x dedither first,
  1x-DitherDeleterV3-Smooth (WTFPL), fixes most of that: smooth muscle, a little flatter
  than UltraSharpV2 (`.chain` models, `fo2hd/upscale.py`).
- **Talking heads:** GTAV-dither alone is closest to UltraSharpV2; PBRify-UpscalerV4
  invents a veined skin texture; the dedither chains turn faces painterly.
- 4x-Nomos2-hq-dat2 and 4x-Nomos2-hq-mosr keep the dither as colored noise.

The pipeline takes a model per art category (`--category-model`), so the release can mix.

## Options
1. **Permissive mix** (release-safe): e.g. GTAV-dither for heads and the interface (and
   fonts), a dedither chain for critters, the best of PBRify-UpscalerV4 / GTAV-dither for
   map art. A full re-batch (one night) and a second tour.
2. **UltraSharpV2 under its own terms:** the HD art released free, crediting Kim2091,
   marked CC BY-NC-SA 4.0 as far as it is ours; best quality, no re-batch. Ideally with
   the author's explicit OK for a free mod first (a message the author would send).

## Decision
- the author asked Kim2091 (2026-11-04) whether UltraSharpV2's output may ship in a free mod. If
  yes: option 2, the release uses the UltraSharpV2 art the author plays with.
- **Kim2091 said yes** (repo owner, 2026-11-04 22:29 UTC,
  <https://github.com/Kim2091/Kim2091-Models/issues/6#issuecomment-6643768678>): "I give
  permission so long as the mod remains free and attribution is given". The request behind it
  described: Nexus Mods, players need their own copy, no paid tiers, no donation points,
  nothing sold; credit by name on the mod page and in the README, with a link to the Ko-fi.
  **Both stay conditions of every release of this art:** `docs/player/art_credits_ultrasharpv2.json`
  carries the credit, the permission and the Ko-fi link to the README and the mod page, and the
  terms to THIRD_PARTY.md. Anything paid, a paid tier or donation points would need Kim2091's
  OK again, or other art. The docs state the author's terms and the model's license; they don't
  relabel the art CC BY-NC-SA as option 2 first imagined, since the permission now says what
  applies (Claude's call, for the author to see at the RC).
- The fonts had no model recorded (`fonts.json` predates the field). Rebuilt with UltraSharpV2
  on 2026-11-05: all 45 atlases byte-identical to the shipped ones (the fallback's differ), so
  the shipped `fonts.json` now names it.
- The fallback, **The author's pick after the pick page (2026-11-04):** "4x-DD3-PBRify-UpscalerV4 and
  4x-PBRify-UpscalerV4 look best to me, sometimes better than UltraSharpV2." Map art and
  characters: **4x-PBRify-UpscalerV4** (CC0); the interface (Pip-Boy, HUD, menus, skilldex,
  inventory, talking heads, fonts): **1x-DitherDeleterV3-Smooth + 4x-PBRify-UpscalerV4**
  (WTFPL + CC0). `models\mix\release-mix-b.mix`; the release batch runs it as the fallback.
  Talking heads follow the interface; they can get their own model if the author prefers.
  Credits for this art: `docs/player/art_credits_mix_b.json` (the dedither model is by
  mutin-choler, WTFPL, per OpenModelDB; `fo2hd release` refuses art the file doesn't cover).
- Fonts follow the interface too, which was Claude's call, not the author's. Checked 2026-11-04:
  the chain and plain PBRify make equally soft text, softer than UltraSharpV2's, and all read
  fine. The author sees them at the RC; the text style is a setting, so it needs no rebuild.

## Consequences
- One art set: the release and the author's own install use the same UltraSharpV2 packs
  (`release_chain.ps1 -NoWait -HdPack <data_root>\hdpack`), and its tour covers the art he plays.
- The mod has to stay free for as long as it ships this art.
- `<release_root>` keeps the fallback's masters and packs. Its batch stopped about two thirds
  through the critters and can resume (`p7_batch.ps1 -DataRoot <release_root> -Model ...
  -CategoryModels ... -From 'hdassets critters' -Detach`) if the author ever prefers that art.
