#include "hd/map_lighting.h"

#ifdef _WIN32

#include <math.h>
#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <string>
#include <vector>

#include <wrl/client.h>

#include "config.h"
#include "debug.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "hd/light_scene.h"
#include "hd/map_renderer.h"
#include "imgui.h"

namespace fallout::hd {

using Microsoft::WRL::ComPtr;

namespace {

    // Ground grid around the map window, 1x pixels: tall objects near the
    // bottom edge stand on ground below it.
    constexpr int kGroundMargin = 64;
    constexpr int kGroundMarginBelow = 288;
    // The grids' origin moves with the world in steps of this many 1x px
    // (the top cascade's probe spacing), so scrolling doesn't move texels
    // or bounce-light probes against the world.
    constexpr int kGroundAlign = 32;
    // Per-light shadow maps: half resolution, centered on the light, big
    // enough for CE's largest radius (8 hexes, ~300 x 130 px).
    constexpr int kShadowWidth = 320;
    constexpr int kShadowHeight = 144;
    constexpr int kMaxLights = 64;

    // Feature bits, shared with the shaders.
    enum : unsigned int {
        kNormals = 1,
        kColors = 2,
        kShadows = 4,
        kPool = 8,
        kTonemap = 16,
        kSun = 32,
        kAo = 64,
        kNight = 128,
        kGi = 256,
    };

    constexpr char kShaderSource[] = R"hlsl(
#define F_NORMALS 1
#define F_COLORS 2
#define F_SHADOWS 4
#define F_POOL 8
#define F_TONEMAP 16
#define F_SUN 32
#define F_AO 64
#define F_GI 256

cbuffer Lighting : register(b0) {
    int2 gTargetSize;   // HD target, pixels
    int gScale;
    int gLightCount;
    int2 gGroundOrigin; // ground point (0, 0) = 1x grid texel gGroundOrigin
    int2 gGroundSize;   // 1x grid
    float gAmbient;
    float3 gAmbientColor;
    float gPoolGain;
    float gShadowStrength;
    float gSunStrength;
    float gAoStrength;
    float4 gSun;        // toward the sun, world (u, v, z), unit
    float2 gSunStep;    // screen px per ground px along the sun's ground direction
    float gSunRise;     // screen px the sun ray climbs per ground px
    uint gFeatures;
    float gMix;
    float gFieldSigma;
    float gShadowRange;
    float gFieldMax;    // soft limit on CE's summed light (1 = full), 0 = none
    int2 gFieldSize;    // half-resolution grids (field, occluder max, sun/AO)
    int2 gShadowSize;
    float gKnee;
    float gLightScale;  // share of the light above ambient kept (fades by day)
    float gSunGain;     // sunlit open ground, relative to the original
    float gTonemapWhite; // 0 = the shoulder keeps hue, 1 = per channel (bright colors whiten)
    float gGiStrength;  // bounce light (radiance cascades)
    float gGiGround;    // lit ground's emission per 64 ground px of ray
    float gGiWall;      // lit walls' emission when a ray hits them
    int gCascade;       // the cascade csGiCascade builds
    int2 gCascadeSize;  // every cascade texture, texels
    int gTonemapCurve;  // 0 = P5's shoulder, 1 = AgX
    float gAgxExposure; // AgX: scene-linear scale before the curve
    float gBloomGlow;   // bloom source: share of the glow heuristic's pixels
    float gBloomHot;    // ... and of light above full white
    int gSunSteps;      // sun shadow march: steps of 3 ground px (more when the sun is low)
    float gPad4;
};

struct Hex {
    float2 pos;
    float intensity;
    float pad;
};

struct Light {
    float2 pos;   // 1x px, center of the light's hex
    float z;      // mount height, screen px
    float radius;
    float intensity;
    float step;   // CE's falloff per hex
    int slice;    // shadow map, -1 = none
    float pool;   // 1 = its share gets the pool gain (lamps, fires; not critters)
    float3 color;
    float pad2;
};

StructuredBuffer<Hex> gHexes : register(t0);
Texture2D<uint> gOccluders : register(t1);
Texture2D<float> gOccluderMax : register(t2);
StructuredBuffer<Light> gLights : register(t3);
Texture2D<float4> gAlbedo : register(t4);
Texture2D<float4> gBaseNormal : register(t5);
Texture2D<float4> gDetailNormal : register(t6);
Texture2D<float4> gSurface : register(t7);
Texture2D<float4> gMaterial : register(t8);
Texture2D<float4> gCoverage : register(t9); // coverage, glow, cycle-color bloom weight (both * coverage)
Texture2D<float4> gField : register(t10);
Texture2DArray<float> gShadows : register(t11);
Texture2D<float2> gSunAo : register(t12);
Texture2D<float4> gCascadeUpper : register(t13);
Texture2D<float4> gGiScene : register(t14);
Texture2D<float4> gFluence : register(t15);
SamplerState gLinear : register(s0);

RWTexture2D<float> gOccluderMaxOut : register(u0);
RWTexture2DArray<float> gShadowsOut : register(u1);
RWTexture2D<float2> gSunAoOut : register(u2);
RWTexture2D<float4> gOut : register(u3);
RWTexture2D<float4> gGiOut : register(u4);
RWTexture2D<float4> gBloomOut : register(u5); // bloom source (P6.3), coverage-weighted

static const float kCos30 = 0.8660254;

// --- CE's per-hex light, splatted onto a half-resolution ground grid ------
// Gaussian weights in ground units (v = 2 * screen y), so the field is
// round on the ground; the light pass reads sum(w * I) / sum(w).

struct FieldVs {
    float4 pos : SV_Position;
    float2 ground : GROUND;
    float2 center : CENTER;
    float intensity : INTENSITY;
};

FieldVs vsField(uint vertexId : SV_VertexID, uint instanceId : SV_InstanceID)
{
    static const float2 kCorners[6] = { float2(0, 0), float2(1, 0), float2(0, 1), float2(0, 1), float2(1, 0), float2(1, 1) };
    Hex h = gHexes[instanceId];
    float2 extent = float2(3.0, 1.5) * gFieldSigma;
    float2 g = h.pos + (kCorners[vertexId] * 2.0 - 1.0) * extent;
    float2 t = (g + gGroundOrigin) / 2.0;
    FieldVs o;
    o.pos = float4(t.x / gFieldSize.x * 2.0 - 1.0, 1.0 - t.y / gFieldSize.y * 2.0, 0, 1);
    o.ground = g;
    o.center = h.pos;
    o.intensity = h.intensity;
    return o;
}

float4 psField(FieldVs i) : SV_Target
{
    float2 d = i.ground - i.center;
    d.y *= 2.0;
    float w = exp(-dot(d, d) / (2.0 * gFieldSigma * gFieldSigma));
    return float4(w * i.intensity, w, 0, 0);
}

// --- Occluders: the 1x heightfield max-pooled to half resolution and
// dilated a little, so marches with ~3 texel steps can't miss a 1 px wall.

[numthreads(8, 8, 1)]
void csOccluderMax(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gFieldSize))) {
        return;
    }
    int2 base = int2(id.xy) * 2;
    float h = 0.0;
    for (int y = -2; y <= 3; y++) {
        for (int x = -2; x <= 3; x++) {
            int2 q = base + int2(x, y);
            if (all(q >= 0) && all(q < gGroundSize)) {
                h = max(h, asfloat(gOccluders.Load(int3(q, 0))));
            }
        }
    }
    gOccluderMaxOut[id.xy] = h;
}

float occluderAt(float2 screen)
{
    int2 q = int2(floor((screen + gGroundOrigin) / 2.0));
    return all(q >= 0) && all(q < gFieldSize) ? gOccluderMax.Load(int3(q, 0)) : 0.0;
}

// --- Shadows from lights: per light, a map of the lowest height a point
// needs to see the light, on a half-resolution grid around the light.
// Marches at most gShadowRange toward the light and stops short of it
// (whatever holds the light doesn't block it), like the prototype: longer
// blocking is already in CE's light field.

[numthreads(8, 8, 1)]
void csShadowMaps(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gShadowSize))) {
        return;
    }
    Light light = gLights[id.z];
    float2 p = light.pos + (float2(id.xy) + 0.5 - gShadowSize * 0.5) * 2.0;
    float2 delta = light.pos - p;
    float dist = length(float2(delta.x, 2.0 * delta.y));
    float visible = -8.0;
    if (dist > 1.0) {
        float tMax = min(min(gShadowRange / dist, 1.0), max(1.0 - 24.0 / dist, 0.0));
        [loop] for (int k = 1; k <= 32; k++) {
            float t = tMax * k / 32.0;
            if (dist * t < 4.0) {
                continue; // the point's own object
            }
            float blocker = occluderAt(p + delta * t);
            // Blocked below this height: blocker > h + (z - h) t + 2.
            visible = max(visible, (blocker - light.z * t - 2.0) / (1.0 - t));
        }
    }
    gShadowsOut[uint3(id.xy, id.z)] = min(visible, 4000.0);
}

// --- Sun shadow and ambient occlusion on the half-resolution ground grid:
// r = the lowest height that sees the sun, g = how much sky the ground
// sees (horizon angles of the heightfield in 8 directions).

