#include "hd/map_layer.h"

#ifdef _WIN32

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <filesystem>
#include <string>
#include <unordered_map>
#include <vector>

#include <wrl/client.h>

#include "art.h"
#include "color.h"
#include "config.h"
#include "debug.h"
#include "display_monitor.h"
#include "game_config.h"
#include "hd/d3d11_presenter.h"
#include "hd/d3d11_shared.h"
#include "hd/draw_list.h"
#include "hd/draw_recorder.h"
#include "hd/first_run.h"
#include "hd/hd_art.h"
#include "hd/map_lighting.h"
#include "hd/map_post.h"
#include "hd/map_renderer.h"
#include "hd/palette_fit.h"
#include "imgui.h"
#include "input.h"
#include "interface.h"
#include "lodepng.h"
#include "map.h"
#include "mouse.h"
#include "svga.h"
#include "tile.h"
#include "window_manager.h"

using Microsoft::WRL::ComPtr;

namespace fallout::hd {

namespace {

    // GPU time of one map redraw (exact 1x pass + HD or Nx pass), from
    // timestamp queries read back a few redraws later without stalling.
    struct GpuTimer {
        ComPtr<ID3D11Query> disjoint;
        ComPtr<ID3D11Query> begin;
        ComPtr<ID3D11Query> end;
        bool pending = false;
    };

    constexpr int kGpuTimers = 4;

    struct State {
        bool enabled = true; // Ctrl+F9 cycles enabled/tint
        bool tint = false;
        bool valid = false; // the GPU map matches the last emission
        int drawnScale = 0; // scale the scaled target was last drawn at
        int width = 0;
        int height = 0;

        ComPtr<ID3D11Texture2D> mask;
        ComPtr<ID3D11ShaderResourceView> maskView;
        int maskWidth = 0;
        int maskHeight = 0;
        std::vector<unsigned char> maskCpu;
        bool maskChanged = true; // maskCpu not yet uploaded
        std::vector<intptr_t> maskInputs; // what maskCpu was built from
        bool maskCacheValid = false;

        // HD color pass (P4.2).
        bool hdDrawn = false; // the HD target matches the current build
        ID3D11ShaderResourceView* hdView = nullptr; // what the compositor shows: post output or the HD target
        int lastScale = 1; // scale the presenter last asked for
        int hdImages = 0; // HD self-test images written
        float basePalette[256 * 4] = {};
        unsigned char cmapSnapshot[768] = {}; // color.pal as loaded, taken on first map frame
        bool cmapTaken = false;
        unsigned char lastCycle[26 * 3] = {}; // _systemCmap 229..254 at the last HD draw
        int hdRedraws = 0; // HD-only redraws (cycle colors, scale)
        bool paletteOk = false; // live palette = gamma(gain * color.pal)
        float gain[3] = { 1.0f, 1.0f, 1.0f };
        float offset[3] = { 0.0f, 0.0f, 0.0f }; // 6-bit units: live = gain * color.pal + offset (P6.4)
        int fadeFrames = 0; // HD frames shown with a fade (gain or offset not neutral)
        int fallbackFrames = 0; // frames that fell back to the exact pass: the palette isn't a fade of color.pal

        // Stats for the overlay.
        int emissions = 0;
        double emitMs = 0.0;
        double buildMs = 0.0;
        double drawMs = 0.0;
        double hdDrawMs = 0.0;
        GpuTimer gpuTimers[kGpuTimers];
        int gpuTimerNext = 0;
        double gpuMs = 0.0; // last measured redraw
        double gpuMsMax = 0.0;
        double gpuMsSum = 0.0;
        int gpuSamples = 0;

        // [hd_debug] map_selftest_ms.
        int selfTestIntervalMs = 0;
        int selfTestTier = 0; // [hd_debug] hd_selftest_tier: HD check at this tier (0 = the window's scale)
        unsigned int lastSelfTest = 0;
        int selfTestRuns = 0;
        int selfTestFailures = 0;
        int liveImages = 0; // comparison images written for live fallback
        std::string lastSelfTestResult = "not run";
        std::string reportDir;
    };

    State* gState = nullptr;

    double millisecondsSince(const LARGE_INTEGER& start)
    {
        LARGE_INTEGER now;
        LARGE_INTEGER frequency;
        QueryPerformanceCounter(&now);
        QueryPerformanceFrequency(&frequency);
        return (now.QuadPart - start.QuadPart) * 1000.0 / frequency.QuadPart;
    }

    Window* visibleIsoWindow()
    {
        Window* iso = windowGetWindow(gIsoWindow);
        if (iso == nullptr || (iso->flags & WINDOW_HIDDEN) != 0 || iso->buffer == nullptr) {
            return nullptr;
        }
        return iso;
    }

    // Everything the mask depends on, except the pixels of transparent
    // windows (whose presence forces a rebuild). Returns false if a
    // transparent window overlaps the map.
    bool maskInputs(Window* iso, int isoIndex, std::vector<intptr_t>& inputs)
    {
        bool cacheable = true;
        inputs.clear();
        inputs.push_back(reinterpret_cast<intptr_t>(iso));
        inputs.push_back(iso->rect.left);
        inputs.push_back(iso->rect.top);
        inputs.push_back(iso->width);
        inputs.push_back(iso->height);
        int count = windowGetCount();
        for (int index = isoIndex + 1; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || (window->flags & WINDOW_HIDDEN) != 0) {
                continue;
            }
            inputs.push_back(reinterpret_cast<intptr_t>(window));
            inputs.push_back(window->flags);
            inputs.push_back(window->rect.left);
            inputs.push_back(window->rect.top);
            inputs.push_back(window->rect.right);
            inputs.push_back(window->rect.bottom);
            if ((window->flags & WINDOW_TRANSPARENT) != 0) {
                cacheable = false;
            }
        }
        inputs.push_back(cursorIsHidden() ? 1 : 0);
        if (!cursorIsHidden()) {
            Rect cursor;
            mouseGetRect(&cursor);
            const unsigned char* shape;
            int pitch;
            unsigned char transparent;
            mouseGetCursorImage(&shape, &pitch, &transparent);
            inputs.push_back(cursor.left);
            inputs.push_back(cursor.top);
            inputs.push_back(cursor.right);
            inputs.push_back(cursor.bottom);
            inputs.push_back(reinterpret_cast<intptr_t>(shape));
            inputs.push_back(pitch);
            inputs.push_back(transparent);
            // Cursor frames are redrawn in place (action menu, animations).
            unsigned int hash = 2166136261u;
            if (shape != nullptr) {
                for (int y = 0; y <= cursor.bottom - cursor.top; y++) {
                    for (int x = 0; x <= cursor.right - cursor.left; x++) {
                        hash = (hash ^ shape[y * pitch + x]) * 16777619u;
                    }
                }
            }
            inputs.push_back(static_cast<intptr_t>(hash));
        }
        return cacheable;
    }

    // Which pixels of the map window the screen shows as map: not under a
    // window above it (pixel-exact for transparent windows, whose zero
    // pixels show what's below), not under an opaque cursor pixel. Reuses
    // the last mask when nothing it depends on changed.
    void buildMask(Window* iso, bool withCursor = true)
    {
        int count = windowGetCount();
        int isoIndex = -1;
        for (int index = 0; index < count; index++) {
            if (windowGetWindowAt(index) == iso) {
                isoIndex = index;
                break;
            }
        }

        std::vector<intptr_t> inputs;
        bool cacheable = withCursor && maskInputs(iso, isoIndex, inputs);
        if (cacheable && gState->maskCacheValid && inputs == gState->maskInputs) {
            return;
        }
        gState->maskCacheValid = cacheable;
        gState->maskInputs.swap(inputs);
        gState->maskChanged = true;

        int width = iso->width;
        int height = iso->height;
        gState->maskCpu.assign(static_cast<size_t>(width) * height, 1);
        unsigned char* mask = gState->maskCpu.data();

        for (int index = isoIndex + 1; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || (window->flags & WINDOW_HIDDEN) != 0) {
                continue;
            }
            int left = std::max(window->rect.left, iso->rect.left);
            int top = std::max(window->rect.top, iso->rect.top);
            int right = std::min(window->rect.right, iso->rect.right);
            int bottom = std::min(window->rect.bottom, iso->rect.bottom);
            if (left > right || top > bottom) {
                continue;
            }
            bool transparent = (window->flags & WINDOW_TRANSPARENT) != 0 && window->buffer != nullptr;
            for (int y = top; y <= bottom; y++) {
                unsigned char* row = mask + static_cast<size_t>(y - iso->rect.top) * width;
                if (!transparent) {
                    memset(row + (left - iso->rect.left), 0, right - left + 1);
                    continue;
                }
                const unsigned char* src = window->buffer + static_cast<size_t>(y - window->rect.top) * window->width;
                for (int x = left; x <= right; x++) {
                    if (src[x - window->rect.left] != 0) {
                        row[x - iso->rect.left] = 0;
                    }
                }
            }
        }

