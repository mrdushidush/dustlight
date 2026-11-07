# ADR-0010: Inventory UX overhaul as pure presentation

**Status:** Accepted, 2026-10-31

## Context
The author finds the original inventory lacking: too few visible items, no information without
clicking, no sorting or filtering, clunky stack moves. Scope: main inventory, loot, barter,
steal and party gear, in a "Fallout-styled, HD" look.

## Decision
- New UI on the GPU UI layer (ADR-0008), entered from the existing `inventory.cc` and
  `game_dialog.cc` entry points when `[hd_ui] inventory=1`.
- **Sorting and filtering are view-only.** The real inventory array order never changes,
  because scripts read items by index.
- **Every mutation goes through the existing functions** (`_move_inventory`, equip, use,
  drop, barter math). Bulk moves are repeated normal moves, so AP costs, weight limits,
  steal rolls (one per move) and prices are unchanged.
- **State-diff test:** the same scripted operations through the old and new UI from the same
  save must produce identical game state.

## Consequences
- A few static logic functions in `inventory.cc` get exposed through a small internal header.
- The old UI stays selectable.
