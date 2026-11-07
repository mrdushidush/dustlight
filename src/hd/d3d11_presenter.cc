#include "hd/d3d11_presenter.h"

#ifdef _WIN32

#include <math.h>
#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <filesystem>
#include <string>
#include <vector>

#include <SDL_syswm.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_4.h>
#include <wrl/client.h>

#include "config.h"
#include "debug.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "hd/map_layer.h"
#include "hd/release_info.h"
#include "hd/ui_layer.h"
#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_sdl2.h"
#include "lodepng.h"

using Microsoft::WRL::ComPtr;

// Laptops with two GPUs: ask the NVIDIA and AMD drivers for the discrete one
// (PLAN.md R3). The drivers look these exports up in the exe; elsewhere they
// do nothing.
extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 1;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}

namespace fallout::hd {

namespace {

    // Full-screen triangle; the palette pass looks up each 8-bit pixel, the
    // overlay pass copies a 32-bit texture (point sampling, like SDL's nearest).
    // The composite pass is the palette pass with the GPU map (hd/map_layer.h)
    // replacing CE's map pixels where the mask and the 1x check allow. The
    // UI pass then draws each HD UI window (hd/ui_layer.h) over its rect,
    // where its cell owns the pixel and expects CE's index there.
    constexpr char kShaderSource[] = R"hlsl(
Texture2D<uint> gIndices : register(t0);
Texture2D<float4> gPalette : register(t1);
Texture2D<float4> gOverlay : register(t2);
Texture2D<uint> gMapMask : register(t3);
Texture2D<uint> gMapExact : register(t4);
Texture2D<uint> gMapScaled : register(t5);
Texture2D<float4> gMapHd : register(t6);
Texture2D<uint> gUiCells : register(t7);
Texture2D<float4> gUiHd : register(t8);
SamplerState gPoint : register(s0);

cbuffer Composite : register(b0) {
    int2 gIsoOrigin;
    int2 gIsoSize;
    int gMapScale;
    int gMapTint;
    int2 gPad;
    int gMapHdOn;
    float3 gGain;     // live palette = gain * color.pal (fades)
    float4 gGamma[16]; // CE's _currentGammaTable, 64 x 6-bit
    float gGrain;     // film grain amplitude on HD map pixels (P6.3)
    uint gGrainSeed;  // changes 24 times a second
    int2 gPad2;
    float3 gOffset;   // ... + offset (fades toward white, P6.4)
    float gPad3;
};

cbuffer UiWindow : register(b1) {
    int2 gUiOrigin;   // window rect, logical screen pixels
    int2 gUiSize;
    int gUiSlot;
    int gUiScale;
    int gUiTint;
    int gUiPad;
};

// Film grain: two hashes per pixel and seed make a triangular noise in
// -1..1, strongest in the mid-tones.
float hash(uint2 p, uint seed)
{
    uint h = p.x * 0x8da6b343u ^ p.y * 0xd8163841u ^ seed * 0xcb1ab31fu;
    h ^= h >> 15;
    h *= 0x2c1b3c6du;
    h ^= h >> 12;
    h *= 0x297a2d39u;
    h ^= h >> 15;
    return (h & 0xFFFFFFu) / 16777215.0;
}

float3 grain(float3 rgb, uint2 p)
{
    float n = hash(p, gGrainSeed) + hash(p, gGrainSeed + 7919u) - 1.0;
    float l = dot(rgb, float3(0.299, 0.587, 0.114));
    return saturate(rgb + n * gGrain * (0.35 + 2.6 * l * (1.0 - l)));
}

// HD map colors are in color.pal's space; the screen palette is
// gamma(fade(color.pal)) in 6-bit steps, so the same transform here.
float gammaAt(int i) { return gGamma[i >> 2][i & 3]; }
float3 display(float3 c)
{
    float3 v = saturate(c * gGain + gOffset) * 63.75;
    float3 o;
    [unroll] for (int k = 0; k < 3; k++) {
        int i = min(int(v[k]), 63);
        int j = min(i + 1, 63);
        o[k] = lerp(gammaAt(i), gammaAt(j), v[k] - i);
    }
    return o * (4.0 / 255.0);
}

struct VSOut {
    float4 pos : SV_Position;
    float2 uv : TEXCOORD0;
};

VSOut vsMain(uint id : SV_VertexID)
{
    VSOut o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}

float4 psPalette(VSOut i) : SV_Target
{
    uint w, h;
    gIndices.GetDimensions(w, h);
    uint2 p = min(uint2(i.uv * float2(w, h)), uint2(w - 1, h - 1));
    uint index = gIndices.Load(int3(p, 0));
    return float4(gPalette.Load(int3(index, 0, 0)).rgb, 1);
}

float4 psOverlay(VSOut i) : SV_Target
{
    return float4(gOverlay.Sample(gPoint, i.uv).rgb, 1);
}

float4 psComposite(VSOut i) : SV_Target
{
    uint w, h;
    gIndices.GetDimensions(w, h);
    float2 f = i.uv * float2(w, h);
    uint2 p = min(uint2(f), uint2(w - 1, h - 1));
    uint index = gIndices.Load(int3(p, 0));
    int2 q = int2(p) - gIsoOrigin;
    uint exact = all(q >= 0) && all(q < gIsoSize) ? gMapExact.Load(int3(q, 0)) : 0;
    if (all(q >= 0) && all(q < gIsoSize) && gMapMask.Load(int3(q, 0)) != 0 && (exact & 0xFF) == index) {
        // The scaled map texel inside logical pixel q under this window pixel.
        int2 o = clamp(int2((f - float2(gIsoOrigin)) * gMapScale), q * gMapScale, q * gMapScale + gMapScale - 1);
        if (gMapHdOn != 0) {
            // Outline pixels (tagged) stay the palette's: CE's index here.
            float3 ce = gPalette.Load(int3(index, 0, 0)).rgb;
            float3 rgb = ce;
            if ((exact & 0x100) == 0) {
                // Premultiplied. Where the HD art leaves part of a pixel the
                // 1x map draws uncovered (a soft HD edge over a spot nothing
                // is drawn under), CE's color of that pixel fills in, as on
                // the HD UI; everywhere else the HD color alone (stacked
                // blends leave alpha a few fp16 steps below 1 there).
                float4 hd = gMapHd.Load(int3(o, 0));
                if (index != 0 && hd.a < 1.0 - 1.0 / 255.0) {
                    rgb = hd.a > 0.0 ? lerp(ce, display(hd.rgb / hd.a), hd.a) : ce;
                } else {
                    rgb = display(hd.rgb);
                }
            }
            if (gGrain > 0.0) {
                rgb = grain(rgb, uint2(i.pos.xy));
            }
            if (gMapTint != 0) {
                rgb = lerp(rgb, float3(0.0, 0.9, 1.0), 0.35);
            }
            return float4(rgb, 1);
        }
        index = gMapScaled.Load(int3(o, 0)) & 0xFF;
        if (gMapTint != 0) {
            float3 rgb = gPalette.Load(int3(index, 0, 0)).rgb;
            return float4(lerp(rgb, float3(0.0, 0.9, 1.0), 0.35), 1);
        }
    }
    return float4(gPalette.Load(int3(index, 0, 0)).rgb, 1);
}

// Drawn with the viewport on one window's rect.
float4 psUi(VSOut i) : SV_Target
{
    float2 f = i.uv * float2(gUiSize);
    int2 q = min(int2(f), gUiSize - 1);
    int2 p = gUiOrigin + q;
    uint cell = gUiCells.Load(int3(p, 0));
    uint index = gIndices.Load(int3(p, 0));
    if ((cell >> 8) != uint(gUiSlot) || (cell & 0xFF) != index) {
        discard;
    }
    int2 o = clamp(int2(f * gUiScale), q * gUiScale, q * gUiScale + gUiScale - 1);
    // Premultiplied; where the HD art doesn't cover the texel (a soft edge
    // over pixels that weren't HD) CE's own color fills in.
    float4 hd = gUiHd.Load(int3(o, 0));
    float3 ce = gPalette.Load(int3(index, 0, 0)).rgb;
    float3 rgb = hd.a > 0.0 ? lerp(ce, display(hd.rgb / hd.a), hd.a) : ce;
    if (gUiTint != 0) {
        rgb = lerp(rgb, float3(1.0, 0.55, 0.0), 0.35);
    }
    return float4(rgb, 1);
}
)hlsl";

