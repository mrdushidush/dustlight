#ifndef FALLOUT_HD_UI_TEXT_H_
#define FALLOUT_HD_UI_TEXT_H_

#ifdef _WIN32

#include <d3d11.h>

namespace fallout::hd {

// HD glyphs for the HD UI's text (P7b.5): the atlases `fo2hd fonts` builds
// from the game's interface fonts, <hd_art_dir>/fonts/fonts.json and
// font<N>-<style>-<tier>x.png (8-bit glyph coverage).
//
//   [hd] ui_text=1            HD text in the HD UI (default 1)
//   [hd] ui_text_style=model  model (the art upscaler's glyphs, the author's
//                             choice), smooth or sharp
//
// A font's atlas is used only if its hash matches the font CE loaded (the
// same glyph sizes and levels), so a different or modded font falls back
// to CE's pixels. All fonts of one tier live in one R8 Texture2DArray, a
// slice per font.

// Loads the atlases for `scale` (2..4) unless they are loaded already.
// False if HD text is off or nothing usable was found (logged once).
bool uiTextLoad(int scale);
void uiTextShutdown();

ID3D11ShaderResourceView* uiTextAtlas();

// Where glyph `ch` of interface font `font` (0..15) sits in the atlas, in
// texels at the loaded scale, and the font's slice. False if the font has
// no matching atlas or the glyph isn't in it.
bool uiTextGlyph(int font, int ch, int* x, int* y, int* slice);

// "model", "smooth", "sharp"; for the overlay.
const char* uiTextStyle();

} // namespace fallout::hd

#endif // _WIN32

#endif /* FALLOUT_HD_UI_TEXT_H_ */
