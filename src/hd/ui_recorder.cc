#include "hd/ui_recorder.h"

#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <filesystem>
#include <iterator>
#include <memory>
#include <string>
#include <vector>

#include "art.h"
#include "color.h"
#include "config.h"
#include "debug.h"
#include "font_manager.h"
#include "game_config.h"
#include "hd/ui_layer.h"
#include "input.h"
#include "map.h"
#include "text_font.h"
#include "window_manager.h"

namespace fallout::hd {

bool gUiRecorderActive = false;

namespace {

    const char* const kKindNames[UiKindCount] = { "unknown", "art", "art-loose", "text", "window-copy", "buffer-copy", "fill", "map" };

    // One frame inside a loaded FRM block.
    struct FrameSpan {
        const unsigned char* pixels;
        int width;
        int height;
        int rotation;
        int frame;
        unsigned int hash; // valid once `hashed`
        bool hashed;
        bool cycle;
    };

    struct ArtBlock {
        const unsigned char* end;
        int fid;
        std::string path; // named art
        bool scanned = false; // `frames` built (on the first blit from it)
        std::vector<FrameSpan> frames;
    };

    // Loaded FRMs by start address; registered whether or not the recorder
    // runs (art loads before hd::init), a map insert per FRM load. Never
    // destroyed: art.cc's named-art cache is a global too and frees its
    // entries during static destruction, in an order we don't control.
    std::map<const unsigned char*, ArtBlock>& artBlocks()
    {
        static auto* blocks = new std::map<const unsigned char*, ArtBlock>();
        return *blocks;
    }
    std::unordered_map<int, UiWindow> gWindows; // by window id
    unsigned int gGenerations = 0;
    int gIntervalMs = 0;
    unsigned int gLastTick = 0;
    int gTicks = 0;
    std::string gReportPath;

    // Text (P7b.5): what the before-hook saw of the draw that follows.
    struct PendingText {
        bool active = false;
        unsigned char* dest = nullptr;
        const char* string = nullptr;
        int color = 0;
        UiTextFallback reason = UiTextOk;
        UiOp op; // the text op it becomes if the 1x check passes
    };
    PendingText gPending;
    long long gTextCounts[UiTextFallbackCount] = {};
    InterfaceFontView gFontViews[16];
    bool gFontViewLoaded[16] = {};

    // CE's interface font `font` (100..), as loaded; nullptr for FON fonts.
    const InterfaceFontView* fontView(int font)
    {
        int index = font - 100;
        if (index < 0 || index >= 16) {
            return nullptr;
        }
        if (!gFontViewLoaded[index]) {
            gFontViewLoaded[index] = interfaceFontGetView(font, &gFontViews[index]);
        }
        return gFontViewLoaded[index] ? &gFontViews[index] : nullptr;
    }

    // interfaceFontDrawImpl's placement: glyphs sit on the bottom of the
    // line, a space advances by the word spacing, `length` (pixels) stops
    // the string. Returns the advance (where CE's pointer ends).
    int layoutText(const InterfaceFontView& f, const char* string, int length, bool monospaced, std::vector<UiGlyphPlace>& out)
    {
        int monospacedWidth = monospaced ? fontGetMonospacedCharacterWidth() : 0;
        int ptr = 0;
        for (const char* p = string; *p != '\0'; p++) {
            unsigned char ch = static_cast<unsigned char>(*p);
            int characterWidth = ch == ' ' ? f.wordSpacing : f.widths[ch];
            int end;
            if (monospaced) {
                end = ptr + monospacedWidth;
                ptr += (monospacedWidth - characterWidth - f.letterSpacing) / 2;
            } else {
                end = ptr + characterWidth + f.letterSpacing;
            }
            if (end > length) {
                break;
            }
            if (f.widths[ch] > 0 && f.heights[ch] > 0) {
                out.push_back({ ch, static_cast<short>(ptr), static_cast<short>(f.maxHeight - f.heights[ch]), static_cast<short>(f.widths[ch]), static_cast<short>(f.heights[ch]) });
            }
            ptr = end;
        }
        return ptr;
    }

    // CE's draw replayed on `pixels` (the op's rect, starting from what was
    // there before): the shadow pass in black at +1,+1, then the text, every
    // glyph level through the color's blend table. `owned` (if given) gets a
    // 1 where a level above 0 landed. False if a level is outside the
    // tables (CE would read past them).
    bool predictText(const InterfaceFontView& f, const UiOp& op, unsigned char* pixels, unsigned char* owned)
    {
        for (int pass = op.shadow ? 0 : 1; pass < 2; pass++) {
            int shift = pass == 0 ? 1 : 0;
            Color color = static_cast<Color>(pass == 0 ? op.shadowColor : op.color);
            Color* table = _getColorBlendTable(color);
            bool ok = true;
            for (const UiGlyphPlace& g : op.glyphs) {
                const unsigned char* data = f.glyphData[g.ch];
                for (int y = 0; y < g.height && ok; y++) {
                    size_t row = static_cast<size_t>(g.y + y + shift) * op.width + g.x + shift;
                    for (int x = 0; x < g.width; x++) {
                        unsigned char level = *data++;
                        if (level > 15) {
                            ok = false;
                            break;
                        }
                        pixels[row + x] = table[(level << 8) + pixels[row + x]];
                        if (owned != nullptr && level != 0) {
                            owned[row + x] = 1;
                        }
                    }
                }
            }
            _freeColorBlendTable(color);
            if (!ok) {
                return false;
            }
        }
        return true;
    }

    unsigned int fnv1a(const unsigned char* data, size_t size)
    {
        unsigned int h = 2166136261u;
        for (size_t i = 0; i < size; i++) {
            h = (h ^ data[i]) * 16777619u;
        }
        return h;
    }

