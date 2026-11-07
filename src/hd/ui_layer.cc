#include "hd/ui_layer.h"

#ifdef _WIN32

#include <math.h>
#include <stdint.h>
#include <string.h>

#include <algorithm>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include <wrl/client.h>

#include "color.h"
#include "config.h"
#include "db.h"
#include "debug.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "hd/hd_art.h"
#include "hd/map_layer.h"
#include "hd/palette_fit.h"
#include "hd/ui_recorder.h"
#include "hd/ui_text.h"
#include "imgui.h"
#include "map.h"
#include "svga.h"
#include "window_manager.h"

using Microsoft::WRL::ComPtr;

namespace fallout::hd {

namespace {

    // One op as a quad in a window's HD target: the HD frame's texels at
    // the target's scale, so every target texel is one pool texel (Load,
    // no filtering). Transparent copies blend over what the window had
    // (premultiplied); plain copies replace it, transparent HD texels
    // becoming CE's index 0, black.
    //
    // A text op (P7b.5) is two passes: the backdrop puts the pixels CE had
    // under the text (palette colors, nearest) wherever no HD op below is
    // in the target yet, so HD glyphs never blend over CE's 1x text or
    // over an older text's HD glyphs; then each glyph's HD coverage in the
    // text color (the shadow's first, in black at +1,+1 pixel).
    constexpr char kShaderSource[] = R"hlsl(
struct UiSprite {
    int4 dest; // x, y, width, height in target texels
    int4 src;  // art: pool x, y, page, flags | dim << 8; glyph: atlas x, y, slice, palette index; backdrop: unused
};
StructuredBuffer<UiSprite> gSprites : register(t0);
Texture2DArray<float4> gPool : register(t1);
Texture2D<uint> gBackdrop : register(t2); // index | 0x100 = keep the target's texel
Texture2DArray<float> gFonts : register(t3); // glyph coverage, a slice per font

cbuffer Target : register(b0) {
    float2 gTargetSize;
    uint gBase; // this draw's first sprite
    uint gScale;
};

cbuffer Palette : register(b1) {
    float4 gPalette[256]; // color.pal, 8-bit / 255
};

#define UI_TRANS 1
#define UI_HARD 2

struct VSOut {
    float4 pos : SV_Position;
    nointerpolation uint sprite : SPRITE;
};

VSOut vsSprite(uint vertex : SV_VertexID, uint instance : SV_InstanceID)
{
    UiSprite s = gSprites[gBase + instance];
    float2 corner = float2(vertex & 1, vertex >> 1);
    float2 p = s.dest.xy + corner * s.dest.zw;
    VSOut o;
    o.pos = float4(p / gTargetSize * float2(2, -2) + float2(-1, 1), 0, 1);
    o.sprite = gBase + instance;
    return o;
}

float4 psSprite(VSOut i) : SV_Target
{
    UiSprite s = gSprites[i.sprite];
    int2 t = int2(i.pos.xy) - s.dest.xy;
    float4 c = gPool.Load(int4(s.src.xy + t, s.src.z, 0));
    float a = (s.src.w & UI_HARD) != 0 ? (c.a >= 0.5 ? 1.0 : 0.0) : c.a;
    // Dimmed like CE's intensityColorTable column (P7b.4, the world map's fog).
    uint dim = (uint(s.src.w) >> 8) & 0xFF;
    if (dim != 0) {
        c.rgb *= dim / 128.0;
    }
    if ((s.src.w & UI_TRANS) != 0) {
        return float4(c.rgb * a, a);
    }
    return float4(c.rgb * a, 1.0);
}

float4 psBackdrop(VSOut i) : SV_Target
{
    UiSprite s = gSprites[i.sprite];
    uint2 t = uint2(int2(i.pos.xy) - s.dest.xy) / gScale;
    uint v = gBackdrop.Load(int3(t, 0));
    if ((v & 0x100) != 0) {
        discard;
    }
    return float4(gPalette[v & 0xFF].rgb, 1.0);
}

float4 psGlyph(VSOut i) : SV_Target
{
    UiSprite s = gSprites[i.sprite];
    int2 t = int2(i.pos.xy) - s.dest.xy;
    float c = gFonts.Load(int4(s.src.xy + t, s.src.z, 0));
    return float4(gPalette[s.src.w].rgb * c, c);
}
)hlsl";

    struct Sprite {
        int dest[4];
        int src[4];
    };
    static_assert(sizeof(Sprite) == 32, "Sprite must match the HLSL UiSprite");

    constexpr int kSpriteTrans = 1;
    constexpr int kSpriteHard = 2;
    constexpr int kMaxSlot = 127; // cells keep the slot in 7 bits

    struct TargetConstants {
        float size[2];
        unsigned int base;
        unsigned int scale;
    };

    // The passes of one window draw, in op order (P7b.5: text ops add a
    // backdrop and a glyph pass each).
    enum PassKind {
        PassArt,
        PassBackdrop,
        PassGlyph,
        PassSnapshot, // a map snapshot op: copied into the target, in draw order
    };

    // An HD snapshot of a rect of the GPU map (P7b.4, the dialogue's map
    // view), RGBA8 at `scale`, for the map snapshot ops that name its id.
    struct Snapshot {
        int scale = 0;
        int width = 0; // 1x pixels
        int height = 0;
        ComPtr<ID3D11Texture2D> texture;
    };
    constexpr size_t kMaxSnapshots = 4;

    struct Pass {
        PassKind kind;
        int first; // into the window's sprites
        int count;
        const UiOp* op; // PassBackdrop: the text op
        unsigned int serial; // ... and its serial
    };

    struct WindowTarget {
        unsigned int generation = 0; // the recorder's window state it belongs to
        int width = 0; // window pixels
        int height = 0;
        int scale = 0;
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11RenderTargetView> rtv;
        ComPtr<ID3D11ShaderResourceView> srv;
        unsigned int drawn = 0; // ops up to this serial have been drawn (or tried)
        bool full = true; // clear and draw all live ops again
        bool pending = false; // an op found its pack still loading, or the upload limit hit
    };

    struct State {
        bool enabled = true; // Ctrl+F9 "off" switches it off with the GPU map
        bool tint = false;
        bool hardAlpha = false; // [hd] ui_hard_alpha: alpha thresholded at 1/2
        bool wasActive = false;
        int scale = 0;

        ComPtr<ID3D11VertexShader> vertexShader;
        ComPtr<ID3D11PixelShader> pixelShader;
        ComPtr<ID3D11PixelShader> backdropShader;
        ComPtr<ID3D11PixelShader> glyphShader;
        ComPtr<ID3D11Buffer> palette; // color.pal for the text passes
        ComPtr<ID3D11Texture2D> backdrop; // R16_UINT, one text rect at a time
        ComPtr<ID3D11ShaderResourceView> backdropView;
        int backdropWidth = 0;
        int backdropHeight = 0;
        bool text = false; // HD glyphs loaded for this scale (hd/ui_text.h)
        ComPtr<ID3D11Buffer> sprites;
        ComPtr<ID3D11ShaderResourceView> spritesView;
        int spriteCapacity = 0;
        ComPtr<ID3D11Buffer> constants;
        ComPtr<ID3D11BlendState> blend;
        ComPtr<ID3D11RasterizerState> raster;