        if (withCursor && !cursorIsHidden()) {
            Rect cursor;
            mouseGetRect(&cursor);
            const unsigned char* shape;
            int pitch;
            unsigned char transparent;
            mouseGetCursorImage(&shape, &pitch, &transparent);
            if (shape != nullptr) {
                int left = std::max(cursor.left, iso->rect.left);
                int top = std::max(cursor.top, iso->rect.top);
                int right = std::min(cursor.right, iso->rect.right);
                int bottom = std::min(cursor.bottom, iso->rect.bottom);
                for (int y = top; y <= bottom; y++) {
                    for (int x = left; x <= right; x++) {
                        if (shape[(y - cursor.top) * pitch + (x - cursor.left)] != transparent) {
                            mask[static_cast<size_t>(y - iso->rect.top) * width + (x - iso->rect.left)] = 0;
                        }
                    }
                }
            }
        }
    }

    bool uploadMask()
    {
        ID3D11Device* device = presenterDevice();
        ID3D11DeviceContext* context = presenterContext();
        if (!gState->mask || gState->maskWidth != gState->width || gState->maskHeight != gState->height) {
            gState->mask.Reset();
            gState->maskView.Reset();
            gState->maskChanged = true;
            D3D11_TEXTURE2D_DESC desc = {};
            desc.Width = gState->width;
            desc.Height = gState->height;
            desc.MipLevels = 1;
            desc.ArraySize = 1;
            desc.Format = DXGI_FORMAT_R8_UINT;
            desc.SampleDesc.Count = 1;
            desc.Usage = D3D11_USAGE_DEFAULT;
            desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            if (FAILED(device->CreateTexture2D(&desc, nullptr, &gState->mask)) || FAILED(device->CreateShaderResourceView(gState->mask.Get(), nullptr, &gState->maskView))) {
                debugPrint("HD: map layer: can't create the mask texture\n");
                gState->mask.Reset();
                return false;
            }
            gState->maskWidth = gState->width;
            gState->maskHeight = gState->height;
        }
        if (gState->maskChanged) {
            context->UpdateSubresource(gState->mask.Get(), 0, nullptr, gState->maskCpu.data(), gState->width, 0);
            gState->maskChanged = false;
        }
        return true;
    }

    // Whether the live palette is color.pal faded by a per-channel gain
    // (CE's fades lerp toward black) and CE's gamma table: then the HD
    // pass's base-space colors can be shown through the same transform.
    // Anything else (another palette loaded, a fade toward a color) falls
    // back to the exact pass for that frame.
    bool analyzePalette()
    {
        if (!gState->cmapTaken) {
            memcpy(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot));
            gState->cmapTaken = true;
        }
        gState->paletteOk = false;
        if (memcmp(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot)) != 0) {
            return false;
        }
        PaletteFit fit = paletteFit();
        for (int c = 0; c < 3; c++) {
            gState->gain[c] = fit.gain[c];
            gState->offset[c] = fit.offset[c];
        }
        gState->paletteOk = fit.ok;
        return fit.ok;
    }

    // Base-space palette for the HD pass: color.pal, plus the live cycle
    // colors (229..254) with the fade taken out again.
    void buildBasePalette()
    {
        for (int i = 0; i < 256; i++) {
            bool cycle = i >= 229 && i <= 254;
            const unsigned char* src = cycle ? &_systemCmap[i * 3] : &_cmap[i * 3];
            for (int c = 0; c < 3; c++) {
                float v = src[c] * 4 / 255.0f;
                if (cycle && gState->gain[c] > 0.05f) {
                    v = std::min(std::max((v - gState->offset[c] / 63.75f) / gState->gain[c], 0.0f), 1.0f);
                }
                gState->basePalette[i * 4 + c] = v;
            }
            gState->basePalette[i * 4 + 3] = 1.0f;
        }
        memcpy(gState->lastCycle, &_systemCmap[229 * 3], sizeof(gState->lastCycle));
    }

    void gpuTimerCollect()
    {
        ID3D11DeviceContext* context = presenterContext();
        for (GpuTimer& timer : gState->gpuTimers) {
            if (!timer.pending || context == nullptr) {
                continue;
            }
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint;
            UINT64 begin;
            UINT64 end;
            if (context->GetData(timer.disjoint.Get(), &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK
                || context->GetData(timer.begin.Get(), &begin, sizeof(begin), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK
                || context->GetData(timer.end.Get(), &end, sizeof(end), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                continue;
            }
            timer.pending = false;
            if (!disjoint.Disjoint && disjoint.Frequency > 0 && end >= begin) {
                gState->gpuMs = (end - begin) * 1000.0 / disjoint.Frequency;
                gState->gpuMsMax = std::max(gState->gpuMsMax, gState->gpuMs);
                gState->gpuMsSum += gState->gpuMs;
                gState->gpuSamples++;
            }
        }
    }

    GpuTimer* gpuTimerBegin()
    {
        ID3D11Device* device = presenterDevice();
        ID3D11DeviceContext* context = presenterContext();
        if (device == nullptr || context == nullptr) {
            return nullptr;
        }
        gpuTimerCollect();
        GpuTimer& timer = gState->gpuTimers[gState->gpuTimerNext];
        if (timer.pending) {
            return nullptr; // all in flight: skip this sample
        }
        if (!timer.disjoint) {
            D3D11_QUERY_DESC desc = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
            D3D11_QUERY_DESC stamp = { D3D11_QUERY_TIMESTAMP, 0 };
            if (FAILED(device->CreateQuery(&desc, &timer.disjoint)) || FAILED(device->CreateQuery(&stamp, &timer.begin)) || FAILED(device->CreateQuery(&stamp, &timer.end))) {
                timer = GpuTimer();
                return nullptr;
            }
        }
        gState->gpuTimerNext = (gState->gpuTimerNext + 1) % kGpuTimers;
        context->Begin(timer.disjoint.Get());
        context->End(timer.begin.Get());
        return &timer;
    }

    void gpuTimerEnd(GpuTimer* timer)
    {
        ID3D11DeviceContext* context = presenterContext();
        if (timer == nullptr || context == nullptr) {
            return;
        }
        context->End(timer->end.Get());
        context->End(timer->disjoint.Get());
        timer->pending = true;
    }

    bool drawHd(int scale)
    {
        LARGE_INTEGER start;
        QueryPerformanceCounter(&start);
        buildBasePalette();
        // The new light (P5) or the P4 pass with CE's light.
        bool newLight = mapLightingActive();
        if (newLight) {
            gState->hdDrawn = mapLightingDraw(gState->width, gState->height, scale, gState->basePalette);
        } else {
            gState->hdDrawn = mapRendererDrawHd(scale, gState->basePalette);
        }
        // Post effects (P6.3) on top; bloom needs the new light's source.
        gState->hdView = mapRendererHdView(scale);
        if (gState->hdDrawn) {
            gState->hdView = mapPostDraw(gState->hdView, newLight ? mapLightingBloomSource() : nullptr, gState->width * scale, gState->height * scale);
        }
        gState->hdDrawMs = millisecondsSince(start);
        return gState->hdDrawn;
    }

    bool rebuild(Window* iso, int scale, int hdTier);

    // Emits the draw list and renders it at 1x and `scale`: the exact pass,
    // or with `hdTier` the HD pass at that scale.
    bool refresh(Window* iso, int scale, int hdTier = 0)
    {
        LARGE_INTEGER start;
        QueryPerformanceCounter(&start);
        if (!drawEmitFullFrame()) {
            // Tiles disabled (map loading) or no map: try again next frame.
            gState->valid = false;
            return false;
        }
        drawClearMapDirty();
        gState->emitMs = millisecondsSince(start);
        gState->emissions++;
        return rebuild(iso, scale, hdTier);
    }

    // Builds and draws the current draw list again (no emission): for HD
    // art that arrived since the last build.
    bool rebuild(Window* iso, int scale, int hdTier)
    {
        LARGE_INTEGER start;
        QueryPerformanceCounter(&start);
        gState->width = iso->width;
        gState->height = iso->height;
        gState->hdDrawn = false;
        gState->valid = mapRendererBuild(iso->width, iso->height, hdTier);
        gState->buildMs = millisecondsSince(start);

        QueryPerformanceCounter(&start);
        GpuTimer* timer = gpuTimerBegin();
        gState->valid = gState->valid && mapRendererDraw(1);
        if (gState->valid && mapRendererHdTier() != 0) {
            drawHd(scale);
            gState->drawnScale = 0; // exact target at `scale` not drawn
        } else {
            gState->valid = gState->valid && (scale == 1 || mapRendererDraw(scale));
            gState->drawnScale = gState->valid ? scale : 0;
        }
        gpuTimerEnd(timer);
        gState->drawMs = millisecondsSince(start);
        return gState->valid;
    }

    void writeComparisonPng(const std::string& path, const unsigned char* cpu, const std::vector<unsigned char>& gpu, int width, int height)
    {
        // CPU | GPU | difference (white where they differ), current palette.
        const unsigned char* palette = directDrawGetPalette();
        std::vector<unsigned char> rgb(static_cast<size_t>(width) * 3 * height * 3);
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                unsigned char a = cpu[static_cast<size_t>(y) * width + x];
                unsigned char b = gpu[static_cast<size_t>(y) * width + x];
                unsigned char* row = &rgb[static_cast<size_t>(y) * width * 3 * 3];
                for (int c = 0; c < 3; c++) {
                    row[x * 3 + c] = palette[a * 3 + c] << 2;
                    row[(width + x) * 3 + c] = palette[b * 3 + c] << 2;
                    row[(2 * width + x) * 3 + c] = a != b ? 255 : (palette[a * 3 + c] << 2) / 4;
                }
            }
        }
        lodepng::encode(path, rgb, width * 3, height, LCT_RGB, 8);
    }

    // For a failed 1x check: the draw records whose rectangle covers (x, y).
    void logRecordsAt(int x, int y)
    {
        const char* names[] = { "pass", "fill", "floor", "roof", "object", "outline", "text" };
        int index = 0;
        for (const DrawRecord& record : drawRecords()) {
            int left = record.op == DrawOp::Fill ? record.clip.left : record.x;
            int top = record.op == DrawOp::Fill ? record.clip.top : record.y;
            int width = record.op == DrawOp::Fill ? record.clip.right - left + 1 : record.width;
            int height = record.op == DrawOp::Fill ? record.clip.bottom - top + 1 : record.height;
            if (record.op == DrawOp::Floor || record.op == DrawOp::Roof) {
                width = 80;
                height = 36;
            }
            if (record.op != DrawOp::Pass && x >= left && x < left + width && y >= top && y < top + height) {
                debugPrint("HD:   record %d %s fid 0x%08x frame %d rot %d at (%d, %d) %dx%d light %d blend %d egg %d outline 0x%08x flags 0x%08x tile %d\n",
                    index, names[static_cast<int>(record.op)], record.fid, record.frame, record.rotation, record.x, record.y, record.width, record.height,
                    record.light, static_cast<int>(record.blend), record.egg ? 1 : 0, record.outline, record.flags, record.tile);
            }
            index++;
        }
    }

    // What draws 1x pixel (x, y) last (art opaque there, in draw order), for
    // the HD hole check: a translucent object, a pixel on the silhouette of
    // its own frame (an 8-neighbor transparent or outside the frame), or
    // neither (or nothing found).
    constexpr int kUncoveredInterior = 0;
    constexpr int kUncoveredEdge = 1;
    constexpr int kUncoveredTranslucent = 2;

    int classifyUncovered(int x, int y)
    {
        int kind = kUncoveredInterior;
        for (const DrawRecord& record : drawRecords()) {
            if (record.op != DrawOp::Floor && record.op != DrawOp::Roof && record.op != DrawOp::Object) {
                continue;
            }
            int fx = x - record.x;
            int fy = y - record.y;
            if (fx < 0 || fy < 0 || (record.op == DrawOp::Object && (fx >= record.width || fy >= record.height))) {
                continue;
            }
            CacheEntry* handle;
            Art* art = artLock(FrmId(record.fid), &handle);
            if (art == nullptr) {
                continue;
            }
            int w = 0;
            int h = 0;
            Rotation rotation = static_cast<Rotation>(record.rotation);
            artGetSize(art, record.frame, rotation, &w, &h);
            const unsigned char* data = artGetFrameData(art, record.frame, rotation);
            if (data != nullptr && fx < w && fy < h && data[fy * w + fx] != 0) {
                kind = kUncoveredInterior;
                if (record.op == DrawOp::Object && record.blend != ObjectBlend::Normal) {
                    kind = kUncoveredTranslucent;
                } else {
                    for (int dy = -1; dy <= 1 && kind == kUncoveredInterior; dy++) {
                        for (int dx = -1; dx <= 1; dx++) {
                            int nx = fx + dx;
                            int ny = fy + dy;
                            if (nx < 0 || ny < 0 || nx >= w || ny >= h || data[ny * w + nx] == 0) {
                                kind = kUncoveredEdge;
                                break;
                            }
                        }
                    }
                }
            }
            artUnlock(handle);
        }
        return kind;
    }

    // Before the forced refresh: map pixels where the composite falls back
    // to CE's pixel because the GPU map disagrees with the screen. Measures
    // how well the per-present emission tracks CE's incremental redraws.
    int liveMismatch(Window* iso, const char* label)
    {
        if (gSdlSurface == nullptr || !mapLayerPrepare(1, true).active) {
            return -1;
        }
        std::vector<unsigned char> exact;
        int width = 0;
        int height = 0;
        if (!mapRendererReadback(1, exact, &width, &height) || width != iso->width || height != iso->height) {
            return -1;
        }
        // What the screen shows of the map, for the comparison image.
        std::vector<unsigned char> shown(exact.size(), 0);
        int mismatched = 0;
        for (int y = 0; y < height; y++) {
            int screenY = y + iso->rect.top;
            if (screenY < 0 || screenY >= gSdlSurface->h) {
                continue;
            }
            const unsigned char* screen = static_cast<const unsigned char*>(gSdlSurface->pixels) + static_cast<size_t>(gSdlSurface->pitch) * screenY;
            for (int x = 0; x < width; x++) {
                int screenX = x + iso->rect.left;
                size_t i = static_cast<size_t>(y) * width + x;
                if (screenX < 0 || screenX >= gSdlSurface->w) {
                    continue;
                }
                shown[i] = gState->maskCpu[i] != 0 ? screen[screenX] : exact[i];
                if (shown[i] != exact[i]) {
                    mismatched++;
                }
            }
        }
        if (mismatched > 0 && gState->liveImages < 3) {
            gState->liveImages++;
            writeComparisonPng(gState->reportDir + "\\" + label + "-live.png", shown.data(), exact, width, height);
        }
        return mismatched;
    }

    // Check (d): the mask (without the cursor) must be exactly the set of
    // screen pixels the window manager fills from the map window. Found by
    // composing all windows twice, the second time with every map pixel
    // flipped; the pixels that change are the map's.
    int ownershipMismatch(Window* iso, int* ownedPixels)
    {
        int screenWidth = screenGetWidth();
        int screenHeight = screenGetHeight();
        Rect screen = { 0, 0, screenWidth - 1, screenHeight - 1 };
        std::vector<unsigned char> normal(static_cast<size_t>(screenWidth) * screenHeight);
        std::vector<unsigned char> flipped(normal.size());
        size_t isoSize = static_cast<size_t>(iso->width) * iso->height;
        windowRenderAllToBuffer(&screen, normal.data());
        for (size_t i = 0; i < isoSize; i++) {
            iso->buffer[i] ^= 1;
        }
        windowRenderAllToBuffer(&screen, flipped.data());
        for (size_t i = 0; i < isoSize; i++) {
            iso->buffer[i] ^= 1;
        }

        buildMask(iso, false);
        int bad = 0;
        *ownedPixels = 0;
        for (int y = 0; y < screenHeight; y++) {
            for (int x = 0; x < screenWidth; x++) {
                size_t i = static_cast<size_t>(y) * screenWidth + x;
                bool owned = normal[i] != flipped[i];
                bool masked = false;
                int qx = x - iso->rect.left;
                int qy = y - iso->rect.top;
                if (qx >= 0 && qy >= 0 && qx < iso->width && qy < iso->height) {
                    masked = gState->maskCpu[static_cast<size_t>(qy) * iso->width + qx] != 0;
                }
                if (owned) {
                    (*ownedPixels)++;
                }
                if (owned != masked) {
                    bad++;
                }
            }
        }
        buildMask(iso);
        return bad;
    }

    // A base-space channel value as psComposite shows it, 0..255.
    float displayChannel(float value, int channel)
    {
        float v = std::min(std::max(value * gState->gain[channel] + gState->offset[channel] / 63.75f, 0.0f), 1.0f) * 63.75f;
        int i = std::min(static_cast<int>(v), 63);
        int j = std::min(i + 1, 63);
        return (_currentGammaTable[i] + (_currentGammaTable[j] - _currentGammaTable[i]) * (v - i)) * 4.0f;
    }

    void writeRgbPng(const std::string& path, const std::vector<unsigned char>& rgb, int width, int height)
    {
        lodepng::encode(path, rgb, width, height, LCT_RGB, 8);
    }

    // Palette-cycle check (P6.1): where the exact map shows a cycle color that
    // has moved away from its phase-0 color (what the HD art was upscaled
    // with), the HD pass (box-averaged over the logical pixel, as displayed)
    // is compared with CE's color there and with the phase-0 color. Animated
    // HD art is much closer to the first; frozen art to the second. Checked
    // with the original light only (the new light changes colors on purpose).
    struct CycleStats {
        bool pass = true;
        std::string text; // per range: pixels compared, error, error if frozen
    };

    CycleStats cycleCheck(const std::vector<unsigned char>& exact, const std::vector<unsigned char>& tags, int width, int height, const std::vector<float>& hd, int tier, const unsigned char* palette)
    {
        struct Range {
            int first;
            int count;
            const char* name;
            long long pixels;
            double error;
            double stepped;
        };
        Range ranges[] = {
            { 229, 4, "slime" },
            { 233, 5, "monitors" },
            { 238, 5, "slow fire" },
            { 243, 5, "fast fire" },
            { 248, 6, "shoreline" },
            { 254, 1, "alarm" },
        };
        int hdWidth = width * tier;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                size_t q = static_cast<size_t>(y) * width + x;
                int index = exact[q];
                if (index < 229 || index > 254 || tags[q] != 0) {
                    continue;
                }
                float phase0[3];
                mapRendererCyclePhase0(index, phase0);
                double frozen[3];
                double moved = 0.0;
                for (int c = 0; c < 3; c++) {
                    frozen[c] = displayChannel(phase0[c], c);
                    moved = std::max(moved, fabs(frozen[c] - (palette[index * 3 + c] << 2)));
                }
                if (moved < 24.0) {
                    continue; // the live color is (nearly) the phase-0 one: can't tell
                }
                for (Range& range : ranges) {
                    if (index < range.first || index >= range.first + range.count) {
                        continue;
                    }
                    double error = 0.0;
                    double frozenError = 0.0;
                    for (int c = 0; c < 3; c++) {
                        double shown = 0.0;
                        for (int dy = 0; dy < tier; dy++) {
                            for (int dx = 0; dx < tier; dx++) {
                                shown += displayChannel(hd[(static_cast<size_t>(y * tier + dy) * hdWidth + x * tier + dx) * 4 + c], c);
                            }
                        }
                        shown /= tier * tier;
                        error += fabs(shown - (palette[index * 3 + c] << 2));
                        frozenError += fabs(shown - frozen[c]);
                    }
                    range.pixels++;
                    range.error += error / 3.0;
                    range.stepped += frozenError / 3.0;
                }
            }
        }
        CycleStats stats;
        for (Range& range : ranges) {
            if (range.pixels == 0) {
                continue;
            }
            double error = range.error / range.pixels;
            double frozen = range.stepped / range.pixels;
            char text[128];
            snprintf(text, sizeof(text), "%s%s %lld px, error %.1f (frozen %.1f)", stats.text.empty() ? "" : "; ", range.name, range.pixels, error, frozen);
            stats.text += text;
            if (range.pixels >= 50 && error > 0.5 * frozen) {
                stats.pass = false;
            }
        }
        return stats;
    }

    // Seam metric (P4.4): color steps between neighboring floor pixels that
    // belong to two different floor tiles ("edge") vs. the same tile
    // ("inside"), for the HD pass and for the exact 1x map. HD pairs are taken
    // only across 1x pixel lines (where a seam would sit), so both images are
    // measured at the same spacing. A pixel counts if a floor tile drew it and
    // no later record's rectangle covers it. edge/inside ratios close to the
    // original's mean the HD art adds no visible seams.
    struct SeamStats {
        double hdEdge = 0.0;
        double hdInside = 0.0;
        double exactEdge = 0.0;
        double exactInside = 0.0;
        long long edgePairs = 0;
    };

    SeamStats seamMetric(const std::vector<unsigned char>& exact, const std::vector<unsigned char>& tags, int width, int height, const std::vector<float>& hd, int tier, const unsigned char* palette)
    {
        size_t size = static_cast<size_t>(width) * height;
        std::vector<int> owner(size, -1);
        int recordIndex = 0;
        for (const DrawRecord& record : drawRecords()) {
            recordIndex++;
            if (record.op == DrawOp::Floor) {
                CacheEntry* handle;
                Art* art = artLock(FrmId(record.fid), &handle);
                if (art == nullptr) {
                    continue;
                }
                int w = 0;
                int h = 0;
                artGetSize(art, record.frame, static_cast<Rotation>(record.rotation), &w, &h);
                unsigned char* data = artGetFrameData(art, record.frame, static_cast<Rotation>(record.rotation));
                for (int y = 0; data != nullptr && y < h; y++) {
                    int py = record.y + y;
                    for (int x = 0; py >= 0 && py < height && x < w; x++) {
                        int px = record.x + x;
                        if (px >= 0 && px < width && data[y * w + x] != 0) {
                            owner[static_cast<size_t>(py) * width + px] = recordIndex;
                        }
                    }
                }
                artUnlock(handle);
            } else if (record.op == DrawOp::Object || record.op == DrawOp::Roof || record.op == DrawOp::Outline || record.op == DrawOp::Text) {
                int w = record.op == DrawOp::Roof ? 80 : record.width;
                int h = record.op == DrawOp::Roof ? 36 : record.height;
                for (int py = std::max(record.y, 0); py < std::min(record.y + h, height); py++) {
                    for (int px = std::max(record.x, 0); px < std::min(record.x + w, width); px++) {
                        owner[static_cast<size_t>(py) * width + px] = -1;
                    }
                }
            }
        }

        SeamStats stats;
        long long counts[4] = {};
        auto exactStep = [&](size_t a, size_t b) {
            int step = 0;
            for (int c = 0; c < 3; c++) {
                step = std::max(step, abs(palette[exact[a] * 3 + c] - palette[exact[b] * 3 + c]) << 2);
            }
            return step;
        };
        auto hdStep = [&](size_t a, size_t b) {
            float step = 0.0f;
            for (int c = 0; c < 3; c++) {
                step = std::max(step, fabsf(displayChannel(hd[a * 4 + c], c) - displayChannel(hd[b * 4 + c], c)));
            }
            return step;
        };
        int hdWidth = width * tier;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                size_t a = static_cast<size_t>(y) * width + x;
                if (owner[a] < 0 || tags[a] != 0) {
                    continue;
                }
                for (int dir = 0; dir < 2; dir++) {
                    int nx = x + (dir == 0 ? 1 : 0);
                    int ny = y + (dir == 1 ? 1 : 0);
                    if (nx >= width || ny >= height) {
                        continue;
                    }
                    size_t b = static_cast<size_t>(ny) * width + nx;
                    if (owner[b] < 0 || tags[b] != 0) {
                        continue;
                    }
                    bool edge = owner[a] != owner[b];
                    double hdSum = 0.0;
                    // The HD pixel pairs across the line between the two 1x pixels.
                    for (int k = 0; k < tier; k++) {
                        int hx0 = dir == 0 ? x * tier + tier - 1 : x * tier + k;
                        int hy0 = dir == 0 ? y * tier + k : y * tier + tier - 1;
                        size_t ha = static_cast<size_t>(hy0) * hdWidth + hx0;
                        size_t hb = ha + (dir == 0 ? 1 : hdWidth);
                        hdSum += hdStep(ha, hb);
                    }
                    if (edge) {
                        stats.exactEdge += exactStep(a, b);
                        stats.hdEdge += hdSum / tier;
                        counts[0]++;
                    } else {
                        stats.exactInside += exactStep(a, b);
                        stats.hdInside += hdSum / tier;
                        counts[1]++;
                    }
                }
            }
        }
        stats.edgePairs = counts[0];
        if (counts[0] > 0) {
            stats.exactEdge /= counts[0];
            stats.hdEdge /= counts[0];
        }
        if (counts[1] > 0) {
            stats.exactInside /= counts[1];
            stats.hdInside /= counts[1];
        }
        return stats;
    }

    // Check (e), HD mode: the HD pass at `tier`, after the HD art it asks
    // for has loaded. Holes (where the exact map has a pixel, alpha low
    // enough that what fills in behind the HD art changes the 8-bit result:
    // alpha < 1 - 1/255; soft edges beside undrawn pixels excepted, see
    // below) must be 0; stacked blends in RGBA16F leave alpha a few
    // fp16 steps below 1 (~0.9985), which no display can show. Its colors are compared with the exact pass's (outline pixels
    // excluded; those come from the exact pass anyway); with [hd_debug]
    // hd_force_original every frame uses the original art, and the two may
    // differ only by CE's palette quantization of light and blends.
    int hdSelfTest(Window* iso, const char* label, int tier)
    {
        bool built = false;
        for (int attempt = 0; attempt < 16; attempt++) {
            built = rebuild(iso, tier, tier);
            if (!built) {
                break;
            }
            bool waited = false;
            for (int wait = 0; wait < 500 && hdArtLoading(); wait++) {
                Sleep(10);
                hdArtPoll();
                waited = true;
            }
            if (!waited && hdArtStats().buildDeferred == 0) {
                break;
            }
        }
        analyzePalette();
        if (!built || mapRendererHdTier() != tier || !gState->hdDrawn) {
            debugPrint("HD: map self-test '%s' hd: FAIL, HD pass not rendered at %dx\n", label, tier);
            return 1;
        }
        std::vector<unsigned char> exact;
        std::vector<unsigned char> tags;
        std::vector<float> hd;
        int width = 0;
        int height = 0;
        int hdWidth = 0;
        int hdHeight = 0;
        if (!mapRendererReadback(1, exact, &width, &height, &tags) || !mapRendererHdReadback(tier, hd, &hdWidth, &hdHeight) || hdWidth != width * tier || hdHeight != height * tier) {
            debugPrint("HD: map self-test '%s' hd: FAIL, readback\n", label);
            return 1;
        }

        const unsigned char* palette = directDrawGetPalette();
        bool images = gState->hdImages < 4;
        std::vector<unsigned char> hdRgb(images ? static_cast<size_t>(hdWidth) * hdHeight * 3 : 0);
        std::vector<unsigned char> exactRgb(hdRgb.size());
        // Where the HD alpha is below 1 - 1/255 on a pixel the 1x map draws,
        // the presenter fills the rest with CE's color of that pixel (P7 fix
        // A). That is a soft HD edge if the pixel lies on the 1x silhouette
        // of the art that draws it (or borders a pixel nothing is drawn on)
        // and the HD art covers at least a quarter of it, or a translucent
        // object over nothing; anywhere else it is a hole: HD coverage
        // missing inside what the 1x map draws.
        constexpr float kMinSoftAlpha = 0.25f;
        // Alpha 254/255 in the BC7 art lands a fp16 step below 1 - 1/255 when
        // nothing is under it; whatever fills in then moves the 8-bit result
        // by at most one level, so it isn't a hole either.
        constexpr float kNearOpaque = 1.0f - 2.0f / 255.0f;
        int nearOpaque = 0;
        std::unordered_map<size_t, int> uncovered; // 1x pixel -> classifyUncovered
        int holes = 0;
        int softEdges = 0;
        float minSoftAlpha = 1.0f;
        int firstHoleX = 0;
        int firstHoleY = 0;
        float firstHoleAlpha = 0.0f;
        float minAlpha = 1.0f;
        constexpr float kHoleAlpha = 1.0f - 1.0f / 255.0f;
        auto bordersUndrawn = [&](int x1, int y1) {
            for (int dy = -1; dy <= 1; dy++) {
                for (int dx = -1; dx <= 1; dx++) {
                    int nx = x1 + dx;
                    int ny = y1 + dy;
                    if ((dx != 0 || dy != 0) && nx >= 0 && ny >= 0 && nx < width && ny < height && exact[static_cast<size_t>(ny) * width + nx] == 0) {
                        return true;
                    }
                }
            }
            return false;
        };
        int outlinePixels = 0;
        long long compared = 0;
        double sumSquares = 0.0;
        int maxError = 0;
        double lumaHd = 0.0;
        double lumaExact = 0.0;
        std::vector<long long> histogram(256, 0);
        for (int y = 0; y < hdHeight; y++) {
            for (int x = 0; x < hdWidth; x++) {
                size_t q = static_cast<size_t>(y / tier) * width + x / tier;
                const float* px = &hd[(static_cast<size_t>(y) * hdWidth + x) * 4];
                unsigned char index = exact[q];
                if (index != 0) {
                    minAlpha = std::min(minAlpha, px[3]);
                }
                // Outline pixels (tagged) show the exact pass's color whatever
                // the HD alpha, so only untagged pixels get filled.
                bool filled = index != 0 && px[3] < kHoleAlpha && tags[q] == 0;
                if (filled) {
                    auto known = uncovered.find(q);
                    int kind = known != uncovered.end() ? known->second : (uncovered[q] = classifyUncovered(x / tier, y / tier));
                    bool soft = kind == kUncoveredTranslucent
                        || ((kind == kUncoveredEdge || bordersUndrawn(x / tier, y / tier)) && px[3] >= kMinSoftAlpha);
                    if (soft) {
                        softEdges++;
                        minSoftAlpha = std::min(minSoftAlpha, px[3]);
                    } else if (px[3] >= kNearOpaque) {
                        nearOpaque++;
                    } else if (holes++ == 0) {
                        firstHoleX = x;
                        firstHoleY = y;
                        firstHoleAlpha = px[3];
                    }
                }
                int error = 0;
                for (int c = 0; c < 3; c++) {
                    int expected = palette[index * 3 + c] << 2;
                    // As the presenter shows it (psComposite).
                    float shown = !filled ? displayChannel(px[c], c)
                        : px[3] > 0.0f    ? expected + (displayChannel(px[c] / px[3], c) - expected) * px[3]
                                          : static_cast<float>(expected);
                    if (images) {
                        size_t o = (static_cast<size_t>(y) * hdWidth + x) * 3 + c;
                        // Outline pixels as the compositor shows them: the exact pass's.
                        hdRgb[o] = tags[q] != 0 ? static_cast<unsigned char>(expected) : static_cast<unsigned char>(std::min(255.0f, shown + 0.5f));
                        exactRgb[o] = static_cast<unsigned char>(expected);
                    }
                    if (tags[q] == 0) {
                        double d = shown - expected;
                        sumSquares += d * d;
                        error = std::max(error, static_cast<int>(fabs(d) + 0.5));
                    }
                }
                if (tags[q] != 0) {
                    outlinePixels++;
                    continue;
                }
                for (int c = 0; c < 3; c++) {
                    constexpr double kLuma[3] = { 0.299, 0.587, 0.114 };
                    lumaHd += kLuma[c] * displayChannel(px[c], c);
                    lumaExact += kLuma[c] * (palette[index * 3 + c] << 2);
                }
                compared++;
                maxError = std::max(maxError, error);
                histogram[std::min(error, 255)]++;
            }
        }
        double mse = compared > 0 ? sumSquares / (compared * 3.0) : 0.0;
        double psnr = mse > 0.0 ? 10.0 * log10(255.0 * 255.0 / mse) : 99.0;
        int p99 = 0;
        long long below = 0;
        for (int e = 0; e < 256; e++) {
            below += histogram[e];
            if (below >= compared * 99 / 100) {
                p99 = e;
                break;
            }
        }

        CycleStats cycles = cycleCheck(exact, tags, width, height, hd, tier, palette);
        SeamStats seams = seamMetric(exact, tags, width, height, hd, tier, palette);
        const HdArtStats& art = hdArtStats();
        // Thresholds for the original-equivalent run, set from the benchmark
        // scenes (dark scenes: CE darkens through the palette, coarse there).
        // The new light (P5) changes colors on purpose: only holes count, and
        // the mean brightness drift against the exact pass is reported.
        constexpr double kMinPsnr = 30.0;
        constexpr int kMaxP99 = 24;
        bool newLight = mapLightingActive();
        bool pass = holes == 0 && (!art.forceOriginal || newLight || (psnr >= kMinPsnr && p99 <= kMaxP99)) && (newLight || cycles.pass);
        double drift = lumaExact > 0.0 ? (lumaHd / lumaExact - 1.0) * 100.0 : 0.0;
        // The same with the post effects (P6.3), as the compositor shows it.
        double postDrift = drift;
        std::vector<float> post;
        int postWidth = 0;
        int postHeight = 0;
        if (mapPostReadback(post, &postWidth, &postHeight) && postWidth == hdWidth && postHeight == hdHeight) {
            double lumaPost = 0.0;
            for (int y = 0; y < hdHeight; y++) {
                for (int x = 0; x < hdWidth; x++) {
                    size_t q = static_cast<size_t>(y / tier) * width + x / tier;
                    if (tags[q] != 0) {
                        continue;
                    }
                    const float* px = &post[(static_cast<size_t>(y) * hdWidth + x) * 4];
                    lumaPost += 0.299 * displayChannel(px[0], 0) + 0.587 * displayChannel(px[1], 1) + 0.114 * displayChannel(px[2], 2);
                }
            }
            postDrift = lumaExact > 0.0 ? (lumaPost / lumaExact - 1.0) * 100.0 : 0.0;
        }
        if (images) {
            gState->hdImages++;
            std::string base = gState->reportDir + "\\" + label;
            writeRgbPng(base + "-hd.png", hdRgb, hdWidth, hdHeight);
            writeRgbPng(base + "-orig.png", exactRgb, hdWidth, hdHeight);
        }
        debugPrint("HD: map self-test '%s' hd: %s (tier %d, %dx%d, %d holes, %d soft-edge px filled from the 1x map (lowest alpha %.3f), %d near-opaque px, min alpha %.4f, HD art on %d of %d frames, %d uploads; vs exact: PSNR %.1f dB, p99 %d, max %d, brightness %+.1f%% (post %+.1f%%); %d outline px from the exact pass; %s light%s)\n",
            label, pass ? "PASS" : "FAIL", tier, hdWidth, hdHeight, holes, softEdges, softEdges > 0 ? minSoftAlpha : 1.0f, nearOpaque, minAlpha, art.buildHd, art.buildFrames, art.buildUploads, psnr, p99, maxError, drift, postDrift, outlinePixels,
            newLight ? "new" : "original", art.forceOriginal ? "; original art forced" : "");
        if (!cycles.text.empty()) {
            debugPrint("HD: map self-test '%s' cycle: %s%s\n", label, cycles.text.c_str(), newLight ? " (new light: not checked)" : cycles.pass ? " PASS"
                                                                                                                                                : " FAIL");
        }
        debugPrint("HD: map self-test '%s' seams: floor steps edge/inside HD %.1f/%.1f = %.2f, original %.1f/%.1f = %.2f (%lld edge pairs)\n",
            label, seams.hdEdge, seams.hdInside, seams.hdInside > 0.0 ? seams.hdEdge / seams.hdInside : 0.0,
            seams.exactEdge, seams.exactInside, seams.exactInside > 0.0 ? seams.exactEdge / seams.exactInside : 0.0, seams.edgePairs);
        if (holes > 0 && images) {
            debugPrint("HD:   first hole at (%d, %d), alpha %.3f, exact index %d; records there:\n", firstHoleX, firstHoleY, firstHoleAlpha,
                exact[static_cast<size_t>(firstHoleY / tier) * width + firstHoleX / tier]);
            logRecordsAt(firstHoleX / tier, firstHoleY / tier);
        }
        return pass ? 0 : 1;
    }

    void selfTestTicker()
    {
        if (gState == nullptr || getTicksSince(gState->lastSelfTest) < static_cast<unsigned int>(gState->selfTestIntervalMs)) {
            return;
        }
        gState->lastSelfTest = getTicks();
        if (visibleIsoWindow() == nullptr) {
            return;
        }
        char label[32];
        snprintf(label, sizeof(label), "auto-%03d", gState->selfTestRuns);
        mapLayerSelfTest(label);
    }

} // namespace