    bool hasCycleColors(const unsigned char* data, size_t size)
    {
        for (size_t i = 0; i < size; i++) {
            if (data[i] >= 229 && data[i] <= 254) {
                return true;
            }
        }
        return false;
    }

    ArtBlock* artAt(const unsigned char* p, const unsigned char** start)
    {
        auto it = artBlocks().upper_bound(p);
        if (it == artBlocks().begin()) {
            return nullptr;
        }
        --it;
        if (p >= it->second.end) {
            return nullptr;
        }
        *start = it->first;
        return &it->second;
    }

    std::string artName(const ArtBlock& block)
    {
        if (block.fid == -1) {
            return block.path;
        }
        const char* path = FrmId(block.fid).filePath();
        return path != nullptr ? std::string(path) : std::to_string(block.fid);
    }

    // The block's frames, from its Art header. Rotations that share another
    // rotation's frames (same data offset) are listed once, under the first.
    // artGetFrame's walk, but every offset is checked against the block before
    // it is followed: a block whose memory no longer holds that FRM (QA
    // 2026-11-03: a crash in artGetFrameData after a save load, the walk sent
    // outside the block by a stale header) yields no frames instead.
    void scanFrames(const unsigned char* start, ArtBlock& block)
    {
        block.scanned = true;
        const long long size = block.end - start;
        if (size < static_cast<long long>(sizeof(Art))) {
            return;
        }
        const Art* art = reinterpret_cast<const Art*>(start);
        const int frameCount = art->frameCount;
        if (frameCount <= 0 || frameCount > 4096) {
            return; // not an FRM header
        }
        for (int rotation = 0; rotation < ROTATION_COUNT; rotation++) {
            bool shared = false;
            for (int earlier = 0; earlier < rotation; earlier++) {
                shared = shared || art->dataOffsets[earlier] == art->dataOffsets[rotation];
            }
            if (shared) {
                continue;
            }
            long long offset = static_cast<long long>(sizeof(Art)) + art->dataOffsets[rotation] + art->padding[rotation];
            for (int frame = 0; frame < frameCount; frame++) {
                if (offset < static_cast<long long>(sizeof(Art)) || offset + static_cast<long long>(sizeof(ArtFrame)) > size) {
                    block.frames.clear();
                    return; // not an FRM layout we understand: no ops from it
                }
                const ArtFrame* header = reinterpret_cast<const ArtFrame*>(start + offset);
                const long long pixelsSize = static_cast<long long>(header->width) * header->height;
                const long long pixelsAt = offset + static_cast<long long>(sizeof(ArtFrame));
                if (header->width <= 0 || header->height <= 0 || header->size < pixelsSize || pixelsAt + header->size > size) {
                    block.frames.clear();
                    return;
                }
                block.frames.push_back({ start + pixelsAt, header->width, header->height, rotation, frame, 0, false, false });
                // art.cc paddingForSize: frames start on a 4-byte boundary.
                offset = pixelsAt + header->size + (4 - header->size % 4) % 4;
            }
        }
    }

    // Forgets the blocks overlapping [start, end): that memory was handed out
    // again, so what was registered there is gone.
    void forgetRange(const unsigned char* start, const unsigned char* end)
    {
        auto& blocks = artBlocks();
        auto it = blocks.lower_bound(start);
        if (it != blocks.begin()) {
            auto previous = std::prev(it);
            if (previous->second.end > start) {
                it = previous;
            }
        }
        while (it != blocks.end() && it->first < end) {
            it = blocks.erase(it);
        }
    }

    // The op for a copy of `width` x `height` pixels at `src` (pitch
    // `srcPitch`) out of `block`, if it is a rectangle inside one frame.
    bool locate(const unsigned char* start, ArtBlock& block, const unsigned char* src, int srcPitch, int width, int height, UiOp& op)
    {
        if (!block.scanned) {
            scanFrames(start, block);
        }
        for (FrameSpan& span : block.frames) {
            size_t size = static_cast<size_t>(span.width) * span.height;
            if (src < span.pixels || src >= span.pixels + size) {
                continue;
            }
            size_t offset = static_cast<size_t>(src - span.pixels);
            int x = static_cast<int>(offset % span.width);
            int y = static_cast<int>(offset / span.width);
            if (srcPitch != span.width || x + width > span.width || y + height > span.height) {
                return false;
            }
            if (!span.hashed) {
                // Hashed once: UI art isn't edited in place (the cursor's is,
                // but it never goes through a window).
                span.hash = fnv1a(span.pixels, size);
                span.cycle = hasCycleColors(span.pixels, size);
                span.hashed = true;
            }
            op.fid = block.fid;
            op.path = block.path;
            op.rotation = span.rotation;
            op.frame = span.frame;
            op.frameWidth = span.width;
            op.frameHeight = span.height;
            op.hash = span.hash;
            op.cycle = span.cycle;
            op.srcX = x;
            op.srcY = y;
            op.width = width;
            op.height = height;
            return true;
        }
        return false;
    }

    Window* windowFor(const unsigned char* p)
    {
        int count = windowGetCount();
        for (int index = 0; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || window->buffer == nullptr) {
                continue;
            }
            if (p >= window->buffer && p < window->buffer + static_cast<size_t>(window->width) * window->height) {
                return window;
            }
        }
        return nullptr;
    }

    UiWindow& stateFor(Window* window)
    {
        UiWindow& s = gWindows[window->id];
        if (s.buffer != window->buffer || s.width != window->width || s.height != window->height) {
            s = UiWindow();
            s.id = window->id;
            s.buffer = window->buffer;
            s.width = window->width;
            s.height = window->height;
            s.generation = ++gGenerations;
            size_t n = static_cast<size_t>(s.width) * s.height;
            s.expected.assign(n, 0);
            s.kind.assign(n, UiKindUnknown);
            s.owner.assign(n, 0);
        }
        return s;
    }