    struct UiWindowConstants {
        int origin[2];
        int size[2];
        int slot;
        int scale;
        int tint;
        int pad;
    };
    static_assert(sizeof(UiWindowConstants) == 32, "UiWindowConstants must match the HLSL cbuffer");

    struct CompositeConstants {
        int isoOrigin[2];
        int isoSize[2];
        int mapScale;
        int mapTint;
        int pad[2];
        int mapHdOn;
        float gain[3];
        float gamma[64];
        float grain;
        unsigned int grainSeed;
        int pad2[2];
        float offset[3];
        float pad3;
    };
    static_assert(sizeof(CompositeConstants) == 336, "CompositeConstants must match the HLSL cbuffer");

    struct Viewport {
        float x;
        float y;
        float width;
        float height;
    };

    struct State {
        SDL_Window* window = nullptr;
        HWND hwnd = nullptr;
        int width = 0;
        int height = 0;
        bool vsync = false;

        ComPtr<ID3D11Device> device;
        ComPtr<ID3D11DeviceContext> context;
        ComPtr<IDXGISwapChain1> swapChain;
        ComPtr<ID3D11RenderTargetView> backBuffer;
        int backBufferWidth = 0;
        int backBufferHeight = 0;

        ComPtr<ID3D11VertexShader> vertexShader;
        ComPtr<ID3D11PixelShader> paletteShader;
        ComPtr<ID3D11PixelShader> overlayShader;
        ComPtr<ID3D11PixelShader> compositeShader;
        ComPtr<ID3D11PixelShader> uiShader;
        ComPtr<ID3D11Buffer> compositeConstants;
        ComPtr<ID3D11Buffer> uiConstants;
        ComPtr<ID3D11SamplerState> pointSampler;

        ComPtr<ID3D11Texture2D> indexTexture;
        ComPtr<ID3D11ShaderResourceView> indexView;
        ComPtr<ID3D11Texture2D> paletteTexture;
        ComPtr<ID3D11ShaderResourceView> paletteView;
        unsigned char palette[256 * 4] = {};
        bool paletteDirty = true;

        ComPtr<ID3D11Texture2D> overlayTexture;
        ComPtr<ID3D11ShaderResourceView> overlayView;
        int overlayWidth = 0;
        int overlayHeight = 0;
        bool overlayActive = false;
        SDL_Rect overlayDest = { 0, 0, 0, 0 };

        // [hd_debug] present_selftest_ms: periodic readback self-test plus a
        // PNG of the presented back buffer, for unattended screen coverage.
        int selfTestIntervalMs = 0;
        Uint32 lastSelfTest = 0;
        int selfTestRuns = 0;
        int selfTestFailures = 0;
        std::string dumpDir;

        // [hd_debug] frame_seq_*: cropped frame sequences (P6.0).
        int seqCount = 0;
        int seqStep = 1;
        int seqRect[4] = {};
        int seqRemaining = 0; // frames still to take in the running sequence
        int seqPresents = 0; // presents since it started
        int seqIndex = 0; // sequences started
        ComPtr<ID3D11Texture2D> seqStaging;
        std::vector<std::vector<unsigned char>> seqFrames; // RGB crops
        std::vector<Uint32> seqTimes;

        // CPU time spent presenting (upload + draw + Present call).
        double presentMsTotal = 0.0;
        double presentMsMax = 0.0;
        int presentCount = 0;

        // Developer overlay (Dear ImGui, display only).
        bool overlayReady = false;
        bool overlayVisible = false;
        float frameMs[240] = {};
        int frameIndex = 0;
        Uint64 lastFrame = 0;
    };

    State* gState = nullptr;

    bool failed(HRESULT hr, const char* what)
    {
        if (FAILED(hr)) {
            debugPrint("HD: D3D11 %s failed (0x%08x)\n", what, static_cast<unsigned>(hr));
            return true;
        }
        return false;
    }

    bool compile(const char* entry, const char* target, ComPtr<ID3DBlob>& blob)
    {
        blob.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_present.hlsl", entry, target));
        return blob != nullptr;
    }