        std::unordered_map<int, WindowTarget> targets; // by window id
        std::map<int, Snapshot> snapshots; // by id, the newest kMaxSnapshots
        int nextSnapshot = 1;
        long long snapshotsMade = 0;

        int screenWidth = 0;
        int screenHeight = 0;
        std::vector<unsigned short> cells;
        ComPtr<ID3D11Texture2D> cellsTexture;
        ComPtr<ID3D11ShaderResourceView> cellsView;
        std::vector<intptr_t> layout; // what the window stack looked like at the last full rebuild
        bool cellsFull = true;

        unsigned char cmapSnapshot[768] = {}; // color.pal as colorPaletteLoad reads it (takeColorPal)
        bool cmapTaken = false;
        const char* lastState = "not run yet"; // the last frame's composite: shown, or why not

        // Stats.
        long long frames = 0;
        long long paletteFallbacks = 0;
        long long opsTried = 0;
        long long opsHd = 0;
        long long textTried = 0;
        long long textHd = 0;
        long long fullDraws = 0;
        int frameOps = 0; // last frame
        int frameHd = 0;
        int frameText = 0;
        int frameTextHd = 0;
        int frameWindows = 0;
        long long cellsRebuilds = 0;
        long long cellsPixels = 0;
        double cellsMs = 0.0;
    };

    State* gState = nullptr;

    bool failed(HRESULT hr, const char* what)
    {
        if (FAILED(hr)) {
            debugPrint("HD: ui layer: %s failed (0x%08x)\n", what, static_cast<unsigned>(hr));
            return true;
        }
        return false;
    }

    double nowMs()
    {
        LARGE_INTEGER counter;
        LARGE_INTEGER frequency;
        QueryPerformanceCounter(&counter);
        QueryPerformanceFrequency(&frequency);
        return counter.QuadPart * 1000.0 / frequency.QuadPart;
    }

    bool ensureSprites(int count)
    {
        if (count <= gState->spriteCapacity) {
            return true;
        }
        int capacity = std::max(count, std::max(256, gState->spriteCapacity * 2));
        gState->sprites.Reset();
        gState->spritesView.Reset();
        gState->spriteCapacity = 0;
        D3D11_BUFFER_DESC desc = {};
        desc.ByteWidth = static_cast<UINT>(capacity * sizeof(Sprite));
        desc.Usage = D3D11_USAGE_DYNAMIC;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        desc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        desc.StructureByteStride = sizeof(Sprite);
        D3D11_SHADER_RESOURCE_VIEW_DESC view = {};
        view.Format = DXGI_FORMAT_UNKNOWN;
        view.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
        view.Buffer.FirstElement = 0;
        view.Buffer.NumElements = static_cast<UINT>(capacity);
        ID3D11Device* device = presenterDevice();
        if (failed(device->CreateBuffer(&desc, nullptr, &gState->sprites), "sprite buffer")
            || failed(device->CreateShaderResourceView(gState->sprites.Get(), &view, &gState->spritesView), "sprite view")) {
            gState->sprites.Reset();
            return false;
        }
        gState->spriteCapacity = capacity;
        return true;
    }

    // The text backdrop texture holds at least `width` x `height`.
    bool ensureBackdrop(int width, int height)
    {
        if (gState->backdrop && width <= gState->backdropWidth && height <= gState->backdropHeight) {
            return true;
        }
        int w = std::max(width, std::max(gState->backdropWidth, 1024));
        int h = std::max(height, std::max(gState->backdropHeight, 32));
        gState->backdrop.Reset();
        gState->backdropView.Reset();
        gState->backdropWidth = 0;
        gState->backdropHeight = 0;
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = static_cast<UINT>(w);
        desc.Height = static_cast<UINT>(h);
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R16_UINT;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        ID3D11Device* device = presenterDevice();
        if (failed(device->CreateTexture2D(&desc, nullptr, &gState->backdrop), "text backdrop")
            || failed(device->CreateShaderResourceView(gState->backdrop.Get(), nullptr, &gState->backdropView), "text backdrop view")) {
            gState->backdrop.Reset();
            return false;
        }
        gState->backdropWidth = w;
        gState->backdropHeight = h;
        return true;
    }