    bool isMapWindow(const Window* window)
    {
        return window->id == gIsoWindow;
    }

    // Scratch buffers (P7b.4): CE composes some art and text in buffers of
    // its own (button faces with labels, tab images, the message display's
    // background) and copies them into windows later. Registered where CE
    // makes them, they are recorder surfaces like windows, under negative
    // ids in gWindows, and their ops carry over when they are copied into
    // a window (transfer()).
    struct Scratch {
        int id;
        int width;
        int height;
    };
    std::map<const unsigned char*, Scratch> gScratch; // by start address
    int gNextScratchId = -2;

    // The recorder's state for the window (not the map's) or scratch buffer
    // that holds `p`, or nullptr.
    UiWindow* surfaceFor(const unsigned char* p)
    {
        Window* window = windowFor(p);
        if (window != nullptr) {
            return isMapWindow(window) ? nullptr : &stateFor(window);
        }
        auto it = gScratch.upper_bound(p);
        if (it == gScratch.begin()) {
            return nullptr;
        }
        --it;
        if (p >= it->first + static_cast<size_t>(it->second.width) * it->second.height) {
            return nullptr;
        }
        auto state = gWindows.find(it->second.id);
        return state != gWindows.end() ? &state->second : nullptr;
    }

    void markDirty(UiWindow& s, int left, int top, int right, int bottom)
    {
        if (s.dirtyLeft > s.dirtyRight) {
            s.dirtyLeft = left;
            s.dirtyTop = top;
            s.dirtyRight = right;
            s.dirtyBottom = bottom;
            return;
        }
        s.dirtyLeft = std::min(s.dirtyLeft, left);
        s.dirtyTop = std::min(s.dirtyTop, top);
        s.dirtyRight = std::max(s.dirtyRight, right);
        s.dirtyBottom = std::max(s.dirtyBottom, bottom);
    }

    // Takes pixels away from the ops that owned them; pixels in a row mostly
    // share an owner, so the counts are applied per run.
    class Release {
    public:
        explicit Release(UiWindow& s)
            : _s(s)
        {
        }
        ~Release() { flush(); }

        void add(unsigned int serial)
        {
            if (serial == _serial) {
                _count++;
                return;
            }
            flush();
            _serial = serial;
            _count = 1;
        }

    private:
        void flush()
        {
            if (_serial != 0) {
                auto it = _s.ops.find(_serial);
                if (it != _s.ops.end()) {
                    it->second.owned -= _count;
                    if (it->second.owned <= 0) {
                        _s.ops.erase(it);
                    }
                }
            }
            _serial = 0;
            _count = 0;
        }

        UiWindow& _s;
        unsigned int _serial = 0;
        int _count = 0;
    };

    void writeCoverage()
    {
        FILE* stream = fopen(gReportPath.c_str(), "at");
        double totalPixels = 0;
        double artPixels = 0;
        double hdPixels = 0;
        int windows = 0;
        int count = windowGetCount();
        for (int index = 0; index < count; index++) {
            Window* window = windowGetWindowAt(index);
            if (window == nullptr || window->buffer == nullptr || isMapWindow(window) || (window->flags & WINDOW_HIDDEN) != WINDOW_NONE) {
                continue;
            }
            UiWindow& s = stateFor(window);
            size_t n = s.expected.size();
            long long explained[UiKindCount] = {};
            long long stale = 0;
            for (size_t i = 0; i < n; i++) {
                if (s.kind[i] != UiKindUnknown && s.buffer[i] != s.expected[i]) {
                    stale++;
                } else {
                    explained[s.kind[i]]++;
                }
            }
            windows++;
            totalPixels += static_cast<double>(n);
            artPixels += static_cast<double>(explained[UiKindArt]);
            // HD as far as it can be: art, text, and fills (the same at any scale).
            hdPixels += static_cast<double>(explained[UiKindArt] + explained[UiKindText] + explained[UiKindFill] + explained[UiKindMap]);
            if (stream == nullptr) {
                continue;
            }
            auto pct = [n](long long v) { return n > 0 ? 100.0 * static_cast<double>(v) / static_cast<double>(n) : 0.0; };
            fprintf(stream, "tick %d window %d at %d,%d size %dx%d:", gTicks, window->id, window->rect.left, window->rect.top, s.width, s.height);
            for (int k = 1; k < UiKindCount; k++) {
                fprintf(stream, " %s %.1f%%", kKindNames[k], pct(explained[k]));
            }
            fprintf(stream, " unknown %.1f%% overwritten %.1f%% | live ops %d | text ops %d | writes", pct(explained[UiKindUnknown]), pct(stale), static_cast<int>(s.ops.size()), s.textOps);
            for (int k = 1; k < UiKindCount; k++) {
                fprintf(stream, " %s %d", kKindNames[k], s.writes[k]);
            }
            if (s.oddPitch > 0) {
                fprintf(stream, " odd-pitch %d", s.oddPitch);
            }
            std::vector<std::pair<int, std::string>> frms;
            for (const auto& entry : s.frms) {
                frms.emplace_back(entry.second, entry.first);
            }
            std::sort(frms.rbegin(), frms.rend());
            fprintf(stream, " | frms");
            for (size_t i = 0; i < frms.size() && i < 8; i++) {
                fprintf(stream, " %s(%d)", frms[i].second.c_str(), frms[i].first);
            }
            fprintf(stream, "\n");
        }
        if (stream != nullptr) {
            fclose(stream);
        }
        debugPrint("HD: ui coverage %d: %d windows, art explains %.1f%% of their pixels, with text and fills %.1f%% (ui_coverage.txt)\n",
            gTicks, windows, totalPixels > 0 ? 100.0 * artPixels / totalPixels : 0.0, totalPixels > 0 ? 100.0 * hdPixels / totalPixels : 0.0);
    }

