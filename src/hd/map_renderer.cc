#include "hd/map_renderer.h"

#ifdef _WIN32

#include <math.h>
#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include <wrl/client.h>

#include "art.h"
#include "color.h"
#include "config.h"
#include "debug.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "hd/draw_list.h"
#include "hd/hd_art.h"
#include "hd/json_reader.h"
#include "hd/sprite_shape.h"
#include "map.h"
#include "obj_types.h"
#include "object.h"
#include "tile.h"

using Microsoft::WRL::ComPtr;

namespace fallout::hd {

namespace {

    // Op codes shared with the shaders (and MapRenderStats::opCounts).
    enum : unsigned int {
        kOpFill = 0,
        kOpLit = 1, // _dark_trans_buf_to_buf
        kOpFloor = 2, // tileRenderFloor's interpolated light
        kOpCopy = 3, // blitBufferToBufferTrans from the atlas
        kOpText = 4, // blitBufferToBufferTrans from the text texture
        kOpTrans = 5, // _dark_translucent_trans_buf_to_buf (compute)
        kOpEgg = 6, // _intensity_mask_buf_to_buf (compute)
    };

    // Rows of the tables texture (256 wide, R8_UINT).
    constexpr int kTableIntensity = 0; // [color][level]
    constexpr int kTableMixAdd = 256; // [a][b]
    constexpr int kTableBlend = 512; // 5 tables x 16 gray rows, [dest]
    constexpr int kTableGray = 592; // common, glass
    constexpr int kTableRows = 594;

    // Order of the translucency tables in kTableBlend.
    enum BlendTable {
        kBlendWall,
        kBlendGlass,
        kBlendSteam,
        kBlendEnergy,
        kBlendRed,
    };
    // The color each table blends toward (object.cc's _obj_render_object).
    // The COLOR_ names are palette lookups, valid only once color.pal is in.
    Color blendTableColor(int table)
    {
        const Color colors[5] = { COLOR_PALE_BLUE, COLOR_CYAN, COLOR_WHITE, COLOR_YELLOW_GREEN, COLOR_RED };
        return colors[table];
    }

    // Sprite::hd[3] flags (HD_ART, HD_HARD_ALPHA in the shader).
    constexpr int kHdArt = 1;
    constexpr int kHdHardAlpha = 2;
    constexpr int kHdEdgeFill = 4;
    constexpr int kHdEdgeClamp = 8;

    // Tiling art's flags. [hd] edge_fill=0 keeps the plain 1x mask, saw teeth and all.
    int hardAlphaFlags()
    {
        static const int flags = [] {
            int on = 1;
            configGetInt(&gGameConfig, "hd", "edge_fill", &on, 1);
            return kHdHardAlpha | (on != 0 ? kHdEdgeFill : 0);
        }();
        return flags;
    }

    // [hd] edge_clamp: HD colors in a tiling piece's edge cells (and the stair fill past
    // them) are kept within the original colors of the cell's 3x3 neighborhood. The packs
    // were upscaled with smeared colors around each piece, which leave light or dark
    // specks just inside the 1x mask; where pieces join they line up as a dashed line.
    // 0 off, 1 floors (default: 0.9.1, the dashed lines along floor seams), 2 floors and
    // roofs, 3 walls too.
    bool edgeClampFor(MapLightCategory category)
    {
        static const int mode = [] {
            int value = 1;
            configGetInt(&gGameConfig, "hd", "edge_clamp", &value, 1);
            return value;
        }();
        return (mode >= 1 && category == kLightFloor) || (mode >= 2 && category == kLightRoof) || (mode >= 3 && category == kLightWall);
    }

    constexpr int kAtlasSize = 4096;
    constexpr int kTextWidth = 2048;
    constexpr int kTextHeight = 1024;

    // tile.cc's floor intensity interpolation (verticies, triangle and row
    // tables), used to precompute which triangle writes each cell.
    constexpr int kVertexOffsets[10] = { 16, 48, 960, 992, 1024, 1936, 1968, 2000, 2912, 2944 };
    constexpr int kRightsideUpTriangles[5][3] = { { 2, 3, 0 }, { 3, 4, 1 }, { 5, 6, 3 }, { 6, 7, 4 }, { 8, 9, 6 } };
    constexpr int kUpsideDownTriangles[5][3] = { { 0, 3, 1 }, { 2, 5, 3 }, { 3, 6, 4 }, { 5, 8, 6 }, { 6, 9, 7 } };
    constexpr int kRightsideUpTable[13][2] = { { -1, 2 }, { 78, 2 }, { 76, 6 }, { 73, 8 }, { 71, 10 }, { 68, 14 }, { 65, 16 }, { 63, 18 }, { 61, 20 }, { 58, 24 }, { 55, 26 }, { 53, 28 }, { 50, 32 } };
    constexpr int kUpsideDownTable[13][2] = { { 0, 32 }, { 48, 32 }, { 49, 30 }, { 52, 26 }, { 55, 24 }, { 57, 22 }, { 60, 18 }, { 63, 16 }, { 65, 14 }, { 67, 12 }, { 70, 8 }, { 73, 6 }, { 75, 4 } };
    constexpr int kIntensityMapWidth = 80;
    constexpr int kIntensityMapSize = 3280;

    constexpr char kShaderSource[] = R"hlsl(
// Must match map_renderer.cc.
#define OP_FILL 0
#define OP_LIT 1
#define OP_FLOOR 2
#define OP_COPY 3
#define OP_TEXT 4
#define OP_TRANS 5
#define OP_EGG 6

struct Sprite {
    int4 rect;   // logical pixels to draw, inclusive (already clipped)
    int2 origin; // logical position of source texel (0, 0)
    int2 src;    // source texel (0, 0) in the atlas or text texture
    int2 size;   // source width, height
    uint op;
    uint param;  // light level, fill color, floor vertex base, outline colors
    int4 egg;    // egg rect (OP_LIT: skip inside), or outline visible frame rect
    int2 eggSrc; // egg mask in the atlas
    uint table;  // translucency: blend table | gray table << 8; outline: see below
    uint tableColor; // translucency: the blend table's color (psHd)
    int4 hd;     // HD art (psHd): pool texel x, y of the frame, pool page, flags
    int4 light;  // new light (psGbuffer): category, flags
};

// Outline pixels carry this above the palette index, so the compositor can
// show them from the exact pass in HD mode. Everything reading the target
// masks it off.
#define OUTLINE_TAG 0x100
#define HD_ART 1
#define HD_HARD_ALPHA 2
#define HD_EDGE_FILL 4
#define HD_EDGE_CLAMP 8
#define HARD_EDGE_SEARCH 8

StructuredBuffer<Sprite> gSprites : register(t0);
Texture2D<uint> gAtlas : register(t1);
Texture2D<uint> gText : register(t2);
Texture2D<uint> gTables : register(t3);
Texture2D<uint> gFloorCells : register(t4);
StructuredBuffer<int> gVertexLight : register(t5);
RWTexture2D<uint> gTarget : register(u0);

cbuffer MapPass : register(b0) {
    int2 gTargetSize;
    int gScale;
    int gSpriteBase;
};

uint intensityColor(uint color, int level) { return gTables.Load(int3(level, color, 0)); }
uint mixAdd(uint a, uint b) { return gTables.Load(int3(b, 256 + a, 0)); }
uint blendColor(uint table, uint gray, uint dest) { return gTables.Load(int3(dest, 512 + table * 16 + gray, 0)); }
uint grayOf(uint table, uint color) { return gTables.Load(int3(color, 592 + table, 0)); }

bool inside(int4 r, int2 p) { return p.x >= r.x && p.x <= r.z && p.y >= r.y && p.y <= r.w; }

static const int3 kRightsideUp[5] = { int3(2, 3, 0), int3(3, 4, 1), int3(5, 6, 3), int3(6, 7, 4), int3(8, 9, 6) };
static const int3 kUpsideDown[5] = { int3(0, 3, 1), int3(2, 5, 3), int3(3, 6, 4), int3(5, 8, 6), int3(6, 9, 7) };

// C integer division (truncates toward zero).
int cdiv(int a, int b) { int q = abs(a) / b; return a < 0 ? -q : q; }

// tileRenderFloor's _intensity_map value for frame texel t. Cells the
// triangle fill never writes stay 0 in CE.
int floorIntensity(uint base, int2 t)
{
    int index = 160 + 80 * t.y + t.x;
    if (index < 0 || index >= 3280) {
        return 0;
    }
    uint cell = gFloorCells.Load(int3(index % 80, index / 80, 0));
    if ((cell & 0x8000) == 0) {
        return 0;
    }
    uint tri = (cell >> 11) & 7;
    int i = int((cell >> 6) & 31);
    int j = int(cell & 63);
    if ((cell & 0x4000) == 0) {
        int3 f = kRightsideUp[tri];
        int value = gVertexLight[base + f.z];
        int stepX = cdiv(gVertexLight[base + f.y] - gVertexLight[base + f.x], 32);
        int stepY = cdiv(gVertexLight[base + f.x] - value, 13);
        return value + i * stepY + j * stepX;
    } else {
        int3 f = kUpsideDown[tri];
        int value = gVertexLight[base + f.x];
        int stepX = cdiv(gVertexLight[base + f.z] - value, 32);
        int stepY = cdiv(gVertexLight[base + f.y] - value, 13);
        return value + i * stepY + j * stepX;
    }
}

struct VSOut {
    float4 pos : SV_Position;
    nointerpolation uint sprite : SPRITE;
};

VSOut vsSprite(uint vertexId : SV_VertexID, uint instanceId : SV_InstanceID)
{
    uint index = gSpriteBase + instanceId;
    Sprite s = gSprites[index];
    // Two triangles: (0,0) (1,0) (0,1) / (0,1) (1,0) (1,1).
    static const float2 kCorners[6] = { float2(0, 0), float2(1, 0), float2(0, 1), float2(0, 1), float2(1, 0), float2(1, 1) };
    float2 lt = float2(s.rect.xy) * gScale;
    float2 rb = float2(s.rect.zw + 1) * gScale;
    float2 p = lerp(lt, rb, kCorners[vertexId]);
    VSOut o;
    o.pos = float4(p.x / gTargetSize.x * 2.0 - 1.0, 1.0 - p.y / gTargetSize.y * 2.0, 0, 1);
    o.sprite = index;
    return o;
}

uint psSprite(VSOut i) : SV_Target
{
    Sprite s = gSprites[i.sprite];
    int2 p = int2(i.pos.xy) / gScale;
    int2 t = p - s.origin;
    uint color;
    switch (s.op) {
    case OP_FILL:
        return s.param;
    case OP_LIT:
        if (s.egg.z >= s.egg.x && inside(s.egg, p)) {
            discard;
        }
        color = gAtlas.Load(int3(s.src + t, 0));
        if (color == 0) {
            discard;
        }
        return color < 0xE5 ? intensityColor(color, s.param) : color;
    case OP_FLOOR:
        color = gAtlas.Load(int3(s.src + t, 0));
        if (color == 0) {
            discard;
        }
        return intensityColor(color, clamp(floorIntensity(s.param, t) >> 9, 0, 255));
    case OP_COPY:
        color = gAtlas.Load(int3(s.src + t, 0));
        if (color == 0) {
            discard;
        }
        return color;
    case OP_TEXT:
        color = gText.Load(int3(s.src + t, 0));
        if (color == 0) {
            discard;
        }
        return color;
    }
    discard;
    return 0;
}

// Destination-reading ops, one thread per target pixel of the sprite rect.
[numthreads(8, 8, 1)]
void csPixelOp(uint3 id : SV_DispatchThreadID)
{
    Sprite s = gSprites[gSpriteBase];
    int2 pix = s.rect.xy * gScale + int2(id.xy);
    if (any(pix >= (s.rect.zw + 1) * gScale) || any(pix >= gTargetSize)) {
        return;
    }
    int2 p = pix / gScale;
    uint color = gAtlas.Load(int3(s.src + (p - s.origin), 0));
    if (color == 0) {
        return;
    }
    uint dest = gTarget[pix] & 0xFF;
    if (s.op == OP_TRANS) {
        uint gray = grayOf(s.table >> 8, color);
        gTarget[pix] = intensityColor(blendColor(s.table & 0xFF, gray, dest), s.param);
    } else {
        color = intensityColor(color, s.param);
        int mask = int(gAtlas.Load(int3(s.eggSrc + (p - s.egg.xy), 0)));
        if (mask != 0) {
            uint under = intensityColor(dest, 128 - mask);
            uint over = intensityColor(color, mask);
            color = mixAdd(over, under);
        }
        gTarget[pix] = color;
    }
}

// objectDrawOutline. Sprite: rect.xy = unclipped object position, size =
// frame size, egg = visible frame rect, param = color | count << 8 |
// band << 16, table = 0 or (paletted: 1 << 16 | blend table << 8 | gray).
// One thread per frame row (row pass), then per column (column pass); both
// write only their own row or column, as in CE's loops.
void writeLogical(int2 p, uint value)
{
    for (int dy = 0; dy < gScale; dy++) {
        for (int dx = 0; dx < gScale; dx++) {
            int2 pix = p * gScale + int2(dx, dy);
            if (all(pix >= 0) && all(pix < gTargetSize)) {
                gTarget[pix] = (value & 0xFF) | OUTLINE_TAG;
            }
        }
    }
}

uint outlineValue(Sprite s, uint color, int2 p)
{
    if ((s.table & 0x10000) == 0) {
        return color & 0xFF;
    }
    uint gray = grayOf(s.table & 0xFF, color);
    return blendColor((s.table >> 8) & 0xFF, gray, gTarget[p * gScale] & 0xFF);
}

uint advanceBand(uint outlineColor, int y, uint color, int count, int band)
{
    if (band != 0) {
        if (y % band == 0) {
            outlineColor = (outlineColor + 1) & 0xFF;
        }
        if (int(outlineColor) > count + int(color) - 1) {
            outlineColor = color;
        }
    }
    return outlineColor;
}

[numthreads(64, 1, 1)]
void csOutlineRows(uint3 id : SV_DispatchThreadID)
{
    Sprite s = gSprites[gSpriteBase];
    int y = int(id.x);
    int w = s.size.x;
    if (y >= s.size.y) {
        return;
    }
    uint color = s.param & 0xFF;
    int count = int((s.param >> 8) & 0xFF);
    int band = int(s.param >> 16);
    uint outlineColor = color;
    for (int yy = 0; yy <= y; yy++) {
        outlineColor = advanceBand(outlineColor, yy, color, count, band);
    }

    int4 vis = s.egg;
    bool yVisible = y >= vis.y && y <= vis.w;
    int mapWidth = gTargetSize.x / gScale;
    int destY = s.rect.y + y;
    bool cycle = true;
    for (int x = 0; x < w; x++) {
        uint pixel = gAtlas.Load(int3(s.src + int2(x, y), 0));
        int destX = s.rect.x + x;
        if (pixel != 0 && cycle) {
            if (yVisible && x >= vis.x && x <= vis.z && destX >= 1) {
                writeLogical(int2(destX - 1, destY), outlineValue(s, outlineColor, int2(destX - 1, destY)));
            }
            cycle = false;
        } else if (pixel == 0 && !cycle) {
            if (yVisible && x >= vis.x && x <= vis.z) {
                writeLogical(int2(destX, destY), outlineValue(s, outlineColor, int2(destX, destY)) & 0xFF);
            }
            cycle = true;
        }
    }
    if (gAtlas.Load(int3(s.src + int2(w - 1, y), 0)) != 0) {
        int destX = s.rect.x + w;
        if (yVisible && w - 1 >= vis.x && w - 1 <= vis.z && destX < mapWidth) {
            writeLogical(int2(destX, destY), outlineValue(s, outlineColor, int2(destX, destY)));
        }
    }
}

[numthreads(64, 1, 1)]
void csOutlineColumns(uint3 id : SV_DispatchThreadID)
{
    Sprite s = gSprites[gSpriteBase];
    int x = int(id.x);
    int h = s.size.y;
    int4 vis = s.egg;
    if (x >= s.size.x || x < vis.x || x > vis.z) {
        return;
    }
    uint color = s.param & 0xFF;
    int count = int((s.param >> 8) & 0xFF);
    int band = int(s.param >> 16);
    int mapHeight = gTargetSize.y / gScale;
    int destX = s.rect.x + x;
    bool cycle = true;
    uint outlineColor = color;
    for (int y = 0; y < h; y++) {
        outlineColor = advanceBand(outlineColor, y, color, count, band);
        bool yVisible = y >= vis.y && y <= vis.w;
        uint pixel = gAtlas.Load(int3(s.src + int2(x, y), 0));
        int destY = s.rect.y + y;
        if (pixel != 0 && cycle) {
            if (yVisible && destY - 1 >= 0) {
                writeLogical(int2(destX, destY - 1), outlineValue(s, outlineColor, int2(destX, destY - 1)));
            }
            cycle = false;
        } else if (pixel == 0 && !cycle) {
            if (yVisible) {
                writeLogical(int2(destX, destY), outlineValue(s, outlineColor, int2(destX, destY)));
            }
            cycle = true;
        }
    }
    if (gAtlas.Load(int3(s.src + int2(x, h - 1), 0)) != 0) {
        int destY = s.rect.y + h;
        if (h - 1 >= vis.y && h - 1 <= vis.w && destY < mapHeight) {
            writeLogical(int2(destX, destY), outlineValue(s, outlineColor, int2(destX, destY)));
        }
    }
}

// ---------------------------------------------------------------------------
// HD color pass (PLAN.md P4.2): the same sprites into an RGBA16F target,
// colors in "base" palette space (color.pal before fades and gamma; the
// compositor applies those). Continuous versions of CE's rules:
//   light        c * level/128, CE's "lighter" half above 128
//   translucency light * lerp(dest, table color, gray(src)/7)  (_buildBlendTable)
//   egg          lit color weighted by the egg mask, filtered at N x
//   cycle colors original art: the exact pass's result from the live palette;
//                HD art: animated by cycleAt (P6.1), lit only on floors
// Every op ends in one dual-source blend: dest = o0 + dest * o1, so o0 is the
// premultiplied color and o1 the factor left for what's below. The alpha
// channel accumulates coverage (holes test).
Texture2DArray<float4> gPool : register(t6);
StructuredBuffer<float4> gBasePalette : register(t7);

struct HdOut {
    float4 color : SV_Target0;
    float4 factor : SV_Target1;
};

float3 baseColor(uint index) { return gBasePalette[index & 0xFF].rgb; }

float3 lit(float3 c, int level)
{
    return level <= 128 ? c * (level / 128.0) : c + (1.0 - c) * ((level - 128) / 128.0);
}

HdOut over(float3 c, float a)
{
    HdOut o;
    o.color = float4(c * a, a);
    o.factor = float4((1.0 - a).xxx, 1.0 - a);
    return o;
}

// Tiling art (floors, roofs, walls) is cut with its 1x mask, so against sharp HD
// art its edge reads as saw teeth. Each stair of the mask gets filled in up to the
// straight line through the stair's corners. Only ever past the mask, never inside
// it, so pieces that join can't open a gap between them, and only at real stairs:
// air on the far side, a riser one cell tall, so holes and checkerboards stay.
bool maskOpaque(Sprite s, int2 e)
{
    if (any(e < int2(0, 0)) || any(e >= s.size)) {
        return false;
    }
    return gAtlas.Load(int3(s.src + e, 0)) != 0;
}

// Cells walked from cell e along `along` to the end of the run on `floor` side
// `toward`; rise = the run ends at a stair's riser.
int stairRun(Sprite s, int2 e, int2 along, int2 toward, out bool rise)
{
    rise = false;
    [loop] for (int k = 1; k <= HARD_EDGE_SEARCH; k++) {
        int2 q = e + along * k;
        if (maskOpaque(s, q)) {
            rise = !maskOpaque(s, q - toward);
            return k - 1;
        }
        if (!maskOpaque(s, q + toward)) {
            return k - 1;
        }
    }
    return HARD_EDGE_SEARCH;
}

// Coverage of a pixel in transparent cell e, at f (0..1) within the cell; `toward`
// is the direction of the mask it fills up to, for the light.
float hardEdgeFill(Sprite s, int2 e, float2 f, out int2 toward)
{
    static const int2 kToward[4] = { int2(0, 1), int2(0, -1), int2(1, 0), int2(-1, 0) };
    float best = 0.0;
    toward = int2(0, 0);
    [loop] for (int d = 0; d < 4; d++) {
        int2 n = kToward[d];
        if (!maskOpaque(s, e + n) || maskOpaque(s, e - n)) {
            continue;
        }
        int2 u = n.y != 0 ? int2(1, 0) : int2(0, 1);
        bool riseBack;
        bool riseOn;
        int back = stairRun(s, e, -u, n, riseBack);
        int on = stairRun(s, e, u, n, riseOn);
        if (!riseBack && !riseOn) {
            continue;
        }
        float along = n.y != 0 ? f.x : f.y;
        float across = n.y > 0 ? 1.0 - f.y : (n.y < 0 ? f.y : (n.x > 0 ? 1.0 - f.x : f.x));
        float run = float(back + on + 1) * (riseBack && riseOn ? 0.5 : 1.0);
        float reach = 0.0;
        if (riseBack) {
            reach = max(reach, 1.0 - (float(back) + along) / run);
        }
        if (riseOn) {
            reach = max(reach, 1.0 - (float(on) + 1.0 - along) / run);
        }
        float cover = reach > 0.0 ? saturate((reach - across) * gScale + 0.5) : 0.0;
        if (cover > best) {
            best = cover;
            toward = n;
        }
    }
    return best;
}

)hlsl"
                                     R"hlsl(
// [hd] edge_clamp: an HD color in cell t (opaque, with a transparent 4-neighbor) or in the
// stair fill toward t, kept within the phase-0 palette colors of the opaque cells around t.
float3 edgeClamp(Sprite s, int2 t, float3 c)
{
    if (!maskOpaque(s, t)) {
        return c;
    }
    if (maskOpaque(s, t + int2(1, 0)) && maskOpaque(s, t - int2(1, 0)) && maskOpaque(s, t + int2(0, 1)) && maskOpaque(s, t - int2(0, 1))) {
        return c;
    }
    float3 lo = float3(1e9, 1e9, 1e9);
    float3 hi = float3(-1e9, -1e9, -1e9);
    [unroll] for (int k = 0; k < 9; k++) {
        int2 e = t + int2(k % 3 - 1, k / 3 - 1);
        if (maskOpaque(s, e)) {
            // HD art holds the cycle colors at phase 0 (gBasePalette[256 + i], filled
            // for 229-254 only); every other color is the live entry.
            uint i = gAtlas.Load(int3(s.src + e, 0)) & 0xFF;
            float3 p = gBasePalette[i >= 229 && i <= 254 ? 256 + i : i].rgb;
            lo = min(lo, p);
            hi = max(hi, p);
        }
    }
    return clamp(c, lo, hi);
}

// Tiling HD art's coverage at target pixel `pix`, in cell t of the frame.
float hardAlpha(Sprite s, int2 pix, int2 t, uint index, out int2 toward)
{
    toward = int2(0, 0);
    if (index != 0) {
        return 1.0;
    }
    if ((s.hd.w & HD_EDGE_FILL) == 0 || s.op == OP_TRANS) {
        return 0.0;
    }
    float2 f = (float2(pix - (pix / gScale) * gScale) + 0.5) / gScale;
    return hardEdgeFill(s, t, f, toward);
}

// Egg weight at a target pixel: 1 where the mask is 0 (no cut), else
// mask/128, bilinear over the 1x mask at the pixel's logical position.
float eggWeight(Sprite s, int2 pix)
{
    float2 f = (float2(pix) + 0.5) / gScale - 0.5 - float2(s.egg.xy);
    int2 i0 = int2(floor(f));
    float2 t = f - float2(i0);
    int2 size = s.egg.zw - s.egg.xy + 1;
    float w[4];
    [unroll] for (int k = 0; k < 4; k++) {
        int2 e = i0 + int2(k & 1, k >> 1);
        uint m = all(e >= 0) && all(e < size) ? gAtlas.Load(int3(s.eggSrc + e, 0)) : 0;
        w[k] = m == 0 ? 1.0 : m / 128.0;
    }
    return lerp(lerp(w[0], w[1], t.x), lerp(w[2], w[3], t.x), t.y);
}
)hlsl"
                                     // MSVC caps one string literal at 16 KB; the source continues.
                                     R"hlsl(
