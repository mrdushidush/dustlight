#include "hd/map_post.h"

#ifdef _WIN32

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <algorithm>
#include <string>
#include <vector>

#include <wrl/client.h>

#include "config.h"
#include "debug.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "imgui.h"

using Microsoft::WRL::ComPtr;

namespace fallout::hd {

namespace {

    constexpr char kShaderSource[] = R"hlsl(
// Post effects on the HD map (PLAN.md P6.3). Bloom: the light pass's bloom
// source is filtered down a chain of half-size levels (13-tap downsample,
// Jimenez 2014) and back up (3x3 tent, each level adding the wider one
// below it), then added "screen"-style. Grade and vignette follow.
cbuffer Post : register(b0) {
    int2 gSize;          // output (and HD map) texels
    int2 gLevelSize;     // the level being written (bloom passes)
    float2 gSourceTexel; // 1 / source size (bloom passes)
    float gSpread;       // weight of the wider level when going up
    float gBloomStrength; // 0 = no bloom
    float3 gWhiteBalance;
    float gSaturation;
    float3 gLift;
    float gVignette;     // 0 = none
    float3 gGamma;
    float gLutSize;      // 0 = no LUT
    float3 gGain;
    int gGrade;          // 0 = no grade
};

Texture2D<float4> gSource : register(t0);
Texture2D<float4> gWide : register(t1);   // csUp: the wider level, already up
Texture2D<float4> gHd : register(t2);
Texture2D<float4> gBloom : register(t3);  // level 0 after the way up
Texture3D<float4> gLut : register(t4);
SamplerState gLinear : register(s0);
RWTexture2D<float4> gOut : register(u0);

float3 tap(float2 uv, float2 offset) { return gSource.SampleLevel(gLinear, uv + offset * gSourceTexel, 0).rgb; }

[numthreads(8, 8, 1)]
void csDown(uint3 id : SV_DispatchThreadID)
{
    if (any(int2(id.xy) >= gLevelSize)) {
        return;
    }
    float2 uv = (float2(id.xy) + 0.5) / float2(gLevelSize);
    float3 e = tap(uv, float2(0, 0));
    float3 corners = tap(uv, float2(-2, -2)) + tap(uv, float2(2, -2)) + tap(uv, float2(-2, 2)) + tap(uv, float2(2, 2));
    float3 edges = tap(uv, float2(0, -2)) + tap(uv, float2(-2, 0)) + tap(uv, float2(2, 0)) + tap(uv, float2(0, 2));
    float3 inner = tap(uv, float2(-1, -1)) + tap(uv, float2(1, -1)) + tap(uv, float2(-1, 1)) + tap(uv, float2(1, 1));
    gOut[id.xy] = float4(e * 0.125 + corners * 0.03125 + edges * 0.0625 + inner * 0.125, 1.0);
}

[numthreads(8, 8, 1)]
void csUp(uint3 id : SV_DispatchThreadID)
{
    if (any(int2(id.xy) >= gLevelSize)) {
        return;
    }
    float2 uv = (float2(id.xy) + 0.5) / float2(gLevelSize);
    float3 wide = 0.0;
    [unroll] for (int y = -1; y <= 1; y++) {
        [unroll] for (int x = -1; x <= 1; x++) {
            float w = (x == 0 ? 2.0 : 1.0) * (y == 0 ? 2.0 : 1.0) / 16.0;
            wide += w * gWide.SampleLevel(gLinear, uv + float2(x, y) * gSourceTexel, 0).rgb;
        }
    }
    gOut[id.xy] = float4(gSource.Load(int3(id.xy, 0)).rgb + gSpread * wide, 1.0);
}

float luma(float3 c) { return dot(c, float3(0.299, 0.587, 0.114)); }

float3 grade(float3 c)
{
    c *= gWhiteBalance;
    c = gGain * (c + gLift * (1.0 - c));
    c = pow(max(c, 0.0), 1.0 / gGamma);
    c = lerp(luma(c).xxx, c, gSaturation);
    if (gLutSize > 0.0) {
        float3 p = saturate(c) * ((gLutSize - 1.0) / gLutSize) + 0.5 / gLutSize;
        c = gLut.SampleLevel(gLinear, p, 0).rgb;
    }
    return max(c, 0.0);
}

[numthreads(8, 8, 1)]
void csPost(uint3 id : SV_DispatchThreadID)
{
    if (any(int2(id.xy) >= gSize)) {
        return;
    }
    float4 hd = gHd.Load(int3(id.xy, 0));
    float3 c = hd.rgb;
    float2 uv = (float2(id.xy) + 0.5) / float2(gSize);
    if (gBloomStrength > 0.0) {
        float3 glow = gBloomStrength * gBloom.SampleLevel(gLinear, uv, 0).rgb;
        c += glow * saturate(1.0 - c);
    }
    if (gGrade != 0) {
        c = grade(c);
    }
    if (gVignette > 0.0) {
        // Elliptical: 0 at the center, 1 in the corners of the map window.
        float d = length((uv - 0.5) * 2.0) * 0.70710678;
        c *= 1.0 - gVignette * smoothstep(0.45, 1.0, d);
    }
    gOut[id.xy] = float4(c, hd.a);
}
)hlsl";