[numthreads(8, 8, 1)]
void csSunAo(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gFieldSize))) {
        return;
    }
    float2 p = (float2(id.xy) + 0.5) * 2.0 - gGroundOrigin;
    float sun = -8.0;
    if ((gFeatures & F_SUN) != 0) {
        // Steps of 3 ground px (the grid is 2 px): coarser steps print the
        // occluder's footprint once per step, as stairs along the edges.
        // 64 at noon (192 ground px); a low sun casts longer shadows.
        [loop] for (int k = 1; k <= gSunSteps; k++) {
            float s = 3.0 * k; // ground px
            float blocker = occluderAt(p + gSunStep * s);
            sun = max(sun, blocker - s * gSunRise - 2.0);
        }
    }
    float ao = 1.0;
    if ((gFeatures & F_AO) != 0) {
        static const float kSteps[4] = { 3.0, 8.0, 16.0, 30.0 };
        float occlusion = 0.0;
        [unroll] for (int d = 0; d < 8; d++) {
            float angle = d * 0.7853982;
            float2 dir = float2(cos(angle), sin(angle) * 0.5); // ground -> screen
            float horizon = 0.0;
            [unroll] for (int k = 0; k < 4; k++) {
                float blocker = occluderAt(p + dir * kSteps[k]) / kCos30;
                horizon = max(horizon, atan2(blocker, kSteps[k]));
            }
            occlusion += horizon / 1.5707963;
        }
        ao = 1.0 - occlusion / 8.0;
    }
    gSunAoOut[id.xy] = float2(min(sun, 4000.0), ao);
}
)hlsl"
                                     // MSVC caps one string literal at 16 KB; the source continues.
                                     R"hlsl(
// --- Light at a point (light pass and bounce light) -------------------------

// How much of a point at `height` sees past the blockers of a "lowest
// visible height" map: four bilinear taps half a texel apart, for soft edges.
float softVisible(Texture2D<float2> map, float2 uv, float2 texel, float height)
{
    float v = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        float2 o = (float2(k & 1, k >> 1) - 0.5) * texel;
        v += 1.0 - smoothstep(-2.0, 4.0, map.SampleLevel(gLinear, uv + o, 0).r - height);
    }
    return v * 0.25;
}

float softVisibleSlice(float3 uvw, float2 texel, float height)
{
    float v = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        float2 o = (float2(k & 1, k >> 1) - 0.5) * texel;
        v += 1.0 - smoothstep(-2.0, 4.0, gShadows.SampleLevel(gLinear, float3(uvw.xy + o, uvw.z), 0) - height);
    }
    return v * 0.25;
}

float3 decodeNormal(float4 v)
{
    return normalize(v.rgb / max(v.a, 1e-4) * 2.0 - 1.0);
}

float shoulder(float x)
{
    return x <= gKnee ? x : gKnee + (1.0 - gKnee) * (1.0 - exp(-(x - gKnee) / (1.0 - gKnee)));
}

// AgX (Troy Sobotka), with Benjamin Wrensch's polynomial fit of the default
// contrast curve. Our colors are display-referred palette values, so they
// are linearized (2.2) first; the curve's output is display-referred again.
// The matrices are the GLSL reference's (column-major), hence mul(v, M).
float3 agxContrast(float3 x)
{
    float3 x2 = x * x;
    float3 x4 = x2 * x2;
    return 15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x + 0.4298 * x2 + 0.1191 * x - 0.00232;
}

float3 agx(float3 c)
{
    const float3x3 inset = float3x3(
        0.842479062253094, 0.0423282422610123, 0.0423756549057051,
        0.0784335999999992, 0.878468636469772, 0.0784336,
        0.0792237451477643, 0.0791661274605434, 0.879142973793104);
    const float3x3 outset = float3x3(
        1.19687900512017, -0.0528968517574562, -0.0529716355144438,
        -0.0980208811401368, 1.15190312990417, -0.0980434501171241,
        -0.0990297440797205, -0.0989611768448433, 1.15107367264116);
    const float minEv = -12.47393;
    const float maxEv = 4.026069;
    float3 v = mul(pow(max(c, 0.0), 2.2) * gAgxExposure, inset);
    v = (clamp(log2(max(v, 1e-10)), minEv, maxEv) - minEv) / (maxEv - minEv);
    return saturate(mul(agxContrast(v), outset));
}

// CE's light, continuous (psHd): darker below 128, lighter above.
float3 litOriginal(float3 c, float level)
{
    return level <= 128.0 ? c * (level / 128.0) : c + (1.0 - c) * ((level - 128.0) / 128.0);
}

// light.cc's falloff: linear per hex out to the light's radius.
float falloff(Light light, float2 ground)
{
    float k = length((ground - light.pos) / float2(32.0, 13.856));
    return max(light.intensity - max(k - 0.5, 0.0) * light.step, 0.0);
}

// How a surface turns toward a light (fo2hd.compose): half-Lambert of the
// shape normal, plus relief from the detail normal that averages out to 1.
float shapeTerm(float3 n, float3 nRef, float3 toLight, float reliefWeight, float shapeWeight)
{
    float wrap = saturate(dot(n, toLight) * 0.5 + 0.5);
    float wrapRef = saturate(dot(nRef, toLight) * 0.5 + 0.5);
    float relief = clamp((wrap + 0.15) / (wrapRef + 0.15), 0.6, 1.5);
    return (1.0 + reliefWeight * (relief - 1.0)) * (1.0 + shapeWeight * (2.0 * wrapRef - 1.0));
}

// CE's light above ambient at a ground point (uv on the half-resolution
// grid). May exceed 1 near lights (HDR). In daylight CE shows no light pools
// (its light is clamped at full), so most of it fades out by day.
float aboveAt(float2 uv)
{
    float2 field = gField.SampleLevel(gLinear, uv, 0).rg;
    float total = max(field.g > 1e-3 ? field.r / field.g : 0.0, gAmbient);
    // CE clamps each hex at full light; the sum is kept for HDR pools, but
    // where many lights stack (Klamath's toxic caves) it rolls off toward
    // gFieldMax instead of growing without bound.
    if (gFieldMax > 1.0 && total > 1.0) {
        total = 1.0 + (gFieldMax - 1.0) * (1.0 - exp(-(total - 1.0) / (gFieldMax - 1.0)));
    } else if (gFieldMax > 0.0) {
        total = min(total, gFieldMax); // 1 or less: a hard limit (was: none at all)
    }
    return (total - gAmbient) * gLightScale;
}

// The HDR pool boost at a ground point (uv as for aboveAt): strong light
// that stands out from its surroundings (the mean on a ring kPoolRing ground
// px away) is boosted, as under a street lamp at night; even light isn't (a
// cave lit by dozens of lights everywhere stayed 2x the original's
// brightness when every lit point counted as a pool, P6).
static const float kPoolRing = 192.0; // ground px, 6 hexes

float poolBoost(float2 uv, float above)
{
    if ((gFeatures & F_POOL) == 0 || above <= 1e-3) {
        return 1.0;
    }
    float wide = 0.0;
    [unroll] for (int k = 0; k < 8; k++) {
        float angle = k * 0.78539816;
        wide += aboveAt(uv + float2(cos(angle), 0.5 * sin(angle)) * kPoolRing / 2.0 / gFieldSize);
    }
    float pool = saturate(2.0 * (above - wide / 8.0) / above);
    return 1.0 + gPoolGain * above * pool;
}

// The light above ambient, `above`, shared among the lights that reach the
// ground point by CE's falloff, each share shaped by the normal, shadowed
// and colored by its light. Lamp and fire shares get the pool boost
// (poolBoost); a critter's own light doesn't, or the player glows.
// Shadows are looked up at `front`: the ground point moved off the surface
// along its normal (frontOf), so a wall face doesn't shadow itself.
float3 lampLight(float2 ground, float2 front, float height, float above, float boost, float3 n, float3 nRef, float reliefWeight, float shapeWeight)
{
    float3 sum = 0.0;
    float weights = 0.0;
    for (int i = 0; i < gLightCount; i++) {
        Light source = gLights[i];
        float w = falloff(source, ground);
        if (w <= 0.0) {
            continue;
        }
        float3 toLight = normalize(float3(source.pos.x - ground.x, 2.0 * (source.pos.y - ground.y), (source.z - height) / kCos30));
        float shape = shapeTerm(n, nRef, toLight, reliefWeight, shapeWeight);
        float shadow = 1.0;
        if ((gFeatures & F_SHADOWS) != 0 && source.slice >= 0) {
            float2 suv = ((front - source.pos) / 2.0 + gShadowSize * 0.5) / gShadowSize;
            if (all(suv >= 0.0) && all(suv <= 1.0)) {
                float visible = softVisibleSlice(float3(suv, source.slice), 1.0 / gShadowSize, height);
                shadow = 1.0 - gShadowStrength * (1.0 - visible);
            }
        }
        float3 color = (gFeatures & F_COLORS) != 0 ? source.color : float3(1.0, 1.0, 1.0);
        sum += w * lerp(1.0, boost, source.pool) * shape * shadow * color;
        weights += w;
    }
    return above * (weights > 0.0 ? sum / weights : float3(boost, boost, boost));
}

// --- Bounce light (P5.7): 2D radiance cascades on the half-resolution
// ground grid. Only the light above ambient bounces: the original art
// already has its ambient bounce painted in, and by day the lamps fade, so
// day scenes keep their brightness. Emitters are ground and walls lit by
// the lamps (their albedo times lampLight); walls (occluders taller than
// kGiWallHeight) also block. Rays run in ground units (v = 2 * screen y),
// so the light spreads round on the ground.
//
// Cascade i: probes every 2^(i+1) grid texels, 4^(i+1) directions stored as
// a 2^(i+1) square block per probe (so every cascade is the same size),
// rays over [r0 (4^i - 1) / 3, r0 (4^(i+1) - 1) / 3] ground px. Built from
// the top down; each ray that gets through takes the four upper directions
// it covers, interpolated between the four nearest upper probes.