// Palette cycling on HD art (PLAN.md P6.1). cycle.cc rotates the colors of
// six ranges in place (229-232 slime, 233-237 monitors, 238-242 slow fire,
// 243-247 fast fire, 248-253 shoreline, 254 alarm); the HD art was upscaled
// with them at phase 0 (gBasePalette[256 + index]). Each HD pixel gets the
// change (live - phase 0) of the 1x pixels around it, blended with bilinear
// weights sharpened toward the nearest one: the original animates pixel by
// pixel (monitor screens are 1-pixel dot patterns), and the HD art keeps its
// detail.
// How each range glows in the new light (P6.2), as shares of the pixel:
//   screens  monitors and the alarm: unlit, as CE draws them (they emit)
//   glow     slime and fire: never darker than the art, but lit like the
//            static pixels of the same goo or flame around them (unlit,
//            they speckled lamp-lit goo with dark dots)
//   bloom    bloom source weight: all of them, slime at half
//   the shoreline is plain water, lit like its floor.
struct Cycle {
    float weight; // share of cycle colors in the pixel (0 = none)
    float screens;
    float glow;
    float bloom;
    float3 delta; // live - phase 0, blended
};

bool isCycle(uint index) { return index >= 229 && index <= 254; }

void cycleShares(uint index, float w, inout Cycle c)
{
    bool slime = index <= 232;
    bool screen = (index >= 233 && index <= 237) || index == 254;
    bool fire = index >= 238 && index <= 247;
    c.weight += w;
    c.screens += screen ? w : 0.0;
    c.glow += slime || fire ? w : 0.0;
    c.bloom += screen || fire ? w : (slime ? 0.5 * w : 0.0);
}

// Original art: the pixel shows its live color already.
Cycle cycleExact(uint index)
{
    Cycle c = (Cycle)0;
    if (isCycle(index)) {
        cycleShares(index, 1.0, c);
    }
    return c;
}

Cycle cycleAt(Sprite s, int2 pix)
{
    Cycle c = (Cycle)0;
    float2 f = (float2(pix) + 0.5) / gScale - 0.5 - float2(s.origin);
    int2 i0 = int2(floor(f));
    float2 t = smoothstep(0.15, 0.85, f - float2(i0));
    [unroll] for (int k = 0; k < 4; k++) {
        int2 e = i0 + int2(k & 1, k >> 1);
        uint index = all(e >= 0) && all(e < s.size) ? gAtlas.Load(int3(s.src + e, 0)) : 0;
        if (!isCycle(index)) {
            continue;
        }
        float w = ((k & 1) != 0 ? t.x : 1.0 - t.x) * ((k >> 1) != 0 ? t.y : 1.0 - t.y);
        cycleShares(index, w, c);
        c.delta += w * (gBasePalette[index].rgb - gBasePalette[256 + index].rgb);
    }
    return c;
}

HdOut psHd(VSOut i)
{
    Sprite s = gSprites[i.sprite];
    int2 pix = int2(i.pos.xy);
    int2 p = pix / gScale;
    int2 t = p - s.origin;
    uint index;
    switch (s.op) {
    case OP_FILL:
        return over(baseColor(s.param), 1.0);
    case OP_TEXT:
        index = gText.Load(int3(s.src + t, 0));
        if (index == 0) {
            discard;
        }
        return over(baseColor(index), 1.0);
    case OP_COPY:
        index = gAtlas.Load(int3(s.src + t, 0));
        if (index == 0) {
            discard;
        }
        return over(baseColor(index), 1.0);
    }

    // Lit art: OP_LIT, OP_FLOOR, OP_EGG, OP_TRANS.
    if (s.op == OP_LIT && s.egg.z >= s.egg.x && inside(s.egg, p)) {
        discard; // drawn by the OP_EGG sprite
    }
    index = gAtlas.Load(int3(s.src + t, 0));
    float4 art;
    float a;
    if ((s.hd.w & HD_ART) != 0) {
        art = gPool.Load(int4(s.hd.xy + pix - s.origin * gScale, s.hd.z, 0));
        int2 toward = int2(0, 0);
        a = (s.hd.w & HD_HARD_ALPHA) != 0 ? hardAlpha(s, pix, t, index, toward) : art.a;
        if ((s.hd.w & HD_EDGE_CLAMP) != 0 && a > 0.0) {
            art.rgb = edgeClamp(s, t + toward, art.rgb);
        }
    } else {
        art = float4(baseColor(index), 1.0);
        a = index != 0 ? 1.0 : 0.0;
    }
    if (a <= 0.0) {
        discard;
    }

    int level = int(s.param);
    if (s.op == OP_FLOOR) {
        level = clamp(floorIntensity(s.param, t) >> 9, 0, 255);
    } else if (s.op == OP_TRANS) {
        level = min(level, 128);
    }

    if (s.op == OP_TRANS) {
        // _dark_translucent_trans_buf_to_buf: gray of the source picks the
        // blend row, the table's color is mixed in, then the light.
        float3 c = art.rgb;
        float gray = (s.table >> 8) == 0 ? (c.b + 3.0 * c.r + 6.0 * c.g) / 10.0 : (c.r + 5.0 * c.g + 4.0 * c.b) / 10.0;
        // CE: 5-bit gray >> 2, i.e. floor(gray * 7.97) in 0..7. Kept
        // continuous for HD art, minus the half step the floor loses on
        // average (else translucency comes out brighter than CE's).
        float g = clamp(gray * 7.97 - 0.5, 0.0, 7.0) / 7.0;
        float light = level / 128.0;
        HdOut o;
        o.color = float4(baseColor(s.tableColor) * (g * light * a), a);
        o.factor = float4((1.0 - a + a * light * (1.0 - g)).xxx, 1.0 - a);
        return o;
    }

    float3 color;
    if ((s.hd.w & HD_ART) != 0) {
        Cycle cycle = cycleAt(s, pix);
        float3 c = max(art.rgb + cycle.delta, 0.0);
        // CE lights cycle colors on floors (and the egg), not on other art.
        color = lerp(lit(c, level), c, s.op == OP_FLOOR || s.op == OP_EGG ? 0.0 : cycle.weight);
    } else if (index >= 0xE5 && (s.op == OP_FLOOR || s.op == OP_EGG)) {
        color = baseColor(intensityColor(index, level)); // CE lights cycle colors here
    } else if (index >= 0xE5) {
        color = baseColor(index); // _dark_trans_buf_to_buf leaves them unlit
    } else {
        color = lit(art.rgb, level);
    }
    if (s.op == OP_EGG) {
        a *= eggWeight(s, pix);
    }
    return over(color, a);
}
)hlsl"
                                     // MSVC caps one string literal at 16 KB; the source continues.
                                     R"hlsl(