    bool createBackBuffer()
    {
        gState->backBuffer.Reset();
        ComPtr<ID3D11Texture2D> texture;
        if (failed(gState->swapChain->GetBuffer(0, IID_PPV_ARGS(&texture)), "GetBuffer")) {
            return false;
        }
        if (failed(gState->device->CreateRenderTargetView(texture.Get(), nullptr, &gState->backBuffer), "CreateRenderTargetView")) {
            return false;
        }
        D3D11_TEXTURE2D_DESC desc;
        texture->GetDesc(&desc);
        gState->backBufferWidth = static_cast<int>(desc.Width);
        gState->backBufferHeight = static_cast<int>(desc.Height);
        return true;
    }

    bool createTexture(int width, int height, DXGI_FORMAT format, D3D11_USAGE usage, UINT cpuAccess, ComPtr<ID3D11Texture2D>& texture, ComPtr<ID3D11ShaderResourceView>& view)
    {
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = width;
        desc.Height = height;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = format;
        desc.SampleDesc.Count = 1;
        desc.Usage = usage;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.CPUAccessFlags = cpuAccess;
        if (failed(gState->device->CreateTexture2D(&desc, nullptr, &texture), "CreateTexture2D")) {
            return false;
        }
        return !failed(gState->device->CreateShaderResourceView(texture.Get(), nullptr, &view), "CreateShaderResourceView");
    }

    // Same placement as SDL_RenderSetLogicalSize: keep the aspect ratio, fill
    // one axis, center on the other.
    Viewport logicalViewport(int targetWidth, int targetHeight)
    {
        float want = static_cast<float>(gState->width) / gState->height;
        float real = static_cast<float>(targetWidth) / targetHeight;
        Viewport vp = { 0, 0, static_cast<float>(targetWidth), static_cast<float>(targetHeight) };
        if (fabsf(want - real) < 0.0001f) {
            return vp;
        }
        if (want > real) {
            float scale = static_cast<float>(targetWidth) / gState->width;
            vp.height = floorf(gState->height * scale);
            vp.y = floorf((targetHeight - vp.height) / 2.0f);
        } else {
            float scale = static_cast<float>(targetHeight) / gState->height;
            vp.width = floorf(gState->width * scale);
            vp.x = floorf((targetWidth - vp.width) / 2.0f);
        }
        return vp;
    }

    void syncPalette(SDL_Surface* surface)
    {
        if (surface->format == nullptr || surface->format->palette == nullptr) {
            return;
        }
        const SDL_Color* colors = surface->format->palette->colors;
        int count = surface->format->palette->ncolors < 256 ? surface->format->palette->ncolors : 256;
        for (int index = 0; index < count; index++) {
            unsigned char* entry = gState->palette + index * 4;
            if (entry[0] != colors[index].r || entry[1] != colors[index].g || entry[2] != colors[index].b || entry[3] != 255) {
                entry[0] = colors[index].r;
                entry[1] = colors[index].g;
                entry[2] = colors[index].b;
                entry[3] = 255;
                gState->paletteDirty = true;
            }
        }
    }

