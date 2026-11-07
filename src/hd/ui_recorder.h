#ifndef FALLOUT_HD_UI_RECORDER_H_
#define FALLOUT_HD_UI_RECORDER_H_

#include <stddef.h>

#include <map>
#include <string>
#include <unordered_map>
#include <vector>

namespace fallout::hd {

// UI recorder (P7b, ADR-0014): what the UI's HD skin knows about CE's windows.
//
// Art is known by its address: art.cc reports every FRM it loads (art cache
// reads by fid, named art by path) and every block it frees, so any blit
// whose source pointer lies inside a loaded FRM is "art" (FRM, frame, x, y).
// draw.cc's two copy primitives (srcCopy, transSrcCopy: every
// blitBufferToBuffer[Trans] and blitBuffer2D) and the text dispatch report
// their writes; writes into a window buffer (except the map's) are recorded.
//
// P7b.0 measures: per window, which share of its pixels a replay of the
// recorded art writes would reproduce exactly ([hd_debug] ui_coverage_ms,
// logged to ui_coverage.txt).
//
// P7b.1 keeps those writes as ops: a copy of a whole-pitch rectangle out of
// one frame of a loaded FRM. Every window pixel knows the op that wrote it
// last, and an op lives while it still owns a pixel, so a window's live ops,
// replayed in order, rebuild every pixel they own. The replay self-test
// checks exactly that against the original art on every coverage tick.
// Nothing is drawn differently; with the recorder off each hook is one
// branch on gUiRecorderActive.
//
// P7b.5 makes text draws ops too (interface fonts only, CE's 100..): the
// text rect is saved before CE draws, and afterwards CE's draw is replayed
// on that copy from the font's own glyphs and color.cc's blend tables (the
// "1x check"). Only if the replay gives exactly the pixels CE drew does the
// text become an op that owns its glyph pixels (shadow included), which the
// HD UI layer then draws with HD glyphs; otherwise the changed pixels are
// plain text pixels, as before, and CE's pixels show.

extern bool gUiRecorderActive;

// What last wrote a window pixel, as far as the recorder knows.
enum UiKind : unsigned char {
    UiKindUnknown, // never written by a recorded copy (fills, effects, direct loops)
    UiKindArt, // copied from a frame of a loaded FRM: owned by an op
    UiKindArtLoose, // copied from a loaded FRM, but not as a frame rectangle (odd pitch)
    UiKindText, // drawn by the current font
    UiKindWindowCopy, // copied from a window buffer
    UiKindBufferCopy, // copied from any other buffer (scratch buffers)
    UiKindFill, // a solid fill (bufferFill): the same at any scale, CE's pixels show
    UiKindMap, // the map window's pixels with an HD snapshot of the GPU map (dialogue's map view): owned by an op
    UiKindCount,
};

// A glyph of a text op, in the op's rect (CE's placement).
struct UiGlyphPlace {
    unsigned char ch;
    short x;
    short y;
    short width;
    short height;
};

// One recorded art copy (or, with font >= 0, one text draw) into a window.
struct UiOp {
    int fid = -1; // art cache fid; -1 for named art, see `path`
    std::string path; // named art: the path CE loaded
    int rotation = 0;
    int frame = 0;
    int frameWidth = 0;
    int frameHeight = 0;
    unsigned int hash = 0; // FNV-1a of the frame's indices (hd/hd_art.h's check)
    bool cycle = false; // the frame has palette-cycle colors (229..254)
    int srcX = 0; // the copied rectangle in the frame ...
    int srcY = 0;
    int destX = 0; // ... and in the window
    int destY = 0;
    int width = 0;
    int height = 0;
    bool trans = false; // index 0 left the window's pixel as it was
    // P7b.4: darkened afterwards through color.cc's intensityColorTable
    // column `dim` (1..127, x dim/128 in 5-bit RGB; 0 = not), as the world
    // map dims the squares it knows but the player hasn't visited.
    int dim = 0;
    int owned = 0; // window pixels whose last recorded write is this op
    bool hd = false; // the UI layer drew it into the window's HD target (P7b.2)

