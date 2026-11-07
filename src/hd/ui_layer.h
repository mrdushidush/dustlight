#ifndef FALLOUT_HD_UI_LAYER_H_
#define FALLOUT_HD_UI_LAYER_H_

#ifdef _WIN32

#include <d3d11.h>

#include <vector>

namespace fallout::hd {

// The HD UI layer (P7b.2, ADR-0014): CE's own screens, drawn with HD art.
//
//   [hd] ui_hd=1      HD UI (needs renderer=d3d11 and the HD art store,
//                     hd/hd_art.h; default 0 while P7b is in progress)
//
// The UI recorder (hd/ui_recorder.h) keeps, per window, the art copies CE
// made into it as ops and, per pixel, the op that wrote it last. This layer
// replays each window's ops into an HD color target at the screen scale,
// from the HD page pool the map uses (the same packs and tiers). An op whose
// pack is still loading is drawn when it arrives: the window's target is
// drawn again from its live ops, in order.
//
// A screen pixel shows HD UI only where (a) its top window is a recorded
// window, (b) the op that wrote it was drawn in HD, (c) its index isn't a
// palette-cycle color, and (d) the index the recorder expects there equals
// the index CE put on the screen. (d) is the map's exactness rule: fills,
// text, effects, the cursor and anything else the recorder doesn't see fall
// back to CE's own pixels. Colors go through the same fade and gamma fit as
// the HD map (hd/palette_fit.h); film grain and the other post effects stay
// map-only.

struct UiWindowDraw {
    int x = 0; // window rect, logical screen pixels
    int y = 0;
    int width = 0;
    int height = 0;
    int slot = 0; // the window's number in `cells`
    ID3D11ShaderResourceView* hd = nullptr; // RGBA8, `scale` texels per logical pixel, color.pal space
};

struct UiComposite {
    bool active = false;
    int scale = 1;
    // R16_UINT, screen sized: slot << 8 | expected index where (a)-(c)
    // hold, 0 elsewhere. The presenter checks (d).
    ID3D11ShaderResourceView* cells = nullptr;
    std::vector<UiWindowDraw> windows; // bottom to top
    float gain[3] = { 1.0f, 1.0f, 1.0f }; // fade, as hd/map_layer.h's MapComposite
    float offset[3] = { 0.0f, 0.0f, 0.0f };
    float gamma[64] = {};
    bool tint = false; // debug view: HD UI pixels tinted orange (Ctrl+F9)
};

bool uiLayerRequested();
bool uiLayerInit();
void uiLayerShutdown();

// Called by the presenter before it draws a frame; `scale` as for the map
// layer. Inactive at 1:1 (self-tests) and while HD art is off (Ctrl+F8).
UiComposite uiLayerPrepare(int scale);

// Follows Ctrl+F9's GPU map mode ("on", "tinted", "off").
void uiLayerSetMode(const char* mapMode);

// The HD UI self-test (run on the UI recorder's coverage ticks): reads every
// window's HD target back and compares, for each pixel the HD UI would show,
// the mean of its scale x scale HD texels (blended with the palette color
// where their alpha is below 1, as the presenter does) with the live
// palette's color for the index CE drew; skipped during fades. Misplaced or
// wrong art shows up as a low PSNR. Logs the result; returns 0 (pass), 1
// (fail) or -1 (not run).
int uiLayerSelfTest(int tick);

// False while CE runs on a palette other than color.pal (the help screen
// loads helpscrn.pal and rebuilds its color tables for it): the HD UI shows
// CE's pixels then, and the replay self-test's text predictions, made with
// those tables, don't match what was drawn with color.pal's. True when it
// can't tell (HD UI off).
bool uiLayerGamePalette();

// P7b.4, the dialogue's map view (hd/ui_recorder.h uiRecordMapView): an HD
// snapshot of the map window's rect (isoX, isoY, width, height) from the GPU
// map, kept for the map snapshot op that draws it. Returns its id (0: none:
// HD UI or GPU map off) and in `owned` per 1x pixel whether HD may show
// there (the GPU map's 1x pass matched CE's pixel).
int uiLayerMapSnapshot(int isoX, int isoY, int width, int height, std::vector<unsigned char>& owned);

// Lines for the developer overlay (inside its ImGui window).
void uiLayerDrawOverlay();

} // namespace fallout::hd

#endif // _WIN32

#endif /* FALLOUT_HD_UI_LAYER_H_ */
