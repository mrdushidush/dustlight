# ADR-0001: Base on the fallout2-ce community fork

**Status:** Accepted, 2026-10-31

## Context
The brief named `alexbatalov/fallout2-ce`, whose last commit is from 2025-02-16. The org fork
`fallout2-ce/fallout2-ce` is 1,482 commits ahead and 0 behind, merges daily, has about 12
active contributors (including sfall maintainers), and lists Epic as a supported store.

## Decision
Our repo is a fork of `fallout2-ce/fallout2-ce`, pinned at `ebb9c716` (2026-10-30) for
Phase 0. The remote is named `upstream`. We merge `upstream/main` about once a month with a
merge commit (never rebase), then rerun the regression captures.

## Consequences
- More bug fixes and better sfall/mod compatibility for free.
- High churn in files we hook (`tile.cc`, `object.cc`, `window_manager.cc`): hooks must stay
  thin and the hooked-file list short (PLAN.md §8).
- Fallout 1 (Phase 8) has no counterpart in that org; revisit when Phase 8 starts.