bool mapLayerRequested()
{
    int value = 1;
    configGetInt(&gGameConfig, "hd", "map_gpu", &value, 1);
    return value != 0;
}

bool mapLayerInit()
{
    mapLayerShutdown();
    if (!mapRendererInit()) {
        return false;
    }
    gState = new State();
    configGetInt(&gGameConfig, "hd_debug", "map_selftest_ms", &gState->selfTestIntervalMs, 0);
    configGetInt(&gGameConfig, "hd_debug", "hd_selftest_tier", &gState->selfTestTier, 0);

    char* captureDir = nullptr;
    configGetString(&gGameConfig, "hd", "capture_dir", &captureDir, "hd_captures");
    std::filesystem::path dir = std::filesystem::path(captureDir).parent_path() / "reports" / "map";
    std::error_code error;
    std::filesystem::create_directories(dir, error);
    gState->reportDir = dir.string();

    hdArtInit();
    mapLightingInit();
    mapPostInit();
    debugPrint("HD: GPU map layer on%s\n", hdArtActive() ? ", HD art" : "");
    return true;
}

void mapLayerStartSelfTests()
{
    // The presenter (and this layer) start before inputInit, which clears
    // the ticker list; hd::init calls this once the game is up.
    if (gState != nullptr && gState->selfTestIntervalMs > 0) {
        gState->lastSelfTest = getTicks();
        tickersAdd(selfTestTicker);
    }
}