    void coverageTicker()
    {
        if (getTicksSince(gLastTick) < static_cast<unsigned int>(gIntervalMs)) {
            return;
        }
        gLastTick = getTicks();
        writeCoverage();
#ifdef _WIN32
        if (!uiLayerGamePalette()) {
            debugPrint("HD: ui replay self-test %d: skipped, palette not color.pal\n", gTicks);
        } else {
            uiReplaySelfTest(gTicks);
        }
        uiLayerSelfTest(gTicks); // when the HD UI is on
#else
        uiReplaySelfTest(gTicks);
#endif
        gTicks++;
    }

} // namespace

std::unordered_map<int, UiWindow>& uiWindows()
{
    return gWindows;
}

void uiArtLoaded(const void* data, size_t size, int fid, const char* path)
{
    if (data == nullptr || size == 0) {
        return;
    }
    const unsigned char* start = static_cast<const unsigned char*>(data);
    ArtBlock block;
    block.end = start + size;
    block.fid = fid;
    block.path = path != nullptr ? std::string(path) : std::string();
    forgetRange(start, block.end);
    artBlocks()[start] = std::move(block);
}

void uiArtFreed(const void* data)
{
    artBlocks().erase(static_cast<const unsigned char*>(data));
}

void uiArtMoved(const void* from, const void* to)
{
    auto& blocks = artBlocks();
    auto it = blocks.find(static_cast<const unsigned char*>(from));
    if (it == blocks.end()) {
        return;
    }
    ArtBlock block = std::move(it->second);
    blocks.erase(it);
    const unsigned char* start = static_cast<const unsigned char*>(to);
    block.end = start + (block.end - static_cast<const unsigned char*>(from));
    // The frame spans point into the old place; built again on the next blit.
    block.scanned = false;
    block.frames.clear();
    forgetRange(start, block.end);
    blocks[start] = std::move(block);
}

void uiWindowDestroyed(int id)
{
    gWindows.erase(id);
}

namespace {

    // A copy out of another recorder surface (a scratch buffer, or another
    // window) into `s` at x0, y0: its ops come along. Each source op that
    // still owns a pixel of the copied rect (with the value it wrote)
    // becomes an op of `s`, clipped and moved, in the source's order; the
    // copied pixels it owns are its own. Text ops come along whole or not
    // at all; through a transparent copy they are marked (transCopy) so
    // their backdrop skips the pixels the copy skipped. Everything else
    // copied is a plain copy (`kind`), as before.
    void transfer(UiWindow& s, int x0, int y0, const UiWindow& from, int fx0, int fy0, int w, int h, bool trans, UiKind kind)
    {
        // The source ops that take part, in serial (= draw) order.
        std::map<unsigned int, unsigned int> remap; // source serial -> serial in `s`
        for (int y = 0; y < h; y++) {
            size_t row = static_cast<size_t>(fy0 + y) * from.width + fx0;
            for (int x = 0; x < w; x++) {
                size_t i = row + x;
                unsigned int owner = from.owner[i];
                if (owner == 0 || (trans && from.buffer[i] == 0) || from.expected[i] != from.buffer[i] || remap.count(owner) != 0) {
                    continue;
                }
                auto op = from.ops.find(owner);
                if (op == from.ops.end()) {
                    continue;
                }
                const UiOp& o = op->second;
                bool whole = o.destX >= fx0 && o.destY >= fy0 && o.destX + o.width <= fx0 + w && o.destY + o.height <= fy0 + h;
                if (o.font >= 0 && !whole) {
                    continue;
                }
                remap[owner] = 0;
            }
        }
        std::vector<std::pair<unsigned int, UiOp>> derived;
        for (auto& entry : remap) {
            entry.second = s.nextSerial++;
            UiOp op = from.ops.at(entry.first);
            int left = std::max(op.destX, fx0);
            int top = std::max(op.destY, fy0);
            int right = std::min(op.destX + op.width, fx0 + w);
            int bottom = std::min(op.destY + op.height, fy0 + h);
            if (op.font < 0) {
                op.srcX += left - op.destX;
                op.srcY += top - op.destY;
                op.width = right - left;
                op.height = bottom - top;
                op.trans = op.trans || trans;
            } else {
                op.transCopy = op.transCopy || trans;
            }
            op.destX = x0 + (left - fx0);
            op.destY = y0 + (top - fy0);
            op.owned = 0;
            op.hd = false;
            derived.emplace_back(entry.second, std::move(op));
        }
        // Text ops' backdrops look up the ops beneath them: those of `s` now.
        for (auto& entry : derived) {
            for (unsigned int& beneath : entry.second.beneath) {
                auto it = remap.find(beneath);
                beneath = it != remap.end() ? it->second : 0;
            }
        }
        std::map<unsigned int, size_t> index; // serial in `s` -> derived[]
        for (size_t k = 0; k < derived.size(); k++) {
            index[derived[k].first] = k;
        }

        {
            Release release(s);
            for (int y = 0; y < h; y++) {
                size_t fromRow = static_cast<size_t>(fy0 + y) * from.width + fx0;
                size_t row = static_cast<size_t>(y0 + y) * s.width + x0;
                for (int x = 0; x < w; x++) {
                    unsigned char value = from.buffer[fromRow + x];
                    if (trans && value == 0) {
                        continue;
                    }
                    size_t i = row + x;
                    if (s.owner[i] != 0) {
                        release.add(s.owner[i]);
                    }
                    s.expected[i] = value;
                    auto mapped = remap.find(from.owner[fromRow + x]);
                    if (from.owner[fromRow + x] != 0 && mapped != remap.end() && from.expected[fromRow + x] == value) {
                        s.owner[i] = mapped->second;
                        s.kind[i] = from.kind[fromRow + x];
                        derived[index[mapped->second]].second.owned++;
                    } else {
                        s.owner[i] = 0;
                        s.kind[i] = kind;
                    }
                }
            }
        }
        for (auto& entry : derived) {
            if (entry.second.owned > 0) {
                if (entry.second.font >= 0) {
                    s.textOps++;
                }
                s.ops.emplace(entry.first, std::move(entry.second));
            }
        }
    }

} // namespace

