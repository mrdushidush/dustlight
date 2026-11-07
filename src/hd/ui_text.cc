#include "hd/ui_text.h"

#ifdef _WIN32

#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <string>
#include <vector>

#include <wrl/client.h>

#include "config.h"
#include "debug.h"
#include "font_manager.h"
#include "game_config.h"
#include "hd/d3d11_shared.h"
#include "hd/json_reader.h"
#include "lodepng.h"

using Microsoft::WRL::ComPtr;

namespace fallout::hd {

namespace {

    constexpr int kFonts = 16;

    struct FontAtlas {
        int slice = -1; // -1: no usable atlas
        std::vector<int> glyphX; // by character, 1x atlas units; -1 = not in the atlas
        std::vector<int> glyphY;
    };

    struct State {
        bool tried = false; // fonts.json read (once)
        bool enabled = true;
        std::string style = "model";
        std::string dir;
        int scale = 0; // the tier loaded; 0 = none
        bool failedScales[5] = {};
        FontAtlas fonts[kFonts];
        int atlasWidth[kFonts] = {}; // 1x units, per font
        int atlasHeight[kFonts] = {};
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11ShaderResourceView> view;
    };

    State* gState = nullptr;

    bool readFile(const std::string& path, std::vector<unsigned char>& out)
    {
        FILE* stream = fopen(path.c_str(), "rb");
        if (stream == nullptr) {
            return false;
        }
        fseek(stream, 0, SEEK_END);
        long size = ftell(stream);
        fseek(stream, 0, SEEK_SET);
        out.resize(size > 0 ? static_cast<size_t>(size) : 0);
        bool ok = size >= 0 && fread(out.data(), 1, out.size(), stream) == out.size();
        fclose(stream);
        return ok;
    }

    // fo2hd.fonts.font_hash: FNV-1a over each glyph's width and height (u16,
    // little endian) and its levels.
    unsigned int fontHash(const InterfaceFontView& f)
    {
        unsigned int h = 2166136261u;
        auto add = [&h](unsigned char b) { h = (h ^ b) * 16777619u; };
        for (int ch = 0; ch < 256; ch++) {
            int w = f.widths[ch];
            int ht = f.heights[ch];
            add(static_cast<unsigned char>(w & 0xFF));
            add(static_cast<unsigned char>((w >> 8) & 0xFF));
            add(static_cast<unsigned char>(ht & 0xFF));
            add(static_cast<unsigned char>((ht >> 8) & 0xFF));
            if (w > 0 && ht > 0) {
                for (int i = 0; i < w * ht; i++) {
                    add(f.glyphData[ch][i]);
                }
            }
        }
        return h;
    }

    // fonts.json: which fonts have atlases that match CE's fonts, and where
    // their glyphs are.
    bool readIndex()
    {
        std::vector<unsigned char> text;
        std::string path = gState->dir + "\\fonts.json";
        if (!readFile(path, text)) {
            debugPrint("HD: ui text: no %s (run fo2hd fonts), CE draws the text\n", path.c_str());
            return false;
        }
        JsonValue root;
        std::string error;
        if (!jsonParse(reinterpret_cast<const char*>(text.data()), text.size(), root, &error) || root["version"].asInt() != 1) {
            debugPrint("HD: ui text: %s unreadable (%s), CE draws the text\n", path.c_str(), error.c_str());
            return false;
        }
        bool styleFound = false;
        for (const JsonValue& style : root["styles"].items) {
            styleFound = styleFound || style.string == gState->style;
        }
        if (!styleFound) {
            debugPrint("HD: ui text: style %s not built, CE draws the text\n", gState->style.c_str());
            return false;
        }
        int usable = 0;
        int slices = 0;
        for (const JsonValue& font : root["fonts"].items) {
            int index = font["index"].asInt(-1);
            if (index < 0 || index >= kFonts) {
                continue;
            }
            InterfaceFontView view;
            if (!interfaceFontGetView(100 + index, &view)) {
                continue;
            }
            unsigned int expected = font["hash"].asUint();
            unsigned int actual = fontHash(view);
            if (actual != expected || font["max_height"].asInt() != view.maxHeight) {
                debugPrint("HD: ui text: font%d.aaf differs from the one the atlas was built from (hash %08x, atlas %08x), CE draws it\n", index, actual, expected);
                continue;
            }
            FontAtlas& atlas = gState->fonts[index];
            atlas.glyphX.assign(256, -1);
            atlas.glyphY.assign(256, -1);
            for (const JsonValue& glyph : font["glyphs"].items) {
                int ch = glyph[static_cast<size_t>(0)].asInt(-1);
                if (ch < 0 || ch > 255 || glyph[3].asInt() != view.widths[ch] || glyph[4].asInt() != view.heights[ch]) {
                    continue;
                }
                atlas.glyphX[ch] = glyph[1].asInt();
                atlas.glyphY[ch] = glyph[2].asInt();
            }
            gState->atlasWidth[index] = font["atlas"][static_cast<size_t>(0)].asInt();
            gState->atlasHeight[index] = font["atlas"][1].asInt();
            atlas.slice = slices++;
            usable++;
        }
        if (usable == 0) {
            debugPrint("HD: ui text: no atlas matches the game's fonts, CE draws the text\n");
            return false;
        }
        return true;
    }