void mapLayerShutdown()
{
    if (gState == nullptr) {
        return;
    }
    tickersRemove(selfTestTicker);
    debugPrint("HD: palette: %d HD frames shown through a fade, %d frames fell back to the exact pass (palette not a fade of color.pal)\n", gState->fadeFrames, gState->fallbackFrames);
    if (gState->selfTestRuns > 0) {
        debugPrint("HD: map self-test summary: %d runs, %d failed\n", gState->selfTestRuns, gState->selfTestFailures);
    }
    if (gState->gpuSamples > 0) {
        debugPrint("HD: map GPU time per redraw: mean %.3f ms, max %.3f ms over %d redraws (%d emissions, %d HD-only redraws)\n",
            gState->gpuMsSum / gState->gpuSamples, gState->gpuMsMax, gState->gpuSamples, gState->emissions, gState->hdRedraws);
    }
    delete gState;
    gState = nullptr;
    mapPostShutdown();
    mapLightingShutdown();
    hdArtShutdown();
    mapRendererShutdown();
}

namespace {

    // A player whose scale has no installed art pack sees the original art
    // without knowing why: say so once, in the game's message window, as soon
    // as it is up (PLAN.md R3). Nothing installed at all is said too.
    void noteMissingTier(int scale)
    {
        static bool done = false;
        if (done || !interfaceBarEnabled()) {
            return;
        }
        done = true;
        int tiers = installedArtTiers(&gGameConfig);
        if ((tiers & (1 << scale)) != 0) {
            return;
        }
        char text[160];
        if (tiers == 0) {
            snprintf(text, sizeof(text), "No HD art is installed: extract an art pack into the game folder.");
        } else {
            int best = (tiers & 16) != 0 ? 4 : ((tiers & 8) != 0 ? 3 : 2);
            snprintf(text, sizeof(text), "No HD art for scale %d is installed. Set scale=%d in fallout2.cfg [screen] for the HD art.", scale, best);
        }
        displayMonitorAddMessage(text);
        debugPrint("HD: %s\n", text);
    }

} // namespace