    // The HD colors' reference: color.pal as colorPaletteLoad reads it
    // (an entry with a channel above 63 is a placeholder and becomes
    // black). Read from the file, not taken from _cmap on the first HD UI
    // frame: when the game starts through the main menu, that frame can
    // come before CE has its palette, and the UI then never matched (P7b,
    // the second UI walk). _cmap is the fallback if the file can't be read.
    void takeColorPal()
    {
        gState->cmapTaken = true;
        File* stream = fileOpen("color.pal", "rb");
        bool ok = stream != nullptr && fileRead(gState->cmapSnapshot, 1, sizeof(gState->cmapSnapshot), stream) == sizeof(gState->cmapSnapshot);
        if (stream != nullptr) {
            fileClose(stream);
        }
        if (!ok) {
            memcpy(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot));
            debugPrint("HD: ui layer: can't read color.pal, using the palette CE has now\n");
            return;
        }
        for (int i = 0; i < 256; i++) {
            unsigned char* c = gState->cmapSnapshot + i * 3;
            if (c[0] > 63 || c[1] > 63 || c[2] > 63) {
                c[0] = 0;
                c[1] = 0;
                c[2] = 0;
            }
        }
        if (memcmp(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot)) != 0) {
            debugPrint("HD: ui layer: CE's palette isn't color.pal yet (HD UI waits for it)\n");
        }
    }

    // color.pal (the snapshot) for the text passes; placeholders (> 63)
    // count as black, as fo2hd.frm.load_palette has index 0.
    void updatePalette()
    {
        float colors[256][4];
        for (int i = 0; i < 256; i++) {
            for (int c = 0; c < 3; c++) {
                unsigned char v = gState->cmapSnapshot[i * 3 + c];
                colors[i][c] = v <= 63 ? (v << 2) / 255.0f : 0.0f;
            }
            colors[i][3] = 1.0f;
        }
        presenterContext()->UpdateSubresource(gState->palette.Get(), 0, nullptr, colors, 0, 0);
    }

    // The window's target exists at this size and scale (else it's made,
    // to be drawn in full).
    bool ensureTarget(WindowTarget& t, const UiWindow& s, int scale)
    {
        if (t.texture && t.generation == s.generation && t.width == s.width && t.height == s.height && t.scale == scale) {
            return true;
        }
        t = WindowTarget();
        t.generation = s.generation;
        t.width = s.width;
        t.height = s.height;
        t.scale = scale;
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = static_cast<UINT>(s.width * scale);
        desc.Height = static_cast<UINT>(s.height * scale);
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        ID3D11Device* device = presenterDevice();
        if (failed(device->CreateTexture2D(&desc, nullptr, &t.texture), "window target")
            || failed(device->CreateRenderTargetView(t.texture.Get(), nullptr, &t.rtv), "window target RTV")
            || failed(device->CreateShaderResourceView(t.texture.Get(), nullptr, &t.srv), "window target SRV")) {
            t.texture.Reset();
            t.rtv.Reset();
            t.srv.Reset();
            return false;
        }
        return true;
    }

    // Draws the window's ops that aren't in its target yet (all of them
    // after a clear when `full`). Returns true if the ops' HD state changed
    // for pixels outside the recorder's dirty rect (a full draw).
    bool drawWindow(WindowTarget& t, UiWindow& s, int scale)
    {
        ID3D11DeviceContext* context = presenterContext();
        bool full = t.full;
        auto begin = s.ops.begin();
        if (full) {
            const float clear[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
            context->ClearRenderTargetView(t.rtv.Get(), clear);
            t.full = false;
            t.pending = false;
            gState->fullDraws++;
        } else {
            begin = s.ops.upper_bound(t.drawn);
        }
        t.drawn = s.nextSerial - 1;

        std::vector<Sprite> sprites;
        std::vector<Pass> passes;
        std::vector<HdFrameTile> tiles;
        auto addPass = [&](PassKind kind, const UiOp* op, unsigned int serial) {
            if (kind != PassBackdrop && !passes.empty() && passes.back().kind == kind && passes.back().first + passes.back().count == static_cast<int>(sprites.size())) {
                return; // continues the previous pass
            }
            passes.push_back({ kind, static_cast<int>(sprites.size()), 0, op, serial });
        };
        for (auto it = begin; it != s.ops.end(); ++it) {
            UiOp& op = it->second;
            if (op.font >= 0) {
                // Text (P7b.5): HD only if every glyph is in the atlas.
                gState->textTried++;
                gState->frameText++;
                op.hd = gState->text;
                for (size_t g = 0; g < op.glyphs.size() && op.hd; g++) {
                    int x;
                    int y;
                    int slice;
                    op.hd = uiTextGlyph(op.font, op.glyphs[g].ch, &x, &y, &slice);
                }
                if (!op.hd) {
                    continue;
                }
                gState->textHd++;
                gState->frameTextHd++;
                addPass(PassBackdrop, &op, it->first);
                sprites.push_back({ { op.destX * scale, op.destY * scale, op.width * scale, op.height * scale }, { 0, 0, 0, 0 } });
                passes.back().count++;
                addPass(PassGlyph, nullptr, 0);
                for (int pass = op.shadow ? 0 : 1; pass < 2; pass++) {
                    int shift = pass == 0 ? scale : 0;
                    int color = pass == 0 ? op.shadowColor : op.color;
                    for (const UiGlyphPlace& g : op.glyphs) {
                        int x;
                        int y;
                        int slice;
                        uiTextGlyph(op.font, g.ch, &x, &y, &slice);
                        sprites.push_back({ { (op.destX + g.x) * scale + shift, (op.destY + g.y) * scale + shift, g.width * scale, g.height * scale }, { x, y, slice, color } });
                        passes.back().count++;
                    }
                }
                continue;
            }
            if (op.snapshot != 0) {
                // The dialogue's map view (P7b.4): copied in, in draw order.
                auto snap = gState->snapshots.find(op.snapshot);
                op.hd = snap != gState->snapshots.end() && snap->second.scale == scale
                    && op.srcX + op.width <= snap->second.width && op.srcY + op.height <= snap->second.height
                    && op.destX + op.width <= t.width && op.destY + op.height <= t.height;
                if (op.hd) {
                    passes.push_back({ PassSnapshot, static_cast<int>(sprites.size()), 0, &op, it->first });
                }
                continue;
            }
            // In tiles: backgrounds wider than a pool page at this scale (the
            // 800-wide interface bar at 3x) come in several.
            op.hd = hdArtFrameTiles(op.fid, op.fid != -1 ? nullptr : op.path.c_str(), op.rotation, op.frame, op.hash, op.frameWidth, op.frameHeight,
                        op.srcX, op.srcY, op.width, op.height, &tiles)
                && !tiles.empty();
            gState->opsTried++;
            gState->frameOps++;
            if (!op.hd) {
                // Over-approximated: any pack in flight keeps the window
                // pending, and the next arrival draws it again in full.
                if (hdArtLoading() || hdArtStats().buildDeferred > 0) {
                    t.pending = true;
                }
                continue;
            }
            gState->opsHd++;
            gState->frameHd++;
            int flags = (op.trans ? kSpriteTrans : 0) | (gState->hardAlpha || tiles[0].frame.hardAlpha ? kSpriteHard : 0) | (op.dim << 8);
            addPass(PassArt, nullptr, 0);
            for (const HdFrameTile& tile : tiles) {
                // The op's part of the frame within this tile.
                int x0 = std::max(op.srcX, tile.x);
                int y0 = std::max(op.srcY, tile.y);
                int x1 = std::min(op.srcX + op.width, tile.x + tile.width);
                int y1 = std::min(op.srcY + op.height, tile.y + tile.height);
                if (x0 >= x1 || y0 >= y1) {
                    continue;
                }
                sprites.push_back({ { (op.destX + x0 - op.srcX) * scale, (op.destY + y0 - op.srcY) * scale, (x1 - x0) * scale, (y1 - y0) * scale },
                    { tile.frame.x + (x0 - tile.x) * scale, tile.frame.y + (y0 - tile.y) * scale, tile.frame.slice, flags } });
                passes.back().count++;
            }
        }
        bool snapshots = std::any_of(passes.begin(), passes.end(), [](const Pass& pass) { return pass.kind == PassSnapshot; });
        if (sprites.empty() && !snapshots) {
            return full;
        }
        if (!sprites.empty()) {
            if (!ensureSprites(static_cast<int>(sprites.size()))) {
                return full;
            }
            D3D11_MAPPED_SUBRESOURCE mapped;
            if (failed(context->Map(gState->sprites.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped), "sprite map")) {
                return full;
            }
            memcpy(mapped.pData, sprites.data(), sprites.size() * sizeof(Sprite));
            context->Unmap(gState->sprites.Get(), 0);
        }

        ID3D11RenderTargetView* rtv = t.rtv.Get();
        context->OMSetRenderTargets(1, &rtv, nullptr);
        context->OMSetBlendState(gState->blend.Get(), nullptr, 0xFFFFFFFF);
        context->RSSetState(gState->raster.Get());
        D3D11_VIEWPORT viewport = { 0.0f, 0.0f, static_cast<float>(t.width * scale), static_cast<float>(t.height * scale), 0.0f, 1.0f };
        context->RSSetViewports(1, &viewport);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
        context->IASetInputLayout(nullptr);
        context->VSSetShader(gState->vertexShader.Get(), nullptr, 0);
        ID3D11Buffer* buffers[2] = { gState->constants.Get(), gState->palette.Get() };
        context->VSSetConstantBuffers(0, 1, buffers);
        context->PSSetConstantBuffers(0, 2, buffers);
        int backdropWidth = 0;
        int backdropHeight = 0;
        for (const Pass& pass : passes) {
            if (pass.kind == PassBackdrop) {
                backdropWidth = std::max(backdropWidth, pass.op->width);
                backdropHeight = std::max(backdropHeight, pass.op->height);
            }
        }
        bool backdropReady = backdropWidth == 0 || ensureBackdrop(backdropWidth, backdropHeight);
        ID3D11ShaderResourceView* spritesView = gState->spritesView.Get();
        context->VSSetShaderResources(0, 1, &spritesView);
        ID3D11ShaderResourceView* views[4] = { spritesView, hdArtPoolView(), gState->backdropView.Get(), uiTextAtlas() };
        context->PSSetShaderResources(0, 4, views);
        std::vector<unsigned short> backdrop;
        for (const Pass& pass : passes) {
            if (pass.kind == PassSnapshot) {
                // Replaces the rect's texels (alpha 0 where CE's pixel shows);
                // the ops drawn before it own nothing under it.
                const UiOp& op = *pass.op;
                const Snapshot& snap = gState->snapshots.at(op.snapshot);
                D3D11_BOX box = { static_cast<UINT>(op.srcX * scale), static_cast<UINT>(op.srcY * scale), 0,
                    static_cast<UINT>((op.srcX + op.width) * scale), static_cast<UINT>((op.srcY + op.height) * scale), 1 };
                context->CopySubresourceRegion(t.texture.Get(), 0, static_cast<UINT>(op.destX * scale), static_cast<UINT>(op.destY * scale), 0, snap.texture.Get(), 0, &box);
                continue;
            }
            if (pass.count == 0) {
                continue;
            }
            if (pass.kind == PassBackdrop) {
                // What CE had under the text; 0x100 where the op that owned
                // the pixel then is HD and so already in the target.
                const UiOp& op = *pass.op;
                if (!backdropReady) {
                    continue;
                }
                backdrop.resize(op.before.size());
                unsigned int lastOwner = 0;
                bool lastHd = false;
                for (size_t i = 0; i < backdrop.size(); i++) {
                    unsigned int owner = op.beneath[i];
                    if (owner != lastOwner) {
                        lastOwner = owner;
                        auto below = s.ops.find(owner);
                        lastHd = owner != 0 && owner < pass.serial && below != s.ops.end() && below->second.hd;
                    }
                    // Through a transparent copy (button faces) the pixels that were 0
                    // and that the text didn't touch were never copied: leave them.
                    bool skipped = false;
                    if (op.transCopy && op.before[i] == 0) {
                        size_t x = i % op.width;
                        size_t y = i / op.width;
                        skipped = s.owner[(op.destY + y) * s.width + op.destX + x] != pass.serial;
                    }
                    backdrop[i] = static_cast<unsigned short>(op.before[i] | (lastHd || skipped ? 0x100 : 0));
                }
                D3D11_BOX box = { 0, 0, 0, static_cast<UINT>(op.width), static_cast<UINT>(op.height), 1 };
                context->UpdateSubresource(gState->backdrop.Get(), 0, &box, backdrop.data(), static_cast<UINT>(op.width * sizeof(unsigned short)), 0);
            }
            TargetConstants constants = { { static_cast<float>(t.width * scale), static_cast<float>(t.height * scale) }, static_cast<unsigned int>(pass.first), static_cast<unsigned int>(scale) };
            context->UpdateSubresource(gState->constants.Get(), 0, nullptr, &constants, 0, 0);
            ID3D11PixelShader* shader = pass.kind == PassArt ? gState->pixelShader.Get() : pass.kind == PassBackdrop ? gState->backdropShader.Get()
                                                                                                                     : gState->glyphShader.Get();
            context->PSSetShader(shader, nullptr, 0);
            context->DrawInstanced(4, static_cast<UINT>(pass.count), 0, 0);
        }
        return full;
    }

    void unbindDrawState()
    {
        ID3D11DeviceContext* context = presenterContext();
        ID3D11ShaderResourceView* nullViews[4] = {};
        context->VSSetShaderResources(0, 1, nullViews);
        context->PSSetShaderResources(0, 4, nullViews);
        context->OMSetRenderTargets(0, nullptr, nullptr);
        context->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        context->RSSetState(nullptr);
    }

    // The window that shows at screen pixel (x, y) as paintCells sees it:
    // hidden windows don't, transparent ones only where their pixel isn't 0
    // (windowGetAtPoint counts both).
    int topWindowAt(int x, int y)
    {
        for (int index = windowGetCount() - 1; index >= 0; index--) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || window->buffer == nullptr || (window->flags & WINDOW_HIDDEN) != 0
                || x < window->rect.left || x > window->rect.right || y < window->rect.top || y > window->rect.bottom) {
                continue;
            }
            if ((window->flags & WINDOW_TRANSPARENT) != 0
                && window->buffer[static_cast<size_t>(y - window->rect.top) * window->width + (x - window->rect.left)] == 0) {
                continue;
            }
            return window->id;
        }
        return -1;
    }

    // The recorder's state for a window, if it is a recorded window with a
    // drawn target and a slot.
    const UiWindow* recordedWindow(const Window* window, int* slot)
    {
        if (window->id == gIsoWindow || window->id + 1 > kMaxSlot) {
            return nullptr;
        }
        auto& windows = uiWindows();
        auto it = windows.find(window->id);
        if (it == windows.end() || it->second.buffer != window->buffer || it->second.width != window->width || it->second.height != window->height) {
            return nullptr;
        }
        auto target = gState->targets.find(window->id);
        if (target == gState->targets.end() || !target->second.texture || target->second.full || target->second.generation != it->second.generation) {
            return nullptr;
        }
        *slot = window->id + 1;
        return &it->second;
    }

    // Paints the cells of a screen rectangle (inclusive) from the window
    // stack, bottom to top; transparent windows' index-0 pixels show what
    // is below.
    void paintCells(int left, int top, int right, int bottom)
    {
        int width = gState->screenWidth;
        for (int y = top; y <= bottom; y++) {
            std::fill_n(gState->cells.begin() + static_cast<size_t>(y) * width + left, right - left + 1, static_cast<unsigned short>(0));
        }
        int count = windowGetCount();
        for (int index = 0; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || window->buffer == nullptr || (window->flags & WINDOW_HIDDEN) != 0) {
                continue;
            }
            int l = std::max(window->rect.left, left);
            int t = std::max(window->rect.top, top);
            int r = std::min(window->rect.right, right);
            int b = std::min(window->rect.bottom, bottom);
            if (l > r || t > b) {
                continue;
            }
            bool transparent = (window->flags & WINDOW_TRANSPARENT) != 0;
            int slot = 0;
            const UiWindow* s = recordedWindow(window, &slot);
            unsigned int lastOwner = 0;
            bool lastHd = false;
            for (int y = t; y <= b; y++) {
                size_t windowRow = static_cast<size_t>(y - window->rect.top) * window->width;
                unsigned short* row = gState->cells.data() + static_cast<size_t>(y) * width;
                for (int x = l; x <= r; x++) {
                    size_t i = windowRow + (x - window->rect.left);
                    if (transparent && window->buffer[i] == 0) {
                        continue;
                    }
                    unsigned short value = 0;
                    unsigned int owner = s != nullptr ? s->owner[i] : 0;
                    if (owner != 0) {
                        if (owner != lastOwner) {
                            lastOwner = owner;
                            auto op = s->ops.find(owner);
                            lastHd = op != s->ops.end() && op->second.hd;
                        }
                        unsigned char expected = s->expected[i];
                        if (lastHd && (expected < 229 || expected > 254)) {
                            value = static_cast<unsigned short>(slot << 8 | expected);
                        }
                    }
                    row[x] = value;
                }
            }
        }
        gState->cellsRebuilds++;
        gState->cellsPixels += static_cast<long long>(right - left + 1) * (bottom - top + 1);
    }

    bool ensureCells()
    {
        int width = screenGetWidth();
        int height = screenGetHeight();
        if (gState->cellsTexture && width == gState->screenWidth && height == gState->screenHeight) {
            return true;
        }
        gState->cellsTexture.Reset();
        gState->cellsView.Reset();
        gState->screenWidth = width;
        gState->screenHeight = height;
        gState->cells.assign(static_cast<size_t>(width) * height, 0);
        gState->cellsFull = true;
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = static_cast<UINT>(width);
        desc.Height = static_cast<UINT>(height);
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R16_UINT;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        ID3D11Device* device = presenterDevice();
        if (width <= 0 || height <= 0 || failed(device->CreateTexture2D(&desc, nullptr, &gState->cellsTexture), "cells texture")
            || failed(device->CreateShaderResourceView(gState->cellsTexture.Get(), nullptr, &gState->cellsView), "cells view")) {
            gState->cellsTexture.Reset();
            return false;
        }
        return true;
    }

    void stackLayout(std::vector<intptr_t>& layout)
    {
        layout.clear();
        layout.push_back(gState->screenWidth);
        layout.push_back(gState->screenHeight);
        int count = windowGetCount();
        for (int index = 0; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr) {
                continue;
            }
            layout.push_back(reinterpret_cast<intptr_t>(window));
            layout.push_back(reinterpret_cast<intptr_t>(window->buffer));
            layout.push_back(window->id);
            layout.push_back(window->flags);
            layout.push_back(window->rect.left);
            layout.push_back(window->rect.top);
            layout.push_back(window->rect.right);
            layout.push_back(window->rect.bottom);
        }
    }

    struct Region {
        int left = 0;
        int top = 0;
        int right = -1;
        int bottom = -1;

        void add(int l, int t, int r, int b)
        {
            if (l > r || t > b) {
                return;
            }
            if (left > right) {
                *this = { l, t, r, b };
                return;
            }
            left = std::min(left, l);
            top = std::min(top, t);
            right = std::max(right, r);
            bottom = std::max(bottom, b);
        }
    };

    // Brings the cells up to date: everything after a change in the window
    // stack, else the pixels the recorder saw change, windows drawn in full
    // and transparent windows (their index-0 pixels can change unseen).
    void updateCells(const std::vector<int>& drawnInFull)
    {
        double start = nowMs();
        std::vector<intptr_t> layout;
        stackLayout(layout);
        if (layout != gState->layout) {
            gState->layout.swap(layout);
            gState->cellsFull = true;
        }

        Region region;
        auto& windows = uiWindows();
        int count = windowGetCount();
        for (int index = 0; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || window->buffer == nullptr) {
                continue;
            }
            auto it = windows.find(window->id);
            if (it != windows.end()) {
                UiWindow& s = it->second;
                if (s.dirtyLeft <= s.dirtyRight) {
                    region.add(window->rect.left + s.dirtyLeft, window->rect.top + s.dirtyTop, window->rect.left + s.dirtyRight, window->rect.top + s.dirtyBottom);
                    s.dirtyLeft = 0;
                    s.dirtyTop = 0;
                    s.dirtyRight = -1;
                    s.dirtyBottom = -1;
                }
            }
            bool full = std::find(drawnInFull.begin(), drawnInFull.end(), window->id) != drawnInFull.end();
            if (full || ((window->flags & WINDOW_TRANSPARENT) != 0 && (window->flags & WINDOW_HIDDEN) == 0)) {
                region.add(window->rect.left, window->rect.top, window->rect.right, window->rect.bottom);
            }
        }
        if (gState->cellsFull) {
            region = { 0, 0, gState->screenWidth - 1, gState->screenHeight - 1 };
            gState->cellsFull = false;
        }
        region.left = std::max(region.left, 0);
        region.top = std::max(region.top, 0);
        region.right = std::min(region.right, gState->screenWidth - 1);
        region.bottom = std::min(region.bottom, gState->screenHeight - 1);
        if (region.left > region.right || region.top > region.bottom) {
            return;
        }
        paintCells(region.left, region.top, region.right, region.bottom);
        D3D11_BOX box = { static_cast<UINT>(region.left), static_cast<UINT>(region.top), 0, static_cast<UINT>(region.right + 1), static_cast<UINT>(region.bottom + 1), 1 };
        const unsigned short* src = gState->cells.data() + static_cast<size_t>(region.top) * gState->screenWidth + region.left;
        presenterContext()->UpdateSubresource(gState->cellsTexture.Get(), 0, &box, src, static_cast<UINT>(gState->screenWidth * sizeof(unsigned short)), 0);
        gState->cellsMs += nowMs() - start;
    }

} // namespace

