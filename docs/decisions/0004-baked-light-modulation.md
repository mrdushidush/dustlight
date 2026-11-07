# ADR-0004: Modulate baked art, with an "original-equivalent" invariant

**Status:** Accepted, 2026-10-31

## Context
Original sprites have lighting painted in. In the original engine, light only darkens:
intensity 65536 shows the art unchanged and ambient is clamped to 25–100 %. The author chose the
look "same mood, more depth".

## Decision
- `final = albedo × light + emissive`, with the raw art treated as "the scene at full light".
- **Invariant:** with all new effects off, the renderer reproduces the original intensity
  field exactly. This is a selectable test mode.
- Normal mapping modulates light around the original value (a flat-facing surface keeps the
  original brightness); shadows and GI darken and tint; lights may exceed 1.0 near sources.
- Light color is derived automatically from the emitting object's art (fallback warm white).
- Directional strength per category: floors full, walls/scenery reduced, critters low.
- No de-lighting in v1.

## Consequences
- The original look is always one toggle away, which keeps mood drift measurable.
- Per-category strengths are tuned in Phase 5 with side-by-sides.