    // Text ops (P7b.5). destX, destY, width and height are the text rect
    // (one pixel wider and taller with a shadow); the art fields are unused.
    int font = -1; // interface font 0..15 (CE's font number - 100); -1: an art op
    int color = 0; // the text's palette index
    int shadowColor = 0; // the shadow's (CE's COLOR_BLACK) if `shadow`
    bool shadow = false; // CE drew it again in black at +1,+1 first
    std::vector<UiGlyphPlace> glyphs;
    std::vector<unsigned char> before; // the rect's pixels before CE drew the text
    std::vector<unsigned int> beneath; // ... and the ops that owned them then (0 = none)
    // Carried out of a scratch buffer through a transparent copy (P7b.4,
    // button faces): the copy skipped the rect's index-0 pixels, so the
    // backdrop must leave the pixels whose `before` is 0 alone.
    bool transCopy = false;

    // Map snapshot ops (P7b.4, the dialogue's map view): `snapshot` is the
    // UI layer's id of an HD copy of a rect of the GPU map; frameWidth x
    // frameHeight is that rect, `pixels` CE's 1x pixels of it (what the
    // replay writes), srcX/srcY where this op's rect starts in it, as with
    // art. The fid/path fields are unused.
    int snapshot = 0;
    std::vector<unsigned char> pixels;
};

struct UiWindow {
    int id = -1;
    unsigned char* buffer = nullptr;
    int width = 0;
    int height = 0;
    unsigned int generation = 0; // changes when the state starts over (new buffer or size)
    // Per pixel: the value the last recorded write left, its kind, and the
    // op that wrote it (serial, 0 = none). A pixel is explained while the
    // buffer still holds that value.
    std::vector<unsigned char> expected;
    std::vector<unsigned char> kind;
    std::vector<unsigned int> owner;
    std::map<unsigned int, UiOp> ops; // live ops by serial = draw order
    unsigned int nextSerial = 1;
    // Pixels changed since the UI layer last took them (window coordinates,
    // inclusive; empty while left > right).
    int dirtyLeft = 0;
    int dirtyTop = 0;
    int dirtyRight = -1;
    int dirtyBottom = -1;
    // Coverage counters (P7b.0).
    std::map<std::string, int> frms; // art blits per FRM
    int writes[UiKindCount] = {};
    int oddPitch = 0;
    int textOps = 0; // text draws that passed the 1x check (P7b.5)
};

// Why text draws didn't become ops (P7b.5), counted for the exit log.
enum UiTextFallback {
    UiTextOk,
    UiTextNotInterfaceFont, // a FON text font (CE's 0..9)
    UiTextUnderlined, // HD text doesn't draw underlines
    UiTextOutside, // the rect (with the shadow) leaves the window, or a glyph starts left of it
    UiTextMismatch, // the 1x replay differs from CE's pixels
    UiTextFallbackCount,
};

// The recorder's windows by window id.
std::unordered_map<int, UiWindow>& uiWindows();

// art.cc: a loaded FRM (Art header + frames) at `data`, `size` bytes. `fid`
// is -1 for named art, which passes its path instead.
void uiArtLoaded(const void* data, size_t size, int fid, const char* path);
void uiArtFreed(const void* data);
// heap.cc: the art cache's blocks live on a heap that frees them on eviction
// and moves unlocked ones to make room (cache.cc never calls its freeProc).
// Both are passed on for every heap block; unknown addresses are ignored.
void uiArtMoved(const void* from, const void* to);

// window_manager.cc: the window is about to be freed.
void uiWindowDestroyed(int id);

// P7b.4: a scratch buffer CE composes art and text in before copying it into
// a window (button faces with labels, tab images, the message display's
// background). Registered where CE makes it (before anything is drawn into
// it), it is a recorder surface like a window, under a negative id in
// uiWindows(), and its ops carry over when it is copied into a window.
// Unregistered before CE frees it. No-ops while the recorder is off.
void uiScratchRegister(unsigned char* buffer, int width, int height);
void uiScratchFree(const unsigned char* buffer);

void uiRecordCopyImpl(unsigned char* dest, int destPitch, const unsigned char* src, int srcPitch, int width, int height, bool trans);
void uiRecordFillImpl(unsigned char* dest, int pitch, int width, int height, unsigned char value);
void uiRecordDimImpl(unsigned char* dest, int pitch, int width, int height, int intensity);
void uiRecordTextBeforeImpl(unsigned char* dest, int pitch, const char* string, int length, int color);
void uiRecordTextImpl(unsigned char* dest, int pitch, const char* string, int length, int color);

// Before srcCopy / transSrcCopy writes `width` x `height` pixels at `dest`.
inline void uiRecordCopy(unsigned char* dest, int destPitch, const unsigned char* src, int srcPitch, int width, int height, bool trans)
{
    if (gUiRecorderActive) {
        uiRecordCopyImpl(dest, destPitch, src, srcPitch, width, height, trans);
    }
}

// Before bufferFill sets `width` x `height` pixels at `dest` to `value`.
inline void uiRecordFill(unsigned char* dest, int pitch, int width, int height, unsigned char value)
{
    if (gUiRecorderActive) {
        uiRecordFillImpl(dest, pitch, width, height, value);
    }
}

// Before CE replaces each pixel of the rect by intensityColorTable[pixel]
// [intensity] (the world map's known, unvisited squares). Art ops owning
// pixels there go on as dimmed copies (UiOp::dim); other pixels are unknown.
inline void uiRecordDim(unsigned char* dest, int pitch, int width, int height, int intensity)
{
    if (gUiRecorderActive) {
        uiRecordDimImpl(dest, pitch, width, height, intensity);
    }
}

// Before a whole frame (`width` x `height`, pitch `width`) is memcpy'd into a
// buffer: CE draws several screens' backgrounds into their windows that way
// (P7b.4: options, Pip-Boy, character screen, dialog boxes, ...).
inline void uiRecordFrameCopy(unsigned char* dest, const unsigned char* src, int width, int height)
{
    uiRecordCopy(dest, width, src, width, width, height, false);
}

// Before and after the current font draws `string` with its top-left corner
// at `dest` (`length`: CE's limit in pixels; `color`: ColorWithFlags).
inline void uiRecordTextBefore(unsigned char* dest, int pitch, const char* string, int length, int color)
{
    if (gUiRecorderActive) {
        uiRecordTextBeforeImpl(dest, pitch, string, length, color);
    }
}

inline void uiRecordText(unsigned char* dest, int pitch, const char* string, int length, int color)
{
    if (gUiRecorderActive) {
        uiRecordTextImpl(dest, pitch, string, length, color);
    }
}

// P7b.4: CE has just copied `width` x `height` pixels at (isoX, isoY) of
// the map window to `dest` (pitch `destPitch`): the dialogue's map view,
// rendered centered on the speaker. With the HD UI on, the UI layer asks the
// GPU map for the same rect in HD (hd/map_layer.h mapLayerSnapshot), and the
// pixels where the GPU map's 1x pass equals CE's become a map snapshot op.
void uiRecordMapViewImpl(unsigned char* dest, int destPitch, int isoX, int isoY, int width, int height);

inline void uiRecordMapView(unsigned char* dest, int destPitch, int isoX, int isoY, int width, int height)
{
    if (gUiRecorderActive) {
        uiRecordMapViewImpl(dest, destPitch, isoX, isoY, width, height);
    }
}

// The fallback counts so far (UiTextFallback order).
const long long* uiTextCounts();

// hd::init, once the game is up: starts the coverage ticker if configured.
void uiRecorderStart();
void uiRecorderShutdown();

// The HD UI layer (hd/ui_layer.h) records from the first window on.
void uiRecorderEnable();

// The P7b.1 self-test: replays each window's live ops in order from the
// original art (locked again by fid or path) and checks that every pixel an
// op owns gets that op's value, the one the recorder expects. Ops whose art
// changed since they were recorded are skipped and counted. Logs the result;
// returns the number of wrong pixels.
int uiReplaySelfTest(int tick);

} // namespace fallout::hd

#endif /* FALLOUT_HD_UI_RECORDER_H_ */
