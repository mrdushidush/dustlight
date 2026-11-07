#ifndef FALLOUT_HD_D3D11_PRESENTER_H_
#define FALLOUT_HD_D3D11_PRESENTER_H_

#include <SDL.h>

namespace fallout::hd {

// Direct3D 11 presentation path (PLAN.md Phase 2, ADR-0002).
//
// CE keeps drawing into its 8-bit SDL surface exactly as before. Instead of
// converting that surface to RGB on the CPU (SDL_BlitSurface on every
// _GNW95_ShowRect and palette change) and presenting through SDL_Renderer,
// this path uploads the 8-bit pixels as an R8 texture and the palette as a
// 256x1 texture, and a pixel shader does the lookup. The picture must be
// identical; presenterSelfTest proves it by reading the frame back.
//
// Selected with fallout2.cfg [hd] renderer=d3d11 (default: sdl). svga.cc
// asks presenterRequested() before creating the window; if the device can't
// be created, the game falls back to the SDL renderer.

// [hd] renderer=d3d11, read straight from the game config (svga init runs
// before hd::init).
bool presenterRequested();

// True once the D3D11 path owns presentation.
bool presenterActive();

bool presenterCreate(SDL_Window* window, int width, int height);
void presenterDestroy();

// Uploads the 8-bit surface and presents it, plus the movie overlay if any.
// The palette is read from the surface itself (whatever CE last gave to
// SDL_SetPaletteColors), so palette changes need no extra hook.
void presenterPresent(SDL_Surface* surface);

// The window size changed (resize, fullscreen toggle).
void presenterResize();

// Movie overlay (movie.cc): a 32-bit ARGB frame drawn scaled into `dest`
// (logical coordinates), on top of the 8-bit screen.
bool presenterOverlayEnsure(int width, int height);
void presenterOverlayUpload(const void* argbPixels, int width, int height, int pitch);
void presenterOverlaySet(bool active, const SDL_Rect& dest);
void presenterOverlayDestroy();

// Shows or hides the developer overlay (Dear ImGui, display only: renderer,
// frame times, map and light info). Starts visible with [hd_debug] overlay=1.
// Returns the new state.
bool presenterToggleOverlay();

// Implemented by the HD module (hd.cc): game-side lines for the overlay.
// Called inside the overlay's ImGui window.
void overlayDrawGameInfo();

// Renders the current 8-bit surface at 1:1 into an offscreen target, reads it
// back and compares it with SDL's own 8-bit -> RGB conversion of the same
// surface (what the SDL path would show). Returns the number of differing
// pixels, or -1 if the test could not run. Logs the result.
int presenterSelfTest(SDL_Surface* surface, const char* label);

// Frame sequences for animation GIFs (PLAN.md P6.0):
//
//   [hd_debug] frame_seq_count=48     frames per sequence (0 = off, the default)
//   [hd_debug] frame_seq_step=2       keep every Nth presented frame
//   [hd_debug] frame_seq_rect=x,y,w,h crop in window pixels (default: all)
//
// Starts the next sequence (Ctrl+F4, usually injected): the presented frames
// are cropped into memory as they go and written once the sequence is done,
// as <reports>/present/seq<k>-<NNN>.png, with a log line giving each frame's
// time. Returns false if sequences are off or one is still running.
bool presenterStartFrameSequence();

} // namespace fallout::hd

#endif /* FALLOUT_HD_D3D11_PRESENTER_H_ */
