# ADR-0006: Single local fork repo, layout, and game-data safety rails

**Status:** Accepted, 2026-10-31

## Context
Personal project; the author wants git local only for now. The brief forbids committing game
data or anything derived from it.

## Decision
- One repo at `D:\dev\fallout2-hd`: upstream code plus `src/hd/` (engine module),
  `tools/pipeline/`, `tools/voice/`, `tools/scripts/`, `docs/decisions/`.
- Remote `upstream` only; no `origin` until the author decides to publish. Phase 0's CI is a local
  script (`tools/scripts/ci.ps1`) with a dormant GitHub Actions file for later.
- Machine paths live in the git-ignored `hd.local.ini` (template: `hd.local.ini.example`).
- `.gitignore` blocks game-data, image, audio, video and model formats, except upstream's
  own assets under `docs/images/`, `files/`, `os/`, `sfall_testing/`.
- Versioned hooks (`git config core.hooksPath tools/git-hooks`) reject the same formats and
  any file over 5 MB at commit and merge time; files identical to the merged-in upstream
  commit pass.

## Consequences
- Screenshots and comparison stills are never committed; reports that show them are
  generated into `data_root`.
- Each clone must run `git config core.hooksPath tools/git-hooks` once
  (`tools/scripts/ci.ps1` checks it).
