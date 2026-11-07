# ADR-0003: 4× asset masters with pre-filtered 3× and 2× tiers

**Status:** Accepted, 2026-10-31

## Context
AI upscalers are natively 4×. CE requires a logical resolution of at least 640×480, so the
usable integer scales are 2× at 1080p, 2× or 3× at 1440p, and 3× or 4× at 4K.

## Decision
The pipeline masters every asset at 4× and bakes 3× (4→3 Lanczos) and 2× tiers offline.
The runtime picks an integer scale and loads only the matching tier, so art is always
sampled 1 texel : 1 pixel. Logical coordinates × scale keep hotspot offsets exact.
A lower master scale for one category (e.g. critters at 2×) is allowed only if Phase 1
shows shimmer that stabilization can't fix; that would get its own ADR.

## Consequences
- Disk use is about 1.8× the 4× masters; VRAM holds only one tier.
- At 1440p, 3× gives 853×480 logical (close to the original framing) and 2× gives 1280×720.