    void uploadSurface(SDL_Surface* surface)
    {
        syncPalette(surface);

        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(gState->context->Map(gState->indexTexture.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
            return;
        }
        const unsigned char* src = static_cast<const unsigned char*>(surface->pixels);
        unsigned char* dst = static_cast<unsigned char*>(mapped.pData);
        int rows = surface->h < gState->height ? surface->h : gState->height;
        int bytes = surface->w < gState->width ? surface->w : gState->width;
        for (int y = 0; y < rows; y++) {
            memcpy(dst + mapped.RowPitch * y, src + surface->pitch * y, bytes);
        }
        gState->context->Unmap(gState->indexTexture.Get(), 0);

        if (gState->paletteDirty) {
            gState->context->UpdateSubresource(gState->paletteTexture.Get(), 0, nullptr, gState->palette, sizeof(gState->palette), 0);
            gState->paletteDirty = false;
        }
    }

    // Writes the back buffer (what the window shows) as an RGB PNG.
    void dumpBackBuffer(const std::string& path)
    {
        ComPtr<ID3D11Texture2D> buffer;
        if (FAILED(gState->swapChain->GetBuffer(0, IID_PPV_ARGS(&buffer)))) {
            return;
        }
        D3D11_TEXTURE2D_DESC desc;
        buffer->GetDesc(&desc);
        desc.Usage = D3D11_USAGE_STAGING;
        desc.BindFlags = 0;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        desc.MiscFlags = 0;
        ComPtr<ID3D11Texture2D> staging;
        if (FAILED(gState->device->CreateTexture2D(&desc, nullptr, &staging))) {
            return;
        }
        gState->context->CopyResource(staging.Get(), buffer.Get());
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(gState->context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) {
            return;
        }
        std::vector<unsigned char> rgb(static_cast<size_t>(desc.Width) * desc.Height * 3);
        for (UINT y = 0; y < desc.Height; y++) {
            const unsigned char* row = static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * y;
            for (UINT x = 0; x < desc.Width; x++) {
                memcpy(&rgb[(static_cast<size_t>(y) * desc.Width + x) * 3], row + x * 4, 3);
            }
        }
        gState->context->Unmap(staging.Get(), 0);
        lodepng::encode(path, rgb, desc.Width, desc.Height, LCT_RGB, 8);
    }

    // One frame of the running sequence: the crop of the back buffer into
    // memory; the PNGs are written when the sequence ends.
    void frameSequenceTake()
    {
        if (gState->seqRemaining <= 0) {
            return;
        }
        if (gState->seqPresents++ % gState->seqStep != 0) {
            return;
        }
        ComPtr<ID3D11Texture2D> buffer;
        if (FAILED(gState->swapChain->GetBuffer(0, IID_PPV_ARGS(&buffer)))) {
            return;
        }
        int x = std::max(0, std::min(gState->seqRect[0], gState->backBufferWidth - 1));
        int y = std::max(0, std::min(gState->seqRect[1], gState->backBufferHeight - 1));
        int w = gState->seqRect[2] > 0 ? std::min(gState->seqRect[2], gState->backBufferWidth - x) : gState->backBufferWidth - x;
        int h = gState->seqRect[3] > 0 ? std::min(gState->seqRect[3], gState->backBufferHeight - y) : gState->backBufferHeight - y;
        D3D11_TEXTURE2D_DESC desc;
        if (gState->seqStaging) {
            gState->seqStaging->GetDesc(&desc);
        }
        if (!gState->seqStaging || static_cast<int>(desc.Width) != w || static_cast<int>(desc.Height) != h) {
            buffer->GetDesc(&desc);
            desc.Width = w;
            desc.Height = h;
            desc.Usage = D3D11_USAGE_STAGING;
            desc.BindFlags = 0;
            desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            desc.MiscFlags = 0;
            gState->seqStaging.Reset();
            if (FAILED(gState->device->CreateTexture2D(&desc, nullptr, &gState->seqStaging))) {
                gState->seqRemaining = 0;
                return;
            }
        }
        D3D11_BOX box = { static_cast<UINT>(x), static_cast<UINT>(y), 0, static_cast<UINT>(x + w), static_cast<UINT>(y + h), 1 };
        gState->context->CopySubresourceRegion(gState->seqStaging.Get(), 0, 0, 0, 0, buffer.Get(), 0, &box);
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(gState->context->Map(gState->seqStaging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) {
            return;
        }
        std::vector<unsigned char> rgb(static_cast<size_t>(w) * h * 3);
        for (int row = 0; row < h; row++) {
            const unsigned char* src = static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * row;
            for (int col = 0; col < w; col++) {
                memcpy(&rgb[(static_cast<size_t>(row) * w + col) * 3], src + col * 4, 3);
            }
        }
        gState->context->Unmap(gState->seqStaging.Get(), 0);
        gState->seqFrames.push_back(std::move(rgb));
        gState->seqTimes.push_back(SDL_GetTicks());
        if (--gState->seqRemaining > 0) {
            return;
        }

        // Stored, unfiltered PNGs: written in a few ms each, so the game loop
        // (and the injected keys that follow) isn't held up for seconds.
        lodepng::State png;
        png.info_raw.colortype = LCT_RGB;
        png.info_raw.bitdepth = 8;
        png.info_png.color.colortype = LCT_RGB;
        png.info_png.color.bitdepth = 8;
        png.encoder.auto_convert = 0;
        png.encoder.filter_strategy = LFS_ZERO;
        png.encoder.zlibsettings.btype = 0;
        png.encoder.zlibsettings.use_lz77 = 0;
        int k = gState->seqIndex - 1;
        std::string times;
        for (size_t i = 0; i < gState->seqFrames.size(); i++) {
            char name[32];
            snprintf(name, sizeof(name), "seq%d-%03d.png", k, static_cast<int>(i));
            std::vector<unsigned char> encoded;
            if (lodepng::encode(encoded, gState->seqFrames[i], w, h, png) == 0) {
                lodepng::save_file(encoded, gState->dumpDir + "\\" + name);
            }
            times += (i == 0 ? "" : ",") + std::to_string(gState->seqTimes[i] - gState->seqTimes[0]);
        }
        // Frame times (ms from the first) go to a file: too long for a log line.
        FILE* stream = fopen((gState->dumpDir + "\\seq" + std::to_string(k) + "-times.txt").c_str(), "w");
        if (stream != nullptr) {
            fprintf(stream, "%s\n", times.c_str());
            fclose(stream);
        }
        debugPrint("HD: frame sequence %d: %d frames %dx%d at (%d, %d) in %u ms\n", k, static_cast<int>(gState->seqFrames.size()), w, h, x, y, gState->seqTimes.back() - gState->seqTimes.front());
        gState->seqFrames.clear();
        gState->seqTimes.clear();
    }

    void overlayInit()
    {
        IMGUI_CHECKVERSION();
        ImGui::CreateContext();
        ImGuiIO& io = ImGui::GetIO();
        io.IniFilename = nullptr; // nothing written next to the game
        ImGui::StyleColorsDark();
        ImGuiStyle& style = ImGui::GetStyle();
        style.WindowRounding = 4.0f;
        style.Colors[ImGuiCol_WindowBg].w = 0.85f;
        int windowWidth;
        int windowHeight;
        SDL_GetWindowSize(gState->window, &windowWidth, &windowHeight);
        style.FontScaleMain = windowHeight >= 1400 ? 2.0f : (windowHeight >= 900 ? 1.5f : 1.0f);
        gState->overlayReady = ImGui_ImplSDL2_InitForD3D(gState->window) && ImGui_ImplDX11_Init(gState->device.Get(), gState->context.Get());
    }

    void overlayShutdown()
    {
        if (ImGui::GetCurrentContext() == nullptr) {
            return;
        }
        if (gState->overlayReady) {
            ImGui_ImplDX11_Shutdown();
            ImGui_ImplSDL2_Shutdown();
        }
        ImGui::DestroyContext();
    }

    void overlayDraw()
    {
        ImGui_ImplDX11_NewFrame();
        ImGui_ImplSDL2_NewFrame();
        ImGui::NewFrame();

        ImGui::SetNextWindowPos(ImVec2(10, 10), ImGuiCond_FirstUseEver);
        ImGui::Begin(HD_RELEASE_NAME, nullptr, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoNav);
        ImGui::Text("renderer  d3d11  (Ctrl+F11 hides this)");
        ImGui::Text("screen    %dx%d logical, %dx%d window, vsync %s", gState->width, gState->height, gState->backBufferWidth, gState->backBufferHeight, gState->vsync ? "on" : "off");

        float sum = 0.0f;
        float worst = 0.0f;
        int count = 0;
        for (float ms : gState->frameMs) {
            if (ms > 0.0f) {
                sum += ms;
                worst = ms > worst ? ms : worst;
                count++;
            }
        }
        float average = count > 0 ? sum / count : 0.0f;
        ImGui::Text("frame     %.2f ms average, %.2f ms worst (%.0f fps)", average, worst, average > 0 ? 1000.0f / average : 0.0f);
        if (gState->presentCount > 0) {
            ImGui::Text("present   %.3f ms CPU per frame", gState->presentMsTotal / gState->presentCount);
        }
        ImGui::PlotLines("##frames", gState->frameMs, 240, gState->frameIndex, nullptr, 0.0f, 40.0f, ImVec2(360 * ImGui::GetStyle().FontScaleMain, 60 * ImGui::GetStyle().FontScaleMain));
        overlayDrawGameInfo();
        mapLayerDrawOverlay();
        uiLayerDrawOverlay();
        ImGui::End();

        ImGui::Render();
        ID3D11RenderTargetView* target = gState->backBuffer.Get();
        gState->context->OMSetRenderTargets(1, &target, nullptr);
        D3D11_VIEWPORT viewport = { 0, 0, static_cast<float>(gState->backBufferWidth), static_cast<float>(gState->backBufferHeight), 0.0f, 1.0f };
        gState->context->RSSetViewports(1, &viewport);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
    }

    // Window pixels per logical pixel for the GPU map (1..4).
    int viewportScale(const Viewport& vp)
    {
        int scale = static_cast<int>(vp.width / gState->width + 0.5f);
        return scale < 1 ? 1 : (scale > 4 ? 4 : scale);
    }

    // Draws the 8-bit screen (and the movie overlay) into `target`, with
    // the GPU map composited in if `map` is active and the HD UI windows
    // over it if `ui` is.
    void draw(ID3D11RenderTargetView* target, const Viewport& vp, bool withOverlay, const MapComposite& map, const UiComposite& ui)
    {
        ID3D11DeviceContext* ctx = gState->context.Get();
        ctx->OMSetRenderTargets(1, &target, nullptr);
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->IASetInputLayout(nullptr);
        ctx->VSSetShader(gState->vertexShader.Get(), nullptr, 0);

        D3D11_VIEWPORT viewport = { vp.x, vp.y, vp.width, vp.height, 0.0f, 1.0f };
        ctx->RSSetViewports(1, &viewport);
        ID3D11ShaderResourceView* views[2] = { gState->indexView.Get(), gState->paletteView.Get() };
        ctx->PSSetShaderResources(0, 2, views);
        if (map.active || ui.active) {
            // The fade and gamma come from the same palette fit for both.
            const float* gain = map.active ? map.gain : ui.gain;
            const float* offset = map.active ? map.offset : ui.offset;
            const float* gamma = map.active ? map.gamma : ui.gamma;
            CompositeConstants constants = { { map.isoX, map.isoY }, { map.isoWidth, map.isoHeight }, map.scale, map.tint ? 1 : 0, { 0, 0 }, map.hd != nullptr ? 1 : 0, {}, {} };
            memcpy(constants.gain, gain, sizeof(constants.gain));
            memcpy(constants.gamma, gamma, sizeof(constants.gamma));
            constants.grain = map.grain;
            memcpy(constants.offset, offset, sizeof(constants.offset));
            constants.grainSeed = SDL_GetTicks() / 42;
            ctx->UpdateSubresource(gState->compositeConstants.Get(), 0, nullptr, &constants, 0, 0);
            ID3D11Buffer* buffer = gState->compositeConstants.Get();
            ctx->PSSetConstantBuffers(0, 1, &buffer);
        }
        if (map.active) {
            ID3D11ShaderResourceView* mapViews[4] = { map.mask, map.exact, map.scaled, map.hd };
            ctx->PSSetShaderResources(3, 4, mapViews);
            ctx->PSSetShader(gState->compositeShader.Get(), nullptr, 0);
        } else {
            ctx->PSSetShader(gState->paletteShader.Get(), nullptr, 0);
        }
        ctx->Draw(3, 0);
        if (map.active) {
            ID3D11ShaderResourceView* nullViews[4] = {};
            ctx->PSSetShaderResources(3, 4, nullViews);
        }

        if (ui.active && gState->uiShader) {
            float scaleX = vp.width / gState->width;
            float scaleY = vp.height / gState->height;
            ctx->PSSetShader(gState->uiShader.Get(), nullptr, 0);
            ID3D11Buffer* buffer = gState->uiConstants.Get();
            ctx->PSSetConstantBuffers(1, 1, &buffer);
            ID3D11ShaderResourceView* cells = ui.cells;
            ctx->PSSetShaderResources(7, 1, &cells);
            for (const UiWindowDraw& window : ui.windows) {
                UiWindowConstants constants = { { window.x, window.y }, { window.width, window.height }, window.slot, ui.scale, ui.tint ? 1 : 0, 0 };
                ctx->UpdateSubresource(gState->uiConstants.Get(), 0, nullptr, &constants, 0, 0);
                D3D11_VIEWPORT rect = { vp.x + window.x * scaleX, vp.y + window.y * scaleY, window.width * scaleX, window.height * scaleY, 0.0f, 1.0f };
                ctx->RSSetViewports(1, &rect);
                ID3D11ShaderResourceView* hd = window.hd;
                ctx->PSSetShaderResources(8, 1, &hd);
                ctx->Draw(3, 0);
            }
            ID3D11ShaderResourceView* nullViews[2] = {};
            ctx->PSSetShaderResources(7, 2, nullViews);
            ctx->RSSetViewports(1, &viewport);
        }

        if (withOverlay && gState->overlayActive && gState->overlayView) {
            float scaleX = vp.width / gState->width;
            float scaleY = vp.height / gState->height;
            D3D11_VIEWPORT overlay = {
                vp.x + gState->overlayDest.x * scaleX,
                vp.y + gState->overlayDest.y * scaleY,
                gState->overlayDest.w * scaleX,
                gState->overlayDest.h * scaleY,
                0.0f,
                1.0f,
            };
            ctx->RSSetViewports(1, &overlay);
            ID3D11ShaderResourceView* overlayView = gState->overlayView.Get();
            ctx->PSSetShaderResources(2, 1, &overlayView);
            ID3D11SamplerState* sampler = gState->pointSampler.Get();
            ctx->PSSetSamplers(0, 1, &sampler);
            ctx->PSSetShader(gState->overlayShader.Get(), nullptr, 0);
            ctx->Draw(3, 0);
        }
    }

} // namespace

ID3D11Device* presenterDevice()
{
    return gState != nullptr ? gState->device.Get() : nullptr;
}

ID3D11DeviceContext* presenterContext()
{
    return gState != nullptr ? gState->context.Get() : nullptr;
}

bool presenterVideoMemory(unsigned long long* usage, unsigned long long* budget)
{
    if (gState == nullptr) {
        return false;
    }
    ComPtr<IDXGIDevice> dxgiDevice;
    ComPtr<IDXGIAdapter> adapter;
    ComPtr<IDXGIAdapter3> adapter3;
    if (FAILED(gState->device.As(&dxgiDevice)) || FAILED(dxgiDevice->GetAdapter(&adapter)) || FAILED(adapter.As(&adapter3))) {
        return false;
    }
    DXGI_QUERY_VIDEO_MEMORY_INFO info = {};
    if (FAILED(adapter3->QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &info))) {
        return false;
    }
    *usage = info.CurrentUsage;
    *budget = info.Budget;
    return true;
}

ID3DBlob* compileShader(const char* source, size_t length, const char* name, const char* entry, const char* target)
{
    ID3DBlob* blob = nullptr;
    ComPtr<ID3DBlob> errors;
    HRESULT hr = D3DCompile(source, length, name, nullptr, nullptr, entry, target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &blob, &errors);
    if (FAILED(hr)) {
        debugPrint("HD: shader %s: %s\n", entry, errors ? static_cast<const char*>(errors->GetBufferPointer()) : "compile failed");
        return nullptr;
    }
    return blob;
}

bool presenterRequested()
{
    char* renderer = nullptr;
    configGetString(&gGameConfig, "hd", "renderer", &renderer, "d3d11");
    return renderer != nullptr && SDL_strcasecmp(renderer, "d3d11") == 0;
}

bool presenterActive()
{
    return gState != nullptr;
}

bool presenterCreate(SDL_Window* window, int width, int height)
{
    presenterDestroy();

    SDL_SysWMinfo info;
    SDL_VERSION(&info.version);
    if (!SDL_GetWindowWMInfo(window, &info) || info.subsystem != SDL_SYSWM_WINDOWS) {
        debugPrint("HD: D3D11 presenter needs a Win32 window\n");
        return false;
    }

    gState = new State();
    gState->window = window;
    gState->hwnd = info.info.win.window;
    gState->width = width;
    gState->height = height;

    int vsync = 0;
    configGetInt(&gGameConfig, "hd", "vsync", &vsync, 0);
    gState->vsync = vsync != 0;
    int debugLayer = 0;
    configGetInt(&gGameConfig, "hd_debug", "d3d_debug", &debugLayer, 0);
    configGetInt(&gGameConfig, "hd_debug", "present_selftest_ms", &gState->selfTestIntervalMs, 0);
    configGetInt(&gGameConfig, "hd_debug", "frame_seq_count", &gState->seqCount, 0);
    configGetInt(&gGameConfig, "hd_debug", "frame_seq_step", &gState->seqStep, 1);
    gState->seqStep = std::max(gState->seqStep, 1);
    char* seqRect = nullptr;
    if (configGetString(&gGameConfig, "hd_debug", "frame_seq_rect", &seqRect, "") && seqRect != nullptr) {
        sscanf(seqRect, "%d,%d,%d,%d", &gState->seqRect[0], &gState->seqRect[1], &gState->seqRect[2], &gState->seqRect[3]);
    }
    if (gState->selfTestIntervalMs > 0 || gState->seqCount > 0) {
        char* captureDir = nullptr;
        configGetString(&gGameConfig, "hd", "capture_dir", &captureDir, "hd_captures");
        std::filesystem::path dir = std::filesystem::path(captureDir).parent_path() / "reports" / "present";
        std::error_code error;
        std::filesystem::create_directories(dir, error);
        gState->dumpDir = dir.string();
    }

    UINT flags = debugLayer != 0 ? D3D11_CREATE_DEVICE_DEBUG : 0;
    const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
    // [hd_debug] d3d_warp=1: Microsoft's software rasterizer instead of the GPU,
    // a vendor-neutral check of the shaders (PLAN.md R6; slow).
    int warp = 0;
    configGetInt(&gGameConfig, "hd_debug", "d3d_warp", &warp, 0);
    D3D_DRIVER_TYPE driver = warp != 0 ? D3D_DRIVER_TYPE_WARP : D3D_DRIVER_TYPE_HARDWARE;
    HRESULT hr = D3D11CreateDevice(nullptr, driver, nullptr, flags, levels, 2, D3D11_SDK_VERSION, &gState->device, nullptr, &gState->context);
    if (failed(hr, "D3D11CreateDevice")) {
        presenterDestroy();
        return false;
    }

    ComPtr<IDXGIDevice> dxgiDevice;
    ComPtr<IDXGIAdapter> adapter;
    ComPtr<IDXGIFactory2> factory;
    if (failed(gState->device.As(&dxgiDevice), "IDXGIDevice")
        || failed(dxgiDevice->GetAdapter(&adapter), "GetAdapter")
        || failed(adapter->GetParent(IID_PPV_ARGS(&factory)), "IDXGIFactory2")) {
        presenterDestroy();
        return false;
    }
    // The GPU in the log: the first thing to know in a player's bug report.
    DXGI_ADAPTER_DESC adapterDesc = {};
    if (SUCCEEDED(adapter->GetDesc(&adapterDesc))) {
        char name[128] = {};
        WideCharToMultiByte(CP_UTF8, 0, adapterDesc.Description, -1, name, sizeof(name) - 1, nullptr, nullptr);
        debugPrint("HD: GPU: %s, %llu MB dedicated video memory%s\n", name,
            static_cast<unsigned long long>(adapterDesc.DedicatedVideoMemory >> 20), warp != 0 ? " (WARP, [hd_debug] d3d_warp=1)" : "");
    }

    int windowWidth;
    int windowHeight;
    SDL_GetWindowSize(window, &windowWidth, &windowHeight);

    DXGI_SWAP_CHAIN_DESC1 desc = {};
    desc.Width = windowWidth;
    desc.Height = windowHeight;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    if (failed(factory->CreateSwapChainForHwnd(gState->device.Get(), gState->hwnd, &desc, nullptr, nullptr, &gState->swapChain), "CreateSwapChainForHwnd")) {
        presenterDestroy();
        return false;
    }
    // Fullscreen is SDL's business (window flags), not DXGI's Alt+Enter.
    factory->MakeWindowAssociation(gState->hwnd, DXGI_MWA_NO_ALT_ENTER);

    ComPtr<ID3DBlob> vs;
    ComPtr<ID3DBlob> psPalette;
    ComPtr<ID3DBlob> psOverlay;
    ComPtr<ID3DBlob> psComposite;
    ComPtr<ID3DBlob> psUi;
    if (!compile("vsMain", "vs_5_0", vs) || !compile("psPalette", "ps_5_0", psPalette) || !compile("psOverlay", "ps_5_0", psOverlay) || !compile("psComposite", "ps_5_0", psComposite)
        || !compile("psUi", "ps_5_0", psUi)
        || failed(gState->device->CreateVertexShader(vs->GetBufferPointer(), vs->GetBufferSize(), nullptr, &gState->vertexShader), "CreateVertexShader")
        || failed(gState->device->CreatePixelShader(psPalette->GetBufferPointer(), psPalette->GetBufferSize(), nullptr, &gState->paletteShader), "CreatePixelShader")
        || failed(gState->device->CreatePixelShader(psOverlay->GetBufferPointer(), psOverlay->GetBufferSize(), nullptr, &gState->overlayShader), "CreatePixelShader")
        || failed(gState->device->CreatePixelShader(psComposite->GetBufferPointer(), psComposite->GetBufferSize(), nullptr, &gState->compositeShader), "CreatePixelShader")
        || failed(gState->device->CreatePixelShader(psUi->GetBufferPointer(), psUi->GetBufferSize(), nullptr, &gState->uiShader), "CreatePixelShader")) {
        presenterDestroy();
        return false;
    }

    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(CompositeConstants);
    constants.Usage = D3D11_USAGE_DEFAULT;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_BUFFER_DESC uiConstants = constants;
    uiConstants.ByteWidth = sizeof(UiWindowConstants);
    if (failed(gState->device->CreateBuffer(&constants, nullptr, &gState->compositeConstants), "CreateBuffer")
        || failed(gState->device->CreateBuffer(&uiConstants, nullptr, &gState->uiConstants), "CreateBuffer")) {
        presenterDestroy();
        return false;
    }

    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    if (failed(gState->device->CreateSamplerState(&sampler, &gState->pointSampler), "CreateSamplerState")
        || !createTexture(width, height, DXGI_FORMAT_R8_UINT, D3D11_USAGE_DYNAMIC, D3D11_CPU_ACCESS_WRITE, gState->indexTexture, gState->indexView)
        || !createTexture(256, 1, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_USAGE_DEFAULT, 0, gState->paletteTexture, gState->paletteView)
        || !createBackBuffer()) {
        presenterDestroy();
        return false;
    }

    overlayInit();
    int overlay = 0;
    configGetInt(&gGameConfig, "hd_debug", "overlay", &overlay, 0);
    gState->overlayVisible = overlay != 0 && gState->overlayReady;

    if (mapLayerRequested() && !mapLayerInit()) {
        debugPrint("HD: GPU map layer failed to start, CE draws the map\n");
    }
    if (uiLayerRequested() && !uiLayerInit()) {
        debugPrint("HD: ui layer failed to start, CE draws the UI\n");
    }

    debugPrint("HD: D3D11 presenter up: %dx%d logical, %dx%d window, vsync %d\n", width, height, windowWidth, windowHeight, gState->vsync ? 1 : 0);
    return true;
}

void presenterDestroy()
{
    if (gState == nullptr) {
        return;
    }
    if (gState->presentCount > 0) {
        debugPrint("HD: D3D11 present stats: %d presents, %.3f ms average, %.3f ms max CPU time\n",
            gState->presentCount, gState->presentMsTotal / gState->presentCount, gState->presentMsMax);
    }
    if (gState->selfTestRuns > 0) {
        debugPrint("HD: present self-test summary: %d runs, %d failed\n", gState->selfTestRuns, gState->selfTestFailures);
    }
    uiLayerShutdown();
    mapLayerShutdown();
    overlayShutdown();
    if (gState->context) {
        gState->context->ClearState();
        gState->context->Flush();
    }
    delete gState;
    gState = nullptr;
}

void presenterPresent(SDL_Surface* surface)
{
    if (gState == nullptr || surface == nullptr) {
        return;
    }

    LARGE_INTEGER start;
    QueryPerformanceCounter(&start);

    uploadSurface(surface);

    const float black[4] = { 0, 0, 0, 1 };
    Viewport vp = logicalViewport(gState->backBufferWidth, gState->backBufferHeight);
    MapComposite map = mapLayerPrepare(viewportScale(vp));
    UiComposite ui = uiLayerPrepare(viewportScale(vp));
    gState->context->ClearRenderTargetView(gState->backBuffer.Get(), black);
    draw(gState->backBuffer.Get(), vp, true, map, ui);

    char label[32] = "";
    // No self-test while a frame sequence runs: its full readback stalls the game.
    if (gState->selfTestIntervalMs > 0 && gState->seqRemaining == 0 && SDL_GetTicks() - gState->lastSelfTest >= static_cast<Uint32>(gState->selfTestIntervalMs)) {
        gState->lastSelfTest = SDL_GetTicks();
        snprintf(label, sizeof(label), "auto-%03d", gState->selfTestRuns);
        int differing = presenterSelfTest(surface, label);
        gState->selfTestRuns++;
        if (differing != 0) {
            gState->selfTestFailures++;
        }
        // The self-test drew elsewhere; draw the real frame again.
        map = mapLayerPrepare(viewportScale(vp));
        gState->context->ClearRenderTargetView(gState->backBuffer.Get(), black);
        draw(gState->backBuffer.Get(), vp, true, map, ui);
    }

    Uint64 now = SDL_GetPerformanceCounter();
    if (gState->lastFrame != 0) {
        gState->frameMs[gState->frameIndex] = static_cast<float>((now - gState->lastFrame) * 1000.0 / SDL_GetPerformanceFrequency());
        gState->frameIndex = (gState->frameIndex + 1) % 240;
    }
    gState->lastFrame = now;

    if (gState->overlayVisible) {
        overlayDraw();
    }

    // Exactly what the window is about to show, overlay included.
    if (label[0] != 0) {
        dumpBackBuffer(gState->dumpDir + "\\" + label + ".png");
    }
    frameSequenceTake();

    HRESULT hr = gState->swapChain->Present(gState->vsync ? 1 : 0, 0);

    LARGE_INTEGER end;
    LARGE_INTEGER frequency;
    QueryPerformanceCounter(&end);
    QueryPerformanceFrequency(&frequency);
    double ms = (end.QuadPart - start.QuadPart) * 1000.0 / frequency.QuadPart;
    gState->presentMsTotal += ms;
    gState->presentMsMax = ms > gState->presentMsMax ? ms : gState->presentMsMax;
    gState->presentCount++;
    if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET) {
        debugPrint("HD: D3D11 device lost (0x%08x)\n", static_cast<unsigned>(hr));
    }
}