void uiScratchRegister(unsigned char* buffer, int width, int height)
{
    if (!gUiRecorderActive || buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    uiScratchFree(buffer);
    int id = gNextScratchId--;
    gScratch[buffer] = { id, width, height };
    UiWindow& s = gWindows[id];
    s = UiWindow();
    s.id = id;
    s.buffer = buffer;
    s.width = width;
    s.height = height;
    s.generation = ++gGenerations;
    size_t n = static_cast<size_t>(width) * height;
    s.expected.assign(n, 0);
    s.kind.assign(n, UiKindUnknown);
    s.owner.assign(n, 0);
}

void uiScratchFree(const unsigned char* buffer)
{
    auto it = gScratch.find(buffer);
    if (it == gScratch.end()) {
        return;
    }
    gWindows.erase(it->second.id);
    gScratch.erase(it);
}

void uiRecordCopyImpl(unsigned char* dest, int destPitch, const unsigned char* src, int srcPitch, int width, int height, bool trans)
{
    UiWindow* surface = width > 0 && height > 0 ? surfaceFor(dest) : nullptr;
    if (surface == nullptr) {
        return;
    }
    UiWindow& s = *surface;
    if (destPitch != s.width) {
        s.oddPitch++;
        return;
    }
    size_t offset = static_cast<size_t>(dest - s.buffer);
    int x0 = static_cast<int>(offset % s.width);
    int y0 = static_cast<int>(offset / s.width);
    int w = std::min(width, s.width - x0);
    int h = std::min(height, s.height - y0);

    const unsigned char* start = nullptr;
    ArtBlock* art = artAt(src, &start);
    UiOp op;
    bool isOp = art != nullptr && locate(start, *art, src, srcPitch, w, h, op);
    UiKind kind = art != nullptr ? (isOp ? UiKindArt : UiKindArtLoose) : (windowFor(src) != nullptr ? UiKindWindowCopy : UiKindBufferCopy);
    if (art == nullptr) {
        // Out of another surface (a scratch buffer, a window): its ops come along.
        UiWindow* from = surfaceFor(src);
        if (from != nullptr && from != &s && srcPitch == from->width) {
            size_t fromOffset = static_cast<size_t>(src - from->buffer);
            int fx0 = static_cast<int>(fromOffset % from->width);
            int fy0 = static_cast<int>(fromOffset / from->width);
            int tw = std::min(w, from->width - fx0);
            int th = std::min(h, from->height - fy0);
            transfer(s, x0, y0, *from, fx0, fy0, tw, th, trans, kind);
            markDirty(s, x0, y0, x0 + tw - 1, y0 + th - 1);
            s.writes[kind]++;
            return;
        }
    }
    unsigned int serial = isOp ? s.nextSerial++ : 0;

    {
        Release release(s);
        for (int y = 0; y < h; y++) {
            const unsigned char* row = src + static_cast<size_t>(y) * srcPitch;
            size_t base = static_cast<size_t>(y0 + y) * s.width + x0;
            for (int x = 0; x < w; x++) {
                if (trans && row[x] == 0) {
                    continue;
                }
                size_t i = base + x;
                s.expected[i] = row[x];
                s.kind[i] = kind;
                if (s.owner[i] != 0) {
                    release.add(s.owner[i]);
                }
                s.owner[i] = serial;
                op.owned++;
            }
        }
    }
    markDirty(s, x0, y0, x0 + w - 1, y0 + h - 1);
    s.writes[kind]++;
    if (art != nullptr && gIntervalMs > 0) {
        s.frms[artName(*art)]++;
    }
    if (isOp && op.owned > 0) {
        op.destX = x0;
        op.destY = y0;
        op.trans = trans;
        s.ops.emplace(serial, std::move(op));
    }
}

namespace {

    // The rect at `dest` (pitch `pitch`) on a recorder surface, clipped to
    // it; nullptr if `dest` isn't on one or the pitch isn't the surface's.
    UiWindow* surfaceRect(unsigned char* dest, int pitch, int width, int height, int* x0, int* y0, int* w, int* h)
    {
        UiWindow* surface = width > 0 && height > 0 ? surfaceFor(dest) : nullptr;
        if (surface == nullptr) {
            return nullptr;
        }
        if (pitch != surface->width) {
            surface->oddPitch++;
            return nullptr;
        }
        size_t offset = static_cast<size_t>(dest - surface->buffer);
        *x0 = static_cast<int>(offset % surface->width);
        *y0 = static_cast<int>(offset / surface->width);
        *w = std::min(width, surface->width - *x0);
        *h = std::min(height, surface->height - *y0);
        return surface;
    }

} // namespace

void uiRecordMapViewImpl(unsigned char* dest, int destPitch, int isoX, int isoY, int width, int height)
{
#ifdef _WIN32
    int x0, y0, w, h;
    UiWindow* surface = surfaceRect(dest, destPitch, width, height, &x0, &y0, &w, &h);
    if (surface == nullptr || w != width || h != height) {
        return;
    }
    std::vector<unsigned char> owned;
    int snapshot = uiLayerMapSnapshot(isoX, isoY, width, height, owned);
    if (snapshot == 0 || owned.size() != static_cast<size_t>(width) * height) {
        return;
    }
    UiWindow& s = *surface;
    unsigned int serial = s.nextSerial++;
    UiOp op;
    op.snapshot = snapshot;
    op.frameWidth = width;
    op.frameHeight = height;
    op.width = width;
    op.height = height;
    op.destX = x0;
    op.destY = y0;
    op.pixels.resize(owned.size());
    {
        Release release(s);
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                size_t i = static_cast<size_t>(y0 + y) * s.width + x0 + x;
                unsigned char value = s.buffer[i];
                op.pixels[static_cast<size_t>(y) * w + x] = value;
                // The copy just recorded these pixels as a window copy.
                if (owned[static_cast<size_t>(y) * w + x] == 0 || s.expected[i] != value) {
                    continue;
                }
                if (s.owner[i] != 0) {
                    release.add(s.owner[i]);
                }
                s.owner[i] = serial;
                s.kind[i] = UiKindMap;
                op.owned++;
            }
        }
    }
    markDirty(s, x0, y0, x0 + w - 1, y0 + h - 1);
    s.writes[UiKindMap]++;
    if (op.owned > 0) {
        s.ops.emplace(serial, std::move(op));
    }
