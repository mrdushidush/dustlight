# ADR-0013: Map effects — palette cycling on HD art, emissives, post chain, HD fades

**Status:** Accepted, 2026-11-02 (built in Phase 6; the author accepted Phase 6 after its report)

## Context
Phase 4/5 drew the original's palette-cycling pixels (water, fire, monitors, alarms, slime)
on HD art as the original's 1× blocks, lit glowing art like everything else, had no post
effects, and fell back to the exact renderer whenever the palette wasn't a plain darkening
of `color.pal` (fade to white). PLAN.md §6.1 row 6 asks for cycle shaders on HD assets,
emissives with bloom, a tone map, grading, grain, vignette and fades on the HD layer.

## Decision
- **Cycling on HD art without new pipeline data.** The HD art was upscaled with the cycle
  colors at phase 0 (`fo2hd.frm.load_palette`). Each HD pixel adds the change
  (live − phase 0) of the 1× cycle pixels around it, read from the 1× art the renderer
  already has, blended with bilinear weights sharpened toward the nearest pixel. The HD
  detail stays, the colors step with CE's own ticks, and 1-pixel patterns (monitor
  screens) animate pixel by pixel as in the original. Interpolating a *position* in the
  cycle instead (tried first) smeared those patterns. Original art keeps CE's exact
  pixels.
- **Emissives.** Palette-cycle colors except the shoreline are emissive: shown at their
  own color, unlit and not tone-mapped, as CE leaves them on objects, and a bloom source.
  Shoreline water is lit like the floor it lies on. P5's glow heuristic (bright, saturated
  pixels near lamps and fires) keeps "never darker than the art" and also feeds the bloom
  at a lower weight. G-buffer coverage target RG8 → RGBA8 (glow, emissive separately).
- **Post chain on the map only.** Light pass (unchanged tone map, or AgX as an option) →
  bloom (13-tap downsample / tent upsample over ~6 half-size levels, added
  "screen"-style) → grade (white balance, lift/gamma/gain, saturation, optional .cube
  LUT) → vignette (elliptical, map window) → compositor: fade + CE's gamma as before, then
  film grain (24 Hz) on HD map pixels. The interface, dialogue and menus are never
  touched. Bloom needs the new light's bloom source; grade and vignette also apply with
  the original light. `[hd_post]`, Ctrl+F5.
- **Fades.** The palette check fits the live palette as `gain × color.pal + offset` per
  channel (least squares over the non-cycling colors): fades to black are a gain, fades
  to white (quitting to the menu, the new-game transition) add an offset. The compositor
  applies both; cycle colors have the fade taken out the same way.
- **Stacked lights.** CE clamps each hex at full light; P5 kept the unclamped sum for HDR
  pools. Where dozens of lights stack (Klamath's toxic caves) that ran to 2–3× the
  original's brightness, so `[hd_light] field_max` rolls the sum off softly.

## Consequences
- No re-bake or pack change for cycling; art with cycle colors costs a few texture reads
  per pixel in the G-buffer and HD passes. A frame with cycle colors is redrawn on every
  cycle tick (5–30 Hz), as before.
- Bloom, grade and vignette run once per map redraw (not per frame); grain per frame in
  the compositor.
- The self-test checks HD cycle colors against CE's live colors (original light), reports
  brightness drift with and without post effects, and counts frames that fall back during
  fades.
