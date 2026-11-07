# ADR-0008: Dear ImGui for the dev overlay; RmlUi for player UI

**Status:** Dear ImGui — Accepted, 2026-10-31. RmlUi — Proposed; confirm in Inventory task I1.

## Context
The inventory overhaul must look "Fallout-styled, HD" and needs real layout (grids,
scrolling, tooltips). Developers also need an overlay for feature toggles and frame timings.

## Decision
- **Dear ImGui** (MIT, official D3D11 backend) for the developer overlay only. Players never
  see it.
- **RmlUi** (MIT, HTML/CSS-like documents and styling) for player-facing UI, starting with
  the inventory and later the Phase 7 UI pass. It needs a D3D11 render interface (ours).

## Consequences
- RmlUi is confirmed or replaced by a superseding ADR after the I1 spike.
- Both libraries go into `third_party/` and `docs/THIRD_PARTY.md` when first used.