#else
    (void)dest;
    (void)destPitch;
    (void)isoX;
    (void)isoY;
    (void)width;
    (void)height;
#endif
}

void uiRecordFillImpl(unsigned char* dest, int pitch, int width, int height, unsigned char value)
{
    int x0, y0, w, h;
    UiWindow* surface = surfaceRect(dest, pitch, width, height, &x0, &y0, &w, &h);
    if (surface == nullptr) {
        return;
    }
    UiWindow& s = *surface;
    {
        Release release(s);
        for (int y = 0; y < h; y++) {
            size_t base = static_cast<size_t>(y0 + y) * s.width + x0;
            for (int x = 0; x < w; x++) {
                size_t i = base + x;
                if (s.owner[i] != 0) {
                    release.add(s.owner[i]);
                }
                s.owner[i] = 0;
                s.expected[i] = value;
                s.kind[i] = UiKindFill;
            }
        }
    }
    markDirty(s, x0, y0, x0 + w - 1, y0 + h - 1);
    s.writes[UiKindFill]++;
}

void uiRecordDimImpl(unsigned char* dest, int pitch, int width, int height, int intensity)
{
    int x0, y0, w, h;
    UiWindow* surface = surfaceRect(dest, pitch, width, height, &x0, &y0, &w, &h);
    if (surface == nullptr || intensity <= 0 || intensity >= 128) {
        return;
    }
    UiWindow& s = *surface;
    // The art ops that own pixels of the rect (still holding what they
    // wrote) go on as dimmed copies clipped to the rect, in their order;
    // text and ops dimmed before don't.
    std::map<unsigned int, unsigned int> remap; // serial -> the dimmed copy's
    for (int y = 0; y < h; y++) {
        size_t base = static_cast<size_t>(y0 + y) * s.width + x0;
        for (int x = 0; x < w; x++) {
            size_t i = base + x;
            unsigned int owner = s.owner[i];
            if (owner == 0 || s.buffer[i] != s.expected[i] || remap.count(owner) != 0) {
                continue;
            }
            auto op = s.ops.find(owner);
            if (op != s.ops.end() && op->second.font < 0 && op->second.dim == 0 && op->second.snapshot == 0) {
                remap[owner] = 0;
            }
        }
    }
    std::vector<std::pair<unsigned int, UiOp>> derived;
    std::map<unsigned int, size_t> index; // the copy's serial -> derived[]
    for (auto& entry : remap) {
        entry.second = s.nextSerial++;
        UiOp op = s.ops.at(entry.first);
        int left = std::max(op.destX, x0);
        int top = std::max(op.destY, y0);
        int right = std::min(op.destX + op.width, x0 + w);
        int bottom = std::min(op.destY + op.height, y0 + h);
        op.srcX += left - op.destX;
        op.srcY += top - op.destY;
        op.destX = left;
        op.destY = top;
        op.width = right - left;
        op.height = bottom - top;
        op.dim = intensity;
        op.owned = 0;
        op.hd = false;
        index[entry.second] = derived.size();
        derived.emplace_back(entry.second, std::move(op));
    }
    {
        Release release(s);
        for (int y = 0; y < h; y++) {
            size_t base = static_cast<size_t>(y0 + y) * s.width + x0;
            for (int x = 0; x < w; x++) {
                size_t i = base + x;
                unsigned char value = intensityColorTable[s.buffer[i]][intensity];
                unsigned int owner = s.owner[i];
                bool held = s.buffer[i] == s.expected[i];
                auto mapped = owner != 0 && held ? remap.find(owner) : remap.end();
                if (owner != 0) {
                    release.add(owner);
                }
                if (mapped != remap.end()) {
                    s.owner[i] = mapped->second;
                    s.kind[i] = UiKindArt;
                    derived[index[mapped->second]].second.owned++;
                } else {
                    // A dimmed fill is still one color; anything else is CE's now.
                    s.owner[i] = 0;
                    s.kind[i] = held && s.kind[i] == UiKindFill ? UiKindFill : UiKindUnknown;
                }
                s.expected[i] = value;
            }
        }
    }
    for (auto& entry : derived) {
        if (entry.second.owned > 0) {
            s.ops.emplace(entry.first, std::move(entry.second));
        }
    }
    markDirty(s, x0, y0, x0 + w - 1, y0 + h - 1);
}