// ---------------------------------------------------------------------------
// G-buffer for the new light (PLAN.md P5.2): the same sprites as psHd,
// unlit, into six targets. Each target is blended "premultiplied over" with
// its own alpha (ONE, INV_SRC_ALPHA), so soft HD edges average attributes;
// the lighting pass divides by the accumulated alpha.
//   0 albedo        RGBA16F  color * w, w (translucency: w = a * gray)
//   1 base normal   RGBA8    (n + 1) / 2 * a, a   (shape only)
//   2 detail normal RGBA8    (n + 1) / 2 * a, a   (shape + albedo detail)
//   3 surface       RGBA16F  height, CE's light level / 128, shape weight; a
//   4 material      RGBA8    unlit, ambient only (roofs), relief weight; a
//   5 coverage      RGBA8    a (the holes test, as the P4.2 alpha), glow * a,
//                            cycle-color bloom weight * a (P6.2), a
// Translucent sprites change only the albedo and coverage: a pixel behind
// glass takes the light of the surface behind it.
// Categories and flags must match map_renderer.h (MapLightCategory).
#define CAT_NONE 0
#define CAT_FLOOR 1
#define CAT_ROOF 2
#define CAT_WALL 3
#define CAT_CRITTER 5
#define LIGHT_FLAT 1
#define LIGHT_GLOW 2

Texture2D<float4> gShape : register(t8);

cbuffer Gbuffer : register(b1) {
    float4 gCategory[9]; // per category: detail strength, relief weight, shape weight, sun shadow ignored
    float gDetailScale;  // Sobel of albedo luminance -> 4x master units
    int2 gOccluderOrigin; // ground px (0, 0) in the occluder grid
    float gPaintedShadow; // [hd_light] painted_shadows: their strength, 1 = as painted
    int2 gOccluderSize;
    float gCheckerAlpha;  // [hd_light] checker_alpha: coverage of checkerboard regions, 1 = as drawn
    int gOccluderPad3;
};

struct GbufOut {
    float4 albedo : SV_Target0;
    float4 baseNormal : SV_Target1;
    float4 detailNormal : SV_Target2;
    float4 surface : SV_Target3;
    float4 material : SV_Target4;
    float4 coverage : SV_Target5;
};

float4 encodeNormal(float3 n, float a) { return float4((n * 0.5 + 0.5) * a, a); }

GbufOut gbufUnlit(float3 c, float a)
{
    GbufOut o;
    o.albedo = float4(c * a, a);
    o.baseNormal = encodeNormal(float3(0.0, 0.0, 1.0), a);
    o.detailNormal = o.baseNormal;
    o.surface = float4(0.0, a, 0.0, a);
    o.material = float4(a, 0.0, 0.0, a);
    o.coverage = float4(a, 0.0, 0.0, a);
    return o;
}

float luma(float3 c) { return dot(c, float3(0.299, 0.587, 0.114)); }

// HD albedo luminance at frame-local HD texel q, clamped to the HD texels
// the sprite has in the pool: the frame, or for a frame kept in tiles
// (addSprite) its tile, light.z (first) to light.w (one past the last),
// each x | y << 16.
float hdLuma(Sprite s, int2 q)
{
    int2 lo = int2(0, 0);
    int2 hi = s.size * gScale;
    if (s.light.w != 0) {
        lo = int2(s.light.z & 0xFFFF, (s.light.z >> 16) & 0xFFFF);
        hi = int2(s.light.w & 0xFFFF, (s.light.w >> 16) & 0xFFFF);
    }
    q = clamp(q, lo, hi - 1);
    return luma(gPool.Load(int4(s.hd.xy + q, s.hd.z, 0)).rgb);
}

// With [hd_light] checker_alpha on, A's bit 6 flags a checkerboard texel and
// heights top out at 1023 px (checkerFlags in map_renderer.cc).
float shapeHeight(float4 raw) { return (round(raw.b * 255.0) + float(uint(round(raw.a * 255.0)) & (gCheckerAlpha < 1.0 ? 63u : 127u)) * 256.0) / 16.0; }
// A texel of a shadow painted into the art (sprite_shape.h): flat on the ground.
bool shapePainted(float4 raw) { return (uint(round(raw.a * 255.0)) & 128u) != 0; }
bool shapeChecker(float4 raw) { return (uint(round(raw.a * 255.0)) & 64u) != 0; }

// How much of the target pixel lies in a checkerboard see-through region:
// bilinear over the 1x texels, like paintedShadowAt.
float checkerAt(Sprite s, int2 pix)
{
    float2 f = (float2(pix) + 0.5) / gScale - 0.5 - float2(s.origin);
    int2 i0 = int2(floor(f));
    float2 t = f - float2(i0);
    float checker = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        int2 e = clamp(i0 + int2(k & 1, k >> 1), int2(0, 0), s.size - 1);
        float w = ((k & 1) != 0 ? t.x : 1.0 - t.x) * ((k >> 1) != 0 ? t.y : 1.0 - t.y);
        checker += shapeChecker(gShape.Load(int3(s.src + e, 0))) ? w : 0.0;
    }
    return checker;
}

// How much of the target pixel lies on a painted shadow: bilinear over the
// 1x texels, so the patch's edge stays soft at the HD scale.
float paintedShadowAt(Sprite s, int2 pix)
{
    float2 f = (float2(pix) + 0.5) / gScale - 0.5 - float2(s.origin);
    int2 i0 = int2(floor(f));
    float2 t = f - float2(i0);
    float painted = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        int2 e = clamp(i0 + int2(k & 1, k >> 1), int2(0, 0), s.size - 1);
        float w = ((k & 1) != 0 ? t.x : 1.0 - t.x) * ((k >> 1) != 0 ? t.y : 1.0 - t.y);
        painted += shapePainted(gShape.Load(int3(s.src + e, 0))) ? w : 0.0;
    }
    return painted;
}

// The sprite's shape (sprite_shape.h) at a target pixel: the normal
// bilinear over the 1x texels; the height bilinear over the opaque ones
// (transparent texels hold 0), else 1 px height steps print 1x blocks into
// the light of nearby lamps.
void shapeAt(Sprite s, int2 pix, out float3 n, out float height)
{
    float2 f = (float2(pix) + 0.5) / gScale - 0.5 - float2(s.origin);
    int2 i0 = int2(floor(f));
    float2 t = f - float2(i0);
    float2 uz = 0.0;
    float heights = 0.0;
    float weights = 0.0;
    [unroll] for (int k = 0; k < 4; k++) {
        int2 e = clamp(i0 + int2(k & 1, k >> 1), int2(0, 0), s.size - 1);
        float w = ((k & 1) != 0 ? t.x : 1.0 - t.x) * ((k >> 1) != 0 ? t.y : 1.0 - t.y);
        float4 raw = gShape.Load(int3(s.src + e, 0));
        uz += raw.rg * w;
        if (gAtlas.Load(int3(s.src + e, 0)) != 0 && !shapePainted(raw)) {
            heights += shapeHeight(raw) * w;
            weights += w;
        }
    }
    uz = uz * 2.0 - 1.0;
    n = float3(uz.x, sqrt(saturate(1.0 - dot(uz, uz))), uz.y);
    int2 texel = clamp(pix / gScale - s.origin, int2(0, 0), s.size - 1);
    height = weights > 1e-3 ? heights / weights : shapeHeight(gShape.Load(int3(s.src + texel, 0)));
}

GbufOut psGbuffer(VSOut i)
{
    Sprite s = gSprites[i.sprite];
    int2 pix = int2(i.pos.xy);
    int2 p = pix / gScale;
    int2 t = p - s.origin;
    uint index;
    switch (s.op) {
    case OP_FILL:
        return gbufUnlit(baseColor(s.param), 1.0);
    case OP_TEXT:
        index = gText.Load(int3(s.src + t, 0));
        if (index == 0) {
            discard;
        }
        return gbufUnlit(baseColor(index), 1.0);
    case OP_COPY:
        index = gAtlas.Load(int3(s.src + t, 0));
        if (index == 0) {
            discard;
        }
        return gbufUnlit(baseColor(index), 1.0);
    }

    if (s.op == OP_LIT && s.egg.z >= s.egg.x && inside(s.egg, p)) {
        discard; // drawn by the OP_EGG sprite
    }
    index = gAtlas.Load(int3(s.src + t, 0));
    bool hd = (s.hd.w & HD_ART) != 0;
    float4 art;
    float a;
    int2 toward = int2(0, 0);
    if (hd) {
        art = gPool.Load(int4(s.hd.xy + pix - s.origin * gScale, s.hd.z, 0));
        a = (s.hd.w & HD_HARD_ALPHA) != 0 ? hardAlpha(s, pix, t, index, toward) : art.a;
        if ((s.hd.w & HD_EDGE_CLAMP) != 0 && a > 0.0) {
            art.rgb = edgeClamp(s, t + toward, art.rgb);
        }
    } else {
        art = float4(baseColor(index), 1.0);
        a = index != 0 ? 1.0 : 0.0;
    }
    // A shadow painted into the art (sprite_shape.h) at gPaintedShadow: the
    // whole patch evenly (checkerboard holes too), near black, so it darkens
    // the ground a little instead of hiding it.
    uint category = uint(s.light.x);
    float painted = 0.0;
    if (s.op == OP_LIT && category >= CAT_WALL && category != CAT_CRITTER && gPaintedShadow < 1.0) {
        painted = paintedShadowAt(s, pix);
        a = lerp(a, gPaintedShadow, painted);
        art.rgb = lerp(art.rgb, float3(0.04, 0.04, 0.04), painted);
    }
    // A checkerboard see-through region (map_renderer.cc checkerFlags) evenly
    // at gCheckerAlpha instead of a dot grid.
    if (gCheckerAlpha < 1.0 && s.op != OP_TRANS) {
        a = lerp(a, gCheckerAlpha, checkerAt(s, pix) * (1.0 - painted));
    }
    if (a <= 0.0) {
        discard;
    }

    int level = int(s.param);
    if (s.op == OP_FLOOR) {
        level = clamp(floorIntensity(s.param, t) >> 9, 0, 255);
    }

    if (s.op == OP_TRANS) {
        // psHd's translucency with the light left out: lerp(dest, table
        // color, gray), i.e. "over" with weight a * gray.
        float3 c = art.rgb;
        float gray = (s.table >> 8) == 0 ? (c.b + 3.0 * c.r + 6.0 * c.g) / 10.0 : (c.r + 5.0 * c.g + 4.0 * c.b) / 10.0;
        float g = clamp(gray * 7.97 - 0.5, 0.0, 7.0) / 7.0;
        GbufOut o = (GbufOut)0;
        o.albedo = float4(baseColor(s.tableColor) * (g * a), g * a);
        o.coverage = float4(a, 0.0, 0.0, a);
        return o;
    }
    if (s.op == OP_EGG) {
        a *= eggWeight(s, pix);
    }
    // Palette-cycle colors (P6.1, P6.2): HD art animated by cycleAt, original
    // art at its live color. All ranges but the shoreline glow: emissive
    // (never darker than the art in the light pass, and the bloom's source).
    // Shoreline water is lit like the floor it lies on.
    Cycle cycle;
    if (hd) {
        cycle = cycleAt(s, pix);
    } else {
        cycle = cycleExact(index);
    }
    art.rgb = max(art.rgb + cycle.delta, 0.0);

    bool flat = category == CAT_FLOOR || category == CAT_ROOF || (s.light.y & LIGHT_FLAT) != 0;
    float3 n = float3(0.0, 0.0, 1.0);
    float height = 0.0;
    if (!flat) {
        shapeAt(s, pix + toward * gScale, n, height);
    }

    // Surface relief from the HD albedo's luminance read as a height field
    // (fo2hd.hdassets.add_detail): Sobel, in the 4x masters' units.
    float4 k = gCategory[category];
    float3 detail = n;
    if (hd && k.x > 0.0) {
        int2 q = pix - s.origin * gScale;
        float l00 = hdLuma(s, q + int2(-1, -1));
        float l10 = hdLuma(s, q + int2(0, -1));
        float l20 = hdLuma(s, q + int2(1, -1));
        float l01 = hdLuma(s, q + int2(-1, 0));
        float l21 = hdLuma(s, q + int2(1, 0));
        float l02 = hdLuma(s, q + int2(-1, 1));
        float l12 = hdLuma(s, q + int2(0, 1));
        float l22 = hdLuma(s, q + int2(1, 1));
        float dx = ((l20 + 2.0 * l21 + l22) - (l00 + 2.0 * l01 + l02)) * gDetailScale;
        float dy = ((l02 + 2.0 * l12 + l22) - (l00 + 2.0 * l10 + l20)) * gDetailScale;
        // Flat: screen y runs along v (foreshortened 2:1); upright: down = -z.
        float3 tv = flat ? float3(0.0, 1.0, 0.0) : float3(0.0, 0.0, -1.0);
        float dv = flat ? dy * 0.5 : dy;
        detail = normalize(n - k.x * (dx * float3(1.0, 0.0, 0.0) + dv * tv));
    }

    GbufOut o;
    o.albedo = float4(art.rgb * a, a);
    o.baseNormal = encodeNormal(n, a);
    o.detailNormal = encodeNormal(detail, a);
    o.surface = float4(height * a, level / 128.0 * a, k.z * a, a);
    // Flames and neon near a lamp or fire: bright, saturated art.
    float glow = 0.0;
    if ((s.light.y & LIGHT_GLOW) != 0) {
        float value = max(art.r, max(art.g, art.b));
        float chroma = (value - min(art.r, min(art.g, art.b))) / max(value, 1e-3);
        glow = smoothstep(0.55, 0.8, value) * smoothstep(0.35, 0.6, chroma);
    }
    glow = max(glow, cycle.glow);
    // g: 1 = roof (ambient only); up to 0.25 = the share of the sun's shadow this art
    // ignores, which flat art doesn't: it lies on the ground and takes shadows like it.
    float materialG = category == CAT_ROOF ? 1.0 : (flat ? 0.0 : k.w * 0.25);
    o.material = float4(cycle.screens * a, materialG * a, k.y * a, a);
    o.coverage = float4(a, glow * a, cycle.bloom * a, a);
    return o;
}

// Occluder heightfield (P5.5): every opaque texel of an upright object
// (walls, scenery, items, misc; not flat ones) puts its height at its ground
// point, 1x, highest wins. Whole frames, also the parts outside the view. One
// thread group per sprite. Critters stay out: their shadows are painted in.
RWTexture2D<uint> gOccluders : register(u1);

