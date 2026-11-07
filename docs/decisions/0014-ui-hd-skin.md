# ADR-0014: UI as an HD skin over CE's own screens

**Status:** Proposed, 2026-11-02 (approach chosen by the author at the start of Phase 7; the
design below is checked against the code when P7b starts)

## Context
PLAN.md §6.1 row 7 asks for HD versions of the remaining UI (interface bar, dialogue,
PipBoy, character screen, world map) "built on the I-track UI layer", i.e. RmlUi
(ADR-0008, still Proposed). That track (I0–I4) has not started; building it first would
cost far more than P7 itself and delay it. At the start of Phase 7 the author chose an **HD
skin**: the existing screens, their layout and logic untouched, drawn with HD art. The
RmlUi inventory redesign stays a separate, later track.

How CE draws its UI (read 2026-11-02): every screen is a window with an 8-bit buffer
(`window_manager.cc`), filled by a handful of `draw.cc` primitives (`blitBufferToBuffer`,
`blitBufferToBufferTrans`, the `blitBuffer2D` variants, `bufferFill`, a few effects such as
`_lighten_buf`) and by text through one function pointer (`fontDrawTextPtr`,
`text_font.cc`). Screens fetch an art frame's pixel pointer once (`artGetFrameData`,
`FrmImage`) and blit from it, often a sub-rectangle, sometimes much later; buttons keep
pointers to their up/down images and the window manager blits them on state changes.
Some screens compose into a scratch buffer first and copy that into the window.

## Decision
- **Know the art by its address.** When CE loads an FRM (the art cache's read, and named
  art loaded by path), the HD module registers the block's address range with its FRM
  identity; the cache's free unregisters it. Any blit source pointer inside a registered
  block maps to (FRM, frame, x, y): no screen code needs to say what it draws.
- **Record writes into window buffers.** Thin hooks in the `draw.cc` primitives (and the
  text dispatch) record each write whose destination lies in a window's buffer: window,
  rectangle, operation, and the source art if the source pointer is registered. The iso
  map window is skipped (the map layer owns it). Writes into scratch buffers are not
  followed in the first version.
- **Two targets per window, the map's exactness rule.** Recognized writes are replayed on
  the GPU into (1) a 1× index target, from the *original* art, and (2) an HD color target
  at N×, from the HD packs (the same art store, tiers and BC7 page pool as the map).
  Unrecognized writes are not replayed. At present time the compositor shows, per screen
  pixel owned by a window, the HD color only where the 1× replay equals the pixel CE put
  on the screen; everywhere else CE's own pixel. Anything the recorder misses (scratch
  buffers, effects, direct pixel loops, a stale address) therefore falls back to the
  original pixels by itself, never to wrong art, and the count of such pixels per window
  is a live coverage figure. Fades and gamma as on the map (ADR-0013).
- **Text later, in the same frame.** v1 leaves text as CE's pixels (shown nearest, crisp).
  Then text writes are recorded (font, string, color, position) and replayed: 1× from CE's
  own glyph bitmaps for the check, N× from HD glyphs built offline from the game's fonts.
- **UI art packs.** The UI masters are built in P7a (`interface` category: heads,
  intrface, inven, skilldex, backgrnd). Packing waits for P7b's first measurements, which
  decide whether transparent UI art uses its HD alpha or the 1× mask (as tiles do, to
  avoid soft edges spilling over CE pixels).
- **Order of screens** follows measured coverage, not a fixed list: a walk through the
  screens logs, per window, the share of pixels the 1× replay explains; screens with large
  unexplained areas get scratch-buffer tracking or a targeted hook.

## Consequences
- Upstream code touched: `art.cc` (register/unregister: two calls), `draw.cc` and
  `text_font.cc` (one recorder call per primitive), nothing in the screens themselves. The
  hooks only record, so CE's 8-bit output stays bit-identical.
- Correctness is checkable: the 1:1 final frame must still equal the SDL path, and the
  1× replay must equal CE's window pixels wherever it claims coverage (self-test, as in
  Phase 3).
- The inventory's layout stays the original's; the I-track redesign can later replace a
  screen, and the HD skin keeps working for every screen it doesn't replace.
- Talking heads (animated, drawn into the dialogue window from art frames) and the world
  map (tiles of big interface FRMs) are covered by the same mechanism; movies are not
  (separate path).
- Risk: per-blit lookup cost (an ordered map of address ranges; measured in P7b) and
  screens that redraw mostly through scratch buffers (measured by the coverage walk).