void uiRecordTextBeforeImpl(unsigned char* dest, int pitch, const char* string, int length, int color)
{
    gPending.active = false;
    UiWindow* surface = string != nullptr ? surfaceFor(dest) : nullptr;
    if (surface == nullptr) {
        return;
    }
    UiWindow& s = *surface;
    if (pitch != s.width) {
        return; // the after-hook counts it
    }
    gPending.active = true;
    gPending.dest = dest;
    gPending.string = string;
    gPending.color = color;
    gPending.reason = UiTextOk;
    gPending.op = UiOp();

    const InterfaceFontView* f = fontView(fontGetCurrent());
    if (f == nullptr) {
        gPending.reason = UiTextNotInterfaceFont;
        return;
    }
    if ((color & DRAW_TEXT_FLAG_UNDERLINED) != 0) {
        gPending.reason = UiTextUnderlined;
        return;
    }
    UiOp& op = gPending.op;
    op.font = fontGetCurrent() - 100;
    op.color = color & 0xFF;
    op.shadow = (color & DRAW_TEXT_FLAG_SHADOWED) != 0;
    op.shadowColor = COLOR_BLACK;
    layoutText(*f, string, length, (color & DRAW_TEXT_FLAG_MONOSPACED) != 0, op.glyphs);
    // The rect ends at the last glyph pixel (trailing spaces write nothing).
    int right = 0;
    int left = 0;
    for (const UiGlyphPlace& g : op.glyphs) {
        right = std::max(right, g.x + g.width);
        left = std::min(left, static_cast<int>(g.x));
    }
    size_t offset = static_cast<size_t>(dest - s.buffer);
    op.destX = static_cast<int>(offset % s.width);
    op.destY = static_cast<int>(offset / s.width);
    op.width = op.glyphs.empty() ? 0 : right + (op.shadow ? 1 : 0);
    op.height = op.glyphs.empty() ? 0 : f->maxHeight + (op.shadow ? 1 : 0);
    if (left < 0 || op.destX + op.width > s.width || op.destY + op.height > s.height) {
        gPending.reason = UiTextOutside;
        return;
    }
    size_t n = static_cast<size_t>(op.width) * op.height;
    op.before.resize(n);
    op.beneath.resize(n);
    for (int y = 0; y < op.height; y++) {
        size_t windowRow = static_cast<size_t>(op.destY + y) * s.width + op.destX;
        memcpy(&op.before[static_cast<size_t>(y) * op.width], s.buffer + windowRow, op.width);
        memcpy(&op.beneath[static_cast<size_t>(y) * op.width], &s.owner[windowRow], op.width * sizeof(unsigned int));
    }
}

void uiRecordTextImpl(unsigned char* dest, int pitch, const char* string, int length, int color)
{
    UiWindow* surface = string != nullptr ? surfaceFor(dest) : nullptr;
    if (surface == nullptr) {
        gPending.active = false;
        return;
    }
    UiWindow& s = *surface;
    if (pitch != s.width) {
        s.oddPitch++;
        gPending.active = false;
        return;
    }
    bool pending = gPending.active && gPending.dest == dest && gPending.string == string && gPending.color == color;
    gPending.active = false;
    if (!pending) {
        gPending.reason = UiTextMismatch;
    }

    if (gPending.reason == UiTextOk) {
        // The 1x check: CE's draw replayed on the saved rect must give
        // exactly the pixels CE wrote.
        UiOp& op = gPending.op;
        size_t n = op.before.size();
        std::vector<unsigned char> predicted(op.before);
        std::vector<unsigned char> owned(n, 0);
        bool exact = predictText(*fontView(op.font + 100), op, predicted.data(), owned.data());
        for (int y = 0; y < op.height && exact; y++) {
            exact = memcmp(&predicted[static_cast<size_t>(y) * op.width], s.buffer + static_cast<size_t>(op.destY + y) * s.width + op.destX, op.width) == 0;
        }
        if (exact) {
            unsigned int serial = s.nextSerial++;
            {
                Release release(s);
                for (int y = 0; y < op.height; y++) {
                    for (int x = 0; x < op.width; x++) {
                        size_t r = static_cast<size_t>(y) * op.width + x;
                        if (owned[r] == 0) {
                            continue;
                        }
                        size_t i = static_cast<size_t>(op.destY + y) * s.width + op.destX + x;
                        s.expected[i] = predicted[r];
                        s.kind[i] = UiKindText;
                        if (s.owner[i] != 0) {
                            release.add(s.owner[i]);
                        }
                        s.owner[i] = serial;
                        op.owned++;
                    }
                }
            }
            gTextCounts[UiTextOk]++;
            if (op.owned > 0) {
                markDirty(s, op.destX, op.destY, op.destX + op.width - 1, op.destY + op.height - 1);
                s.writes[UiKindText]++;
                s.textOps++;
                s.ops.emplace(serial, std::move(op));
            }
            return;
        }
        gPending.reason = UiTextMismatch;
    }
    gTextCounts[gPending.reason]++;

    size_t offset = static_cast<size_t>(dest - s.buffer);
    int x0 = static_cast<int>(offset % s.width);
    int y0 = static_cast<int>(offset / s.width);
    int w = std::min(std::min(fontGetStringWidth(string), length) + 1, s.width - x0);
    int h = std::min(fontGetLineHeight() + 1, s.height - y0);
    if (w <= 0 || h <= 0) {
        return;
    }
    // Not an op: glyph pixels are the ones that now differ from what was
    // expected there; CE's pixels show for them.
    {
        Release release(s);
        for (int y = 0; y < h; y++) {
            size_t base = static_cast<size_t>(y0 + y) * s.width + x0;
            for (int x = 0; x < w; x++) {
                size_t i = base + x;
                if (s.buffer[i] != s.expected[i] || s.kind[i] == UiKindUnknown) {
                    s.expected[i] = s.buffer[i];
                    s.kind[i] = UiKindText;
                    if (s.owner[i] != 0) {
                        release.add(s.owner[i]);
                        s.owner[i] = 0;
                    }
                }
            }
        }
    }
    markDirty(s, x0, y0, x0 + w - 1, y0 + h - 1);
    s.writes[UiKindText]++;
}