[numthreads(64, 1, 1)]
void csOccluders(uint3 group : SV_GroupID, uint3 thread : SV_GroupThreadID)
{
    Sprite s = gSprites[gSpriteBase + group.x];
    if (s.op != OP_LIT || uint(s.light.x) < CAT_WALL || uint(s.light.x) == CAT_CRITTER || (s.light.y & LIGHT_FLAT) != 0) {
        return;
    }
    int count = s.size.x * s.size.y;
    for (int k = int(thread.x); k < count; k += 64) {
        int2 e = int2(k % s.size.x, k / s.size.x);
        if (gAtlas.Load(int3(s.src + e, 0)) == 0) {
            continue;
        }
        float4 raw = gShape.Load(int3(s.src + e, 0));
        if (shapePainted(raw)) {
            continue; // a painted shadow casts none
        }
        float height = shapeHeight(raw);
        int2 ground = s.origin + e + int2(0, int(round(height))) + gOccluderOrigin;
        if (height > 3.0 && all(ground >= 0) && all(ground < gOccluderSize)) {
            InterlockedMax(gOccluders[ground], asuint(height));
        }
    }
}
)hlsl";

    struct Sprite {
        int rect[4];
        int origin[2];
        int src[2];
        int size[2];
        unsigned int op;
        unsigned int param;
        int egg[4];
        int eggSrc[2];
        unsigned int table;
        unsigned int tableColor;
        int hd[4];
        int light[4];
    };
    static_assert(sizeof(Sprite) == 112, "Sprite must match the HLSL struct");

    enum class CommandKind {
        Raster,
        PixelOp,
        Outline,
    };

    struct Command {
        CommandKind kind;
        int first;
        int count;
    };

    struct PassConstants {
        int targetSize[2];
        int scale;
        int spriteBase;
    };

    // cbuffer Gbuffer (psGbuffer, csOccluders).
    struct GbufferConstants {
        float category[kLightCategoryCount][4];
        float detailScale;
        int occluderOrigin[2];
        float paintedShadow;
        int occluderSize[2];
        float checkerAlpha;
        int pad3;
    };
    static_assert(sizeof(GbufferConstants) == 176, "GbufferConstants must match the HLSL cbuffer");

    struct Target {
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11RenderTargetView> rtv;
        ComPtr<ID3D11UnorderedAccessView> uav;
        ComPtr<ID3D11ShaderResourceView> srv;
        int width = 0;
        int height = 0;
    };

    struct HdTarget {
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11RenderTargetView> rtv;
        ComPtr<ID3D11ShaderResourceView> srv;
        ComPtr<ID3D11UnorderedAccessView> uav; // the lighting pass writes here
        int width = 0;
        int height = 0;
    };

    // Shelf packer for the atlas and the text texture.
    struct Packer {
        struct Shelf {
            int y;
            int height;
            int x;
        };

        int width = 0;
        int height = 0;
        int nextY = 0;
        std::vector<Shelf> shelves;

        void reset(int w, int h)
        {
            width = w;
            height = h;
            nextY = 0;
            shelves.clear();
        }

        bool alloc(int w, int h, int* x, int* y)
        {
            if (w > width) {
                return false;
            }
            for (Shelf& shelf : shelves) {
                if (h <= shelf.height && h * 2 > shelf.height && shelf.x + w <= width) {
                    *x = shelf.x;
                    *y = shelf.y;
                    shelf.x += w;
                    return true;
                }
            }
            if (nextY + h > height) {
                return false;
            }
            shelves.push_back({ nextY, h, w });
            *x = 0;
            *y = nextY;
            nextY += h;
            return true;
        }
    };

    struct AtlasEntry {
        int x;
        int y;
        int width;
        int height;
        unsigned int hash; // FNV-1a of the indices (HD packs carry the same)
        bool hasCycle; // uses palette-cycle colors (229..254)
        bool hasShape; // its shape is in the shape atlas (new light)
        // Interface art only: the pixels last uploaded (see atlasFrame).
        std::vector<unsigned char> pixels;
    };

    struct State {
        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* context = nullptr;

        ComPtr<ID3D11VertexShader> spriteVs;
        ComPtr<ID3D11PixelShader> spritePs;
        ComPtr<ID3D11ComputeShader> pixelOpCs;
        ComPtr<ID3D11ComputeShader> outlineRowsCs;
        ComPtr<ID3D11ComputeShader> outlineColumnsCs;
        ComPtr<ID3D11RasterizerState> rasterizer;
        ComPtr<ID3D11Buffer> constants;

        ComPtr<ID3D11Texture2D> atlas;
        ComPtr<ID3D11ShaderResourceView> atlasView;
        Packer atlasPacker;
        std::unordered_map<unsigned long long, AtlasEntry> atlasEntries;

        ComPtr<ID3D11Texture2D> text;
        ComPtr<ID3D11ShaderResourceView> textView;
        Packer textPacker;

        ComPtr<ID3D11Texture2D> tables;
        ComPtr<ID3D11ShaderResourceView> tablesView;
        std::vector<unsigned char> tablesCpu;
        bool tablesValid = false;

        ComPtr<ID3D11Texture2D> floorCells;
        ComPtr<ID3D11ShaderResourceView> floorCellsView;

        ComPtr<ID3D11Buffer> sprites;
        ComPtr<ID3D11ShaderResourceView> spritesView;
        int spriteCapacity = 0;
        ComPtr<ID3D11Buffer> vertexLight;
        ComPtr<ID3D11ShaderResourceView> vertexLightView;
        int vertexLightCapacity = 0;

        std::vector<Sprite> spriteList;
        std::vector<int> vertexLightList;
        std::vector<Command> commands;
        int mapWidth = 0;
        int mapHeight = 0;
        bool built = false;

        Target targets[5]; // by scale
        MapRenderStats stats;

        // HD color pass (P4.2).
        ComPtr<ID3D11PixelShader> hdPs;
        ComPtr<ID3D11BlendState> hdBlend;
        ComPtr<ID3D11Buffer> basePalette;
        ComPtr<ID3D11ShaderResourceView> basePaletteView;
        HdTarget hdTargets[5]; // by scale

        // New light (P5.2): the sprites' shapes in an atlas parallel to the
        // index atlas (same coordinates), the G-buffer pass and the
        // occluder scatter.
        ComPtr<ID3D11Texture2D> shapeAtlas;
        ComPtr<ID3D11ShaderResourceView> shapeAtlasView;
        bool shapesWanted = false;
        bool paintedFaded = false; // painted shadows drawn faint in the last G-buffer draw
        std::vector<unsigned char> shapeScratch;
        std::vector<std::pair<int, int>> glowPoints; // screen centers of lamps' and fires' hexes (kLightGlow)
        ComPtr<ID3D11PixelShader> gbufferPs;
        ComPtr<ID3D11BlendState> gbufferBlend;
        ComPtr<ID3D11ComputeShader> occludersCs;
        ComPtr<ID3D11Buffer> gbufferConstants;
        int hdTier = 0; // tier the sprites' HD fields were built for, 0 = none
        bool hasCycle = false; // the built frame shows palette-cycle colors
    };

    State* gState = nullptr;

    unsigned int fnv1a(const unsigned char* data, size_t size)
    {
        unsigned int h = 2166136261u;
        for (size_t i = 0; i < size; i++) {
            h = (h ^ data[i]) * 16777619u;
        }
        return h;
    }

    bool failed(HRESULT hr, const char* what)
    {
        if (FAILED(hr)) {
            debugPrint("HD: map renderer: %s failed (0x%08x)\n", what, static_cast<unsigned>(hr));
            return true;
        }
        return false;
    }

    bool createTexture(int width, int height, DXGI_FORMAT format, const void* data, int pitch, D3D11_USAGE usage, ComPtr<ID3D11Texture2D>& texture, ComPtr<ID3D11ShaderResourceView>& view)
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
        D3D11_SUBRESOURCE_DATA init = { data, static_cast<UINT>(pitch), 0 };
        if (failed(gState->device->CreateTexture2D(&desc, data != nullptr ? &init : nullptr, &texture), "CreateTexture2D")) {
            return false;
        }
        return !failed(gState->device->CreateShaderResourceView(texture.Get(), nullptr, &view), "CreateShaderResourceView");
    }

    // Grows a dynamic structured buffer to hold `count` elements.
    bool ensureStructured(ComPtr<ID3D11Buffer>& buffer, ComPtr<ID3D11ShaderResourceView>& view, int& capacity, int count, int stride)
    {
        if (count <= capacity && buffer) {
            return true;
        }
        int newCapacity = std::max(count, std::max(1024, capacity * 2));
        D3D11_BUFFER_DESC desc = {};
        desc.ByteWidth = newCapacity * stride;
        desc.Usage = D3D11_USAGE_DYNAMIC;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        desc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        desc.StructureByteStride = stride;
        buffer.Reset();
        view.Reset();
        if (failed(gState->device->CreateBuffer(&desc, nullptr, &buffer), "CreateBuffer")
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

    // Palette-cycle colors at phase 0: cycle.cc's tables before the first
    // rotation (8-bit, the game keeps them >> 2), the alarm at its peak.
    // The HD art was upscaled with these (fo2hd.frm.load_palette).
    constexpr unsigned char kCyclePhase0[26][3] = {
        { 0, 108, 0 }, { 11, 115, 7 }, { 27, 123, 15 }, { 43, 131, 27 }, // slime 229
        { 107, 107, 111 }, { 99, 103, 127 }, { 87, 107, 143 }, { 0, 147, 163 }, { 107, 187, 255 }, // monitors 233
        { 255, 0, 0 }, { 215, 0, 0 }, { 147, 43, 11 }, { 255, 119, 0 }, { 255, 59, 0 }, // slow fire 238
        { 71, 0, 0 }, { 123, 0, 0 }, { 179, 0, 0 }, { 123, 0, 0 }, { 71, 0, 0 }, // fast fire 243
        { 83, 63, 43 }, { 75, 59, 43 }, { 67, 55, 39 }, { 63, 51, 39 }, { 55, 47, 35 }, { 51, 43, 35 }, // shoreline 248
        { 252, 0, 0 }, // alarm 254
    };

    // The base palette for the HD passes: the caller's 256 colors, then at
    // 256 + index the phase-0 cycle colors (the shaders' cycleAt).
    bool uploadBasePalette(const float* palette)
    {
        float colors[512 * 4] = {};
        memcpy(colors, palette, 256 * 4 * sizeof(float));
        for (int i = 0; i < 26; i++) {
            for (int c = 0; c < 3; c++) {
                colors[(256 + 229 + i) * 4 + c] = (kCyclePhase0[i][c] >> 2) * 4 / 255.0f;
            }
            colors[(256 + 229 + i) * 4 + 3] = 1.0f;
        }
        return upload(gState->basePalette.Get(), colors, sizeof(colors));
    }

    // Which triangle (and which row/column of it) last writes each cell of
    // tile.cc's _intensity_map, replaying its fill order.
    std::vector<unsigned short> buildFloorCells()
    {
        std::vector<unsigned short> cells(kIntensityMapSize, 0);
        for (int tri = 0; tri < 5; tri++) {
            int p = kVertexOffsets[kRightsideUpTriangles[tri][2]];
            for (int i = 0; i < 13; i++) {
                p += kRightsideUpTable[i][0];
                for (int j = 0; j < kRightsideUpTable[i][1]; j++) {
                    cells[p++] = static_cast<unsigned short>(0x8000 | (tri << 11) | (i << 6) | j);
                }
            }
        }
        for (int tri = 0; tri < 5; tri++) {
            int p = kVertexOffsets[kUpsideDownTriangles[tri][0]];
            for (int i = 0; i < 13; i++) {
                p += kUpsideDownTable[i][0];
                for (int j = 0; j < kUpsideDownTable[i][1]; j++) {
                    cells[p++] = static_cast<unsigned short>(0x8000 | 0x4000 | (tri << 11) | (i << 6) | j);
                }
            }
        }
        return cells;
    }

    // CE's lookup tables, laid out as the shaders read them. Re-uploaded
    // when any of them changes (palette loads rebuild intensityColorTable).
    void syncTables()
    {
        std::vector<unsigned char> rows(256 * kTableRows, 0);
        for (int color = 0; color < 256; color++) {
            memcpy(&rows[(kTableIntensity + color) * 256], intensityColorTable[color], 256);
            memcpy(&rows[(kTableMixAdd + color) * 256], colorMixAddTable[color], 256);
        }

        for (int table = 0; table < 5; table++) {
            const Color* blend = _blendTable[blendTableColor(table)];
            if (blend != nullptr) {
                memcpy(&rows[(kTableBlend + table * 16) * 256], blend, 16 * 256);
            }
        }

        memcpy(&rows[kTableGray * 256], _commonGrayTable, 256);
        // object.cc keeps its glass gray table private; same formula.
        for (int index = 0; index < 256; index++) {
            int rgb = Color2RGB(static_cast<Color>(index));
            int r = (rgb & 0x7C00) >> 10;
            int g = (rgb & 0x3E0) >> 5;
            int b = rgb & 0x1F;
            rows[(kTableGray + 1) * 256 + index] = static_cast<unsigned char>(((r + 5 * g + 4 * b) / 10) >> 2);
        }
        rows[(kTableGray + 1) * 256] = 0;

        if (gState->tablesValid && rows == gState->tablesCpu) {
            return;
        }
        gState->tablesCpu.swap(rows);
        gState->context->UpdateSubresource(gState->tables.Get(), 0, nullptr, gState->tablesCpu.data(), 256, 0);
        gState->tablesValid = true;
    }

    unsigned long long atlasKey(int fid, int frame, int rotation)
    {
        return (static_cast<unsigned long long>(static_cast<unsigned int>(fid)) << 32) | (static_cast<unsigned int>(rotation) << 16) | static_cast<unsigned int>(frame & 0xFFFF);
    }

    void resetAtlas()
    {
        gState->atlasEntries.clear();
        gState->atlasPacker.reset(kAtlasSize, kAtlasSize);
    }

    // The frame's place in the atlas, uploading it on first use. Returns
    // null if the art can't be loaded or the atlas is full (`full` set).
    //
    // Art is immutable except interface art: game_mouse.cc draws action
    // points, accuracy and the "no path" X into the hex and bouncing cursor
    // frames in place. Those frames are compared on every use and uploaded
    // again when they change.
    // Round scenery (the author, 2026-11-09: trees, cacti, bushes, rocks and
    // barrels should look more 3D) gets a rounded body (sprite_shape.h) of
    // [hd_light] round_body strength and its own light category (round=).
    // Known by art name; cliffs, fences and buildings stay as they were.
    // Default 0.5: the author's blind vote of 2026-11-10 (0.5 won 5 of 6 against
    // 0, 1 and 1.5; "the difference was very hard to tell").
    float roundBodyStrength()
    {
        static const float strength = [] {
            double value = 0.5;
            configGetDouble(&gGameConfig, "hd_light", "round_body", &value);
            return static_cast<float>(std::max(value, 0.0));
        }();
        return strength;
    }

    bool roundScenery(int fid)
    {
        if (FrmId(fid).objectType() != OBJ_TYPE_SCENERY || roundBodyStrength() <= 0.0f) {
            return false;
        }
        static std::unordered_map<int, bool> known;
        int key = fid & 0x0F000FFF;
        auto it = known.find(key);
        if (it != known.end()) {
            return it->second;
        }
        char name[16] = {};
        bool round = false;
        if (artCopyFileName(FrmId(fid), name) == 0) {
            for (char& c : name) {
                c = static_cast<char>(c >= 'A' && c <= 'Z' ? c - 'A' + 'a' : c);
            }
            static const char* const kPrefixes[] = { "tree", "tre1", "bush", "cac", "rock", "drock", "modrock", "mbrocks", "barrel", "brl", "weed", "cvweed" };
            for (const char* prefix : kPrefixes) {
                if (strncmp(name, prefix, strlen(prefix)) == 0) {
                    round = true;
                    break;
                }
            }
        }
        known[key] = round;
        return round;
    }

    // Shadows painted into the original world art (the author, 2026-11-10: "the
    // old shadows are still a pain"; they fall to the lower right whatever
    // the sun does). tools/scripts/painted_shadows.py writes a 1x mask per
    // listed sprite, direction and frame to hd\painted_shadows.json; masked
    // texels lie flat on the ground in the shape (sprite_shape.h), cast no
    // sun shadow and are drawn [hd_light] painted_shadows strong (1 = as
    // painted, 0 = gone). Without the file the art stays as it was. Default
    // 0.15 by the author's blind vote (round 8b, 2026-11-11: painted 3 of 23,
    // 0.3 / 0.15 / 0 near even at 14 / 14 / 13; he took 0.15).
    float paintedShadowStrength()
    {
        static const float strength = [] {
            double value = 0.15;
            configGetDouble(&gGameConfig, "hd_light", "painted_shadows", &value);
            return static_cast<float>(std::min(std::max(value, 0.0), 1.0));
        }();
        return strength;
    }

    struct PaintedShadowMask {
        int rotation;
        int frame;
        int width;
        int height;
        std::vector<int> runs; // y, x, length
    };

    const std::unordered_map<std::string, std::vector<PaintedShadowMask>>& paintedShadowMasks()
    {
        static const auto table = [] {
            std::unordered_map<std::string, std::vector<PaintedShadowMask>> result;
            if (paintedShadowStrength() >= 1.0f) {
                return result;
            }
            std::vector<char> text;
            if (FILE* file = fopen("hd\\painted_shadows.json", "rb")) {
                char buffer[65536];
                size_t n;
                while ((n = fread(buffer, 1, sizeof(buffer), file)) > 0) {
                    text.insert(text.end(), buffer, buffer + n);
                }
                fclose(file);
            }
            JsonValue root;
            std::string error;
            if (text.empty() || !jsonParse(text.data(), text.size(), root, &error)) {
                debugPrint("HD: painted shadows: no hd\\painted_shadows.json%s%s; painted shadows stay as they are\n", error.empty() ? "" : ", ", error.c_str());
                return result;
            }
            size_t frames = 0;
            for (const auto& sprite : root["sprites"].members) {
                std::vector<PaintedShadowMask>& masks = result[sprite.first];
                for (const JsonValue& entry : sprite.second.items) {
                    PaintedShadowMask mask = { entry["rotation"].asInt(), entry["frame"].asInt(), entry["width"].asInt(), entry["height"].asInt(), {} };
                    for (const JsonValue& value : entry["runs"].items) {
                        mask.runs.push_back(value.asInt());
                    }
                    masks.push_back(std::move(mask));
                    frames++;
                }
            }
            debugPrint("HD: painted shadows: %d sprites, %d frames, drawn %.2f strong\n", static_cast<int>(result.size()), static_cast<int>(frames), paintedShadowStrength());
            return result;
        }();
        return table;
    }

    // The frame's painted shadow as one byte per texel (1 = shadow) into
    // `mask`; false when it has none.
    bool paintedShadow(int fid, int frame, int rotation, int width, int height, std::vector<unsigned char>& mask)
    {
        static const char* const kKinds[] = { "items", "critters", "scenery", "walls", "tiles", "misc" };
        int type = FrmId(fid).objectType();
        if (type != OBJ_TYPE_ITEM && type != OBJ_TYPE_SCENERY && type != OBJ_TYPE_WALL && type != OBJ_TYPE_MISC) {
            return false;
        }
        const auto& table = paintedShadowMasks();
        char name[16] = {};
        if (table.empty() || artCopyFileName(FrmId(fid), name) != 0) {
            return false;
        }
        std::string key = std::string(kKinds[type]) + "/";
        for (const char* c = name; *c != '\0' && *c != '.'; c++) {
            key += static_cast<char>(*c >= 'A' && *c <= 'Z' ? *c - 'A' + 'a' : *c);
        }
        auto it = table.find(key);
        if (it == table.end()) {
            return false;
        }
        // Its own direction's mask, else direction 0's (the tool writes only
        // directions whose pixels differ).
        const PaintedShadowMask* found = nullptr;
        for (const PaintedShadowMask& m : it->second) {
            if (m.frame == frame && m.width == width && m.height == height && (m.rotation == rotation || (m.rotation == 0 && found == nullptr))) {
                found = &m;
            }
        }
        if (found == nullptr) {
            return false;
        }
        mask.assign(static_cast<size_t>(width) * height, 0);
        for (size_t k = 0; k + 2 < found->runs.size(); k += 3) {
            int y = found->runs[k];
            int x = found->runs[k + 1];
            int length = found->runs[k + 2];
            if (y >= 0 && y < height && x >= 0 && x + length <= width) {
                memset(mask.data() + static_cast<size_t>(y) * width + x, 1, length);
            }
        }
        return true;
    }

    // Checkerboard see-through (the author, 2026-11-12, after the eye's patrol of
    // 0.9.3). 71 original pieces fake translucency with a checkerboard of
    // see-through pixels: the shadow sprites of the Vertibird, the shuttle,
    // the Reno signs and the radar dish (already drawn evenly as painted
    // shadows), dark roof tiles in tent doorways and on roof edges, black
    // wall tops, exit grids (left as they are). Kept as is, HD shows a
    // coarse dot grid, and the tiling edge fill rounds the dots along a
    // region's border into dark beads. [hd_light] checker_alpha < 1 draws
    // such regions evenly at that coverage; 1 leaves the art as it is. Default
    // 0.5, the dither's own average, by the author's pick from a before/after of
    // 14 tour views (2026-11-12, reports\art-lab\checker).
    float checkerAlpha()
    {
        static const float value = [] {
            double v = 0.5;
            configGetDouble(&gGameConfig, "hd_light", "checker_alpha", &v);
            return static_cast<float>(std::min(std::max(v, 0.0), 1.0));
        }();
        return value;
    }

    // The frame's checkerboard regions as one byte per texel (1 = in one)
    // into `mask`; false when it has none. A texel is strict when its four
    // sides have the other opacity and its four diagonals the same (a 3x3
    // checkerboard around it); 8-connected strict texels make a region when
    // there are at least 8 of them. Each region then takes, twice, the texels
    // next to it that are still mostly checkerboard (6 of the 8 tests: its
    // border rows), and last the see-through texels on its four sides, so
    // the edge fill has nothing there to round into beads. 1 px diagonal
    // lines score 6 too but have no strict texel, so they stay as drawn.
    bool checkerRegions(const unsigned char* pixels, int width, int height, std::vector<unsigned char>& mask)
    {
        auto opaque = [&](int x, int y) {
            x = std::min(std::max(x, 0), width - 1);
            y = std::min(std::max(y, 0), height - 1);
            return pixels[static_cast<size_t>(y) * width + x] != 0;
        };
        auto score = [&](int x, int y) {
            bool o = opaque(x, y);
            int s = 0;
            s += opaque(x - 1, y) != o;
            s += opaque(x + 1, y) != o;
            s += opaque(x, y - 1) != o;
            s += opaque(x, y + 1) != o;
            s += opaque(x - 1, y - 1) == o;
            s += opaque(x + 1, y - 1) == o;
            s += opaque(x - 1, y + 1) == o;
            s += opaque(x + 1, y + 1) == o;
            return s;
        };
        size_t count = static_cast<size_t>(width) * height;
        std::vector<unsigned char> strict(count, 0);
        bool any = false;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                if (score(x, y) == 8) {
                    strict[static_cast<size_t>(y) * width + x] = 1;
                    any = true;
                }
            }
        }
        if (!any) {
            return false;
        }
        mask.assign(count, 0);
        std::vector<int> stack;
        std::vector<int> part;
        any = false;
        for (size_t start = 0; start < count; start++) {
            if (strict[start] != 1) {
                continue;
            }
            part.clear();
            stack.assign(1, static_cast<int>(start));
            strict[start] = 2;
            while (!stack.empty()) {
                int i = stack.back();
                stack.pop_back();
                part.push_back(i);
                int x = i % width;
                int y = i / width;
                for (int dy = -1; dy <= 1; dy++) {
                    for (int dx = -1; dx <= 1; dx++) {
                        int nx = x + dx;
                        int ny = y + dy;
                        if (nx >= 0 && ny >= 0 && nx < width && ny < height && strict[static_cast<size_t>(ny) * width + nx] == 1) {
                            strict[static_cast<size_t>(ny) * width + nx] = 2;
                            stack.push_back(ny * width + nx);
                        }
                    }
                }
            }
            if (part.size() >= 8) {
                for (int i : part) {
                    mask[i] = 1;
                }
                any = true;
            }
        }
        if (!any) {
            return false;
        }
        std::vector<unsigned char> before;
        for (int pass = 0; pass < 3; pass++) {
            before = mask;
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < width; x++) {
                    size_t i = static_cast<size_t>(y) * width + x;
                    if (before[i] != 0 || (pass == 2 && opaque(x, y))) {
                        continue;
                    }
                    bool touches = false;
                    for (int dy = -1; dy <= 1 && !touches; dy++) {
                        for (int dx = -1; dx <= 1 && !touches; dx++) {
                            int nx = x + dx;
                            int ny = y + dy;
                            bool side = dx == 0 || dy == 0;
                            if ((pass < 2 || side) && nx >= 0 && ny >= 0 && nx < width && ny < height && before[static_cast<size_t>(ny) * width + nx] != 0) {
                                touches = true;
                            }
                        }
                    }
                    if (touches && (pass == 2 || score(x, y) >= 6)) {
                        mask[i] = 1;
                    }
                }
            }
        }
        return true;
    }

    // The frame's shape (sprite_shape.h) into the shape atlas, at the
    // entry's place. With checker_alpha on, the checkerboard regions'
    // see-through texels count as solid for the shape (no bumps at every
    // hole) and are flagged in A's bit 6; heights then top out at 1023 px.
    void uploadShape(int fid, int frame, int rotation, AtlasEntry& entry, const unsigned char* data)
    {
        gState->shapeScratch.resize(static_cast<size_t>(entry.width) * entry.height * 4);
        std::vector<unsigned char> painted;
        bool hasPainted = paintedShadow(fid, frame, rotation, entry.width, entry.height, painted);
        std::vector<unsigned char> checker;
        std::vector<unsigned char> filled;
        // Not the exit grids (misc art): they overlap one another, and where
        // two even half-covers overlap they print rectangles; checkerboards
        // of the same phase overlap without a trace.
        bool hasChecker = checkerAlpha() < 1.0f && FrmId(fid).objectType() != OBJ_TYPE_MISC && checkerRegions(data, entry.width, entry.height, checker);
        const unsigned char* shapeSource = data;
        if (hasChecker) {
            filled.assign(data, data + static_cast<size_t>(entry.width) * entry.height);
            for (size_t i = 0; i < filled.size(); i++) {
                if (checker[i] != 0 && filled[i] == 0) {
                    filled[i] = 1;
                }
            }
            shapeSource = filled.data();
        }
        spriteShape(shapeSource, entry.width, entry.height, FrmId(fid).objectType(), gState->shapeScratch.data(), roundScenery(fid) ? roundBodyStrength() : 0.0f, hasPainted ? painted.data() : nullptr);
        if (checkerAlpha() < 1.0f) {
            unsigned char* rgba = gState->shapeScratch.data();
            for (size_t i = 0; i < static_cast<size_t>(entry.width) * entry.height; i++) {
                if ((rgba[i * 4 + 3] & 0x40) != 0) {
                    rgba[i * 4 + 2] = 0xFF;
                    rgba[i * 4 + 3] = static_cast<unsigned char>((rgba[i * 4 + 3] & 0x80) | 0x3F);
                }
                if (hasChecker && checker[i] != 0) {
                    rgba[i * 4 + 3] |= 0x40;
                }
            }
        }
        D3D11_BOX box = { static_cast<UINT>(entry.x), static_cast<UINT>(entry.y), 0, static_cast<UINT>(entry.x + entry.width), static_cast<UINT>(entry.y + entry.height), 1 };
        gState->context->UpdateSubresource(gState->shapeAtlas.Get(), 0, &box, gState->shapeScratch.data(), entry.width * 4, 0);
        entry.hasShape = true;
    }

    // Shapes are computed only while the new light is on; frames that went
    // into the atlas before get theirs on next use.
    void ensureShape(int fid, int frame, int rotation, AtlasEntry& entry)
    {
        if (!gState->shapesWanted || entry.hasShape) {
            return;
        }
        CacheEntry* handle;
        Art* art = artLock(FrmId(fid), &handle);
        if (art == nullptr) {
            return;
        }
        unsigned char* data = artGetFrameData(art, frame, static_cast<Rotation>(rotation));
        if (data != nullptr) {
            uploadShape(fid, frame, rotation, entry, data);
        }
        artUnlock(handle);
    }

    const AtlasEntry* atlasFrame(int fid, int frame, int rotation, bool* full)
    {
        unsigned long long key = atlasKey(fid, frame, rotation);
        auto it = gState->atlasEntries.find(key);
        bool mutableArt = FrmId(fid).objectType() == OBJ_TYPE_INTERFACE;
        if (it != gState->atlasEntries.end() && !mutableArt) {
            ensureShape(fid, frame, rotation, it->second);
            return &it->second;
        }

        CacheEntry* handle;
        Art* art = artLock(FrmId(fid), &handle);
        if (art == nullptr) {
            return nullptr;
        }
        int width = 0;
        int height = 0;
        artGetSize(art, frame, static_cast<Rotation>(rotation), &width, &height);
        unsigned char* data = artGetFrameData(art, frame, static_cast<Rotation>(rotation));
        size_t size = static_cast<size_t>(width) * height;
        bool ok = data != nullptr && width > 0 && height > 0;

        AtlasEntry* entry = nullptr;
        if (ok && it != gState->atlasEntries.end() && it->second.width == width && it->second.height == height) {
            entry = &it->second;
            if (memcmp(entry->pixels.data(), data, size) == 0) {
                artUnlock(handle);
                return entry;
            }
        } else if (ok) {
            AtlasEntry fresh = { 0, 0, width, height, 0, false, false, {} };
            if (!gState->atlasPacker.alloc(width, height, &fresh.x, &fresh.y)) {
                *full = true;
                ok = false;
            } else {
                gState->atlasEntries.erase(key);
                entry = &gState->atlasEntries.emplace(key, std::move(fresh)).first->second;
            }
        }
        if (ok) {
            D3D11_BOX box = { static_cast<UINT>(entry->x), static_cast<UINT>(entry->y), 0, static_cast<UINT>(entry->x + width), static_cast<UINT>(entry->y + height), 1 };
            gState->context->UpdateSubresource(gState->atlas.Get(), 0, &box, data, width, 0);
            gState->stats.atlasUploads++;
            entry->hash = fnv1a(data, size);
            entry->hasCycle = false;
            for (size_t i = 0; i < size; i++) {
                if (data[i] >= 0xE5 && data[i] != 0xFF) {
                    entry->hasCycle = true;
                    break;
                }
            }
            if (mutableArt) {
                entry->pixels.assign(data, data + size);
            }
            entry->hasShape = false;
            if (gState->shapesWanted && !mutableArt) {
                uploadShape(fid, frame, rotation, *entry, data);
            }
        }
        artUnlock(handle);
        return ok ? entry : nullptr;
    }

    bool intersect(const Rect& a, const Rect& b, Rect* out)
    {
        out->left = std::max(a.left, b.left);
        out->top = std::max(a.top, b.top);
        out->right = std::min(a.right, b.right);
        out->bottom = std::min(a.bottom, b.bottom);
        return out->left <= out->right && out->top <= out->bottom;
    }

    Sprite makeSprite(unsigned int op, const Rect& rect, int originX, int originY, const AtlasEntry* source)
    {
        if (source != nullptr && source->hasCycle) {
            gState->hasCycle = true;
        }
        Sprite s = {};
        s.op = op;
        s.rect[0] = rect.left;
        s.rect[1] = rect.top;
        s.rect[2] = rect.right;
        s.rect[3] = rect.bottom;
        s.origin[0] = originX;
        s.origin[1] = originY;
        if (source != nullptr) {
            s.src[0] = source->x;
            s.src[1] = source->y;
            s.size[0] = source->width;
            s.size[1] = source->height;
        }
        s.egg[0] = 0;
        s.egg[1] = 0;
        s.egg[2] = -1; // no egg
        s.egg[3] = -1;
        return s;
    }

    void addOneSprite(CommandKind kind, const Sprite& sprite)
    {
        int index = static_cast<int>(gState->spriteList.size());
        gState->spriteList.push_back(sprite);
        if (kind == CommandKind::Raster && !gState->commands.empty() && gState->commands.back().kind == CommandKind::Raster) {
            gState->commands.back().count++;
        } else {
            gState->commands.push_back({ kind, index, 1 });
        }
        if (kind == CommandKind::PixelOp) {
            gState->stats.computeOps++;
        } else if (kind == CommandKind::Outline) {
            gState->stats.outlines++;
        }
        gState->stats.opCounts[kind == CommandKind::Outline ? kMapOpOutline : sprite.op]++;
    }

    // `tiles` (from setHd): the frame is larger than a pool page, so its HD
    // art is in tiles, each in its own place in the pool. One sprite per tile
    // then, over the tile's part of the rect: every pass reads the art at
    // the pixel it draws (hdLuma's neighbors stay inside the tile, light.zw),
    // so the pieces draw what one sprite would. The occluder pass walks the
    // whole frame per piece; its max makes that the same.
    void addSprite(CommandKind kind, const Sprite& sprite, const std::vector<HdFrameTile>* tiles = nullptr)
    {
        if (tiles == nullptr || tiles->empty() || (sprite.hd[3] & kHdArt) == 0) {
            addOneSprite(kind, sprite);
            return;
        }
        int tier = gState->hdTier;
        Rect rect = { sprite.rect[0], sprite.rect[1], sprite.rect[2], sprite.rect[3] };
        for (const HdFrameTile& tile : *tiles) {
            Rect part = { sprite.origin[0] + tile.x, sprite.origin[1] + tile.y, sprite.origin[0] + tile.x + tile.width - 1, sprite.origin[1] + tile.y + tile.height - 1 };
            Rect cut;
            if (!intersect(rect, part, &cut)) {
                continue;
            }
            Sprite piece = sprite;
            piece.rect[0] = cut.left;
            piece.rect[1] = cut.top;
            piece.rect[2] = cut.right;
            piece.rect[3] = cut.bottom;
            piece.hd[0] = tile.frame.x - tile.x * tier;
            piece.hd[1] = tile.frame.y - tile.y * tier;
            piece.hd[2] = tile.frame.slice;
            piece.light[2] = (tile.x * tier) | ((tile.y * tier) << 16);
            piece.light[3] = ((tile.x + tile.width) * tier) | (((tile.y + tile.height) * tier) << 16);
            addOneSprite(kind, piece);
        }
    }

    MapLightCategory lightCategory(const DrawRecord& record)
    {
        if (record.op == DrawOp::Floor) {
            return kLightFloor;
        }
        if (record.op == DrawOp::Roof) {
            return kLightRoof;
        }
        switch (FrmId(record.fid).objectType()) {
        case OBJ_TYPE_WALL:
            return kLightWall;
        case OBJ_TYPE_SCENERY:
            return roundScenery(record.fid) ? kLightRound : kLightScenery;
        case OBJ_TYPE_CRITTER:
            return kLightCritter;
        case OBJ_TYPE_ITEM:
            return kLightItem;
        case OBJ_TYPE_MISC:
            return kLightMisc;
        default:
            return kLightNone;
        }
    }

    // Hexes of the lights that make nearby flames and neon glow (kLightGlow):
    // objects with a light on the current elevation, critters excepted. Kept
    // as screen points: tileDistanceBetween walks the hex path, too slow for
    // every sprite against every light.
    void gatherGlowPoints()
    {
        gState->glowPoints.clear();
        for (int tile = 0; tile < HEX_GRID_SIZE; tile++) {
            for (ObjectListNode* node = objectListAtTile(tile); node != nullptr; node = node->next) {
                Object* object = node->obj;
                int x;
                int y;
                if (object->elevation == gElevation && object->lightDistance > 0 && object->lightIntensity > 0
                    && FrmId(object->fid).objectType() != OBJ_TYPE_CRITTER && tileToScreenXY(tile, &x, &y) == 0) {
                    gState->glowPoints.emplace_back(x, y);
                    break;
                }
            }
        }
    }

    // Within about 2 hexes of a glow light (ground units: v = 2 * screen y,
    // a hex is 32 wide).
    bool nearGlowPoint(int tile)
    {
        int x;
        int y;
        if (gState->glowPoints.empty() || tileToScreenXY(tile, &x, &y) != 0) {
            return false;
        }
        for (const auto& light : gState->glowPoints) {
            int dx = x - light.first;
            int dy = 2 * (y - light.second);
            if (dx * dx + dy * dy <= 80 * 80) {
                return true;
            }
        }
        return false;
    }

    // Where the HD pass finds this frame's HD art (hd/hd_art.h), or zeros:
    // the original art. Also the sprite's light category and flags for the
    // G-buffer. A frame larger than a pool page comes in `tiles` (the part
    // of it the sprite's rect shows), for addSprite to split the sprite by;
    // without `tiles` such a frame keeps the original art.
    void setHd(Sprite& s, const DrawRecord& record, int frame, int rotation, const AtlasEntry* art, std::vector<HdFrameTile>* tiles = nullptr)
    {
        s.light[0] = lightCategory(record);
        s.light[1] = record.op == DrawOp::Object && (record.flags & OBJECT_FLAT) != 0 ? kLightFlat : 0;
        if (gState->shapesWanted && record.op == DrawOp::Object && record.tile >= 0 && nearGlowPoint(record.tile)) {
            s.light[1] |= kLightGlow;
        }
        if (gState->hdTier == 0) {
            return;
        }
        if (tiles != nullptr && !hdArtFitsPage(art->width, art->height)) {
            int x = s.rect[0] - s.origin[0];
            int y = s.rect[1] - s.origin[1];
            if (hdArtFrameTiles(record.fid, nullptr, rotation, frame, art->hash, art->width, art->height, x, y, s.rect[2] - s.rect[0] + 1, s.rect[3] - s.rect[1] + 1, tiles)) {
                s.hd[3] = kHdArt | (tiles->front().frame.hardAlpha ? hardAlphaFlags() : 0);
                if (tiles->front().frame.hardAlpha && edgeClampFor(static_cast<MapLightCategory>(s.light[0]))) {
                    s.hd[3] |= kHdEdgeClamp;
                }
            }
            return;
        }
        HdFrame hd;
        if (hdArtFrame(record.fid, rotation, frame, art->hash, art->width, art->height, &hd)) {
            s.hd[0] = hd.x;
            s.hd[1] = hd.y;
            s.hd[2] = hd.slice;
            s.hd[3] = kHdArt | (hd.hardAlpha ? hardAlphaFlags() : 0);
            if (hd.hardAlpha && edgeClampFor(static_cast<MapLightCategory>(s.light[0]))) {
                s.hd[3] |= kHdEdgeClamp;
            }
        }
    }

    // A lit blit, split around the egg the way roofs and walls are: outside
    // the egg rect as _dark_trans_buf_to_buf, inside through the egg mask.
    void addLitWithEgg(const Rect& rect, int originX, int originY, const AtlasEntry* art, int light, const DrawRecord& record, bool* full, int frame = 0, int rotation = 0)
    {
        Sprite lit = makeSprite(kOpLit, rect, originX, originY, art);
        lit.param = light / 512;
        std::vector<HdFrameTile> tiles;
        setHd(lit, record, frame, rotation, art, &tiles);

        Rect cut;
        if (record.egg && intersect(record.eggRect, rect, &cut)) {
            const AtlasEntry* egg = atlasFrame(record.eggFid, 0, 0, full);
            if (egg != nullptr) {
                lit.egg[0] = record.eggRect.left;
                lit.egg[1] = record.eggRect.top;
                lit.egg[2] = record.eggRect.right;
                lit.egg[3] = record.eggRect.bottom;
                addSprite(CommandKind::Raster, lit, &tiles);

                Sprite mask = makeSprite(kOpEgg, cut, originX, originY, art);
                mask.param = light / 512;
                memcpy(mask.hd, lit.hd, sizeof(mask.hd));
                memcpy(mask.light, lit.light, sizeof(mask.light));
                mask.egg[0] = record.eggRect.left;
                mask.egg[1] = record.eggRect.top;
                mask.egg[2] = record.eggRect.right;
                mask.egg[3] = record.eggRect.bottom;
                mask.eggSrc[0] = egg->x;
                mask.eggSrc[1] = egg->y;
                addSprite(CommandKind::PixelOp, mask, &tiles);
                return;
            }
        }
        addSprite(CommandKind::Raster, lit, &tiles);
    }

    void addFloor(const DrawRecord& record, bool* full)
    {
        const AtlasEntry* art = atlasFrame(record.fid, 0, 0, full);
        if (art == nullptr) {
            return;
        }

        // tileRenderFloor's clipping, quirks included (the width isn't
        // reduced when the refresh rect starts left of the window).
        int W = gState->mapWidth;
        int H = gState->mapHeight;
        int left = record.clip.left;
        int top = record.clip.top;
        int width = record.clip.right - record.clip.left + 1;
        int height = record.clip.bottom - record.clip.top + 1;
        int x = record.x;
        int y = record.y;
        if (left < 0) {
            left = 0;
        }
        if (top < 0) {
            top = 0;
        }
        if (left + width > W) {
            width = W - left;
        }
        if (top + height > H) {
            height = H - top;
        }
        if (x >= W || x > record.clip.right || y >= H || y > record.clip.bottom) {
            return;
        }

        int frameWidth = art->width;
        int frameHeight = art->height;
        int w;
        int h;
        if (left < x) {
            w = frameWidth + x <= left + width ? frameWidth : left + width - x;
        } else {
            int sx = left - x;
            x = left;
            w = std::min(frameWidth - sx, width);
        }
        if (top < y) {
            h = frameHeight + y <= top + height ? frameHeight : top + height - y;
        } else {
            int sy = top - y;
            y = top;
            h = std::min(frameHeight - sy, height);
        }
        if (w <= 0 || h <= 0) {
            return;
        }

        Rect rect = { x, y, x + w - 1, y + h - 1 };
        bool uniform = true;
        for (int i = 1; i < 10; i++) {
            if (record.vertexIntensities[i] != record.vertexIntensities[0]) {
                uniform = false;
                break;
            }
        }

        Sprite s = makeSprite(uniform ? kOpLit : kOpFloor, rect, record.x, record.y, art);
        setHd(s, record, 0, 0, art);
        if (uniform) {
            s.param = record.vertexIntensities[0] / 512;
        } else {
            s.param = static_cast<unsigned int>(gState->vertexLightList.size());
            gState->vertexLightList.insert(gState->vertexLightList.end(), record.vertexIntensities, record.vertexIntensities + 10);
        }
        addSprite(CommandKind::Raster, s);
    }

    void addRoof(const DrawRecord& record, bool* full)
    {
        const AtlasEntry* art = atlasFrame(record.fid, 0, 0, full);
        if (art == nullptr) {
            return;
        }
        Rect rect;
        Rect tileRect = { record.x, record.y, record.x + art->width - 1, record.y + art->height - 1 };
        if (!intersect(tileRect, record.clip, &rect)) {
            return;
        }
        addLitWithEgg(rect, record.x, record.y, art, record.light, record, full);
    }

    void addObject(const DrawRecord& record, bool* full)
    {
        const AtlasEntry* art = atlasFrame(record.fid, record.frame, record.rotation, full);
        if (art == nullptr) {
            return;
        }
        Rect rect;
        Rect objectRect = { record.x, record.y, record.x + record.width - 1, record.y + record.height - 1 };
        if (!intersect(objectRect, record.clip, &rect)) {
            return;
        }

        switch (record.blend) {
        case ObjectBlend::Interface:
            addSprite(CommandKind::Raster, makeSprite(kOpCopy, rect, record.x, record.y, art));
            return;
        case ObjectBlend::Normal:
            addLitWithEgg(rect, record.x, record.y, art, record.light, record, full, record.frame, record.rotation);
            return;
        default:
            break;
        }

        Sprite s = makeSprite(kOpTrans, rect, record.x, record.y, art);
        int light = record.light;
        unsigned int table = kBlendWall;
        unsigned int gray = 0;
        switch (record.blend) {
        case ObjectBlend::TransRed:
            table = kBlendRed;
            break;
        case ObjectBlend::TransWall:
            table = kBlendWall;
            light = 0x10000;
            break;
        case ObjectBlend::TransGlass:
            table = kBlendGlass;
            gray = 1;
            break;
        case ObjectBlend::TransSteam:
            table = kBlendSteam;
            break;
        case ObjectBlend::TransEnergy:
            table = kBlendEnergy;
            break;
        default:
            break;
        }
        s.param = light / 512;
        s.table = table | (gray << 8);
        s.tableColor = blendTableColor(table);
        std::vector<HdFrameTile> tiles;
        setHd(s, record, record.frame, record.rotation, art, &tiles);
        addSprite(CommandKind::PixelOp, s, &tiles);
    }

    void addOutline(const DrawRecord& record, bool* full)
    {
        const AtlasEntry* art = atlasFrame(record.fid, record.frame, record.rotation, full);
        if (art == nullptr) {
            return;
        }
        int frameWidth = record.width;
        int frameHeight = record.height;
        Rect objectRect = { record.x, record.y, record.x + frameWidth - 1, record.y + frameHeight - 1 };
        Rect visible;
        if (!intersect(objectRect, record.clip, &visible)) {
            return;
        }

        // objectDrawOutline's choice of colors.
        int paletted = (record.outline & OUTLINE_PALETTED) != 0 ? 1 : 0;
        int type = record.outline & OUTLINE_TYPE_MAX;
        int color;
        int count = 0;
        int band = 0;
        int blend = 0;
        switch (type) {
        case OUTLINE_TYPE_HOSTILE:
            color = 243;
            paletted = 0;
            count = 5;
            band = frameHeight / 5;
            break;
        case OUTLINE_TYPE_SAME_TEAM:
            color = COLOR_RED;
            blend = kBlendRed;
            break;
        case OUTLINE_TYPE_BODY:
            color = COLOR_GREY_2;
            blend = kBlendWall;
            break;
        case OUTLINE_TYPE_FRIENDLY:
            count = 4;
            band = frameHeight / 4;
            color = 229;
            paletted = 0;
            break;
        case OUTLINE_TYPE_ITEM:
            color = COLOR_LIGHT_GOLD_2;
            blend = kBlendRed;
            break;
        case OUTLINE_TYPE_BLOCKED:
            color = 61;
            paletted = 0;
            count = 1;
            band = frameHeight;
            break;
        default:
            color = COLOR_MAGENTA;
            paletted = 0;
            break;
        }

        Sprite s = makeSprite(0, objectRect, record.x, record.y, art);
        s.size[0] = frameWidth;
        s.size[1] = frameHeight;
        s.egg[0] = visible.left - record.x;
        s.egg[1] = visible.top - record.y;
        s.egg[2] = visible.right - record.x;
        s.egg[3] = visible.bottom - record.y;
        s.param = static_cast<unsigned int>(color & 0xFF) | (static_cast<unsigned int>(count) << 8) | (static_cast<unsigned int>(band) << 16);
        s.table = paletted != 0 ? (0x10000u | (static_cast<unsigned int>(blend) << 8)) : 0;
        addSprite(CommandKind::Outline, s);
    }

    void addText(const DrawRecord& record)
    {
        Rect rect;
        Rect textRect = { record.x, record.y, record.x + record.width - 1, record.y + record.height - 1 };
        if (!intersect(textRect, record.clip, &rect)) {
            return;
        }
        AtlasEntry entry = { 0, 0, record.width, record.height, 0, false, {} };
        if (!gState->textPacker.alloc(record.width, record.height, &entry.x, &entry.y)) {
            debugPrint("HD: map renderer: text texture full, %dx%d text skipped\n", record.width, record.height);
            return;
        }
        const std::vector<unsigned char>& pixels = drawTextPixels(record.textIndex);
        D3D11_BOX box = { static_cast<UINT>(entry.x), static_cast<UINT>(entry.y), 0, static_cast<UINT>(entry.x + record.width), static_cast<UINT>(entry.y + record.height), 1 };
        gState->context->UpdateSubresource(gState->text.Get(), 0, &box, pixels.data(), record.width, 0);
        addSprite(CommandKind::Raster, makeSprite(kOpText, rect, record.x, record.y, &entry));
    }

    // One attempt at turning the draw list into sprites; false if the atlas
    // filled up on the way.
    bool buildOnce()
    {
        gState->spriteList.clear();
        gState->vertexLightList.clear();
        gState->commands.clear();
        gState->textPacker.reset(kTextWidth, kTextHeight);
        gState->stats.computeOps = 0;
        gState->stats.outlines = 0;
        gState->hasCycle = false;
        memset(gState->stats.opCounts, 0, sizeof(gState->stats.opCounts));

        Rect window = { 0, 0, gState->mapWidth - 1, gState->mapHeight - 1 };
        bool full = false;
        for (const DrawRecord& record : drawRecords()) {
            switch (record.op) {
            case DrawOp::Pass:
                break;
            case DrawOp::Fill: {
                Rect rect;
                if (intersect(record.clip, window, &rect)) {
                    Sprite s = makeSprite(kOpFill, rect, 0, 0, nullptr);
                    s.param = static_cast<unsigned int>(record.color);
                    if (record.color >= 0xE5 && record.color < 0xFF) {
                        gState->hasCycle = true;
                    }
                    addSprite(CommandKind::Raster, s);
                }
                break;
            }
            case DrawOp::Floor:
                addFloor(record, &full);
                break;
            case DrawOp::Roof:
                addRoof(record, &full);
                break;
            case DrawOp::Object:
                addObject(record, &full);
                break;
            case DrawOp::Outline:
                addOutline(record, &full);
                break;
            case DrawOp::Text:
                addText(record);
                break;
            }
            if (full) {
                return false;
            }
        }
        return true;
    }

    bool ensureTarget(int scale)
    {
        Target& target = gState->targets[scale];
        int width = gState->mapWidth * scale;
        int height = gState->mapHeight * scale;
        if (target.texture && target.width == width && target.height == height) {
            return true;
        }
        target = Target();
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = width;
        desc.Height = height;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R32_UINT;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
        if (failed(gState->device->CreateTexture2D(&desc, nullptr, &target.texture), "map target")
            || failed(gState->device->CreateRenderTargetView(target.texture.Get(), nullptr, &target.rtv), "map target RTV")
            || failed(gState->device->CreateUnorderedAccessView(target.texture.Get(), nullptr, &target.uav), "map target UAV")
            || failed(gState->device->CreateShaderResourceView(target.texture.Get(), nullptr, &target.srv), "map target SRV")) {
            target = Target();
            return false;
        }
        target.width = width;
        target.height = height;
        return true;
    }

    bool ensureHdTarget(int scale)
    {
        HdTarget& target = gState->hdTargets[scale];
        int width = gState->mapWidth * scale;
        int height = gState->mapHeight * scale;
        if (target.texture && target.width == width && target.height == height) {
            return true;
        }
        target = HdTarget();
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = width;
        desc.Height = height;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
        if (failed(gState->device->CreateTexture2D(&desc, nullptr, &target.texture), "HD target")
            || failed(gState->device->CreateRenderTargetView(target.texture.Get(), nullptr, &target.rtv), "HD target RTV")
            || failed(gState->device->CreateShaderResourceView(target.texture.Get(), nullptr, &target.srv), "HD target SRV")
            || failed(gState->device->CreateUnorderedAccessView(target.texture.Get(), nullptr, &target.uav), "HD target UAV")) {
            target = HdTarget();
            return false;
        }
        target.width = width;
        target.height = height;
        return true;
    }

    void setPassConstants(int width, int height, int scale, int base)
    {
        PassConstants constants = { { width, height }, scale, base };
        upload(gState->constants.Get(), &constants, sizeof(constants));
    }

} // namespace