bool uiLayerRequested()
{
    int value = 1;
    configGetInt(&gGameConfig, "hd", "ui_hd", &value, 1);
    return value != 0;
}

bool uiLayerInit()
{
    uiLayerShutdown();
    ID3D11Device* device = presenterDevice();
    if (device == nullptr || hdArtPoolView() == nullptr) {
        debugPrint("HD: ui layer needs the D3D11 presenter and the HD art store ([hd] map_gpu=1, hd_art=1)\n");
        return false;
    }
    gState = new State();
    int hard = 0;
    configGetInt(&gGameConfig, "hd", "ui_hard_alpha", &hard, 0);
    gState->hardAlpha = hard != 0;

    ComPtr<ID3DBlob> vs;
    ComPtr<ID3DBlob> ps;
    ComPtr<ID3DBlob> backdropPs;
    ComPtr<ID3DBlob> glyphPs;
    vs.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_ui.hlsl", "vsSprite", "vs_5_0"));
    ps.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_ui.hlsl", "psSprite", "ps_5_0"));
    backdropPs.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_ui.hlsl", "psBackdrop", "ps_5_0"));
    glyphPs.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_ui.hlsl", "psGlyph", "ps_5_0"));
    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(TargetConstants);
    constants.Usage = D3D11_USAGE_DEFAULT;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_BUFFER_DESC palette = {};
    palette.ByteWidth = 256 * 4 * sizeof(float);
    palette.Usage = D3D11_USAGE_DEFAULT;
    palette.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_BLEND_DESC blend = {};
    blend.RenderTarget[0].BlendEnable = TRUE;
    blend.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    D3D11_RASTERIZER_DESC raster = {};
    raster.FillMode = D3D11_FILL_SOLID;
    raster.CullMode = D3D11_CULL_NONE;
    raster.DepthClipEnable = TRUE;
    if (!vs || !ps || !backdropPs || !glyphPs
        || failed(device->CreateVertexShader(vs->GetBufferPointer(), vs->GetBufferSize(), nullptr, &gState->vertexShader), "vertex shader")
        || failed(device->CreatePixelShader(ps->GetBufferPointer(), ps->GetBufferSize(), nullptr, &gState->pixelShader), "pixel shader")
        || failed(device->CreatePixelShader(backdropPs->GetBufferPointer(), backdropPs->GetBufferSize(), nullptr, &gState->backdropShader), "backdrop shader")
        || failed(device->CreatePixelShader(glyphPs->GetBufferPointer(), glyphPs->GetBufferSize(), nullptr, &gState->glyphShader), "glyph shader")
        || failed(device->CreateBuffer(&constants, nullptr, &gState->constants), "constants")
        || failed(device->CreateBuffer(&palette, nullptr, &gState->palette), "palette")
        || failed(device->CreateBlendState(&blend, &gState->blend), "blend state")
        || failed(device->CreateRasterizerState(&raster, &gState->raster), "rasterizer state")) {
        debugPrint("HD: ui layer: shader or state creation failed, HD UI off\n");
        uiLayerShutdown();
        return false;
    }
    uiRecorderEnable();
    debugPrint("HD: ui layer up (%s alpha)\n", gState->hardAlpha ? "hard" : "HD");
    return true;
}