int uiReplaySelfTest(int tick)
{
    int wrong = 0;
    int checkedWindows = 0;
    int replayed = 0;
    int skipped = 0;
    long long checked = 0;
    char firstFailure[160] = "";
    for (auto& entry : gWindows) {
        UiWindow& s = entry.second;
        if (s.ops.empty()) {
            continue;
        }
        size_t n = s.expected.size();
        std::vector<unsigned char> image(n, 0);
        std::vector<unsigned int> writer(n, 0);
        std::vector<unsigned int> skippedOps;
        for (const auto& item : s.ops) {
            const UiOp& op = item.second;
            if (op.font >= 0) {
                // Text: CE's draw replayed from the saved rect.
                std::vector<unsigned char> pixels(op.before);
                std::vector<unsigned char> owned(pixels.size(), 0);
                const InterfaceFontView* f = fontView(op.font + 100);
                if (f == nullptr || !predictText(*f, op, pixels.data(), owned.data())) {
                    skippedOps.push_back(item.first);
                    skipped++;
                    continue;
                }
                for (int y = 0; y < op.height; y++) {
                    for (int x = 0; x < op.width; x++) {
                        size_t r = static_cast<size_t>(y) * op.width + x;
                        if (owned[r] != 0) {
                            size_t i = static_cast<size_t>(op.destY + y) * s.width + op.destX + x;
                            image[i] = pixels[r];
                            writer[i] = item.first;
                        }
                    }
                }
                replayed++;
                continue;
            }
            if (op.snapshot != 0) {
                // A map snapshot (the dialogue's map view): CE's 1x pixels as copied.
                for (int y = 0; y < op.height; y++) {
                    const unsigned char* row = op.pixels.data() + static_cast<size_t>(op.srcY + y) * op.frameWidth + op.srcX;
                    size_t base = static_cast<size_t>(op.destY + y) * s.width + op.destX;
                    for (int x = 0; x < op.width; x++) {
                        if (op.trans && row[x] == 0) {
                            continue;
                        }
                        image[base + x] = row[x];
                        writer[base + x] = item.first;
                    }
                }
                replayed++;
                continue;
            }
            FrmImage frm;
            bool locked = op.fid != -1 ? frm.lock(FrmId(op.fid), op.frame, static_cast<Rotation>(op.rotation)) : frm.lock(op.path.c_str(), op.frame, static_cast<Rotation>(op.rotation));
            const unsigned char* data = locked ? frm.getData() : nullptr;
            if (data == nullptr || frm.getWidth() != op.frameWidth || frm.getHeight() != op.frameHeight
                || fnv1a(data, static_cast<size_t>(op.frameWidth) * op.frameHeight) != op.hash) {
                skippedOps.push_back(item.first);
                skipped++;
                continue;
            }
            for (int y = 0; y < op.height; y++) {
                const unsigned char* row = data + static_cast<size_t>(op.srcY + y) * op.frameWidth + op.srcX;
                size_t base = static_cast<size_t>(op.destY + y) * s.width + op.destX;
                for (int x = 0; x < op.width; x++) {
                    if (op.trans && row[x] == 0) {
                        continue;
                    }
                    image[base + x] = op.dim != 0 ? intensityColorTable[row[x]][op.dim] : row[x];
                    writer[base + x] = item.first;
                }
            }
            replayed++;
        }
        std::sort(skippedOps.begin(), skippedOps.end());
        for (size_t i = 0; i < n; i++) {
            if (s.owner[i] == 0 || std::binary_search(skippedOps.begin(), skippedOps.end(), s.owner[i])) {
                continue;
            }
            checked++;
            if (writer[i] != s.owner[i] || image[i] != s.expected[i]) {
                if (wrong == 0) {
                    snprintf(firstFailure, sizeof(firstFailure), "; first: window %d pixel %d,%d op %u wrote %u (%d), expected %d",
                        s.id, static_cast<int>(i % s.width), static_cast<int>(i / s.width), s.owner[i], writer[i], image[i], s.expected[i]);
                }
                wrong++;
            }
        }
        checkedWindows++;
    }
    debugPrint("HD: ui replay self-test %d: %s, %d windows, %d ops replayed, %d skipped (art changed), %lld px checked, %d wrong%s\n",
        tick, wrong == 0 ? "PASS" : "FAIL", checkedWindows, replayed, skipped, checked, wrong, firstFailure);
    return wrong;
}

void uiRecorderStart()
{
    configGetInt(&gGameConfig, "hd_debug", "ui_coverage_ms", &gIntervalMs, 0);
    if (gIntervalMs <= 0) {
        return;
    }
    char* captureDir = nullptr;
    configGetString(&gGameConfig, "hd", "capture_dir", &captureDir, "hd_captures");
    std::filesystem::path dir = std::filesystem::path(captureDir).parent_path() / "reports" / "ui";
    std::error_code error;
    std::filesystem::create_directories(dir, error);
    gReportPath = (dir / "ui_coverage.txt").string();
    remove(gReportPath.c_str());
    gUiRecorderActive = true;
    gLastTick = getTicks();
    tickersAdd(coverageTicker);
    debugPrint("HD: ui recorder on (coverage and replay self-test every %d ms -> %s)\n", gIntervalMs, gReportPath.c_str());
}

void uiRecorderEnable()
{
    gUiRecorderActive = true;
}

const long long* uiTextCounts()
{
    return gTextCounts;
}

void uiRecorderShutdown()
{
    if (gUiRecorderActive) {
        tickersRemove(coverageTicker);
        gUiRecorderActive = false;
    }
    long long total = 0;
    for (long long count : gTextCounts) {
        total += count;
    }
    if (total > 0) {
        debugPrint("HD: ui text: %lld draws into windows and scratch buffers, %lld passed the 1x check (%.1f%%); not ops: %lld FON font, %lld underlined, %lld outside the window, %lld mismatch\n",
            total, gTextCounts[UiTextOk], 100.0 * gTextCounts[UiTextOk] / total, gTextCounts[UiTextNotInterfaceFont], gTextCounts[UiTextUnderlined],
            gTextCounts[UiTextOutside], gTextCounts[UiTextMismatch]);
    }
    gWindows.clear();
    gScratch.clear();
}

} // namespace fallout::hd
