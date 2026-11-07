# ADR-0007: Working title kept private; new code under the Sustainable Use License

**Status:** Accepted, 2026-10-31

## Context
CE is licensed under the Sustainable Use License v1.0 (non-commercial distribution allowed;
notices must be kept; modified copies need a prominent "modified" notice). "Fallout" is a
trademark of its owner.

## Decision
- "Fallout HD-2D" stays the working title while the project is private. Rename before any
  publication.
- New code is licensed under the same Sustainable Use License v1.0. `NOTICE.md` carries the
  "modified by" notice from day one.
- Third-party code and ML models are listed with their licenses in `docs/THIRD_PARTY.md`
  when first added. Non-commercial model licenses are acceptable for personal use and are
  flagged there.

## Consequences
- One license for the whole repo; no compatibility questions.
- Publishing later means a rename and a review of `docs/THIRD_PARTY.md`.

## Update 2026-11-04 (release track, PLAN.md R4)
- Name for the public release: **Dustlight** (the author: "Dustlight for now"; may still
  change). Not "Fallout ..." (it says "for Fallout 2") and not "HD-2D" (a registered
  Square Enix trademark). The name and version live in `src/hd/release_info.h`.
- First public version: **0.9 beta** (tested on one PC only).
- `docs/THIRD_PARTY.md` reviewed for the release: the shipped components and their
  licenses are listed for players in `docs/player/THIRD_PARTY.md`; every license text
  ships in `hd/docs/licenses/`.