    bool loadTier(int scale)
    {
        int width = 0;
        int height = 0;
        int slices = 0;
        for (int index = 0; index < kFonts; index++) {
            if (gState->fonts[index].slice >= 0) {
                width = std::max(width, gState->atlasWidth[index] * scale);
                height = std::max(height, gState->atlasHeight[index] * scale);
                slices = std::max(slices, gState->fonts[index].slice + 1);
            }
        }
        size_t sliceBytes = static_cast<size_t>(width) * height;
        std::vector<unsigned char> pixels(sliceBytes * slices, 0);
        for (int index = 0; index < kFonts; index++) {
            const FontAtlas& atlas = gState->fonts[index];
            if (atlas.slice < 0) {
                continue;
            }
            char name[64];
            snprintf(name, sizeof(name), "\\font%d-%s-%dx.png", index, gState->style.c_str(), scale);
            std::vector<unsigned char> png;
            std::vector<unsigned char> image;
            unsigned w = 0;
            unsigned h = 0;
            if (!readFile(gState->dir + name, png) || lodepng::decode(image, w, h, png, LCT_GREY, 8) != 0
                || static_cast<int>(w) != gState->atlasWidth[index] * scale || static_cast<int>(h) != gState->atlasHeight[index] * scale) {
                debugPrint("HD: ui text: %s%s missing or the wrong size, HD text off at %dx\n", gState->dir.c_str(), name, scale);
                return false;
            }
            unsigned char* slice = pixels.data() + sliceBytes * atlas.slice;
            for (unsigned y = 0; y < h; y++) {
                memcpy(slice + static_cast<size_t>(y) * width, image.data() + static_cast<size_t>(y) * w, w);
            }
        }
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = static_cast<UINT>(width);
        desc.Height = static_cast<UINT>(height);
        desc.MipLevels = 1;
        desc.ArraySize = static_cast<UINT>(slices);
        desc.Format = DXGI_FORMAT_R8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_IMMUTABLE;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        std::vector<D3D11_SUBRESOURCE_DATA> data(slices);
        for (int s = 0; s < slices; s++) {
            data[s].pSysMem = pixels.data() + sliceBytes * s;
            data[s].SysMemPitch = static_cast<UINT>(width);
        }
        ID3D11Device* device = presenterDevice();
        gState->texture.Reset();
        gState->view.Reset();
        if (device == nullptr || FAILED(device->CreateTexture2D(&desc, data.data(), &gState->texture))
            || FAILED(device->CreateShaderResourceView(gState->texture.Get(), nullptr, &gState->view))) {
            debugPrint("HD: ui text: atlas texture (%dx%d, %d slices) failed, HD text off\n", width, height, slices);
            gState->texture.Reset();
            gState->view.Reset();
            return false;
        }
        debugPrint("HD: ui text: %s glyphs at %dx, %d fonts (%dx%d atlas, %.1f MB)\n", gState->style.c_str(), scale, slices, width, height,
            sliceBytes * slices / 1048576.0);
        return true;
    }

} // namespace

bool uiTextLoad(int scale)
{
    if (gState == nullptr) {
        gState = new State();
        int enabled = 1;
        configGetInt(&gGameConfig, "hd", "ui_text", &enabled, 1);
        gState->enabled = enabled != 0;
        char* style = nullptr;
        if (configGetString(&gGameConfig, "hd", "ui_text_style", &style, "model") && style != nullptr && style[0] != '\0') {
            gState->style = style;
        }
        char* dir = nullptr;
        configGetString(&gGameConfig, "hd", "hd_art_dir", &dir, "hd");
        gState->dir = dir != nullptr ? dir : "hd";
        while (!gState->dir.empty() && (gState->dir.back() == '\\' || gState->dir.back() == '/')) {
            gState->dir.pop_back();
        }
        gState->dir += "\\fonts";
    }
    if (!gState->enabled || scale < 2 || scale > 4) {
        return false;
    }
    if (!interfaceFontsLoaded()) {
        return false; // the first frames come before CE loads its fonts; asked again each frame
    }
    if (!gState->tried) {
        gState->tried = true;
        if (!readIndex()) {
            gState->enabled = false;
            return false;
        }
    }
    if (gState->scale == scale && gState->view) {
        return true;
    }
    if (gState->failedScales[scale]) {
        return false;
    }
    gState->scale = 0;
    if (!loadTier(scale)) {
        gState->failedScales[scale] = true;
        return false;
    }
    gState->scale = scale;
    return true;
}

void uiTextShutdown()
{
    delete gState;
    gState = nullptr;
}

ID3D11ShaderResourceView* uiTextAtlas()
{
    return gState != nullptr ? gState->view.Get() : nullptr;
}

bool uiTextGlyph(int font, int ch, int* x, int* y, int* slice)
{
    if (gState == nullptr || gState->scale == 0 || font < 0 || font >= kFonts || ch < 0 || ch > 255) {
        return false;
    }
    const FontAtlas& atlas = gState->fonts[font];
    if (atlas.slice < 0 || atlas.glyphX[ch] < 0) {
        return false;
    }
    *x = atlas.glyphX[ch] * gState->scale;
    *y = atlas.glyphY[ch] * gState->scale;
    *slice = atlas.slice;
    return true;
}

const char* uiTextStyle()
{
    return gState != nullptr ? gState->style.c_str() : "model";
}

} // namespace fallout::hd

#endif // _WIN32