MapComposite mapLayerPrepare(int scale, bool selfTest)
{
    MapComposite composite;
    if (gState == nullptr || !gState->enabled) {
        return composite;
    }
    Window* iso = visibleIsoWindow();
    if (iso == nullptr) {
        return composite;
    }
    scale = std::max(1, std::min(scale, 4));
    if (!selfTest) {
        gState->lastScale = scale; // the HD self-test checks the scale the window shows
    }

    // HD art needs a scaled output; at 1:1 the exact pass is the picture.
    int hdTier = hdArtActive() && scale >= 2 ? scale : 0;
    if (hdTier != 0 && !selfTest) {
        noteMissingTier(scale);
    }
    bool arrived = hdTier != 0 && hdArtPoll();
    if (hdTier != 0) {
        if (!analyzePalette()) {
            gState->fallbackFrames++;
        } else if (gState->offset[0] != 0.0f || gState->offset[1] != 0.0f || gState->offset[2] != 0.0f || fabsf(gState->gain[0] - 1.0f) > 0.01f) {
            gState->fadeFrames++;
        }
    }

    if (drawMapDirty() || !gState->valid || iso->width != gState->width || iso->height != gState->height) {
        if (!refresh(iso, scale, hdTier)) {
            return composite;
        }
    } else if (arrived || mapRendererHdTier() != hdTier) {
        if (!rebuild(iso, scale, hdTier)) {
            return composite;
        }
    } else if (hdTier != 0 && (!gState->hdDrawn || (mapRendererHasCycle() && memcmp(gState->lastCycle, &_systemCmap[229 * 3], sizeof(gState->lastCycle)) != 0))) {
        // Palette cycling changes colors without a CE redraw.
        GpuTimer* timer = gpuTimerBegin();
        drawHd(scale);
        gpuTimerEnd(timer);
        gState->hdRedraws++;
    }

    bool useHd = hdTier != 0 && gState->hdDrawn && gState->paletteOk;
    if (!useHd && scale != 1 && scale != gState->drawnScale) {
        if (!mapRendererDraw(scale)) {
            return composite;
        }
        gState->drawnScale = scale;
    }

    buildMask(iso);
    if (!uploadMask()) {
        return composite;
    }

    composite.active = true;
    composite.isoX = iso->rect.left;
    composite.isoY = iso->rect.top;
    composite.isoWidth = iso->width;
    composite.isoHeight = iso->height;
    composite.scale = scale;
    composite.mask = gState->maskView.Get();
    composite.exact = mapRendererView(1);
    composite.scaled = useHd ? nullptr : mapRendererView(scale);
    composite.hd = useHd ? (gState->hdView != nullptr ? gState->hdView : mapRendererHdView(scale)) : nullptr;
    composite.grain = useHd ? mapPostGrain() : 0.0f;
    for (int c = 0; c < 3; c++) {
        composite.gain[c] = gState->gain[c];
        composite.offset[c] = gState->offset[c] / 63.75f;
    }
    for (int i = 0; i < 64; i++) {
        composite.gamma[i] = _currentGammaTable[i];
    }
    composite.tint = gState->tint;
    return composite;
}