bool presenterStartFrameSequence()
{
    if (gState == nullptr || gState->seqCount <= 0 || gState->seqRemaining > 0) {
        return false;
    }
    gState->seqRemaining = gState->seqCount;
    gState->seqPresents = 0;
    gState->seqIndex++;
    return true;
}

bool presenterToggleOverlay()
{
    if (gState == nullptr || !gState->overlayReady) {
        return false;
    }
    gState->overlayVisible = !gState->overlayVisible;
    return gState->overlayVisible;
}

void presenterResize()
{
    if (gState == nullptr) {
        return;
    }
    int width;
    int height;
    SDL_GetWindowSize(gState->window, &width, &height);
    if (width <= 0 || height <= 0 || (width == gState->backBufferWidth && height == gState->backBufferHeight)) {
        return;
    }
    gState->context->OMSetRenderTargets(0, nullptr, nullptr);
    gState->backBuffer.Reset();
    if (!failed(gState->swapChain->ResizeBuffers(0, width, height, DXGI_FORMAT_UNKNOWN, 0), "ResizeBuffers")) {
        createBackBuffer();
    }
}

bool presenterOverlayEnsure(int width, int height)
{
    if (gState == nullptr) {
        return false;
    }
    if (gState->overlayTexture && gState->overlayWidth == width && gState->overlayHeight == height) {
        return true;
    }
    presenterOverlayDestroy();
    // ARGB8888 in SDL terms is B,G,R,A bytes in memory.
    if (!createTexture(width, height, DXGI_FORMAT_B8G8R8A8_UNORM, D3D11_USAGE_DYNAMIC, D3D11_CPU_ACCESS_WRITE, gState->overlayTexture, gState->overlayView)) {
        return false;
    }
    gState->overlayWidth = width;
    gState->overlayHeight = height;
    return true;
}