bool mapRendererInit()
{
    mapRendererShutdown();
    ID3D11Device* device = presenterDevice();
    ID3D11DeviceContext* context = presenterContext();
    if (device == nullptr || context == nullptr) {
        return false;
    }

    gState = new State();
    gState->device = device;
    gState->context = context;

    ComPtr<ID3DBlob> vs;
    ComPtr<ID3DBlob> ps;
    ComPtr<ID3DBlob> pixelOp;
    ComPtr<ID3DBlob> rows;
    ComPtr<ID3DBlob> columns;
    vs.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "vsSprite", "vs_5_0"));
    ps.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "psSprite", "ps_5_0"));
    pixelOp.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "csPixelOp", "cs_5_0"));
    rows.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "csOutlineRows", "cs_5_0"));
    columns.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "csOutlineColumns", "cs_5_0"));
    ComPtr<ID3DBlob> hd;
    hd.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "psHd", "ps_5_0"));
    if (!vs || !ps || !pixelOp || !rows || !columns || !hd
        || failed(device->CreatePixelShader(hd->GetBufferPointer(), hd->GetBufferSize(), nullptr, &gState->hdPs), "HD PS")
        || failed(device->CreateVertexShader(vs->GetBufferPointer(), vs->GetBufferSize(), nullptr, &gState->spriteVs), "sprite VS")
        || failed(device->CreatePixelShader(ps->GetBufferPointer(), ps->GetBufferSize(), nullptr, &gState->spritePs), "sprite PS")
        || failed(device->CreateComputeShader(pixelOp->GetBufferPointer(), pixelOp->GetBufferSize(), nullptr, &gState->pixelOpCs), "pixel op CS")
        || failed(device->CreateComputeShader(rows->GetBufferPointer(), rows->GetBufferSize(), nullptr, &gState->outlineRowsCs), "outline rows CS")
        || failed(device->CreateComputeShader(columns->GetBufferPointer(), columns->GetBufferSize(), nullptr, &gState->outlineColumnsCs), "outline columns CS")) {
        mapRendererShutdown();
        return false;
    }

    D3D11_RASTERIZER_DESC rasterizer = {};
    rasterizer.FillMode = D3D11_FILL_SOLID;
    rasterizer.CullMode = D3D11_CULL_NONE;
    rasterizer.DepthClipEnable = TRUE;
    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(PassConstants);
    constants.Usage = D3D11_USAGE_DYNAMIC;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    constants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    std::vector<unsigned short> cells = buildFloorCells();
    if (failed(device->CreateRasterizerState(&rasterizer, &gState->rasterizer), "rasterizer state")
        || failed(device->CreateBuffer(&constants, nullptr, &gState->constants), "constants")
        || !createTexture(kAtlasSize, kAtlasSize, DXGI_FORMAT_R8_UINT, nullptr, 0, D3D11_USAGE_DEFAULT, gState->atlas, gState->atlasView)
        || !createTexture(kTextWidth, kTextHeight, DXGI_FORMAT_R8_UINT, nullptr, 0, D3D11_USAGE_DEFAULT, gState->text, gState->textView)
        || !createTexture(256, kTableRows, DXGI_FORMAT_R8_UINT, nullptr, 0, D3D11_USAGE_DEFAULT, gState->tables, gState->tablesView)
        || !createTexture(kIntensityMapWidth, kIntensityMapSize / kIntensityMapWidth, DXGI_FORMAT_R16_UINT, cells.data(), kIntensityMapWidth * 2, D3D11_USAGE_IMMUTABLE, gState->floorCells, gState->floorCellsView)) {
        mapRendererShutdown();
        return false;
    }

    // HD pass: dest = o0 + dest * o1 (see psHd), and the base palette.
    D3D11_BLEND_DESC blend = {};
    blend.RenderTarget[0].BlendEnable = TRUE;
    blend.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlend = D3D11_BLEND_SRC1_COLOR;
    blend.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_SRC1_ALPHA;
    blend.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    int paletteCapacity = 0;
    if (failed(device->CreateBlendState(&blend, &gState->hdBlend), "HD blend state")
        || !ensureStructured(gState->basePalette, gState->basePaletteView, paletteCapacity, 512, 4 * sizeof(float))) {
        mapRendererShutdown();
        return false;
    }

    // New light (P5.2): G-buffer targets each blended "premultiplied over"
    // with their own alpha; the shape atlas; the occluder scatter.
    ComPtr<ID3DBlob> gbuffer;
    ComPtr<ID3DBlob> occluders;
    gbuffer.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "psGbuffer", "ps_5_0"));
    occluders.Attach(compileShader(kShaderSource, sizeof(kShaderSource) - 1, "hd_map.hlsl", "csOccluders", "cs_5_0"));
    D3D11_BLEND_DESC over = {};
    over.IndependentBlendEnable = FALSE;
    over.RenderTarget[0].BlendEnable = TRUE;
    over.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    over.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    over.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    over.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    over.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    over.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    over.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    D3D11_BUFFER_DESC gbufferConstants = {};
    gbufferConstants.ByteWidth = sizeof(GbufferConstants);
    gbufferConstants.Usage = D3D11_USAGE_DYNAMIC;
    gbufferConstants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    gbufferConstants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (!gbuffer || !occluders
        || failed(device->CreatePixelShader(gbuffer->GetBufferPointer(), gbuffer->GetBufferSize(), nullptr, &gState->gbufferPs), "G-buffer PS")
        || failed(device->CreateComputeShader(occluders->GetBufferPointer(), occluders->GetBufferSize(), nullptr, &gState->occludersCs), "occluders CS")
        || failed(device->CreateBlendState(&over, &gState->gbufferBlend), "G-buffer blend state")
        || failed(device->CreateBuffer(&gbufferConstants, nullptr, &gState->gbufferConstants), "G-buffer constants")
        || !createTexture(kAtlasSize, kAtlasSize, DXGI_FORMAT_R8G8B8A8_UNORM, nullptr, 0, D3D11_USAGE_DEFAULT, gState->shapeAtlas, gState->shapeAtlasView)) {
        mapRendererShutdown();
        return false;
    }

    resetAtlas();
    debugPrint("HD: map renderer up\n");
    return true;
}