namespace {

    float halfBitsToFloat(unsigned short h)
    {
        unsigned int exponent = (h >> 10) & 0x1F;
        unsigned int mantissa = h & 0x3FF;
        float value;
        if (exponent == 0) {
            value = mantissa / 1024.0f / 16384.0f;
        } else if (exponent == 31) {
            value = mantissa == 0 ? 65504.0f : 0.0f;
        } else {
            value = (1.0f + mantissa / 1024.0f) * powf(2.0f, static_cast<float>(exponent) - 15.0f);
        }
        return (h & 0x8000) != 0 ? -value : value;
    }

} // namespace

bool mapLayerSnapshot(int x, int y, int width, int height, std::vector<unsigned char>& rgba, std::vector<unsigned char>& owned, int* scaleOut)
{
    if (gState == nullptr || !gState->enabled || width <= 0 || height <= 0) {
        return false;
    }
    Window* iso = visibleIsoWindow();
    int scale = gState->lastScale;
    if (iso == nullptr || scale < 2 || scale > 4 || !hdArtActive() || x < 0 || y < 0 || x + width > iso->width || y + height > iso->height) {
        return false;
    }
    // The map as CE just drew it (the dialogue's view): emitted, built and
    // drawn now. CE moves the view back right after; that redraw marks the
    // map dirty again, so the next frame shows the map as before.
    if (!refresh(iso, scale, scale) || !gState->hdDrawn || gState->hdView == nullptr) {
        return false;
    }
    std::vector<unsigned char> exact;
    std::vector<unsigned char> tags;
    int exactWidth = 0;
    int exactHeight = 0;
    if (!mapRendererReadback(1, exact, &exactWidth, &exactHeight, &tags) || exactWidth != iso->width || exactHeight != iso->height) {
        return false;
    }

    ComPtr<ID3D11Resource> resource;
    gState->hdView->GetResource(&resource);
    ComPtr<ID3D11Texture2D> texture;
    if (!resource || FAILED(resource.As(&texture))) {
        return false;
    }
    D3D11_TEXTURE2D_DESC desc;
    texture->GetDesc(&desc);
    if (desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT || desc.Width < static_cast<UINT>((x + width) * scale) || desc.Height < static_cast<UINT>((y + height) * scale)) {
        debugPrint("HD: map snapshot: unexpected HD target (format %d, %ux%u)\n", static_cast<int>(desc.Format), desc.Width, desc.Height);
        return false;
    }
    D3D11_TEXTURE2D_DESC stagingDesc = desc;
    stagingDesc.Width = static_cast<UINT>(width * scale);
    stagingDesc.Height = static_cast<UINT>(height * scale);
    stagingDesc.MipLevels = 1;
    stagingDesc.ArraySize = 1;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.BindFlags = 0;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    stagingDesc.MiscFlags = 0;
    ComPtr<ID3D11Texture2D> staging;
    ID3D11DeviceContext* context = presenterContext();
    if (FAILED(presenterDevice()->CreateTexture2D(&stagingDesc, nullptr, &staging))) {
        return false;
    }
    D3D11_BOX box = { static_cast<UINT>(x * scale), static_cast<UINT>(y * scale), 0, static_cast<UINT>((x + width) * scale), static_cast<UINT>((y + height) * scale), 1 };
    context->CopySubresourceRegion(staging.Get(), 0, 0, 0, 0, texture.Get(), 0, &box);
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) {
        return false;
    }

    owned.assign(static_cast<size_t>(width) * height, 0);
    int ownedCount = 0;
    for (int py = 0; py < height; py++) {
        for (int px = 0; px < width; px++) {
            size_t q = static_cast<size_t>(y + py) * iso->width + x + px;
            if (exact[q] == iso->buffer[q] && (tags.empty() || tags[q] == 0)) {
                owned[static_cast<size_t>(py) * width + px] = 1;
                ownedCount++;
            }
        }
    }
    int hdWidth = width * scale;
    int hdHeight = height * scale;
    rgba.assign(static_cast<size_t>(hdWidth) * hdHeight * 4, 0);
    for (int ty = 0; ty < hdHeight; ty++) {
        const unsigned short* row = reinterpret_cast<const unsigned short*>(static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * ty);
        for (int tx = 0; tx < hdWidth; tx++) {
            if (owned[static_cast<size_t>(ty / scale) * width + tx / scale] == 0) {
                continue; // alpha 0: CE's pixel
            }
            unsigned char* out = &rgba[(static_cast<size_t>(ty) * hdWidth + tx) * 4];
            for (int c = 0; c < 4; c++) {
                float v = std::min(std::max(halfBitsToFloat(row[tx * 4 + c]), 0.0f), 1.0f);
                out[c] = static_cast<unsigned char>(v * 255.0f + 0.5f);
            }
        }
    }
    context->Unmap(staging.Get(), 0);
    *scaleOut = scale;
    debugPrint("HD: map snapshot %dx%d at %d,%d (scale %d): %d of %d px match CE's\n", width, height, x, y, scale, ownedCount, width * height);
    return true;
}