    struct PostConstants {
        int size[2];
        int levelSize[2];
        float sourceTexel[2];
        float spread;
        float bloomStrength;
        float whiteBalance[3];
        float saturation;
        float lift[3];
        float vignette;
        float gamma[3];
        float lutSize;
        float gain[3];
        int grade;
    };
    static_assert(sizeof(PostConstants) == 96, "PostConstants must match the HLSL cbuffer");

    constexpr int kMaxLevels = 6;

    enum Effect : unsigned int {
        kBloom = 1,
        kGrade = 2,
        kVignette = 4,
        kGrain = 8,
    };

    struct EffectName {
        unsigned int bit;
        const char* name;
    };
    constexpr EffectName kEffects[] = {
        { kBloom, "bloom" },
        { kGrade, "grade" },
        { kVignette, "vignette" },
        { kGrain, "grain" },
    };
    constexpr int kEffectCount = sizeof(kEffects) / sizeof(kEffects[0]);

    struct Settings {
        bool enabled = true;
        unsigned int switches = kBloom | kGrade | kVignette | kGrain;
        int cycle = 0; // Ctrl+F5: 0 all on, 1..kEffectCount one off, kEffectCount + 1 all off
        float bloomStrength = 1.5f;
        float bloomSpread = 0.8f;
        float temperature = 0.0f;
        float lift[3] = { 0.0f, 0.0f, 0.0f };
        float gamma[3] = { 1.0f, 1.0f, 1.0f };
        float gain[3] = { 1.0f, 1.0f, 1.0f };
        float saturation = 1.0f;
        std::string lutPath;
        float vignette = 0.2f;
        float grain = 0.025f;
    };

    struct Texture {
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11ShaderResourceView> srv;
        ComPtr<ID3D11UnorderedAccessView> uav;
        int width = 0;
        int height = 0;
    };

    // GPU time of one post draw, read back a few draws later.
    struct Timer {
        ComPtr<ID3D11Query> disjoint;
        ComPtr<ID3D11Query> begin;
        ComPtr<ID3D11Query> end;
        bool pending = false;
    };
    constexpr int kTimers = 4;

    struct State {
        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* context = nullptr;
        Settings settings;
        ComPtr<ID3D11ComputeShader> downCs;
        ComPtr<ID3D11ComputeShader> upCs;
        ComPtr<ID3D11ComputeShader> postCs;
        ComPtr<ID3D11Buffer> constants;
        ComPtr<ID3D11SamplerState> linear;
        Texture down[kMaxLevels];
        Texture up[kMaxLevels];
        int levels = 0;
        Texture output;
        bool outputValid = false;
        ComPtr<ID3D11Texture3D> lut;
        ComPtr<ID3D11ShaderResourceView> lutView;
        int lutSize = 0;
        Timer timers[kTimers];
        int timerNext = 0;
        double gpuMs = 0.0;
        double gpuMsSum = 0.0;
        double gpuMsMax = 0.0;
        int gpuSamples = 0;
        int draws = 0;
    };