void mapRendererShutdown()
{
    delete gState;
    gState = nullptr;
}

bool mapRendererBuild(int width, int height, int hdTier)
{
    if (gState == nullptr || width <= 0 || height <= 0) {
        return false;
    }
    gState->built = false;
    gState->mapWidth = width;
    gState->mapHeight = height;
    gState->stats.records = static_cast<int>(drawRecords().size());

    syncTables();
    if (gState->shapesWanted) {
        gatherGlowPoints();
    }
    gState->hdTier = hdTier >= 2 && hdTier <= 4 && hdArtActive() ? hdTier : 0;
    if (gState->hdTier != 0) {
        hdArtBeginBuild(gState->hdTier);
    }
    bool ok = buildOnce();
    if (!ok) {
        // Atlas full: start it over with only what this frame needs.
        resetAtlas();
        gState->stats.atlasResets++;
        ok = buildOnce();
        if (!ok) {
            debugPrint("HD: map renderer: one frame needs more than the %dx%d atlas\n", kAtlasSize, kAtlasSize);
        }
    }
    if (gState->hdTier != 0) {
        hdArtEndBuild();
    }
    if (!ok) {
        return false;
    }

    int spriteCount = static_cast<int>(gState->spriteList.size());
    int lightCount = static_cast<int>(gState->vertexLightList.size());
    if (!ensureStructured(gState->sprites, gState->spritesView, gState->spriteCapacity, spriteCount, sizeof(Sprite))
        || !ensureStructured(gState->vertexLight, gState->vertexLightView, gState->vertexLightCapacity, std::max(lightCount, 1), sizeof(int))
        || !upload(gState->sprites.Get(), gState->spriteList.data(), spriteCount * sizeof(Sprite))
        || !upload(gState->vertexLight.Get(), gState->vertexLightList.data(), lightCount * sizeof(int))) {
        return false;
    }

    gState->stats.sprites = spriteCount;
    gState->stats.rasterBatches = 0;
    for (const Command& command : gState->commands) {
        if (command.kind == CommandKind::Raster) {
            gState->stats.rasterBatches++;
        }
    }
    gState->stats.atlasUsed = static_cast<float>(gState->atlasPacker.nextY) / kAtlasSize;
    gState->built = true;
    return true;
}