static const int kCascades = 4;
static const float kGiRay0 = 4.0;        // cascade 0 ray length, ground px
static const float kGiStep = 2.0;        // march step, ground px
static const float kGiWallHeight = 16.0; // occluders at least this tall block
static const float3 kGiDefaultAlbedo = float3(0.3, 0.27, 0.24); // off screen

[numthreads(8, 8, 1)]
void csGiScene(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gFieldSize))) {
        return;
    }
    float2 ground = (float2(id.xy) + 0.5) * 2.0 - gGroundOrigin;
    float2 uv = (float2(id.xy) + 0.5) / gFieldSize;
    float wall = gOccluderMax.Load(int3(id.xy, 0));
    bool opaque = wall >= kGiWallHeight;
    float above = aboveAt(uv);
    float3 emission = 0.0;
    if (above > 1e-3) {
        // Walls: lit at half their height (at most 24 px up).
        float height = opaque ? min(wall, 48.0) * 0.5 : 0.0;
        float3 up = float3(0.0, 0.0, 1.0);
        float3 light = lampLight(ground, ground, height, above, poolBoost(uv, above), up, up, 0.0, 0.0);
        float2 screen = float2(ground.x, ground.y - height) * gScale;
        float3 albedo = kGiDefaultAlbedo;
        if (all(screen >= 0.0) && all(screen < float2(gTargetSize))) {
            float4 a = gAlbedo.SampleLevel(gLinear, screen / gTargetSize, 0);
            if (a.a > 0.5) {
                albedo = a.rgb / a.a;
            }
        }
        emission = albedo * light * (opaque ? gGiWall : gGiGround / 64.0);
    }
    gGiOut[id.xy] = float4(emission, opaque ? 1.0 : 0.0);
}

[numthreads(8, 8, 1)]
void csGiCascade(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gCascadeSize))) {
        return;
    }
    int side = 2 << gCascade; // block side = probe spacing in grid texels
    int2 probe = int2(id.xy) / side;
    int2 sub = int2(id.xy) - probe * side;
    int dir = sub.y * side + sub.x;
    float angle = (dir + 0.5) * 6.2831853 / (side * side);
    float2 d = float2(cos(angle), sin(angle));
    float2 perGround = float2(d.x * 0.5, d.y * 0.25); // grid texels per ground px
    float2 center = (float2(probe) + 0.5) * side;
    float start = kGiRay0 * ((1 << (2 * gCascade)) - 1) / 3.0;
    float end = kGiRay0 * ((1 << (2 * gCascade + 2)) - 1) / 3.0;
    // A probe inside a wall (walls are a few texels thick) ignores that
    // wall: its own ground point is the wall's base.
    int2 home = int2(floor(center));
    bool inWall = all(home < gFieldSize) && gGiScene.Load(int3(home, 0)).a > 0.5;

    float3 radiance = 0.0;
    float transmit = 1.0;
    [loop] for (float r = start + kGiStep * 0.5; r < end; r += kGiStep) {
        int2 q = int2(floor(center + perGround * r));
        if (any(q < 0) || any(q >= gFieldSize)) {
            break;
        }
        float4 s = gGiScene.Load(int3(q, 0));
        if (s.a > 0.5) {
            if (inWall && r < 16.0) {
                continue;
            }
            radiance += s.rgb;
            transmit = 0.0;
            break;
        }
        inWall = false;
        radiance += s.rgb * kGiStep;
    }

    if (transmit > 0.0 && gCascade < kCascades - 1) {
        int upperSide = side * 2;
        int2 upperCount = gCascadeSize / upperSide;
        float2 f = center / upperSide - 0.5;
        int2 q0 = int2(floor(f));
        float2 t = f - float2(q0);
        float3 upper = 0.0;
        [unroll] for (int k = 0; k < 4; k++) {
            int2 qp = clamp(q0 + int2(k & 1, k >> 1), int2(0, 0), upperCount - 1);
            float w = ((k & 1) != 0 ? t.x : 1.0 - t.x) * ((k >> 1) != 0 ? t.y : 1.0 - t.y);
            float3 acc = 0.0;
            [unroll] for (int j = 0; j < 4; j++) {
                int e = dir * 4 + j;
                acc += gCascadeUpper.Load(int3(qp * upperSide + int2(e % upperSide, e / upperSide), 0)).rgb;
            }
            upper += w * acc * 0.25;
        }
        radiance += transmit * upper;
    }
    gGiOut[id.xy] = float4(radiance, transmit);
}

// Fluence per cascade-0 probe: the mean over its 4 directions.
[numthreads(8, 8, 1)]
void csGiResolve(uint3 id : SV_DispatchThreadID)
{
    if (any(id.xy >= uint2(gCascadeSize / 2))) {
        return;
    }
    float3 sum = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        sum += gCascadeUpper.Load(int3(int2(id.xy) * 2 + int2(k & 1, k >> 1), 0)).rgb;
    }
    gGiOut[id.xy] = float4(sum * 0.25, 1.0);
}
)hlsl"
                                     // MSVC caps one string literal at 16 KB; the source continues.
                                     R"hlsl(
// --- The light pass ---------------------------------------------------------

// The shadow, sun and AO maps hold one value per ground point, and a wall's
// face shares its ground point with the wall (dilated by csOccluderMax): read
// there, every face would be in its own shadow. Faces read the maps a few px
// in front of themselves along the base normal (ground v = 2 * screen y).
static const float kFrontOffset = 5.0;

float2 frontOf(float2 ground, float3 nRef)
{
    return ground + float2(nRef.x, nRef.y * 0.5) * kFrontOffset;
}