    State* gState = nullptr;

    bool failed(HRESULT hr, const char* what)
    {
        if (FAILED(hr)) {
            debugPrint("HD: post %s failed (0x%08x)\n", what, static_cast<unsigned>(hr));
            return true;
        }
        return false;
    }

    std::string configString(const char* key, const char* defaultValue)
    {
        char* value = nullptr;
        configGetString(&gGameConfig, "hd_post", key, &value, defaultValue);
        return value != nullptr ? value : defaultValue;
    }

    void configFloat(const char* key, float* value)
    {
        std::string text = configString(key, "");
        if (!text.empty()) {
            *value = static_cast<float>(atof(text.c_str()));
        }
    }

    // "a,b,c" into up to `count` floats; missing ones keep their value.
    void configFloats(const char* key, float* values, int count)
    {
        std::string text = configString(key, "");
        size_t pos = 0;
        for (int i = 0; i < count && pos < text.size(); i++) {
            size_t comma = text.find(',', pos);
            std::string item = text.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos);
            if (!item.empty()) {
                values[i] = static_cast<float>(atof(item.c_str()));
            }
            if (comma == std::string::npos) {
                break;
            }
            pos = comma + 1;
        }
    }

    void loadSettings(Settings& s)
    {
        int enabled = 1;
        configGetInt(&gGameConfig, "hd_post", "enabled", &enabled, 1);
        s.enabled = enabled != 0;
        for (const EffectName& effect : kEffects) {
            int value = 1;
            configGetInt(&gGameConfig, "hd_post", effect.name, &value, 1);
            s.switches = value != 0 ? (s.switches | effect.bit) : (s.switches & ~effect.bit);
        }
        configFloat("bloom_strength", &s.bloomStrength);
        configFloat("bloom_spread", &s.bloomSpread);
        configFloat("grade_temperature", &s.temperature);
        configFloats("grade_lift", s.lift, 3);
        configFloats("grade_gamma", s.gamma, 3);
        configFloats("grade_gain", s.gain, 3);
        configFloat("grade_saturation", &s.saturation);
        s.lutPath = configString("grade_lut", "");
        configFloat("vignette_strength", &s.vignette);
        configFloat("grain_strength", &s.grain);
    }

    unsigned int effects()
    {
        const Settings& s = gState->settings;
        if (!s.enabled || s.cycle == kEffectCount + 1) {
            return 0;
        }
        unsigned int on = s.switches;
        if (s.cycle >= 1 && s.cycle <= kEffectCount) {
            on &= ~kEffects[s.cycle - 1].bit;
        }
        return on;
    }

    bool createTexture(Texture& t, int width, int height)
    {
        if (t.texture && t.width == width && t.height == height) {
            return true;
        }
        t = Texture();
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = width;
        desc.Height = height;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
        if (failed(gState->device->CreateTexture2D(&desc, nullptr, &t.texture), "texture")
            || failed(gState->device->CreateShaderResourceView(t.texture.Get(), nullptr, &t.srv), "SRV")
            || failed(gState->device->CreateUnorderedAccessView(t.texture.Get(), nullptr, &t.uav), "UAV")) {
            t = Texture();
            return false;
        }
        t.width = width;
        t.height = height;
        return true;
    }

    // The bloom chain for a source of width x height: half size down to
    // about 1/64, or until a side gets shorter than 8 texels.
    bool ensureLevels(int width, int height)
    {
        int w = width;
        int h = height;
        int levels = 0;
        while (levels < kMaxLevels) {
            w = (w + 1) / 2;
            h = (h + 1) / 2;
            if (w < 8 || h < 8) {
                break;
            }
            if (!createTexture(gState->down[levels], w, h) || !createTexture(gState->up[levels], w, h)) {
                return false;
            }
            levels++;
        }
        gState->levels = levels;
        return levels > 0;
    }

    // A .cube file (LUT_3D_SIZE N, then N^3 lines "r g b", red fastest).
    bool loadLut(const std::string& path)
    {
        FILE* stream = fopen(path.c_str(), "r");
        if (stream == nullptr) {
            debugPrint("HD: post: can't open LUT %s\n", path.c_str());
            return false;
        }
        int size = 0;
        std::vector<float> data;
        char line[256];
        while (fgets(line, sizeof(line), stream) != nullptr) {
            float r;
            float g;
            float b;
            if (strncmp(line, "LUT_3D_SIZE", 11) == 0) {
                size = atoi(line + 11);
                data.reserve(static_cast<size_t>(size) * size * size * 4);
            } else if (size > 0 && sscanf(line, "%f %f %f", &r, &g, &b) == 3) {
                data.insert(data.end(), { r, g, b, 1.0f });
            }
        }
        fclose(stream);
        if (size < 2 || size > 128 || data.size() != static_cast<size_t>(size) * size * size * 4) {
            debugPrint("HD: post: LUT %s has %zu entries for size %d\n", path.c_str(), data.size() / 4, size);
            return false;
        }
        D3D11_TEXTURE3D_DESC desc = {};
        desc.Width = desc.Height = desc.Depth = size;
        desc.MipLevels = 1;
        desc.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        desc.Usage = D3D11_USAGE_IMMUTABLE;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA initial = { data.data(), static_cast<UINT>(size * 16), static_cast<UINT>(size * size * 16) };
        if (failed(gState->device->CreateTexture3D(&desc, &initial, &gState->lut), "LUT")
            || failed(gState->device->CreateShaderResourceView(gState->lut.Get(), nullptr, &gState->lutView), "LUT view")) {
            return false;
        }
        gState->lutSize = size;
        debugPrint("HD: post: LUT %s, %d^3\n", path.c_str(), size);
        return true;
    }

    void timerCollect()
    {
        for (Timer& timer : gState->timers) {
            if (!timer.pending) {
                continue;
            }
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint;
            UINT64 begin;
            UINT64 end;
            if (gState->context->GetData(timer.disjoint.Get(), &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK
                || gState->context->GetData(timer.begin.Get(), &begin, sizeof(begin), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK
                || gState->context->GetData(timer.end.Get(), &end, sizeof(end), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                continue;
            }
            timer.pending = false;
            if (!disjoint.Disjoint && disjoint.Frequency > 0 && end >= begin) {
                gState->gpuMs = (end - begin) * 1000.0 / disjoint.Frequency;
                gState->gpuMsSum += gState->gpuMs;
                gState->gpuMsMax = std::max(gState->gpuMsMax, gState->gpuMs);
                gState->gpuSamples++;
            }
        }
    }

    Timer* timerBegin()
    {
        timerCollect();
        Timer& timer = gState->timers[gState->timerNext];
        if (timer.pending) {
            return nullptr;
        }
        if (!timer.disjoint) {
            D3D11_QUERY_DESC disjoint = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
            D3D11_QUERY_DESC stamp = { D3D11_QUERY_TIMESTAMP, 0 };
            if (FAILED(gState->device->CreateQuery(&disjoint, &timer.disjoint)) || FAILED(gState->device->CreateQuery(&stamp, &timer.begin))
                || FAILED(gState->device->CreateQuery(&stamp, &timer.end))) {
                timer = Timer();
                return nullptr;
            }
        }
        gState->timerNext = (gState->timerNext + 1) % kTimers;
        gState->context->Begin(timer.disjoint.Get());
        gState->context->End(timer.begin.Get());
        return &timer;
    }

    void timerEnd(Timer* timer)
    {
        if (timer == nullptr) {
            return;
        }
        gState->context->End(timer->end.Get());
        gState->context->End(timer->disjoint.Get());
        timer->pending = true;
    }

    bool upload(const PostConstants& constants)
    {
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (failed(gState->context->Map(gState->constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped), "Map")) {
            return false;
        }
        memcpy(mapped.pData, &constants, sizeof(constants));
        gState->context->Unmap(gState->constants.Get(), 0);
        return true;
    }

    UINT groups(int size)
    {
        return static_cast<UINT>((size + 7) / 8);
    }

} // namespace

bool mapPostInit()
{
    mapPostShutdown();
    ID3D11Device* device = presenterDevice();
    ID3D11DeviceContext* context = presenterContext();
    if (device == nullptr || context == nullptr) {
        return false;
    }
    gState = new State();
    gState->device = device;
    gState->context = context;
    loadSettings(gState->settings);

    auto compile = [](const char* entry) {
        return compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_post.hlsl", entry, "cs_5_0");
    };
    ComPtr<ID3DBlob> down;
    ComPtr<ID3DBlob> up;
    ComPtr<ID3DBlob> post;
    down.Attach(compile("csDown"));
    up.Attach(compile("csUp"));
    post.Attach(compile("csPost"));
    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(PostConstants);
    constants.Usage = D3D11_USAGE_DYNAMIC;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    constants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    if (!down || !up || !post
        || failed(device->CreateComputeShader(down->GetBufferPointer(), down->GetBufferSize(), nullptr, &gState->downCs), "down CS")
        || failed(device->CreateComputeShader(up->GetBufferPointer(), up->GetBufferSize(), nullptr, &gState->upCs), "up CS")
        || failed(device->CreateComputeShader(post->GetBufferPointer(), post->GetBufferSize(), nullptr, &gState->postCs), "post CS")
        || failed(device->CreateBuffer(&constants, nullptr, &gState->constants), "constants")
        || failed(device->CreateSamplerState(&sampler, &gState->linear), "sampler")) {
        mapPostShutdown();
        return false;
    }
    if (!gState->settings.lutPath.empty()) {
        loadLut(gState->settings.lutPath);
    }
    const Settings& s = gState->settings;
    debugPrint("HD: post effects up, %s, switches 0x%x (bloom %.2f, vignette %.2f, grain %.3f, lut %d)\n", s.enabled ? "on" : "off", s.switches,
        s.bloomStrength, s.vignette, s.grain, gState->lutSize);
    return true;
}

void mapPostShutdown()
{
    if (gState == nullptr) {
        return;
    }
    if (gState->gpuSamples > 0) {
        debugPrint("HD: post GPU ms per draw: mean %.3f, max %.2f over %d draws\n", gState->gpuMsSum / gState->gpuSamples, gState->gpuMsMax, gState->gpuSamples);
    }
    delete gState;
    gState = nullptr;
}

bool mapPostActive()
{
    return gState != nullptr && (effects() & (kBloom | kGrade | kVignette)) != 0;
}

ID3D11ShaderResourceView* mapPostDraw(ID3D11ShaderResourceView* hd, ID3D11ShaderResourceView* bloom, int width, int height)
{
    if (gState == nullptr) {
        return hd;
    }
    gState->outputValid = false;
    const Settings& s = gState->settings;
    unsigned int on = effects();
    bool useBloom = (on & kBloom) != 0 && bloom != nullptr && s.bloomStrength > 0.0f;
    bool useGrade = (on & kGrade) != 0;
    bool useVignette = (on & kVignette) != 0 && s.vignette > 0.0f;
    if (!useBloom && !useGrade && !useVignette) {
        return hd;
    }
    if (!createTexture(gState->output, width, height) || (useBloom && !ensureLevels(width, height))) {
        return hd;
    }

    ID3D11DeviceContext* ctx = gState->context;
    Timer* timer = timerBegin();
    PostConstants constants = {};
    constants.size[0] = width;
    constants.size[1] = height;
    ID3D11Buffer* cb = gState->constants.Get();
    ID3D11SamplerState* sampler = gState->linear.Get();
    ID3D11ShaderResourceView* nullViews[5] = {};
    ID3D11UnorderedAccessView* nullUav = nullptr;
    ctx->CSSetConstantBuffers(0, 1, &cb);
    ctx->CSSetSamplers(0, 1, &sampler);

    if (useBloom) {
        // Down: source -> level 0 -> ... -> level n-1.
        ctx->CSSetShader(gState->downCs.Get(), nullptr, 0);
        for (int i = 0; i < gState->levels; i++) {
            ID3D11ShaderResourceView* source = i == 0 ? bloom : gState->down[i - 1].srv.Get();
            int sw = i == 0 ? width : gState->down[i - 1].width;
            int sh = i == 0 ? height : gState->down[i - 1].height;
            Texture& level = gState->down[i];
            constants.levelSize[0] = level.width;
            constants.levelSize[1] = level.height;
            constants.sourceTexel[0] = 1.0f / sw;
            constants.sourceTexel[1] = 1.0f / sh;
            if (!upload(constants)) {
                return hd;
            }
            ID3D11UnorderedAccessView* uav = level.uav.Get();
            ctx->CSSetShaderResources(0, 1, &source);
            ctx->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
            ctx->Dispatch(groups(level.width), groups(level.height), 1);
            ctx->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
            ctx->CSSetShaderResources(0, 1, nullViews);
        }
        // Up: level i = down i + spread * tent(level i + 1), from the second
        // smallest; the smallest is its own way up.
        ctx->CSSetShader(gState->upCs.Get(), nullptr, 0);
        constants.spread = s.bloomSpread;
        for (int i = gState->levels - 2; i >= 0; i--) {
            Texture& level = gState->up[i];
            ID3D11ShaderResourceView* wide = i + 2 == gState->levels ? gState->down[i + 1].srv.Get() : gState->up[i + 1].srv.Get();
            ID3D11ShaderResourceView* views[2] = { gState->down[i].srv.Get(), wide };
            constants.levelSize[0] = level.width;
            constants.levelSize[1] = level.height;
            constants.sourceTexel[0] = 1.0f / gState->down[i + 1].width;
            constants.sourceTexel[1] = 1.0f / gState->down[i + 1].height;
            if (!upload(constants)) {
                return hd;
            }
            ID3D11UnorderedAccessView* uav = level.uav.Get();
            ctx->CSSetShaderResources(0, 2, views);
            ctx->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
            ctx->Dispatch(groups(level.width), groups(level.height), 1);
            ctx->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
            ctx->CSSetShaderResources(0, 2, nullViews);
        }
    }

    // The up chain sums 1 + spread + spread^2 + ... of the source's energy.
    float norm = 0.0f;
    for (int i = 0, n = std::max(gState->levels, 1); i < n; i++) {
        norm += powf(s.bloomSpread, static_cast<float>(i));
    }
    constants.bloomStrength = useBloom ? s.bloomStrength / std::max(norm, 1e-3f) : 0.0f;
    // White balance: warm raises red and lowers blue, at constant luma.
    float t = std::max(-1.0f, std::min(s.temperature, 1.0f)) * 0.15f;
    float wb[3] = { 1.0f + t, 1.0f, 1.0f - t };
    float wbLuma = 0.299f * wb[0] + 0.587f * wb[1] + 0.114f * wb[2];
    for (int c = 0; c < 3; c++) {
        constants.whiteBalance[c] = wb[c] / wbLuma;
        constants.lift[c] = s.lift[c];
        constants.gamma[c] = std::max(s.gamma[c], 0.05f);
        constants.gain[c] = s.gain[c];
    }
    constants.saturation = s.saturation;
    constants.lutSize = gState->lutView ? static_cast<float>(gState->lutSize) : 0.0f;
    constants.grade = useGrade ? 1 : 0;
    constants.vignette = useVignette ? s.vignette : 0.0f;
    if (!upload(constants)) {
        return hd;
    }
    // Level 0 on the way up (with a single level, the way down's).
    ID3D11ShaderResourceView* glow = nullptr;
    if (useBloom) {
        glow = gState->levels > 1 ? gState->up[0].srv.Get() : gState->down[0].srv.Get();
    }
    ID3D11ShaderResourceView* views[3] = { hd, glow, gState->lutView.Get() };
    ID3D11UnorderedAccessView* output = gState->output.uav.Get();
    ctx->CSSetShader(gState->postCs.Get(), nullptr, 0);
    ctx->CSSetShaderResources(2, 3, views);
    ctx->CSSetUnorderedAccessViews(0, 1, &output, nullptr);
    ctx->Dispatch(groups(width), groups(height), 1);
    ctx->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
    ctx->CSSetShaderResources(0, 5, nullViews);
    timerEnd(timer);
    gState->outputValid = true;
    gState->draws++;
    return gState->output.srv.Get();
}

bool mapPostReadback(std::vector<float>& rgba, int* width, int* height)
{
    if (gState == nullptr || !gState->outputValid) {
        return false;
    }
    D3D11_TEXTURE2D_DESC desc;
    gState->output.texture->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    ComPtr<ID3D11Texture2D> staging;
    if (failed(gState->device->CreateTexture2D(&desc, nullptr, &staging), "staging")) {
        return false;
    }
    gState->context->CopyResource(staging.Get(), gState->output.texture.Get());
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (failed(gState->context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map staging")) {
        return false;
    }
    rgba.resize(static_cast<size_t>(desc.Width) * desc.Height * 4);
    for (UINT y = 0; y < desc.Height; y++) {
        const unsigned short* row = reinterpret_cast<const unsigned short*>(static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * y);
        for (UINT x = 0; x < desc.Width * 4; x++) {
            // IEEE half -> float.
            unsigned short h = row[x];
            unsigned int sign = (h & 0x8000u) << 16;
            unsigned int exponent = (h >> 10) & 0x1F;
            unsigned int mantissa = h & 0x3FF;
            float value;
            if (exponent == 0) {
                value = ldexpf(static_cast<float>(mantissa), -24);
            } else if (exponent == 31) {
                value = mantissa != 0 ? NAN : INFINITY;
            } else {
                value = ldexpf(static_cast<float>(mantissa | 0x400), static_cast<int>(exponent) - 25);
            }
            rgba[static_cast<size_t>(y) * desc.Width * 4 + x] = sign != 0 ? -value : value;
        }
    }
    gState->context->Unmap(staging.Get(), 0);
    *width = static_cast<int>(desc.Width);
    *height = static_cast<int>(desc.Height);
    return true;
}

float mapPostGrain()
{
    if (gState == nullptr || (effects() & kGrain) == 0) {
        return 0.0f;
    }
    return gState->settings.grain;
}

const char* mapPostCycle()
{
    if (gState == nullptr) {
        return "unavailable";
    }
    Settings& s = gState->settings;
    if (!s.enabled) {
        s.enabled = true;
        s.cycle = 0;
        return "post effects on";
    }
    s.cycle = (s.cycle + 1) % (kEffectCount + 2);
    static char name[32];
    if (s.cycle == 0) {
        return "post effects on";
    }
    if (s.cycle == kEffectCount + 1) {
        return "post effects off";
    }
    snprintf(name, sizeof(name), "no %s", kEffects[s.cycle - 1].name);
    return name;
}

void mapPostDrawOverlay()
{
    if (gState == nullptr) {
        return;
    }
    timerCollect();
    const Settings& s = gState->settings;
    unsigned int on = effects();
    ImGui::Text("post      bloom %d grade %d vignette %d grain %d (Ctrl+F5), GPU %.2f ms", (on & kBloom) != 0, (on & kGrade) != 0, (on & kVignette) != 0,
        (on & kGrain) != 0, gState->gpuMs);
    ImGui::Text("          bloom %.2f spread %.2f, %d levels; vignette %.2f; grain %.3f; LUT %d", s.bloomStrength, s.bloomSpread, gState->levels, s.vignette, s.grain,
        gState->lutSize);
}

} // namespace fallout::hd

#endif // _WIN32
