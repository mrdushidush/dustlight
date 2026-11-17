#include "hd/first_run.h"

#include <stdio.h>
#include <string.h>

#include <string>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#include "svga.h"

namespace fallout::hd {

namespace {

    std::string gReport;

    // The game needs at least 640x480 logical pixels (svga.cc
    // _GNW95_init_mode_ex falls back to scale 1 otherwise).
    bool fits(int width, int height, int scale)
    {
        return width / scale >= 640 && height / scale >= 480;
    }

    bool fileExists(const std::string& path)
    {
        FILE* f = fopen(path.c_str(), "rb");
        if (f == nullptr) {
            return false;
        }
        fclose(f);
        return true;
    }

} // namespace

std::vector<std::string> hdArtDirs(Config* config)
{
    char* value = nullptr;
    std::string list;
    if (configGetString(config, "hd", "hd_art_dir", &value) && value != nullptr && value[0] != '\0') {
        list = value;
    } else {
        // P8.7 (the author, 2026-11-15: both art packs inside Et Tu): the Fallout 1
        // art zip unpacks into hd-fo1\ next to Fallout 2's hd\; with its tier
        // marker there, it comes first without anyone editing fallout2.cfg.
        bool fo1 = false;
        for (int tier = 2; tier <= 4 && !fo1; tier++) {
            fo1 = fileExists("hd-fo1\\pack_" + std::to_string(tier) + "x.json");
        }
        list = fo1 ? "hd-fo1|hd" : "hd";
    }
    std::vector<std::string> dirs;
    size_t start = 0;
    while (start <= list.size()) {
        size_t end = list.find('|', start);
        if (end == std::string::npos) {
            end = list.size();
        }
        std::string dir = list.substr(start, end - start);
        while (!dir.empty() && (dir.back() == '\\' || dir.back() == '/' || dir.back() == ' ' || dir.back() == '\t')) {
            dir.pop_back();
        }
        size_t first = dir.find_first_not_of(" \t");
        if (first != std::string::npos) {
            dirs.push_back(dir.substr(first));
        }
        start = end + 1;
    }
    if (dirs.empty()) {
        dirs.push_back("hd");
    }
    return dirs;
}

int installedArtTiers(Config* config)
{
    int common = 0x1c; // tiers 2..4
    int any = 0;
    for (const std::string& root : hdArtDirs(config)) {
        int mask = 0;
        for (int tier = 2; tier <= 4; tier++) {
            char marker[64];
            snprintf(marker, sizeof(marker), "\\pack_%dx.json", tier);
            char bar[64];
            snprintf(bar, sizeof(bar), "\\art\\intrface\\iface\\albedo_%dx.dds", tier);
            if (fileExists(root + marker) || fileExists(root + bar)) {
                mask |= 1 << tier;
            }
        }
        if (mask != 0) {
            common &= mask;
            any |= mask;
        }
    }
    return common != 0 && any != 0 ? common : any;
}

bool firstRunScreenSetup(Config* config)
{
    int autoScreen = 1;
    configGetInt(config, "hd", "auto_screen", &autoScreen, 1);
    if (autoScreen == 0) {
        return false;
    }

#ifdef _WIN32
    // The display mode itself, not a DPI-scaled size: the game window covers
    // the whole primary display in physical pixels.
    DEVMODEW mode = {};
    mode.dmSize = sizeof(mode);
    if (!EnumDisplaySettingsW(nullptr, ENUM_CURRENT_SETTINGS, &mode) || mode.dmPelsWidth < 640 || mode.dmPelsHeight < 480) {
        gReport = "HD: first start: desktop size unknown, screen settings left as they are";
        configSetInt(config, "hd", "auto_screen", 0);
        return true;
    }
    int width = static_cast<int>(mode.dmPelsWidth);
    int height = static_cast<int>(mode.dmPelsHeight);

    int tiers = installedArtTiers(config);
    int scale = 0;
    for (int tier = 4; tier >= 2; tier--) {
        if ((tiers & (1 << tier)) != 0 && fits(width, height, tier)) {
            scale = tier;
            break;
        }
    }
    bool matched = scale != 0;
    if (!matched) {
        // No art for a scale that fits: the largest scale that does (the
        // original art, drawn by the HD renderer).
        for (scale = 4; scale > 1 && !fits(width, height, scale); scale--) {
        }
    }

    configSetInt(config, "screen", "resolution_x", width);
    configSetInt(config, "screen", "resolution_y", height);
    configSetInt(config, "screen", "windowed", static_cast<int>(WindowMode::WindowedFullscreen));
    configSetInt(config, "screen", "scale", scale);
    configSetInt(config, "hd", "auto_screen", 0);

    char text[256];
    snprintf(text, sizeof(text), "HD: first start: %dx%d borderless, scale %d (%s; installed tiers:%s%s%s%s)",
        width, height, scale,
        matched ? "matches the installed HD art" : "no HD art for a scale that fits",
        (tiers & 4) != 0 ? " 2x" : "", (tiers & 8) != 0 ? " 3x" : "", (tiers & 16) != 0 ? " 4x" : "",
        tiers == 0 ? " none" : "");
    gReport = text;
    return true;
#else
    configSetInt(config, "hd", "auto_screen", 0);
    return true;
#endif
}

const char* firstRunReport()
{
    return gReport.c_str();
}

} // namespace fallout::hd