bool mapRendererDraw(int scale)
{
    if (gState == nullptr || !gState->built || scale < 1 || scale > 4 || !ensureTarget(scale)) {
        return false;
    }

    ID3D11DeviceContext* ctx = gState->context;
    Target& target = gState->targets[scale];
    const UINT zero[4] = { 0, 0, 0, 0 };
    ctx->ClearUnorderedAccessViewUint(target.uav.Get(), zero);

    ID3D11ShaderResourceView* views[6] = {
        gState->spritesView.Get(),
        gState->atlasView.Get(),
        gState->textView.Get(),
        gState->tablesView.Get(),
        gState->floorCellsView.Get(),
        gState->vertexLightView.Get(),
    };
    ID3D11Buffer* constants = gState->constants.Get();
    ID3D11ShaderResourceView* nullViews[6] = {};
    ID3D11UnorderedAccessView* nullUav = nullptr;

    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->IASetInputLayout(nullptr);
    ctx->VSSetShader(gState->spriteVs.Get(), nullptr, 0);
    ctx->VSSetShaderResources(0, 1, views);
    ctx->VSSetConstantBuffers(0, 1, &constants);
    ctx->PSSetShader(gState->spritePs.Get(), nullptr, 0);
    ctx->PSSetShaderResources(0, 6, views);
    ctx->PSSetConstantBuffers(0, 1, &constants);
    ctx->CSSetShaderResources(0, 6, views);
    ctx->CSSetConstantBuffers(0, 1, &constants);
    ctx->RSSetState(gState->rasterizer.Get());
    ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx->OMSetDepthStencilState(nullptr, 0);
    D3D11_VIEWPORT viewport = { 0, 0, static_cast<float>(target.width), static_cast<float>(target.height), 0.0f, 1.0f };
    ctx->RSSetViewports(1, &viewport);

    // The target is bound either as render target or as UAV; switching
    // unbinds the other, and D3D11 orders the work across the switch.
    bool rtvBound = false;
    bool uavBound = false;
    for (const Command& command : gState->commands) {
        setPassConstants(target.width, target.height, scale, command.first);
        if (command.kind == CommandKind::Raster) {
            if (!rtvBound) {
                ctx->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
                uavBound = false;
                ID3D11RenderTargetView* rtv = target.rtv.Get();
                ctx->OMSetRenderTargets(1, &rtv, nullptr);
                rtvBound = true;
            }
            ctx->DrawInstanced(6, command.count, 0, 0);
            continue;
        }

        if (!uavBound) {
            ctx->OMSetRenderTargets(0, nullptr, nullptr);
            rtvBound = false;
            ID3D11UnorderedAccessView* uav = target.uav.Get();
            ctx->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
            uavBound = true;
        }
        const Sprite& s = gState->spriteList[command.first];
        if (command.kind == CommandKind::PixelOp) {
            int width = (s.rect[2] - s.rect[0] + 1) * scale;
            int height = (s.rect[3] - s.rect[1] + 1) * scale;
            ctx->CSSetShader(gState->pixelOpCs.Get(), nullptr, 0);
            ctx->Dispatch((width + 7) / 8, (height + 7) / 8, 1);
        } else {
            ctx->CSSetShader(gState->outlineRowsCs.Get(), nullptr, 0);
            ctx->Dispatch((s.size[1] + 63) / 64, 1, 1);
            ctx->CSSetShader(gState->outlineColumnsCs.Get(), nullptr, 0);
            ctx->Dispatch((s.size[0] + 63) / 64, 1, 1);
        }
    }

    ctx->OMSetRenderTargets(0, nullptr, nullptr);
    ctx->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
    ctx->VSSetShaderResources(0, 1, nullViews);
    ctx->PSSetShaderResources(0, 6, nullViews);
    ctx->CSSetShaderResources(0, 6, nullViews);
    ctx->RSSetState(nullptr);
    return true;
}