[numthreads(8, 8, 1)]
void csLight(uint3 id : SV_DispatchThreadID)
{
    int2 pix = int2(id.xy);
    if (any(pix >= gTargetSize)) {
        return;
    }
    float4 cover = gCoverage[pix];
    float coverage = cover.r;
    float4 albedo = gAlbedo[pix];
    if (albedo.a <= 0.0) {
        gOut[pix] = float4(0.0, 0.0, 0.0, coverage);
        gBloomOut[pix] = 0.0;
        return;
    }
    float3 c = albedo.rgb / albedo.a;
    float4 material = gMaterial[pix];
    float4 surface = gSurface[pix];
    float ma = max(material.a, 1e-4);
    float sa = max(surface.a, 1e-4);
    float unlit = material.a > 0.0 ? material.r / ma : 1.0;
    float ambientOnly = material.g / ma;
    float glow = saturate(cover.g / max(cover.r, 1e-4));
    float emissive = saturate(cover.b / max(cover.r, 1e-4)); // bloom weight of cycle colors
    float reliefWeight = material.b / ma;
    float height = surface.r / sa;
    float level = surface.g / sa * 128.0;
    float shapeWeight = surface.b / sa;
    float3 nRef = decodeNormal(gBaseNormal[pix]);
    float3 n = decodeNormal(gDetailNormal[pix]);
    if ((gFeatures & F_NORMALS) == 0) {
        reliefWeight = 0.0;
        shapeWeight = 0.0;
    }

    // CE's light at the pixel's ground point.
    float2 p = (float2(pix) + 0.5) / gScale;
    float2 ground = float2(p.x, p.y + height);
    float2 uv = (ground + gGroundOrigin) / 2.0 / gFieldSize;
    float above = aboveAt(uv);
    float2 front = frontOf(ground, nRef);
    float2 frontUv = (front + gGroundOrigin) / 2.0 / gFieldSize;

    float3 light;
    if (ambientOnly >= 0.5) {
        light = gAmbient * gAmbientColor; // roofs: ambient only, as in CE
    } else {
        float sky = 1.0;
        float2 sunAo = gSunAo.SampleLevel(gLinear, frontUv, 0);
        if ((gFeatures & F_AO) != 0) {
            sky *= lerp(1.0, lerp(sunAo.g, 1.0, saturate(height / 24.0)), gAoStrength);
        }
        if ((gFeatures & F_SUN) != 0 && gSunStrength > 0.0) {
            float sunLit = softVisible(gSunAo, frontUv, 1.0 / gFieldSize, height);
            // Art that ignores the shadow loses no light to it, so it gets no gain to make up for it.
            float noShadow = saturate(ambientOnly * 4.0);
            sunLit = lerp(sunLit, 1.0, noShadow);
            // Energy kept where the surface faces the viewer (ADR-0004): the
            // reference is the sun's shape on a surface facing up (floors)
            // or toward the camera (upright art), blended by the normal.
            float up = saturate(nRef.z);
            float3 facing = normalize(float3(0.0, 1.0 - up, up));
            float reference = 1.0 + shapeWeight * (2.0 * saturate(dot(facing, gSun.xyz) * 0.5 + 0.5) - 1.0);
            float shape = shapeTerm(n, nRef, gSun.xyz, reliefWeight, shapeWeight) / reference;
            sky *= (1.0 - gSunStrength) + gSunStrength * lerp(gSunGain, 1.0, noShadow) * shape * sunLit;
        }
        light = gAmbient * gAmbientColor * sky;
        light += lampLight(ground, front, height, above, poolBoost(uv, above), n, nRef, reliefWeight, shapeWeight);
        if ((gFeatures & F_GI) != 0) {
            // Fluence texel j sits at grid texel 2j + 1.
            float2 g = (ground + gGroundOrigin) / 2.0;
            light += gGiStrength * gFluence.SampleLevel(gLinear, g / gCascadeSize, 0).rgb;
        }
    }

    // Flames and neon near a light (the P5 heuristic) never go darker than
    // the art; they also feed the bloom.
    light = lerp(light, max(light, float3(1.0, 1.0, 1.0)), glow);

    float3 result = c * light;
    float3 hot = max(result - 1.0, 0.0);
    if ((gFeatures & F_TONEMAP) != 0 && gTonemapCurve == 1) {
        result = agx(result);
    } else if ((gFeatures & F_TONEMAP) != 0) {
        // The shoulder per channel turns bright reds orange (green catches
        // up); on the largest channel it keeps the hue. A share of the
        // per-channel result lets very bright pools still whiten.
        float peak = max(result.r, max(result.g, result.b));
        float3 hue = peak > gKnee ? result * (shoulder(peak) / peak) : result;
        float3 white = float3(shoulder(result.r), shoulder(result.g), shoulder(result.b));
        result = lerp(hue, white, gTonemapWhite);
    }
    result = lerp(result, litOriginal(c, level), gMix);
    // Unlit: interface, text, and screens and alarm lights (P6.2).
    result = lerp(result, c, saturate(unlit));
    gOut[pix] = float4(result * albedo.a, coverage);
    // Bloom source (P6.3): what glows. Palette-cycle colors at their own
    // color (screens, alarms, fire; slime at half), the glow heuristic's
    // flames and neon as lit, light above full white (hot pools) if asked for.
    float3 bloom = c * emissive + result * glow * gBloomGlow + hot * gBloomHot;
    gBloomOut[pix] = float4(bloom * albedo.a, 0.0);
}
)hlsl";

    struct LightingConstants {
        int targetSize[2];
        int scale;
        int lightCount;
        int groundOrigin[2];
        int groundSize[2];
        float ambient;
        float ambientColor[3];
        float poolGain;
        float shadowStrength;
        float sunStrength;
        float aoStrength;
        float sun[4];
        float sunStep[2];
        float sunRise;
        unsigned int features;
        float mix;
        float fieldSigma;
        float shadowRange;
        float fieldMax;
        int fieldSize[2];
        int shadowSize[2];
        float knee;
        float lightScale;
        float sunGain;
        float tonemapWhite;
        float giStrength;
        float giGround;
        float giWall;
        int cascade;
        int cascadeSize[2];
        int tonemapCurve;
        float agxExposure;
        float bloomGlow;
        float bloomHot;
        int sunSteps;
        float pad4;
    };
    static_assert(sizeof(LightingConstants) == 192, "LightingConstants must match the HLSL cbuffer");

    constexpr int kCascades = 4; // must match the shader

    struct GpuHex {
        float pos[2];
        float intensity;
        float pad;
    };

    struct GpuLight {
        float pos[2];
        float z;
        float radius;
        float intensity;
        float step;
        int slice;
        float pool;
        float color[3];
        float pad2;
    };
    static_assert(sizeof(GpuLight) == 48, "GpuLight must match the HLSL struct");

    // Effects in Ctrl+F6 order, with their [hd_light] switch names.
    struct Effect {
        unsigned int bit;
        const char* name;
    };
    constexpr Effect kEffects[] = {
        { kNormals, "normals" },
        { kColors, "colors" },
        { kShadows, "shadows" },
        { kPool, "pool" },
        { kTonemap, "tonemap" },
        { kSun, "sun" },
        { kAo, "ao" },
        { kNight, "night" },
        { kGi, "gi" },
    };
    constexpr int kEffectCount = sizeof(kEffects) / sizeof(kEffects[0]);

    struct Settings {
        bool newLight = true;
        int quality = 2; // 0 low, 1 medium, 2 high
        unsigned int switches = kNormals | kColors | kShadows | kPool | kTonemap | kSun | kAo | kNight | kGi;
        int effectOff = -1; // Ctrl+F6: index into kEffects, -1 = none
        float poolGain = 0.6f;
        float shadowStrength = 0.5f;
        float shadowRange = 64.0f;
        float sunStrength = 0.3f;
        float sunGain = 1.3f;
        float dayLights = 0.15f;
        float sunDir[3] = { -1.0f, -0.6f, 1.5f };
        // The sun's path through the day (the author, 2026-11-09): sunDir is noon;
        // from 5:00 to 19:00 the sun turns sunSwing degrees around (morning
        // counterclockwise of noon, evening clockwise: east, south, west as
        // the maps' north is up-right) and sinks to sunLow degrees above the
        // horizon, so shadows turn and grow longer. Moderate by default: the
        // art's own painted shadows fall to the lower right whatever the hour.
        // sun_path=0: the fixed sun of 0.9.
        int sunPath = 1;
        float sunSwing = 40.0f;
        float sunLow = 22.0f;
        float aoStrength = 0.4f;
        float nightAmbient = 0.92f;
        float nightTint[3] = { 0.82f, 0.88f, 1.0f };
        float fieldSigma = 14.0f;
        float fieldMax = 1.5f;
        float originalMix = 0.0f;
        float knee = 0.8f;
        float tonemapWhite = 0.3f;
        int tonemapCurve = 0; // 0 = shoulder (P5), 1 = AgX
        float agxExposure = 1.0f;
        float bloomGlow = 0.8f;
        float bloomHot = 0.0f;
        float giStrength = 1.0f;
        float giGround = 0.25f;
        float giWall = 0.5f;
        // Per category: detail strength (fo2hd.hdassets DETAIL_STRENGTH),
        // relief weight and shape weight (fo2hd.compose NORMAL_DETAIL,
        // NORMAL_SHAPE), and how much of the sun's shadow the art ignores.
        // Walls and scenery ignore all of it: the shadow map holds one height
        // per ground point, so a face reads its own wall and its neighbors
        // there, column by column, which drew stripes on walls and bright
        // bands between cliff pieces. They still cast it on what lies around.
        float category[kLightCategoryCount][4] = {
            { 0.0f, 0.0f, 0.0f, 0.0f }, // none
            { 1.2f, 0.8f, 0.3f, 0.0f }, // floor
            { 1.0f, 0.0f, 0.0f, 0.0f }, // roof
            { 0.8f, 0.6f, 0.45f, 1.0f }, // wall
            { 1.0f, 0.6f, 0.3f, 1.0f }, // scenery
            { 0.6f, 0.5f, 0.2f, 0.0f }, // critter
            { 0.8f, 0.5f, 0.2f, 0.0f }, // item
            { 0.8f, 0.5f, 0.2f, 0.0f }, // misc
            // Round scenery (map_renderer.cc roundScenery): its rounded body
            // takes the sun's shape fully; like other scenery it ignores the
            // sun shadow map and still casts into it.
            { 1.0f, 0.6f, 1.0f, 1.0f }, // round
        };
    };

    struct Texture {
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11RenderTargetView> rtv;
        ComPtr<ID3D11ShaderResourceView> srv;
        ComPtr<ID3D11UnorderedAccessView> uav;
        int width = 0;
        int height = 0;
    };

    // GPU time per pass, from timestamp queries read a few frames later.
    constexpr int kTimerPasses = 6;
    constexpr int kTimerFrames = 4;
    const char* const kPassNames[kTimerPasses] = { "gbuffer", "field", "shadows", "sun/ao", "gi", "light" };

    struct TimerFrame {
        ComPtr<ID3D11Query> disjoint;
        ComPtr<ID3D11Query> stamps[kTimerPasses + 1];
        bool pending = false;
    };

    struct State {
        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* context = nullptr;
        Settings settings;

        ComPtr<ID3D11VertexShader> fieldVs;
        ComPtr<ID3D11PixelShader> fieldPs;
        ComPtr<ID3D11ComputeShader> occluderMaxCs;
        ComPtr<ID3D11ComputeShader> shadowMapsCs;
        ComPtr<ID3D11ComputeShader> sunAoCs;
        ComPtr<ID3D11ComputeShader> lightCs;
        ComPtr<ID3D11ComputeShader> giSceneCs;
        ComPtr<ID3D11ComputeShader> giCascadeCs;
        ComPtr<ID3D11ComputeShader> giResolveCs;
        ComPtr<ID3D11BlendState> additive;
        ComPtr<ID3D11SamplerState> linear;
        ComPtr<ID3D11RasterizerState> rasterizer;
        ComPtr<ID3D11Buffer> constants;

        ComPtr<ID3D11Buffer> hexes;
        ComPtr<ID3D11ShaderResourceView> hexesView;
        int hexCapacity = 0;
        ComPtr<ID3D11Buffer> lights;
        ComPtr<ID3D11ShaderResourceView> lightsView;
        int lightCapacity = 0;

        Texture gbuffer[6];
        Texture occluders; // 1x ground grid, R32_UINT
        Texture occluderMax; // half resolution, R16F
        Texture field; // half resolution, RGBA16F
        Texture sunAo; // half resolution, RG16F
        Texture shadows; // array, R16F
        Texture giScene; // half resolution: emission, opaque flag
        Texture cascades[kCascades]; // RGBA16F, all the same size
        Texture fluence; // one texel per cascade-0 probe
        Texture bloom; // bloom source at the HD target's size, RGBA16F (P6.3)
        bool bloomValid = false; // written by the last draw
        int scale = 0;

        TimerFrame timers[kTimerFrames];
        int timerNext = 0;
        double passMs[kTimerPasses] = {};
        double passSumMs[kTimerPasses] = {}; // for the mean at exit
        double passMaxMs[kTimerPasses + 1] = {}; // last = whole frame
        int passFrames = 0;

        // Last frame's inputs, for the overlay.
        int lightCount = 0;
        int shadowedLights = 0;
        int hexCount = 0;
        float ambient = 0.0f;
        float night = 0.0f;
        float day = 0.0f;
        int timeOfDay = 0;
        unsigned int features = 0;
        double cpuMs = 0.0;
    };

    State* gState = nullptr;

    bool failed(HRESULT hr, const char* what)
    {
        if (FAILED(hr)) {
            debugPrint("HD: lighting: %s failed (0x%08x)\n", what, static_cast<unsigned>(hr));
            return true;
        }
        return false;
    }

    std::string configString(const char* key, const char* defaultValue)
    {
        char* value = nullptr;
        configGetString(&gGameConfig, "hd_light", key, &value, defaultValue);
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

    void configSwitch(const char* key, unsigned int bit, unsigned int* switches)
    {
        int value = (*switches & bit) != 0 ? 1 : 0;
        configGetInt(&gGameConfig, "hd_light", key, &value, value);
        *switches = value != 0 ? (*switches | bit) : (*switches & ~bit);
    }

    void loadSettings(Settings& s)
    {
        s.newLight = configString("mode", "new") != "original";
        std::string quality = configString("quality", "high");
        s.quality = quality == "low" ? 0 : (quality == "medium" ? 1 : 2);
        for (const Effect& effect : kEffects) {
            configSwitch(effect.name, effect.bit, &s.switches);
        }
        configFloat("pool_gain", &s.poolGain);
        configFloat("shadow_strength", &s.shadowStrength);
        configFloat("shadow_range", &s.shadowRange);
        configFloat("sun_strength", &s.sunStrength);
        configFloats("sun_dir", s.sunDir, 3);
        configGetInt(&gGameConfig, "hd_light", "sun_path", &s.sunPath, s.sunPath);
        configFloat("sun_swing", &s.sunSwing);
        configFloat("sun_low", &s.sunLow);
        configFloat("sun_gain", &s.sunGain);
        configFloat("day_lights", &s.dayLights);
        configFloat("ao_strength", &s.aoStrength);
        configFloat("night_ambient", &s.nightAmbient);
        configFloats("night_tint", s.nightTint, 3);
        configFloat("field_sigma", &s.fieldSigma);
        configFloat("field_max", &s.fieldMax);
        configFloat("original_mix", &s.originalMix);
        configFloat("knee", &s.knee);
        configFloat("tonemap_white", &s.tonemapWhite);
        s.tonemapCurve = configString("tonemap_curve", "shoulder") == "agx" ? 1 : 0;
        configFloat("agx_exposure", &s.agxExposure);
        configFloat("bloom_glow", &s.bloomGlow);
        configFloat("bloom_hot", &s.bloomHot);
        configFloat("gi_strength", &s.giStrength);
        configFloat("gi_ground", &s.giGround);
        configFloat("gi_wall", &s.giWall);
        const char* names[kLightCategoryCount] = { "none", "floor", "roof", "wall", "scenery", "critter", "item", "misc", "round" };
        for (int i = 1; i < kLightCategoryCount; i++) {
            configFloats(names[i], s.category[i], 4);
        }
    }

    // What the quality tier allows: low = no shadows, sun or AO.
    unsigned int features()
    {
        const Settings& s = gState->settings;
        // low: no shadows, sun, AO or bounce; medium: no bounce; high: all.
        unsigned int allowed = s.quality == 0 ? ~(kShadows | kSun | kAo | kGi) : (s.quality == 1 ? ~kGi : ~0u);
        if (s.effectOff >= 0) {
            allowed &= ~kEffects[s.effectOff].bit;
        }
        return s.switches & allowed;
    }

    bool createTexture(Texture& t, int width, int height, DXGI_FORMAT format, UINT bind, int arraySize = 1)
    {
        if (t.texture && t.width == width && t.height == height) {
            return true;
        }
        t = Texture();
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = width;
        desc.Height = height;
        desc.MipLevels = 1;
        desc.ArraySize = arraySize;
        desc.Format = format;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = bind;
        if (failed(gState->device->CreateTexture2D(&desc, nullptr, &t.texture), "texture")
            || ((bind & D3D11_BIND_RENDER_TARGET) != 0 && failed(gState->device->CreateRenderTargetView(t.texture.Get(), nullptr, &t.rtv), "RTV"))
            || ((bind & D3D11_BIND_SHADER_RESOURCE) != 0 && failed(gState->device->CreateShaderResourceView(t.texture.Get(), nullptr, &t.srv), "SRV"))
            || ((bind & D3D11_BIND_UNORDERED_ACCESS) != 0 && failed(gState->device->CreateUnorderedAccessView(t.texture.Get(), nullptr, &t.uav), "UAV"))) {
            t = Texture();
            return false;
        }
        t.width = width;
        t.height = height;
        return true;
    }

    bool ensureStructured(ComPtr<ID3D11Buffer>& buffer, ComPtr<ID3D11ShaderResourceView>& view, int& capacity, int count, int stride)
    {
        if (count <= capacity && buffer) {
            return true;
        }
        int newCapacity = std::max(count, std::max(256, capacity * 2));
        D3D11_BUFFER_DESC desc = {};
        desc.ByteWidth = newCapacity * stride;
        desc.Usage = D3D11_USAGE_DYNAMIC;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        desc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        desc.StructureByteStride = stride;
        buffer.Reset();
        view.Reset();
        if (failed(gState->device->CreateBuffer(&desc, nullptr, &buffer), "buffer")
            || failed(gState->device->CreateShaderResourceView(buffer.Get(), nullptr, &view), "buffer view")) {
            capacity = 0;
            return false;
        }
        capacity = newCapacity;
        return true;
    }

    bool upload(ID3D11Buffer* buffer, const void* data, size_t bytes)
    {
        if (bytes == 0) {
            return true;
        }
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (failed(gState->context->Map(buffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped), "Map")) {
            return false;
        }
        memcpy(mapped.pData, data, bytes);
        gState->context->Unmap(buffer, 0);
        return true;
    }

    // Every cascade holds the same number of texels (probes shrink by 4 as
    // directions grow by 4): the grid rounded up to the top cascade's
    // probe spacing, 2^kCascades.
    bool createCascades(int gridWidth, int gridHeight)
    {
        const UINT cs = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
        int align = 1 << kCascades;
        int w = (gridWidth + align - 1) / align * align;
        int h = (gridHeight + align - 1) / align * align;
        for (Texture& cascade : gState->cascades) {
            if (!createTexture(cascade, w, h, DXGI_FORMAT_R16G16B16A16_FLOAT, cs)) {
                return false;
            }
        }
        return createTexture(gState->fluence, w / 2, h / 2, DXGI_FORMAT_R16G16B16A16_FLOAT, cs);
    }

    bool ensureTargets(int width, int height, int scale)
    {
        const UINT rt = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        const UINT cs = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
        int w = width * scale;
        int h = height * scale;
        int gw = width + 2 * kGroundMargin + kGroundAlign;
        int gh = height + kGroundMargin + kGroundMarginBelow + kGroundAlign;
        int fw = (gw + 1) / 2;
        int fh = (gh + 1) / 2;
        gState->scale = scale;
        return createTexture(gState->bloom, w, h, DXGI_FORMAT_R16G16B16A16_FLOAT, cs)
            && createTexture(gState->gbuffer[0], w, h, DXGI_FORMAT_R16G16B16A16_FLOAT, rt)
            && createTexture(gState->gbuffer[1], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, rt)
            && createTexture(gState->gbuffer[2], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, rt)
            && createTexture(gState->gbuffer[3], w, h, DXGI_FORMAT_R16G16B16A16_FLOAT, rt)
            && createTexture(gState->gbuffer[4], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, rt)
            && createTexture(gState->gbuffer[5], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, rt)
            && createTexture(gState->occluders, gw, gh, DXGI_FORMAT_R32_UINT, cs)
            && createTexture(gState->occluderMax, fw, fh, DXGI_FORMAT_R16_FLOAT, cs)
            && createTexture(gState->field, fw, fh, DXGI_FORMAT_R16G16B16A16_FLOAT, rt)
            && createTexture(gState->sunAo, fw, fh, DXGI_FORMAT_R16G16_FLOAT, cs)
            && createTexture(gState->shadows, kShadowWidth, kShadowHeight, DXGI_FORMAT_R16_FLOAT, cs, kMaxLights)
            && createTexture(gState->giScene, fw, fh, DXGI_FORMAT_R16G16B16A16_FLOAT, cs)
            && createCascades(fw, fh);
    }

    // Night: 1 from 20:00 to 05:00, ramps over the two hours either side;
    // only where the ambient is actually dimmed (caves stay as they are).
    float nightAmount(int timeOfDay, float ambient)
    {
        float hour = (timeOfDay / 100) + (timeOfDay % 100) / 60.0f;
        float night;
        if (hour >= 20.0f || hour < 5.0f) {
            night = 1.0f;
        } else if (hour >= 18.0f) {
            night = (hour - 18.0f) / 2.0f;
        } else if (hour < 7.0f) {
            night = (7.0f - hour) / 2.0f;
        } else {
            night = 0.0f;
        }
        return night * std::min(std::max((0.95f - ambient) / 0.15f, 0.0f), 1.0f);
    }

    // Daylight for the sun: bright ambient outside the night hours.
    float dayAmount(int timeOfDay, float ambient)
    {
        float hour = (timeOfDay / 100) + (timeOfDay % 100) / 60.0f;
        float day = hour >= 7.0f && hour < 18.0f ? 1.0f : 0.0f;
        if (hour >= 5.0f && hour < 7.0f) {
            day = (hour - 5.0f) / 2.0f;
        } else if (hour >= 18.0f && hour < 20.0f) {
            day = (20.0f - hour) / 2.0f;
        }
        return day * std::min(std::max((ambient - 0.75f) / 0.2f, 0.0f), 1.0f);
    }

    // Toward the sun (unit, world u v z) at this time of day: Settings::sunDir
    // at noon, turned and lowered toward 5:00 and 19:00 when sunPath is on.
    void sunDirection(const Settings& s, int timeOfDay, float out[3])
    {
        float length = sqrtf(s.sunDir[0] * s.sunDir[0] + s.sunDir[1] * s.sunDir[1] + s.sunDir[2] * s.sunDir[2]);
        for (int k = 0; k < 3; k++) {
            out[k] = length > 0.0f ? s.sunDir[k] / length : (k == 2 ? 1.0f : 0.0f);
        }
        if (s.sunPath == 0) {
            return;
        }
        const float degree = 0.017453293f;
        float hour = (timeOfDay / 100) + (timeOfDay % 100) / 60.0f;
        float t = std::min(std::max((hour - 12.0f) / 7.0f, -1.0f), 1.0f);
        float ground = sqrtf(out[0] * out[0] + out[1] * out[1]);
        float noonHeight = atan2f(out[2], ground);
        float low = std::min(s.sunLow * degree, noonHeight);
        float height = low + (noonHeight - low) * cosf(t * 1.5707963f);
        // u right, v down the screen: a growing angle turns clockwise on screen.
        float turn = (ground > 1e-4f ? atan2f(out[1], out[0]) : 0.0f) + s.sunSwing * degree * t;
        out[0] = cosf(height) * cosf(turn);
        out[1] = cosf(height) * sinf(turn);
        out[2] = sinf(height);
    }

    void timerStamp(TimerFrame* frame, int index)
    {
        if (frame != nullptr) {
            gState->context->End(frame->stamps[index].Get());
        }
    }

    void timersCollect()
    {
        for (TimerFrame& frame : gState->timers) {
            if (!frame.pending) {
                continue;
            }
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint;
            if (gState->context->GetData(frame.disjoint.Get(), &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                continue;
            }
            UINT64 stamps[kTimerPasses + 1];
            bool ready = true;
            for (int i = 0; i <= kTimerPasses; i++) {
                if (gState->context->GetData(frame.stamps[i].Get(), &stamps[i], sizeof(UINT64), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                    ready = false;
                    break;
                }
            }
            if (!ready) {
                continue;
            }
            frame.pending = false;
            if (disjoint.Disjoint || disjoint.Frequency == 0) {
                continue;
            }
            double total = 0.0;
            for (int i = 0; i < kTimerPasses; i++) {
                double ms = static_cast<double>(stamps[i + 1] - stamps[i]) * 1000.0 / disjoint.Frequency;
                gState->passMs[i] = ms;
                gState->passSumMs[i] += ms;
                gState->passMaxMs[i] = std::max(gState->passMaxMs[i], ms);
                total += ms;
            }
            gState->passMaxMs[kTimerPasses] = std::max(gState->passMaxMs[kTimerPasses], total);
            gState->passFrames++;
        }
    }

    TimerFrame* timerBegin()
    {
        timersCollect();
        TimerFrame& frame = gState->timers[gState->timerNext];
        if (frame.pending) {
            return nullptr;
        }
        if (!frame.disjoint) {
            D3D11_QUERY_DESC disjoint = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
            D3D11_QUERY_DESC stamp = { D3D11_QUERY_TIMESTAMP, 0 };
            if (FAILED(gState->device->CreateQuery(&disjoint, &frame.disjoint))) {
                return nullptr;
            }
            for (auto& query : frame.stamps) {
                if (FAILED(gState->device->CreateQuery(&stamp, &query))) {
                    frame = TimerFrame();
                    return nullptr;
                }
            }
        }
        gState->timerNext = (gState->timerNext + 1) % kTimerFrames;
        gState->context->Begin(frame.disjoint.Get());
        gState->context->End(frame.stamps[0].Get());
        return &frame;
    }

    void timerEnd(TimerFrame* frame)
    {
        if (frame != nullptr) {
            gState->context->End(frame->disjoint.Get());
            frame->pending = true;
        }
    }

} // namespace

bool mapLightingInit()
{
    mapLightingShutdown();
    ID3D11Device* device = presenterDevice();
    ID3D11DeviceContext* context = presenterContext();
    if (device == nullptr || context == nullptr) {
        return false;
    }
    gState = new State();
    gState->device = device;
    gState->context = context;
    loadSettings(gState->settings);

    auto compile = [](const char* entry, const char* target) {
        return compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_lighting.hlsl", entry, target);
    };
    ComPtr<ID3DBlob> fieldVs;
    ComPtr<ID3DBlob> fieldPs;
    ComPtr<ID3DBlob> occluderMax;
    ComPtr<ID3DBlob> shadowMaps;
    ComPtr<ID3DBlob> sunAo;
    ComPtr<ID3DBlob> light;
    ComPtr<ID3DBlob> giScene;
    ComPtr<ID3DBlob> giCascade;
    ComPtr<ID3DBlob> giResolve;
    fieldVs.Attach(compile("vsField", "vs_5_0"));
    fieldPs.Attach(compile("psField", "ps_5_0"));
    occluderMax.Attach(compile("csOccluderMax", "cs_5_0"));
    shadowMaps.Attach(compile("csShadowMaps", "cs_5_0"));
    sunAo.Attach(compile("csSunAo", "cs_5_0"));
    light.Attach(compile("csLight", "cs_5_0"));
    giScene.Attach(compile("csGiScene", "cs_5_0"));
    giCascade.Attach(compile("csGiCascade", "cs_5_0"));
    giResolve.Attach(compile("csGiResolve", "cs_5_0"));

    D3D11_BLEND_DESC additive = {};
    additive.RenderTarget[0].BlendEnable = TRUE;
    additive.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    additive.RenderTarget[0].DestBlend = D3D11_BLEND_ONE;
    additive.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    additive.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    additive.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
    additive.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    additive.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    D3D11_RASTERIZER_DESC rasterizer = {};
    rasterizer.FillMode = D3D11_FILL_SOLID;
    rasterizer.CullMode = D3D11_CULL_NONE;
    rasterizer.DepthClipEnable = TRUE;
    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(LightingConstants);
    constants.Usage = D3D11_USAGE_DYNAMIC;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    constants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;

    if (!fieldVs || !fieldPs || !occluderMax || !shadowMaps || !sunAo || !light || !giScene || !giCascade || !giResolve
        || failed(device->CreateVertexShader(fieldVs->GetBufferPointer(), fieldVs->GetBufferSize(), nullptr, &gState->fieldVs), "field VS")
        || failed(device->CreatePixelShader(fieldPs->GetBufferPointer(), fieldPs->GetBufferSize(), nullptr, &gState->fieldPs), "field PS")
        || failed(device->CreateComputeShader(occluderMax->GetBufferPointer(), occluderMax->GetBufferSize(), nullptr, &gState->occluderMaxCs), "occluder max CS")
        || failed(device->CreateComputeShader(shadowMaps->GetBufferPointer(), shadowMaps->GetBufferSize(), nullptr, &gState->shadowMapsCs), "shadow maps CS")
        || failed(device->CreateComputeShader(sunAo->GetBufferPointer(), sunAo->GetBufferSize(), nullptr, &gState->sunAoCs), "sun/AO CS")
        || failed(device->CreateComputeShader(light->GetBufferPointer(), light->GetBufferSize(), nullptr, &gState->lightCs), "light CS")
        || failed(device->CreateComputeShader(giScene->GetBufferPointer(), giScene->GetBufferSize(), nullptr, &gState->giSceneCs), "GI scene CS")
        || failed(device->CreateComputeShader(giCascade->GetBufferPointer(), giCascade->GetBufferSize(), nullptr, &gState->giCascadeCs), "GI cascade CS")
        || failed(device->CreateComputeShader(giResolve->GetBufferPointer(), giResolve->GetBufferSize(), nullptr, &gState->giResolveCs), "GI resolve CS")
        || failed(device->CreateBlendState(&additive, &gState->additive), "additive blend")
        || failed(device->CreateSamplerState(&sampler, &gState->linear), "sampler")
        || failed(device->CreateRasterizerState(&rasterizer, &gState->rasterizer), "rasterizer")
        || failed(device->CreateBuffer(&constants, nullptr, &gState->constants), "constants")) {
        mapLightingShutdown();
        return false;
    }

    mapRendererWantShapes(gState->settings.newLight);
    const Settings& s = gState->settings;
    debugPrint("HD: lighting up, mode %s, quality %d, switches 0x%x\n", s.newLight ? "new" : "original", s.quality, s.switches);
    return true;
}

void mapLightingShutdown()
{
    if (gState != nullptr && gState->passFrames > 0) {
        char line[256];
        int n = snprintf(line, sizeof(line), "HD: lighting GPU ms per pass over %d draws, mean (max):", gState->passFrames);
        double total = 0.0;
        for (int i = 0; i < kTimerPasses && n < static_cast<int>(sizeof(line)); i++) {
            double mean = gState->passSumMs[i] / gState->passFrames;
            total += mean;
            n += snprintf(line + n, sizeof(line) - n, " %s %.3f (%.2f)", kPassNames[i], mean, gState->passMaxMs[i]);
        }
        debugPrint("%s; total %.3f (%.2f)\n", line, total, gState->passMaxMs[kTimerPasses]);
    }
    delete gState;
    gState = nullptr;
}

bool mapLightingActive()
{
    return gState != nullptr && gState->settings.newLight;
}

const char* mapLightingToggle()
{
    if (gState == nullptr) {
        return "lighting unavailable";
    }
    gState->settings.newLight = !gState->settings.newLight;
    mapRendererWantShapes(gState->settings.newLight);
    return gState->settings.newLight ? "new light" : "original light";
}

const char* mapLightingCycleEffect()
{
    if (gState == nullptr) {
        return "lighting unavailable";
    }
    int& off = gState->settings.effectOff;
    off = off + 1 >= kEffectCount ? -1 : off + 1;
    static char name[32];
    snprintf(name, sizeof(name), off < 0 ? "all effects" : "no %s", off < 0 ? "" : kEffects[off].name);
    return name;
}

bool mapLightingDraw(int mapWidth, int mapHeight, int scale, const float* palette)
{
    if (gState == nullptr || mapWidth <= 0 || mapHeight <= 0) {
        return false;
    }
    LARGE_INTEGER start;
    LARGE_INTEGER frequency;
    QueryPerformanceCounter(&start);
    QueryPerformanceFrequency(&frequency);

    ID3D11UnorderedAccessView* output = mapRendererHdUav(scale);
    if (output == nullptr || !ensureTargets(mapWidth, mapHeight, scale)) {
        return false;
    }
    const Settings& s = gState->settings;
    unsigned int enabled = features();

    // Inputs from CE.
    const LightScene& scene = lightSceneGather(mapWidth, mapHeight, kGroundMargin, kGroundMarginBelow, palette);
    // Ground (0, 0) = grid texel origin; texel edges stay put in the world.
    int origin[2] = {
        kGroundMargin + ((-scene.worldX) % kGroundAlign + kGroundAlign) % kGroundAlign,
        kGroundMargin + ((-scene.worldY) % kGroundAlign + kGroundAlign) % kGroundAlign,
    };
    std::vector<GpuHex> hexes;
    hexes.reserve(scene.hexes.size());
    for (const SceneHex& hex : scene.hexes) {
        hexes.push_back({ { hex.x, hex.y }, hex.intensity, 0.0f });
    }
    std::vector<GpuLight> lights;
    for (const SceneLight& light : scene.lights) {
        if (static_cast<int>(lights.size()) >= kMaxLights) {
            break;
        }
        GpuLight g = {};
        g.pos[0] = light.x;
        g.pos[1] = light.y;
        g.z = light.z;
        g.radius = light.radius;
        g.intensity = light.intensity;
        g.step = (light.intensity - 655.0f / 65536.0f) / (light.radius + 1.0f);
        g.slice = static_cast<int>(lights.size());
        g.pool = light.kind == LightKind::Critter ? 0.0f : 1.0f;
        memcpy(g.color, light.color, sizeof(g.color));
        lights.push_back(g);
    }
    if (!ensureStructured(gState->hexes, gState->hexesView, gState->hexCapacity, std::max(static_cast<int>(hexes.size()), 1), sizeof(GpuHex))
        || !ensureStructured(gState->lights, gState->lightsView, gState->lightCapacity, std::max(static_cast<int>(lights.size()), 1), sizeof(GpuLight))
        || !upload(gState->hexes.Get(), hexes.data(), hexes.size() * sizeof(GpuHex))
        || !upload(gState->lights.Get(), lights.data(), lights.size() * sizeof(GpuLight))) {
        return false;
    }

    float night = (enabled & kNight) != 0 ? nightAmount(scene.timeOfDay, scene.ambient) : 0.0f;
    float daylight = dayAmount(scene.timeOfDay, scene.ambient);
    float day = (enabled & kSun) != 0 ? daylight : 0.0f;
    LightingConstants constants = {};
    constants.targetSize[0] = mapWidth * scale;
    constants.targetSize[1] = mapHeight * scale;
    constants.scale = scale;
    constants.lightCount = static_cast<int>(lights.size());
    constants.groundOrigin[0] = origin[0];
    constants.groundOrigin[1] = origin[1];
    constants.groundSize[0] = gState->occluders.width;
    constants.groundSize[1] = gState->occluders.height;
    constants.ambient = scene.ambient;
    for (int k = 0; k < 3; k++) {
        constants.ambientColor[k] = 1.0f + night * (s.nightAmbient * s.nightTint[k] - 1.0f);
    }
    constants.poolGain = s.poolGain;
    constants.shadowStrength = s.shadowStrength;
    constants.sunStrength = s.sunStrength * day;
    constants.aoStrength = s.aoStrength;
    float sun[3];
    sunDirection(s, scene.timeOfDay, sun);
    float sunGround = std::max(sqrtf(sun[0] * sun[0] + sun[1] * sun[1]), 1e-3f);
    constants.sun[0] = sun[0];
    constants.sun[1] = sun[1];
    constants.sun[2] = sun[2];
    constants.sunStep[0] = sun[0] / sunGround;
    constants.sunStep[1] = sun[1] / sunGround * 0.5f;
    constants.sunRise = sun[2] / sunGround * 0.8660254f;
    // Long enough for a 200 px tall blocker's whole shadow: 64 steps (192
    // ground px) at noon as in 0.9, at most 160 (480 px) when the sun is low.
    constants.sunSteps = std::min(std::max(static_cast<int>(ceilf(200.0f / (3.0f * std::max(constants.sunRise, 1e-3f)))), 64), 160);
    constants.features = enabled;
    constants.mix = s.originalMix;
    constants.fieldSigma = s.fieldSigma;
    constants.shadowRange = s.shadowRange;
    constants.fieldMax = s.fieldMax;
    constants.fieldSize[0] = gState->field.width;
    constants.fieldSize[1] = gState->field.height;
    constants.shadowSize[0] = kShadowWidth;
    constants.shadowSize[1] = kShadowHeight;
    constants.knee = s.knee;
    constants.tonemapWhite = s.tonemapWhite;
    constants.tonemapCurve = s.tonemapCurve;
    constants.agxExposure = s.agxExposure;
    constants.bloomGlow = s.bloomGlow;
    constants.bloomHot = s.bloomHot;
    constants.lightScale = 1.0f - daylight * (1.0f - s.dayLights);
    constants.sunGain = s.sunGain;
    constants.giStrength = s.giStrength;
    constants.giGround = s.giGround;
    constants.giWall = s.giWall;
    constants.cascadeSize[0] = gState->cascades[0].width;
    constants.cascadeSize[1] = gState->cascades[0].height;
    if (!upload(gState->constants.Get(), &constants, sizeof(constants))) {
        return false;
    }

    ID3D11DeviceContext* ctx = gState->context;
    TimerFrame* timer = timerBegin();

    // G-buffer and occluder heightfield.
    const UINT zero[4] = {};
    ctx->ClearUnorderedAccessViewUint(gState->occluders.uav.Get(), zero);
    MapGbufferDraw draw;
    for (int i = 0; i < 6; i++) {
        draw.targets[i] = gState->gbuffer[i].rtv.Get();
    }
    draw.occluders = gState->occluders.uav.Get();
    draw.occluderOrigin[0] = origin[0];
    draw.occluderOrigin[1] = origin[1];
    draw.occluderSize[0] = gState->occluders.width;
    draw.occluderSize[1] = gState->occluders.height;
    memcpy(draw.category, s.category, sizeof(draw.category));
    // Sobel on the tier's pixels -> the 4x masters' units (hdassets).
    draw.detailScale = scale / 4.0f;
    draw.sun = (enabled & kSun) != 0;
    if (!mapRendererDrawGbuffer(scale, palette, draw)) {
        timerEnd(timer);
        return false;
    }
    timerStamp(timer, 1);

    ID3D11Buffer* cb = gState->constants.Get();
    ID3D11ShaderResourceView* nullViews[16] = {};
    ID3D11UnorderedAccessView* nullUavs[6] = {};

    // CE's light field.
    const float clear[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    ctx->ClearRenderTargetView(gState->field.rtv.Get(), clear);
    if (!hexes.empty()) {
        ID3D11ShaderResourceView* hexView = gState->hexesView.Get();
        ID3D11RenderTargetView* rtv = gState->field.rtv.Get();
        D3D11_VIEWPORT viewport = { 0, 0, static_cast<float>(gState->field.width), static_cast<float>(gState->field.height), 0.0f, 1.0f };
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->IASetInputLayout(nullptr);
        ctx->VSSetShader(gState->fieldVs.Get(), nullptr, 0);
        ctx->VSSetShaderResources(0, 1, &hexView);
        ctx->VSSetConstantBuffers(0, 1, &cb);
        ctx->PSSetShader(gState->fieldPs.Get(), nullptr, 0);
        ctx->PSSetConstantBuffers(0, 1, &cb);
        ctx->RSSetState(gState->rasterizer.Get());
        ctx->RSSetViewports(1, &viewport);
        ctx->OMSetBlendState(gState->additive.Get(), nullptr, 0xFFFFFFFF);
        ctx->OMSetDepthStencilState(nullptr, 0);
        ctx->OMSetRenderTargets(1, &rtv, nullptr);
        ctx->DrawInstanced(6, static_cast<UINT>(hexes.size()), 0, 0);
        ctx->OMSetRenderTargets(0, nullptr, nullptr);
        ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        ctx->VSSetShaderResources(0, 1, nullViews);
        ctx->RSSetState(nullptr);
    }
    timerStamp(timer, 2);

    auto groups = [](int size) { return static_cast<UINT>((size + 7) / 8); };
    ctx->CSSetConstantBuffers(0, 1, &cb);

    // Occluders, half resolution; shadow maps per light.
    ID3D11ShaderResourceView* occluderView = gState->occluders.srv.Get();
    ID3D11UnorderedAccessView* occluderMaxUav = gState->occluderMax.uav.Get();
    ctx->CSSetShader(gState->occluderMaxCs.Get(), nullptr, 0);
    ctx->CSSetShaderResources(1, 1, &occluderView);
    ctx->CSSetUnorderedAccessViews(0, 1, &occluderMaxUav, nullptr);
    ctx->Dispatch(groups(gState->occluderMax.width), groups(gState->occluderMax.height), 1);
    ctx->CSSetUnorderedAccessViews(0, 1, nullUavs, nullptr);
    ctx->CSSetShaderResources(1, 1, nullViews);

    ID3D11ShaderResourceView* occluderMaxView = gState->occluderMax.srv.Get();
    ID3D11ShaderResourceView* lightsView = gState->lightsView.Get();
    ctx->CSSetShaderResources(2, 1, &occluderMaxView);
    ctx->CSSetShaderResources(3, 1, &lightsView);
    if ((enabled & kShadows) != 0 && !lights.empty()) {
        ID3D11UnorderedAccessView* shadowsUav = gState->shadows.uav.Get();
        ctx->CSSetShader(gState->shadowMapsCs.Get(), nullptr, 0);
        ctx->CSSetUnorderedAccessViews(1, 1, &shadowsUav, nullptr);
        ctx->Dispatch(groups(kShadowWidth), groups(kShadowHeight), static_cast<UINT>(lights.size()));
        ctx->CSSetUnorderedAccessViews(1, 1, nullUavs, nullptr);
    }
    timerStamp(timer, 3);

    // Sun shadow and ambient occlusion.
    ID3D11UnorderedAccessView* sunAoUav = gState->sunAo.uav.Get();
    ctx->CSSetShader(gState->sunAoCs.Get(), nullptr, 0);
    ctx->CSSetUnorderedAccessViews(2, 1, &sunAoUav, nullptr);
    ctx->Dispatch(groups(gState->sunAo.width), groups(gState->sunAo.height), 1);
    ctx->CSSetUnorderedAccessViews(2, 1, nullUavs, nullptr);
    timerStamp(timer, 4);

    // G-buffer, light field, shadows and sun/AO for the bounce light and the
    // light pass.
    ID3D11ShaderResourceView* views[9] = {
        gState->gbuffer[0].srv.Get(),
        gState->gbuffer[1].srv.Get(),
        gState->gbuffer[2].srv.Get(),
        gState->gbuffer[3].srv.Get(),
        gState->gbuffer[4].srv.Get(),
        gState->gbuffer[5].srv.Get(),
        gState->field.srv.Get(),
        gState->shadows.srv.Get(),
        gState->sunAo.srv.Get(),
    };
    ID3D11SamplerState* sampler = gState->linear.Get();
    ctx->CSSetShaderResources(4, 9, views);
    ctx->CSSetSamplers(0, 1, &sampler);

    // Bounce light: emitters, the cascades from the top down, fluence.
    if ((enabled & kGi) != 0) {
        ID3D11UnorderedAccessView* uav = gState->giScene.uav.Get();
        ctx->CSSetShader(gState->giSceneCs.Get(), nullptr, 0);
        ctx->CSSetUnorderedAccessViews(4, 1, &uav, nullptr);
        ctx->Dispatch(groups(gState->giScene.width), groups(gState->giScene.height), 1);
        ctx->CSSetUnorderedAccessViews(4, 1, nullUavs, nullptr);

        ID3D11ShaderResourceView* scene = gState->giScene.srv.Get();
        ctx->CSSetShaderResources(14, 1, &scene);
        ctx->CSSetShader(gState->giCascadeCs.Get(), nullptr, 0);
        for (int i = kCascades - 1; i >= 0; i--) {
            constants.cascade = i;
            if (!upload(gState->constants.Get(), &constants, sizeof(constants))) {
                break;
            }
            ID3D11ShaderResourceView* upper = i + 1 < kCascades ? gState->cascades[i + 1].srv.Get() : nullptr;
            uav = gState->cascades[i].uav.Get();
            ctx->CSSetShaderResources(13, 1, &upper);
            ctx->CSSetUnorderedAccessViews(4, 1, &uav, nullptr);
            ctx->Dispatch(groups(gState->cascades[i].width), groups(gState->cascades[i].height), 1);
            ctx->CSSetUnorderedAccessViews(4, 1, nullUavs, nullptr);
        }
        ID3D11ShaderResourceView* cascade0 = gState->cascades[0].srv.Get();
        uav = gState->fluence.uav.Get();
        ctx->CSSetShader(gState->giResolveCs.Get(), nullptr, 0);
        ctx->CSSetShaderResources(13, 1, &cascade0);
        ctx->CSSetUnorderedAccessViews(4, 1, &uav, nullptr);
        ctx->Dispatch(groups(gState->fluence.width), groups(gState->fluence.height), 1);
        ctx->CSSetUnorderedAccessViews(4, 1, nullUavs, nullptr);
        ctx->CSSetShaderResources(13, 2, nullViews);
    }
    timerStamp(timer, 5);

    // The light pass.
    ID3D11ShaderResourceView* fluence = gState->fluence.srv.Get();
    ctx->CSSetShader(gState->lightCs.Get(), nullptr, 0);
    ctx->CSSetShaderResources(15, 1, &fluence);
    ID3D11UnorderedAccessView* bloomUav = gState->bloom.uav.Get();
    ctx->CSSetUnorderedAccessViews(3, 1, &output, nullptr);
    ctx->CSSetUnorderedAccessViews(5, 1, &bloomUav, nullptr);
    ctx->Dispatch(groups(mapWidth * scale), groups(mapHeight * scale), 1);
    ctx->CSSetUnorderedAccessViews(0, 6, nullUavs, nullptr);
    gState->bloomValid = true;
    ctx->CSSetShaderResources(0, 16, nullViews);
    timerStamp(timer, 6);
    timerEnd(timer);

    gState->lightCount = static_cast<int>(lights.size());
    gState->shadowedLights = (enabled & kShadows) != 0 ? gState->lightCount : 0;
    gState->hexCount = static_cast<int>(hexes.size());
    gState->ambient = scene.ambient;
    gState->night = night;
    gState->day = day;
    gState->timeOfDay = scene.timeOfDay;
    gState->features = enabled;
    LARGE_INTEGER end;
    QueryPerformanceCounter(&end);
    gState->cpuMs = static_cast<double>(end.QuadPart - start.QuadPart) * 1000.0 / frequency.QuadPart;
    return true;
}

ID3D11ShaderResourceView* mapLightingBloomSource()
{
    // Quality low: no bloom.
    if (gState == nullptr || !gState->settings.newLight || !gState->bloomValid || gState->settings.quality == 0) {
        return nullptr;
    }
    return gState->bloom.srv.Get();
}

void mapLightingDrawOverlay()
{
    if (gState == nullptr) {
        return;
    }
    const Settings& s = gState->settings;
    const char* qualities[3] = { "low", "medium", "high" };
    ImGui::Text("light     %s, quality %s (Ctrl+F7)", s.newLight ? "new" : "original", qualities[s.quality]);
    if (!s.newLight) {
        return;
    }
    unsigned int f = gState->features;
    ImGui::Text("effects   normals %d colors %d shadows %d pool %d tonemap %d sun %d ao %d night %d gi %d (Ctrl+F6: one off)",
        (f & kNormals) != 0, (f & kColors) != 0, (f & kShadows) != 0, (f & kPool) != 0, (f & kTonemap) != 0, (f & kSun) != 0, (f & kAo) != 0, (f & kNight) != 0, (f & kGi) != 0);
    ImGui::Text("scene     %02d:%02d, ambient %.3f, night %.2f, day %.2f, %d lights, %d hexes", gState->timeOfDay / 100, gState->timeOfDay % 100, gState->ambient, gState->night, gState->day, gState->lightCount, gState->hexCount);
    char line[160];
    int n = snprintf(line, sizeof(line), "gpu ms   ");
    for (int i = 0; i < kTimerPasses && n < static_cast<int>(sizeof(line)); i++) {
        n += snprintf(line + n, sizeof(line) - n, " %s %.2f", kPassNames[i], gState->passMs[i]);
    }
    ImGui::TextUnformatted(line);
    ImGui::Text("cpu       %.2f ms per light draw", gState->cpuMs);
}

} // namespace fallout::hd

#endif // _WIN32