void uiLayerShutdown()
{
    if (gState == nullptr) {
        return;
    }
    if (gState->frames > 0) {
        debugPrint("HD: ui layer: %lld frames, %lld ops drawn (%lld HD), %lld text ops drawn (%lld HD, %s glyphs), %lld full window draws, %lld cells rebuilds (%.1f Mpx, %.3f ms per frame), %lld frames with a palette that isn't a fade\n",
            gState->frames, gState->opsTried, gState->opsHd, gState->textTried, gState->textHd, uiTextStyle(), gState->fullDraws, gState->cellsRebuilds, gState->cellsPixels / 1e6,
            gState->cellsMs / static_cast<double>(gState->frames), gState->paletteFallbacks);
    }
    delete gState;
    gState = nullptr;
    uiTextShutdown();
}

UiComposite uiLayerPrepare(int scale)
{
    UiComposite composite;
    if (gState == nullptr) {
        return composite;
    }
    bool active = gState->enabled && scale >= 2 && hdArtActive();
    if (!active) {
        gState->wasActive = false;
        gState->lastState = "off (Ctrl+F8/F9, 1:1 or no HD art)";
        return composite;
    }
    scale = std::min(scale, 4);
    if (!gState->wasActive || scale != gState->scale) {
        // HD art was off (Ctrl+F8, Ctrl+F9) or the scale changed: the
        // targets missed ops or have the wrong size.
        for (auto& entry : gState->targets) {
            entry.second.full = true;
        }
        gState->cellsFull = true;
        gState->scale = scale;
        gState->wasActive = true;
        gState->text = false;
    }
    if (!gState->text && uiTextLoad(scale)) {
        // HD glyphs (re)loaded for this scale (the fonts load after the
        // first frames): ops drawn before drew their text in CE's pixels.
        gState->text = true;
        for (auto& entry : gState->targets) {
            entry.second.full = true;
        }
        gState->cellsFull = true;
    }
    if (!ensureCells()) {
        return composite;
    }
    // The text passes need color.pal; it is the HD colors' reference too.
    if (!gState->cmapTaken) {
        takeColorPal();
        updatePalette();
    }

    // Windows CE deleted (or whose buffer changed) lose their targets.
    auto& windows = uiWindows();
    for (auto it = gState->targets.begin(); it != gState->targets.end();) {
        auto window = windows.find(it->first);
        if (window == windows.end() || window->second.generation != it->second.generation) {
            it = gState->targets.erase(it);
        } else {
            ++it;
        }
    }

    bool arrived = hdArtPoll(HdArtClient::Ui);
    gState->frameOps = 0;
    gState->frameHd = 0;
    gState->frameText = 0;
    gState->frameTextHd = 0;
    std::vector<int> drawnInFull;
    bool building = false;
    for (auto& entry : windows) {
        UiWindow& s = entry.second;
        if (entry.first < 0) {
            continue; // a scratch buffer: its ops reach the screen through the windows they are copied into
        }
        auto found = gState->targets.find(entry.first);
        if (found == gState->targets.end() && s.ops.empty()) {
            continue;
        }
        WindowTarget& t = gState->targets[entry.first];
        if (!ensureTarget(t, s, scale)) {
            gState->targets.erase(entry.first);
            continue;
        }
        if (arrived && t.pending) {
            t.full = true;
        }
        if (!t.full && s.nextSerial - 1 <= t.drawn) {
            continue;
        }
        if (!building) {
            hdArtBeginBuild(scale, HdArtClient::Ui);
            building = true;
        }
        if (drawWindow(t, s, scale)) {
            drawnInFull.push_back(entry.first);
        }
    }
    if (building) {
        hdArtEndBuild();
        unbindDrawState();
    }

    updateCells(drawnInFull);
    gState->frames++;

    // The HD colors need the live palette to be a fade of color.pal.
    PaletteFit fit;
    if (memcmp(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot)) != 0) {
        gState->paletteFallbacks++;
        gState->lastState = "palette not color.pal";
        return composite;
    }
    if (!(fit = paletteFit()).ok) {
        gState->paletteFallbacks++;
        gState->lastState = "palette not a fade";
        return composite;
    }

    int count = windowGetCount();
    for (int index = 0; index < count; index++) {
        Window* window = windowGetWindowAt(index);
        if (window == nullptr || window->buffer == nullptr || (window->flags & WINDOW_HIDDEN) != 0) {
            continue;
        }
        int slot = 0;
        if (recordedWindow(window, &slot) == nullptr) {
            continue;
        }
        UiWindowDraw draw;
        draw.x = window->rect.left;
        draw.y = window->rect.top;
        draw.width = window->width;
        draw.height = window->height;
        draw.slot = slot;
        draw.hd = gState->targets[window->id].srv.Get();
        composite.windows.push_back(draw);
    }
    gState->frameWindows = static_cast<int>(composite.windows.size());
    if (composite.windows.empty()) {
        gState->lastState = "no recorded window drawn";
        return composite;
    }
    gState->lastState = "shown";
    composite.active = true;
    composite.scale = scale;
    composite.cells = gState->cellsView.Get();
    for (int c = 0; c < 3; c++) {
        composite.gain[c] = fit.gain[c];
        composite.offset[c] = fit.offset[c] / 63.75f;
    }
    for (int i = 0; i < 64; i++) {
        composite.gamma[i] = _currentGammaTable[i];
    }
    composite.tint = gState->tint;
    return composite;
}