void presenterOverlayUpload(const void* argbPixels, int width, int height, int pitch)
{
    if (gState == nullptr || !gState->overlayTexture) {
        return;
    }
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(gState->context->Map(gState->overlayTexture.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        return;
    }
    for (int y = 0; y < height; y++) {
        memcpy(static_cast<unsigned char*>(mapped.pData) + mapped.RowPitch * y, static_cast<const unsigned char*>(argbPixels) + pitch * y, width * 4);
    }
    gState->context->Unmap(gState->overlayTexture.Get(), 0);
}

void presenterOverlaySet(bool active, const SDL_Rect& dest)
{
    if (gState == nullptr) {
        return;
    }
    gState->overlayActive = active;
    gState->overlayDest = dest;
}

void presenterOverlayDestroy()
{
    if (gState == nullptr) {
        return;
    }
    gState->overlayView.Reset();
    gState->overlayTexture.Reset();
    gState->overlayWidth = 0;
    gState->overlayHeight = 0;
    gState->overlayActive = false;
}

int presenterSelfTest(SDL_Surface* surface, const char* label)
{
    if (gState == nullptr || surface == nullptr) {
        return -1;
    }

    int width = gState->width;
    int height = gState->height;

    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    ComPtr<ID3D11Texture2D> target;
    ComPtr<ID3D11RenderTargetView> targetView;
    desc.Usage = D3D11_USAGE_DEFAULT;
    if (failed(gState->device->CreateTexture2D(&desc, nullptr, &target), "self-test target")
        || failed(gState->device->CreateRenderTargetView(target.Get(), nullptr, &targetView), "self-test view")) {
        return -1;
    }
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    if (failed(gState->device->CreateTexture2D(&desc, nullptr, &staging), "self-test staging")) {
        return -1;
    }

    uploadSurface(surface);
    MapComposite map = mapLayerPrepare(1, true);
    map.tint = false; // the tinted debug view is meant to differ
    draw(targetView.Get(), Viewport { 0, 0, static_cast<float>(width), static_cast<float>(height) }, false, map, UiComposite());
    gState->context->CopyResource(staging.Get(), target.Get());

    // The SDL path's own conversion of the same pixels.
    SDL_Surface* reference = SDL_CreateRGBSurfaceWithFormat(0, width, height, 32, SDL_PIXELFORMAT_RGB888);
    if (reference == nullptr) {
        return -1;
    }
    SDL_BlitSurface(surface, nullptr, reference, nullptr);

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (failed(gState->context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "self-test map")) {
        SDL_FreeSurface(reference);
        return -1;
    }
    int differing = 0;
    int firstX = -1;
    int firstY = -1;
    for (int y = 0; y < height; y++) {
        const unsigned char* gpu = static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * y;
        const Uint32* cpu = reinterpret_cast<const Uint32*>(static_cast<const unsigned char*>(reference->pixels) + reference->pitch * y);
        for (int x = 0; x < width; x++) {
            Uint8 r;
            Uint8 g;
            Uint8 b;
            SDL_GetRGB(cpu[x], reference->format, &r, &g, &b);
            if (gpu[x * 4] != r || gpu[x * 4 + 1] != g || gpu[x * 4 + 2] != b) {
                if (differing == 0) {
                    firstX = x;
                    firstY = y;
                }
                differing++;
            }
        }
    }
    gState->context->Unmap(staging.Get(), 0);
    SDL_FreeSurface(reference);

    if (differing == 0) {
        debugPrint("HD: present self-test '%s': PASS (%dx%d, 0 px differ%s)\n", label, width, height, map.active ? ", GPU map composited" : "");
    } else {
        debugPrint("HD: present self-test '%s': FAIL, %d px differ, first at (%d, %d)\n", label, differing, firstX, firstY);
    }
    return differing;
}

} // namespace fallout::hd

#else // !_WIN32

namespace fallout::hd {

bool presenterRequested() { return false; }
bool presenterActive() { return false; }
bool presenterCreate(SDL_Window*, int, int) { return false; }
void presenterDestroy() { }
void presenterPresent(SDL_Surface*) { }
void presenterResize() { }
bool presenterOverlayEnsure(int, int) { return false; }
void presenterOverlayUpload(const void*, int, int, int) { }
void presenterOverlaySet(bool, const SDL_Rect&) { }
void presenterOverlayDestroy() { }
int presenterSelfTest(SDL_Surface*, const char*) { return -1; }
bool presenterToggleOverlay() { return false; }

} // namespace fallout::hd

#endif // _WIN32
