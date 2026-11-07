# ADR-0009: Text-to-speech for unvoiced dialogue and holodisks

**Status:** Accepted, 2026-10-31 (scope and principles; the model is chosen in task V1)

## Context
Most Fallout 2 dialogue is text only; only talking-head characters are voiced. The author asked
for voice on the text that has none.

## Decision
- **Scope:** NPC dialogue replies and holodisks/notes. Not floating barks, narration,
  look-at text, player options, or lines that already have audio.
- **Generation:** offline batch for all static lines with a high-quality local model; a
  small runtime TTS (sidecar process) covers dynamic lines.
- **Voices:** each NPC gets one consistent synthetic voice, auto-cast from proto, art and
  script data, plus a per-line emotion tag derived from dialogue context. No cloning of
  the original cast.
- **Integration:** a hook in `_scr_get_msg_str_speech` plays generated audio through CE's
  existing non-lip-sync speech path when a line has no audio; a holodisk hook in
  `pipboy.cc`. Toggles under `[hd_voice]`.
- Presentation only: no script, balance or save changes.

## Consequences
- Needs a line inventory that tells replies from options (static scan of compiled scripts).
- Output audio lives under `data_root/voice`, never in the repo.
