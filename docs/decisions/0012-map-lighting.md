# ADR-0012: Map lighting starts from CE's own light field

**Status:** Accepted, 2026-11-01 (built in Phase 5; the author accepted Phase 5 after its report)

## Context
ADR-0004 fixed the principle (modulate the baked art; the original look stays one toggle
away). Phase 5 had to decide how the new light is computed at runtime. Options: a
physically based light model from the light sources alone, or keeping CE's per-hex light
(light.cc, which already includes wall blocking and the map makers' placement of hidden
light objects) as the authority and only adding direction, color, shadows and bounce on
top. The P1.9 Python compositor, which the author approved at the go/no-go, used the second.

## Decision
- **Field first:** CE's unclamped per-hex light (`lightGetTrueTileIntensity`) is splatted
  onto a half-resolution ground grid (Gaussian in ground units). A pixel's light is the
  ambient (darker and bluer at night, sun-shaped by day) plus the field's light above
  ambient, shared among the lights that reach the point by CE's own falloff. Each share is
  shaped by the surface normal, shadowed, colored by its light and, for lamps and fires,
  boosted near strong light (HDR pools); a critter's own light gets no boost.
- **Inputs from the 1x art:** per-frame shape (normal, height above the ground line)
  computed on upload from the 1x silhouette (P1.7 heuristics), so original and HD art get
  the same shape; relief from the HD albedo's luminance. Heights are interpolated over the
  opaque 1x texels.
- **Light colors** from bright, saturated art pixels around each light's column
  (brightness = largest channel, so neon reds count), mixed half and half with warm
  white, cached per light object (world-anchored, stable while scrolling).
- **Shadows** are short-range heightfield marches (CE's field already holds long-range
  blocking); **sun and AO** from the same heightfield. The sun keeps energy where a
  surface faces the viewer (ADR-0004): its shape is normalized to a surface facing up
  (floors) or toward the camera (upright art), so day scenes gain shadows without the
  camera-facing walls, which turn away from a top-left sun, getting darker. **Bounce light** from 2D radiance
  cascades on the ground grid, emitters = ground and walls lit by the lamps (the ambient's
  bounce is already painted into the art).
- **Tone map:** shoulder on the largest channel (keeps hue), blended with a per-channel
  shoulder (`tonemap_white`) so very bright pools still whiten.
- **World-anchored grids:** the ground grids' origin moves with the world in 32 px steps
  (the top cascade's probe spacing), so scrolling doesn't move texels or probes.
- **Original light stays:** `[hd_light] mode=original` (Ctrl+F7) runs the Phase 4 pass
  unchanged; its checks still pass.
- Every effect has a switch; quality tiers low (no shadows, sun, AO, bounce) / medium (no
  bounce) / high. Look values are provisional settings (`[hd_light]`), tuned with
  side-by-sides.

## Consequences
- Map makers' light placement and wall blocking carry over for free; the mood stays close
  to the original by construction. Brightness drift per scene is measured in every
  self-test.
- Where several lights overlap, CE's field sums them, so pools can get much hotter than
  the original (which clamps at full light). Kept, as in the approved prototype; the tone
  map rolls them off.
- Glowing art (neon, monitors, fire) is lit, not emissive, until Phase 6. Stopgap: bright,
  saturated pixels of objects within 2 hexes of a lamp or fire never get darker than the
  art, so a fire bowl at noon or in a sun shadow doesn't dim.
- Shapes from 1x silhouettes are coarse on HD art; better normals (ML or authored) can
  replace the inputs without changing the light model.