void uiLayerSetMode(const char* mapMode)
{
    if (gState == nullptr || mapMode == nullptr) {
        return;
    }
    if (strcmp(mapMode, "tinted") == 0) {
        gState->enabled = true;
        gState->tint = true;
    } else if (strcmp(mapMode, "off") == 0) {
        gState->enabled = false;
        gState->tint = false;
    } else if (strcmp(mapMode, "on") == 0) {
        gState->enabled = true;
        gState->tint = false;
    }
}

bool uiLayerGamePalette()
{
    return gState == nullptr || !gState->cmapTaken || memcmp(gState->cmapSnapshot, _cmap, sizeof(gState->cmapSnapshot)) == 0;
}

int uiLayerMapSnapshot(int isoX, int isoY, int width, int height, std::vector<unsigned char>& owned)
{
    if (gState == nullptr || !gState->enabled) {
        return 0;
    }
    std::vector<unsigned char> rgba;
    int scale = 0;
    if (!mapLayerSnapshot(isoX, isoY, width, height, rgba, owned, &scale)) {
        return 0;
    }
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = static_cast<UINT>(width * scale);
    desc.Height = static_cast<UINT>(height * scale);
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA data = { rgba.data(), static_cast<UINT>(width * scale * 4), 0 };
    Snapshot snap;
    if (failed(presenterDevice()->CreateTexture2D(&desc, &data, &snap.texture), "map snapshot texture")) {
        return 0;
    }
    snap.scale = scale;
    snap.width = width;
    snap.height = height;
    int id = gState->nextSnapshot++;
    gState->snapshots[id] = std::move(snap);
    while (gState->snapshots.size() > kMaxSnapshots) {
        gState->snapshots.erase(gState->snapshots.begin());
    }
    gState->snapshotsMade++;
    return id;
}