const char* mapLayerToggleHd()
{
    if (gState == nullptr) {
        return "unavailable";
    }
    if (hdArtStats().pages == 0) {
        return "unavailable ([hd] hd_art=0)";
    }
    return hdArtToggle() ? "HD art" : "original art";
}

const char* mapLayerCycleMode()
{
    if (gState == nullptr) {
        return "unavailable";
    }
    if (gState->enabled && !gState->tint) {
        gState->tint = true;
        return "tinted";
    }
    if (gState->enabled) {
        gState->enabled = false;
        gState->tint = false;
        return "off";
    }
    gState->enabled = true;
    return "on";
}

int mapLayerSelfTest(const char* label)
{
    if (gState == nullptr || !gState->enabled) {
        return -1;
    }
    Window* iso = visibleIsoWindow();
    if (iso == nullptr) {
        debugPrint("HD: map self-test '%s': skipped, map window hidden\n", label);
        return -1;
    }

    gState->selfTestRuns++;
    int failures = 0;

    // Informational: how much of the map currently falls back to CE's pixels.
    int live = liveMismatch(iso, label);

    // (a) 1x GPU map vs the map window buffer after a full CPU refresh.
    // CE's refresh skips the clear inside the map's visible area (the EDG
    // file), so where no art covers a pixel the window keeps what an earlier
    // frame left there, while the GPU map shows its clear (the live fallback
    // shows CE's pixels there). So the check refreshes CE over a cleared
    // window, as a capture does, counts the leftovers, and puts them back
    // afterwards (P7 fix B).
    tileWindowRefresh();
    size_t isoPixels = static_cast<size_t>(iso->width) * iso->height;
    std::vector<unsigned char> leftover(iso->buffer, iso->buffer + isoPixels);
    memset(iso->buffer, 0, isoPixels);
    tileWindowRefresh();
    int leftovers = 0;
    for (size_t i = 0; i < isoPixels; i++) {
        leftovers += leftover[i] != iso->buffer[i] ? 1 : 0;
    }
    auto restoreLeftovers = [&]() {
        if (leftovers > 0) {
            memcpy(iso->buffer, leftover.data(), isoPixels);
            windowRefresh(iso->id);
        }
    };
    if (!refresh(iso, 1)) {
        restoreLeftovers();
        if (!drawListComplete()) {
            // Tiles disabled (map loading, movie): CE doesn't draw the map either.
            gState->selfTestRuns--;
            debugPrint("HD: map self-test '%s': skipped, map not drawable right now\n", label);
            return -1;
        }
        debugPrint("HD: map self-test '%s': FAIL, map not rendered\n", label);
        gState->selfTestFailures++;
        gState->lastSelfTestResult = std::string(label) + ": not rendered";
        return 1;
    }
    const MapRenderStats& stats = mapRendererStats();
    std::vector<unsigned char> exact;
    int width = 0;
    int height = 0;
    int differing = -1;
    if (mapRendererReadback(1, exact, &width, &height) && width == iso->width && height == iso->height) {
        differing = 0;
        int firstX = -1;
        int firstY = -1;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                if (exact[static_cast<size_t>(y) * width + x] != iso->buffer[static_cast<size_t>(y) * iso->width + x]) {
                    if (differing == 0) {
                        firstX = x;
                        firstY = y;
                    }
                    differing++;
                }
            }
        }
        if (differing == 0) {
            const int* ops = stats.opCounts;
            debugPrint("HD: map self-test '%s' 1x: PASS (%dx%d, 0 px differ; %d px of CE leftovers where no art draws, compared cleared; %d records, %d sprites in %d raster batches; ops fill %d lit %d floor %d iface %d text %d trans %d egg %d outline %d; emit %.2f ms, build %.2f ms, draw %.2f ms)\n",
                label, width, height, leftovers, stats.records, stats.sprites, stats.rasterBatches, ops[0], ops[1], ops[2], ops[3], ops[4], ops[5], ops[6], ops[7], gState->emitMs, gState->buildMs, gState->drawMs);
        } else {
            std::string path = gState->reportDir + "\\" + label + "-1x.png";
            writeComparisonPng(path, iso->buffer, exact, width, height);
            debugPrint("HD: map self-test '%s' 1x: FAIL, %d px differ, first at (%d, %d); %d px of CE leftovers where no art draws, compared cleared; %s\n", label, differing, firstX, firstY, leftovers, path.c_str());
            logRecordsAt(firstX, firstY);
        }
    } else {
        debugPrint("HD: map self-test '%s' 1x: FAIL, readback\n", label);
    }
    restoreLeftovers();
    if (differing != 0) {
        failures++;
    }

    // (b) 2x, 3x, 4x are the 1x image upscaled nearest.
    for (int scale = 2; scale <= 4; scale++) {
        std::vector<unsigned char> scaled;
        int scaledWidth = 0;
        int scaledHeight = 0;
        if (!mapRendererDraw(scale) || !mapRendererReadback(scale, scaled, &scaledWidth, &scaledHeight) || scaledWidth != width * scale || scaledHeight != height * scale) {
            debugPrint("HD: map self-test '%s' %dx: FAIL, not rendered\n", label, scale);
            failures++;
            continue;
        }
        int bad = 0;
        int firstX = -1;
        int firstY = -1;
        for (int y = 0; y < scaledHeight; y++) {
            for (int x = 0; x < scaledWidth; x++) {
                if (scaled[static_cast<size_t>(y) * scaledWidth + x] != exact[static_cast<size_t>(y / scale) * width + x / scale]) {
                    if (bad++ == 0) {
                        firstX = x;
                        firstY = y;
                    }
                }
            }
        }
        if (bad == 0) {
            debugPrint("HD: map self-test '%s' %dx: PASS (%dx%d = 1x upscaled nearest)\n", label, scale, scaledWidth, scaledHeight);
        } else {
            debugPrint("HD: map self-test '%s' %dx: FAIL, %d px differ from 1x upscaled nearest, first at (%d, %d)\n", label, scale, bad, firstX, firstY);
            failures++;
            if (gState->liveImages < 6) {
                // 1x | the scaled image's top-left sample of each 1x pixel | difference.
                gState->liveImages++;
                std::vector<unsigned char> sampled(exact.size());
                for (int y = 0; y < height; y++) {
                    for (int x = 0; x < width; x++) {
                        sampled[static_cast<size_t>(y) * width + x] = scaled[static_cast<size_t>(y * scale) * scaledWidth + x * scale];
                    }
                }
                std::string path = gState->reportDir + "\\" + label + "-" + std::to_string(scale) + "x.png";
                writeComparisonPng(path, exact.data(), sampled, width, height);
                logRecordsAt(firstX / scale, firstY / scale);
            }
        }
    }
    gState->drawnScale = 0; // the presenter redraws its own scale

    // (c) The composited frame at 1:1 vs the SDL path.
    int composite = presenterSelfTest(gSdlSurface, label);
    if (composite != 0) {
        failures++;
    }

    // (d) The mask vs the window manager's own idea of what shows the map.
    int owned = 0;
    int ownership = ownershipMismatch(iso, &owned);
    if (ownership == 0) {
        debugPrint("HD: map self-test '%s' mask: PASS (%d screen px show the map; live fallback before the refresh: %d px)\n", label, owned, live);
    } else {
        debugPrint("HD: map self-test '%s' mask: FAIL, %d px differ from the window manager's (%d px show the map)\n", label, ownership, owned);
        failures++;
    }

    // (e) The HD pass, at the presenter's scale (2x when it runs 1:1).
    if (hdArtActive()) {
        int tier = gState->selfTestTier >= 2 && gState->selfTestTier <= 4 ? gState->selfTestTier : std::max(2, gState->lastScale);
        failures += hdSelfTest(iso, label, tier);
    }

    if (failures > 0) {
        gState->selfTestFailures++;
    }
    char result[128];
    snprintf(result, sizeof(result), "%s: %s (1x %d px, live fallback %d px)", label, failures == 0 ? "PASS" : "FAIL", differing, live);
    gState->lastSelfTestResult = result;
    return failures;
}

