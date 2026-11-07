#ifndef FONT_MANAGER_H
#define FONT_MANAGER_H

#include "color.h"
#include "text_font.h"

namespace fallout {

extern FontManager gModernFontManager;

int interfaceFontsInit();
void interfaceFontsExit();

// HD text (hd/ui_recorder.cc, hd/ui_text.cc): an interface font as CE loaded
// it. Glyph `ch` is widths[ch] x heights[ch] levels at glyphData[ch].
struct InterfaceFontView {
    int maxHeight;
    int letterSpacing;
    int wordSpacing;
    int widths[256];
    int heights[256];
    const unsigned char* glyphData[256];
};

// `font` is the font number (100..); false if that font isn't loaded.
bool interfaceFontGetView(int font, InterfaceFontView* view);

// The interface fonts are loaded (the presenter draws frames before that).
bool interfaceFontsLoaded();
// Scaled interface font drawing supports plain color text plus DRAW_TEXT_FLAG_SHADOWED.
// Unsupported flags like DRAW_TEXT_FLAG_MONOSPACED and DRAW_TEXT_FLAG_UNDERLINED are ignored with a debug log.
void interfaceFontDrawTextScaled2D(const Buffer2D& dest, int x, int y, const char* string, ColorWithFlags color, float scale);
int interfaceFontGetStringWidthScaled(const char* string, ColorWithFlags color, float scale);

} // namespace fallout

#endif /* FONT_MANAGER_H */