int uiLayerSelfTest(int tick)
{
    if (gState == nullptr || !gState->wasActive) {
        return -1;
    }
    // A screen with a palette of its own (the help screen loads helpscrn.pal)
    // shows CE's pixels (uiLayerPrepare), and the HD art, built in
    // color.pal's colors, has nothing to match there.
    if (!uiLayerGamePalette()) {
        debugPrint("HD: ui hd self-test %d: skipped, palette not color.pal\n", tick);
        return -1;
    }
    // The reference is the live palette (color.pal's placeholders at 0 and
    // 229..255 are filled in at runtime), so not during a fade.
    PaletteFit fit = paletteFit();
    for (int c = 0; c < 3; c++) {
        if (!fit.ok || fabsf(fit.gain[c] - 1.0f) > 0.01f || fit.offset[c] != 0.0f) {
            debugPrint("HD: ui hd self-test %d: skipped, palette faded\n", tick);
            return -1;
        }
    }
    // Provisional (P7b.2): HD masters area-downscaled come out near the
    // original; a frame off by a pixel or the wrong frame doesn't.
    constexpr double kMinPsnr = 25.0;
    // Text (P7b.5), measured on its own: HD glyphs averaged back to 1x are
    // 20-31 dB from CE's coverage (fo2hd fonts, coverage units; the small
    // font1 lowest), while a glyph off by a pixel lands far below.
    // Provisional.
    constexpr double kMinTextPsnr = 16.0;
    long long textCompared = 0;
    double textSquares = 0.0;
    long long mapCompared = 0; // map snapshot pixels (the dialogue's map view)
    double mapSquares = 0.0;
    // [art, text][reaches the screen, buffer changed since, another window on
    // top, cell wrong, CE's screen holds another index there]
    long long reach[2][5] = {};
    struct ReachStats {
        long long counts[5] = {};
        std::string first[5]; // the first pixel of each miss, for the log
    };
    std::map<int, ReachStats> windowReach;
    char firstTextMiss[128] = "";
    ID3D11Device* device = presenterDevice();
    ID3D11DeviceContext* context = presenterContext();
    auto& windows = uiWindows();
    int scale = gState->scale;
    int checkedWindows = 0;
    long long compared = 0;
    long long lowAlpha = 0;
    double sumSquares = 0.0;
    double worstPsnr = 99.0;
    int worstWindow = -1;
    for (auto& entry : gState->targets) {
        WindowTarget& t = entry.second;
        auto found = windows.find(entry.first);
        if (found == windows.end() || found->second.generation != t.generation || t.full || !t.texture || t.scale != scale) {
            continue;
        }
        const UiWindow& s = found->second;
        Window* window = windowGetWindow(entry.first);
        if (window != nullptr && (window->buffer != s.buffer || (window->flags & WINDOW_HIDDEN) != 0)) {
            window = nullptr; // a hidden window reaches nothing (barter hides the reply and options)
        }
        D3D11_TEXTURE2D_DESC desc;
        t.texture->GetDesc(&desc);
        desc.Usage = D3D11_USAGE_STAGING;
        desc.BindFlags = 0;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> staging;
        if (failed(device->CreateTexture2D(&desc, nullptr, &staging), "self-test staging")) {
            continue;
        }
        context->CopyResource(staging.Get(), t.texture.Get());
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (failed(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "self-test map")) {
            continue;
        }
        const unsigned char* texels = static_cast<const unsigned char*>(mapped.pData);
        long long windowCompared = 0;
        double windowSquares = 0.0;
        unsigned int lastOwner = 0;
        bool lastHd = false;
        bool lastText = false;
        bool lastMap = false;
        for (int y = 0; y < s.height; y++) {
            for (int x = 0; x < s.width; x++) {
                size_t i = static_cast<size_t>(y) * s.width + x;
                unsigned int owner = s.owner[i];
                if (owner == 0) {
                    continue;
                }
                if (owner != lastOwner) {
                    lastOwner = owner;
                    auto op = s.ops.find(owner);
                    lastHd = op != s.ops.end() && op->second.hd;
                    lastText = op != s.ops.end() && op->second.font >= 0;
                    lastMap = op != s.ops.end() && op->second.snapshot != 0;
                }
                unsigned char index = s.expected[i];
                if (!lastHd || index >= 229) {
                    continue;
                }
                double sum[4] = {};
                for (int dy = 0; dy < scale; dy++) {
                    const unsigned char* row = texels + static_cast<size_t>(y * scale + dy) * mapped.RowPitch + static_cast<size_t>(x * scale) * 4;
                    for (int dx = 0; dx < scale * 4; dx++) {
                        sum[dx & 3] += row[dx];
                    }
                }
                double n = static_cast<double>(scale) * scale;
                double alpha = sum[3] / n / 255.0;
                if (alpha < 0.5) {
                    lowAlpha++;
                }
                double squares = 0.0;
                for (int c = 0; c < 3; c++) {
                    double reference = _systemCmap[index * 3 + c] << 2;
                    double shown = sum[c] / n + (1.0 - alpha) * reference;
                    double d = shown - reference;
                    squares += d * d;
                }
                // Would the screen show this texel? The cells (CPU copy) must
                // name this window and index, the buffer must still hold the
                // index, and the window must be on top there (P7b.5: the
                // walk's HD text never reached the screen).
                if (window != nullptr && !((window->flags & WINDOW_TRANSPARENT) != 0 && s.buffer[i] == 0)) {
                    int sx = window->rect.left + x;
                    int sy = window->rect.top + y;
                    int k = lastText ? 1 : 0;
                    if (sx >= 0 && sy >= 0 && sx < gState->screenWidth && sy < gState->screenHeight) {
                        unsigned short cell = gState->cells[static_cast<size_t>(sy) * gState->screenWidth + sx];
                        bool cellOk = cell == static_cast<unsigned short>((window->id + 1) << 8 | index);
                        bool bufferOk = s.buffer[i] == index;
                        int topId = topWindowAt(sx, sy);
                        bool top = topId == window->id;
                        // psUi also needs CE's screen to hold the index there.
                        int screenIndex = -1;
                        if (gSdlSurface != nullptr && sx < gSdlSurface->w && sy < gSdlSurface->h) {
                            screenIndex = static_cast<const unsigned char*>(gSdlSurface->pixels)[static_cast<size_t>(gSdlSurface->pitch) * sy + sx];
                        }
                        bool screenOk = screenIndex < 0 || screenIndex == index;
                        int category = cellOk && bufferOk && top && screenOk ? 0 : !bufferOk ? 1
                            : !top                                                           ? 2
                            : !cellOk                                                        ? 3
                                                                                             : 4;
                        reach[k][category]++;
                        ReachStats& rs = windowReach[window->id];
                        rs.counts[category]++;
                        if (category >= 2 && rs.first[category].empty()) {
                            Window* topWindow = topId >= 0 ? windowGetWindow(topId) : nullptr;
                            char text[256];
                            snprintf(text, sizeof(text), "px %d,%d (screen %d,%d) cell %04x want %04x buffer %d screen %d; top window %d flags %#x rect %d,%d-%d,%d",
                                x, y, sx, sy, cell, (window->id + 1) << 8 | index, s.buffer[i], screenIndex, topId,
                                topWindow != nullptr ? topWindow->flags : 0, topWindow != nullptr ? topWindow->rect.left : 0, topWindow != nullptr ? topWindow->rect.top : 0,
                                topWindow != nullptr ? topWindow->rect.right : 0, topWindow != nullptr ? topWindow->rect.bottom : 0);
                            rs.first[category] = text;
                        }
                        if (k == 1 && !cellOk && firstTextMiss[0] == '\0') {
                            snprintf(firstTextMiss, sizeof(firstTextMiss), "window %d px %d,%d cell %04x want %04x buffer %d top %d",
                                window->id, x, y, cell, (window->id + 1) << 8 | index, s.buffer[i], windowGetAtPoint(sx, sy));
                        }
                    }
                }
                if (lastText) {
                    textSquares += squares;
                    textCompared++;
                    continue;
                }
                if (lastMap) {
                    // The map's own HD look (new light, post effects) is
                    // checked by the map self-test, not against the palette.
                    mapSquares += squares;
                    mapCompared++;
                    continue;
                }
                windowSquares += squares;
                windowCompared++;
            }
        }
        context->Unmap(staging.Get(), 0);
        // Art that still shows CE's pixels (no pack, a mismatch, or the
        // pool couldn't place it): the biggest, for the log.
        std::vector<const UiOp*> flat;
        for (const auto& op : s.ops) {
            if (!op.second.hd && op.second.font < 0 && op.second.snapshot == 0 && op.second.owned >= 1000) {
                flat.push_back(&op.second);
            }
        }
        std::sort(flat.begin(), flat.end(), [](const UiOp* a, const UiOp* b) { return a->owned > b->owned; });
        for (size_t k = 0; k < flat.size() && k < 3; k++) {
            const UiOp& op = *flat[k];
            debugPrint("HD: ui hd self-test %d: window %d: %d px of %s%s (fid 0x%08x frame %d, %dx%d) at 1x: no HD art drawn\n",
                tick, entry.first, op.owned, op.fid == -1 ? "named " : "", op.fid == -1 ? op.path.c_str() : "art", static_cast<unsigned int>(op.fid), op.frame,
                op.frameWidth, op.frameHeight);
        }
        if (windowCompared == 0) {
            continue;
        }
        checkedWindows++;
        compared += windowCompared;
        sumSquares += windowSquares;
        double mse = windowSquares / (windowCompared * 3.0);
        double psnr = mse > 0.0 ? 10.0 * log10(255.0 * 255.0 / mse) : 99.0;
        if (psnr < worstPsnr) {
            worstPsnr = psnr;
            worstWindow = entry.first;
        }
    }
    if (compared == 0 && textCompared == 0) {
        debugPrint("HD: ui hd self-test %d: skipped, no HD UI pixels\n", tick);
        return -1;
    }
    if (mapCompared > 0) {
        double mapMse = mapSquares / (mapCompared * 3.0);
        debugPrint("HD: ui hd self-test %d: map view %lld px, %.1f dB vs the palette (the map's light and effects; not judged)\n",
            tick, mapCompared, mapMse > 0.0 ? 10.0 * log10(255.0 * 255.0 / mapMse) : 99.0);
    }
    double mse = compared > 0 ? sumSquares / (compared * 3.0) : 0.0;
    double psnr = mse > 0.0 ? 10.0 * log10(255.0 * 255.0 / mse) : 99.0;
    double textMse = textCompared > 0 ? textSquares / (textCompared * 3.0) : 0.0;
    double textPsnr = textMse > 0.0 ? 10.0 * log10(255.0 * 255.0 / textMse) : 99.0;
    bool pass = worstPsnr >= kMinPsnr && textPsnr >= kMinTextPsnr;
    debugPrint("HD: ui hd self-test %d: %s (%d windows, %lld px, PSNR %.1f dB vs the palette, worst window %d at %.1f dB, %lld px with HD alpha below 1/2; text %lld px, %.1f dB)\n",
        tick, pass ? "PASS" : "FAIL", checkedWindows, compared, psnr, worstWindow, worstPsnr, lowAlpha, textCompared, textPsnr);
    // The self-test reads the targets; whether they reach the screen is this.
    debugPrint("HD: ui composite %d: %s, %d windows, palette fallbacks %lld of %lld frames\n",
        tick, gState->lastState, gState->frameWindows, gState->paletteFallbacks, gState->frames);
    debugPrint("HD: ui reach %d: art %lld shown, %lld changed, %lld covered, %lld cell wrong, %lld screen differs; text %lld shown, %lld changed, %lld covered, %lld cell wrong, %lld screen differs\n",
        tick, reach[0][0], reach[0][1], reach[0][2], reach[0][3], reach[0][4], reach[1][0], reach[1][1], reach[1][2], reach[1][3], reach[1][4]);
    // Per window, and where the misses start (P7: the interface bar beside
    // the dialogue and barter screens stayed 1x).
    for (auto& entry : windowReach) {
        const ReachStats& rs = entry.second;
        if (rs.counts[2] + rs.counts[3] + rs.counts[4] == 0) {
            continue;
        }
        debugPrint("HD: ui reach %d: window %d: %lld shown, %lld changed, %lld covered, %lld cell wrong, %lld screen differs\n",
            tick, entry.first, rs.counts[0], rs.counts[1], rs.counts[2], rs.counts[3], rs.counts[4]);
        const char* names[5] = { "", "", "covered", "cell wrong", "screen differs" };
        for (int c = 2; c < 5; c++) {
            if (!rs.first[c].empty()) {
                debugPrint("HD: ui reach %d: window %d first %s: %s\n", tick, entry.first, names[c], rs.first[c].c_str());
            }
        }
    }
    if (firstTextMiss[0] != '\0') {
        debugPrint("HD: ui reach %d: first text cell miss: %s\n", tick, firstTextMiss);
    }
    return pass ? 0 : 1;
}

void uiLayerDrawOverlay()
{
    if (gState == nullptr) {
        ImGui::Text("ui hd     off ([hd] ui_hd=0)");
        return;
    }
    const HdArtStats& art = hdArtStats();
    ImGui::Text("ui hd     %s%s, %d windows shown; last frame %d ops (%d HD); %lld full window draws",
        gState->enabled ? "on" : "off", gState->tint ? " (tinted)" : "", gState->frameWindows, gState->frameOps, gState->frameHd, gState->fullDraws);
    ImGui::Text("ui text   %s glyphs%s; last draw %d text ops (%d HD); %lld of %lld HD so far",
        uiTextStyle(), gState->text ? "" : " (not loaded: CE's text)", gState->frameText, gState->frameTextHd, gState->textHd, gState->textTried);
    ImGui::Text("ui art    last build %d of %d frames HD, %d uploads (%.1f MB), %d deferred; cells %.3f ms per frame",
        art.uiHd, art.uiFrames, art.uiUploads, art.uiUploadBytes / 1048576.0, art.uiDeferred,
        gState->frames > 0 ? gState->cellsMs / static_cast<double>(gState->frames) : 0.0);
}

} // namespace fallout::hd

#endif // _WIN32