void mapLayerDrawOverlay()
{
    ImGui::Separator();
    if (gState == nullptr) {
        ImGui::Text("map gpu   off ([hd] map_gpu=1 turns it on)");
        return;
    }
    const MapRenderStats& stats = mapRendererStats();
    ImGui::Text("map gpu   %s, %s (Ctrl+F9: on/tinted/off)", gState->enabled ? (gState->tint ? "tinted" : "on") : "off", gState->valid ? "drawn" : "not drawn");
    ImGui::Text("draws     %d records -> %d sprites, %d batches, %d compute, %d outlines", stats.records, stats.sprites, stats.rasterBatches, stats.computeOps, stats.outlines);
    ImGui::Text("cost      emit %.2f ms, build %.2f ms, draw %.2f ms (%d emissions)", gState->emitMs, gState->buildMs, gState->drawMs, gState->emissions);
    ImGui::Text("gpu       %.3f ms last redraw, mean %.3f, max %.3f", gState->gpuMs, gState->gpuSamples > 0 ? gState->gpuMsSum / gState->gpuSamples : 0.0, gState->gpuMsMax);
    ImGui::Text("atlas     %.0f%% rows used, %d uploads, %d resets", stats.atlasUsed * 100.0f, stats.atlasUploads, stats.atlasResets);
    ImGui::Text("hd pass   tier %d, %s, palette %s (gain %.2f %.2f %.2f), draw %.2f ms, %d cycle redraws", mapRendererHdTier(), gState->hdDrawn ? "drawn" : "not drawn",
        gState->paletteOk ? "ok" : "not color.pal", gState->gain[0], gState->gain[1], gState->gain[2], gState->hdDrawMs, gState->hdRedraws);
    hdArtDrawOverlay();
    mapLightingDrawOverlay();
    mapPostDrawOverlay();
    ImGui::Text("selftest  %s (Ctrl+F10 runs it)", gState->lastSelfTestResult.c_str());
}

} // namespace fallout::hd

#endif // _WIN32