ID3D11ShaderResourceView* mapRendererView(int scale)
{
    if (gState == nullptr || scale < 1 || scale > 4) {
        return nullptr;
    }
    return gState->targets[scale].srv.Get();
}

int mapRendererHdTier()
{
    return gState != nullptr && gState->built ? gState->hdTier : 0;
}

bool mapRendererHasCycle()
{
    return gState != nullptr && gState->hasCycle;
}

void mapRendererCyclePhase0(int index, float rgb[3])
{
    for (int c = 0; c < 3; c++) {
        rgb[c] = index >= 229 && index <= 254 ? (kCyclePhase0[index - 229][c] >> 2) * 4 / 255.0f : 0.0f;
    }
}

bool mapRendererDrawHd(int scale, const float* palette)
{
    if (gState == nullptr || !gState->built || gState->hdTier == 0 || scale != gState->hdTier) {
        return false;
    }
    if (!ensureHdTarget(scale)) {
        return false;
    }
    HdTarget& target = gState->hdTargets[scale];
    int width = target.width;
    int height = target.height;
    if (!uploadBasePalette(palette)) {
        return false;
    }

    ID3D11DeviceContext* ctx = gState->context;
    const float clear[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    ctx->ClearRenderTargetView(target.rtv.Get(), clear);

    ID3D11ShaderResourceView* views[8] = {
        gState->spritesView.Get(),
        gState->atlasView.Get(),
        gState->textView.Get(),
        gState->tablesView.Get(),
        gState->floorCellsView.Get(),
        gState->vertexLightView.Get(),
        hdArtPoolView(),
        gState->basePaletteView.Get(),
    };
    ID3D11ShaderResourceView* nullViews[8] = {};
    ID3D11Buffer* constants = gState->constants.Get();
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->IASetInputLayout(nullptr);
    ctx->VSSetShader(gState->spriteVs.Get(), nullptr, 0);
    ctx->VSSetShaderResources(0, 1, views);
    ctx->VSSetConstantBuffers(0, 1, &constants);
    ctx->PSSetShader(gState->hdPs.Get(), nullptr, 0);
    ctx->PSSetShaderResources(0, 8, views);
    ctx->PSSetConstantBuffers(0, 1, &constants);
    ctx->RSSetState(gState->rasterizer.Get());
    ctx->OMSetBlendState(gState->hdBlend.Get(), nullptr, 0xFFFFFFFF);
    ctx->OMSetDepthStencilState(nullptr, 0);
    D3D11_VIEWPORT viewport = { 0, 0, static_cast<float>(width), static_cast<float>(height), 0.0f, 1.0f };
    ctx->RSSetViewports(1, &viewport);
    ID3D11RenderTargetView* rtv = target.rtv.Get();
    ctx->OMSetRenderTargets(1, &rtv, nullptr);

    // Same command list as the exact pass; compute ops become single
    // sprites (psHd handles them with blending), outlines come from the
    // exact pass at composite time.
    for (const Command& command : gState->commands) {
        if (command.kind == CommandKind::Outline) {
            continue;
        }
        setPassConstants(width, height, scale, command.first);
        ctx->DrawInstanced(6, command.count, 0, 0);
    }

    ctx->OMSetRenderTargets(0, nullptr, nullptr);
    ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx->VSSetShaderResources(0, 1, nullViews);
    ctx->PSSetShaderResources(0, 8, nullViews);
    ctx->RSSetState(nullptr);
    return true;
}

ID3D11ShaderResourceView* mapRendererHdView(int scale)
{
    if (gState == nullptr || scale < 2 || scale > 4) {
        return nullptr;
    }
    return gState->hdTargets[scale].srv.Get();
}

ID3D11UnorderedAccessView* mapRendererHdUav(int scale)
{
    if (gState == nullptr || scale < 2 || scale > 4 || !ensureHdTarget(scale)) {
        return nullptr;
    }
    return gState->hdTargets[scale].uav.Get();
}

bool mapRendererPaintedShadowTexel(int fid, int frame, int rotation, int width, int height, int x, int y)
{
    // The self-test asks pixel by pixel; one frame's mask is kept.
    static int lastFid = -1;
    static int lastFrame = -1;
    static int lastRotation = -1;
    static bool lastHas = false;
    static std::vector<unsigned char> lastMask;
    if (gState == nullptr || !gState->shapesWanted || !gState->paintedFaded || paintedShadowStrength() >= 1.0f || x < 0 || y < 0 || x >= width || y >= height) {
        return false;
    }
    if (fid != lastFid || frame != lastFrame || rotation != lastRotation || lastMask.size() != static_cast<size_t>(width) * height) {
        lastFid = fid;
        lastFrame = frame;
        lastRotation = rotation;
        lastHas = paintedShadow(fid, frame, rotation, width, height, lastMask);
        if (!lastHas) {
            lastMask.assign(static_cast<size_t>(width) * height, 0);
        }
    }
    return lastHas && lastMask[static_cast<size_t>(y) * width + x] != 0;
}

void mapRendererWantShapes(bool wanted)
{
    if (gState != nullptr) {
        gState->shapesWanted = wanted;
    }
}

bool mapRendererDrawGbuffer(int scale, const float* palette, const MapGbufferDraw& draw)
{
    if (gState == nullptr || !gState->built || gState->hdTier == 0 || scale != gState->hdTier) {
        return false;
    }
    int width = gState->mapWidth * scale;
    int height = gState->mapHeight * scale;
    GbufferConstants constants = {};
    memcpy(constants.category, draw.category, sizeof(constants.category));
    constants.detailScale = draw.detailScale;
    constants.paintedShadow = draw.sun ? paintedShadowStrength() : 1.0f;
    gState->paintedFaded = constants.paintedShadow < 1.0f;
    constants.checkerAlpha = checkerAlpha();
    constants.occluderOrigin[0] = draw.occluderOrigin[0];
    constants.occluderOrigin[1] = draw.occluderOrigin[1];
    constants.occluderSize[0] = draw.occluderSize[0];
    constants.occluderSize[1] = draw.occluderSize[1];
    if (!uploadBasePalette(palette)
        || !upload(gState->gbufferConstants.Get(), &constants, sizeof(constants))) {
        return false;
    }

    ID3D11DeviceContext* ctx = gState->context;
    const float clear[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    for (ID3D11RenderTargetView* target : draw.targets) {
        ctx->ClearRenderTargetView(target, clear);
    }

    ID3D11ShaderResourceView* views[9] = {
        gState->spritesView.Get(),
        gState->atlasView.Get(),
        gState->textView.Get(),
        gState->tablesView.Get(),
        gState->floorCellsView.Get(),
        gState->vertexLightView.Get(),
        hdArtPoolView(),
        gState->basePaletteView.Get(),
        gState->shapeAtlasView.Get(),
    };
    ID3D11ShaderResourceView* nullViews[9] = {};
    ID3D11Buffer* buffers[2] = { gState->constants.Get(), gState->gbufferConstants.Get() };
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->IASetInputLayout(nullptr);
    ctx->VSSetShader(gState->spriteVs.Get(), nullptr, 0);
    ctx->VSSetShaderResources(0, 1, views);
    ctx->VSSetConstantBuffers(0, 1, buffers);
    ctx->PSSetShader(gState->gbufferPs.Get(), nullptr, 0);
    ctx->PSSetShaderResources(0, 9, views);
    ctx->PSSetConstantBuffers(0, 2, buffers);
    ctx->RSSetState(gState->rasterizer.Get());
    ctx->OMSetBlendState(gState->gbufferBlend.Get(), nullptr, 0xFFFFFFFF);
    ctx->OMSetDepthStencilState(nullptr, 0);
    D3D11_VIEWPORT viewport = { 0, 0, static_cast<float>(width), static_cast<float>(height), 0.0f, 1.0f };
    ctx->RSSetViewports(1, &viewport);
    ctx->OMSetRenderTargets(6, draw.targets, nullptr);

    // Same commands as the HD pass (outlines come from the exact pass).
    for (const Command& command : gState->commands) {
        if (command.kind == CommandKind::Outline) {
            continue;
        }
        setPassConstants(width, height, scale, command.first);
        ctx->DrawInstanced(6, command.count, 0, 0);
    }
    ctx->OMSetRenderTargets(0, nullptr, nullptr);
    ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx->VSSetShaderResources(0, 1, nullViews);
    ctx->PSSetShaderResources(0, 9, nullViews);
    ctx->RSSetState(nullptr);

    // Occluder heightfield: one thread group per sprite.
    int spriteCount = static_cast<int>(gState->spriteList.size());
    if (draw.occluders != nullptr && spriteCount > 0) {
        setPassConstants(width, height, scale, 0);
        ctx->CSSetShader(gState->occludersCs.Get(), nullptr, 0);
        ctx->CSSetShaderResources(0, 9, views);
        ctx->CSSetConstantBuffers(0, 2, buffers);
        ID3D11UnorderedAccessView* uavs[2] = { nullptr, draw.occluders };
        ctx->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);
        ctx->Dispatch(static_cast<UINT>(spriteCount), 1, 1);
        ID3D11UnorderedAccessView* nullUavs[2] = {};
        ctx->CSSetUnorderedAccessViews(0, 2, nullUavs, nullptr);
        ctx->CSSetShaderResources(0, 9, nullViews);
    }
    return true;
}

namespace {

    float halfToFloat(unsigned short h)
    {
        unsigned int sign = (h >> 15) & 1;
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
        return sign != 0 ? -value : value;
    }

} // namespace

bool mapRendererHdReadback(int scale, std::vector<float>& rgba, int* width, int* height)
{
    if (gState == nullptr || scale < 2 || scale > 4 || !gState->hdTargets[scale].texture) {
        return false;
    }
    const HdTarget& target = gState->hdTargets[scale];
    D3D11_TEXTURE2D_DESC desc;
    target.texture->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    if (failed(gState->device->CreateTexture2D(&desc, nullptr, &staging), "HD readback staging")) {
        return false;
    }
    gState->context->CopyResource(staging.Get(), target.texture.Get());
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (failed(gState->context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "HD readback map")) {
        return false;
    }
    rgba.resize(static_cast<size_t>(target.width) * target.height * 4);
    for (int y = 0; y < target.height; y++) {
        const unsigned short* row = reinterpret_cast<const unsigned short*>(static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * y);
        for (int x = 0; x < target.width * 4; x++) {
            rgba[static_cast<size_t>(y) * target.width * 4 + x] = halfToFloat(row[x]);
        }
    }
    gState->context->Unmap(staging.Get(), 0);
    *width = target.width;
    *height = target.height;
    return true;
}

bool mapRendererReadback(int scale, std::vector<unsigned char>& pixels, int* width, int* height, std::vector<unsigned char>* tags)
{
    if (gState == nullptr || scale < 1 || scale > 4 || !gState->targets[scale].texture) {
        return false;
    }
    const Target& target = gState->targets[scale];
    D3D11_TEXTURE2D_DESC desc;
    target.texture->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    if (failed(gState->device->CreateTexture2D(&desc, nullptr, &staging), "readback staging")) {
        return false;
    }
    gState->context->CopyResource(staging.Get(), target.texture.Get());
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (failed(gState->context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "readback map")) {
        return false;
    }
    pixels.resize(static_cast<size_t>(target.width) * target.height);
    if (tags != nullptr) {
        tags->resize(pixels.size());
    }
    for (int y = 0; y < target.height; y++) {
        const unsigned int* row = reinterpret_cast<const unsigned int*>(static_cast<const unsigned char*>(mapped.pData) + mapped.RowPitch * y);
        for (int x = 0; x < target.width; x++) {
            pixels[static_cast<size_t>(y) * target.width + x] = static_cast<unsigned char>(row[x]);
            if (tags != nullptr) {
                (*tags)[static_cast<size_t>(y) * target.width + x] = (row[x] >> 8) & 1;
            }
        }
    }
    gState->context->Unmap(staging.Get(), 0);
    *width = target.width;
    *height = target.height;
    return true;
}

const MapRenderStats& mapRendererStats()
{
    static MapRenderStats empty;
    return gState != nullptr ? gState->stats : empty;
}

long long mapRendererGpuBytes()
{
    if (gState == nullptr) {
        return 0;
    }
    long long bytes = 5LL * kAtlasSize * kAtlasSize + static_cast<long long>(kTextWidth) * kTextHeight + 256LL * kTableRows + kIntensityMapSize * 2LL;
    for (int scale = 1; scale <= 4; scale++) {
        bytes += 4LL * gState->targets[scale].width * gState->targets[scale].height;
        bytes += 8LL * gState->hdTargets[scale].width * gState->hdTargets[scale].height;
    }
    return bytes;
}

} // namespace fallout::hd

#endif // _WIN32
