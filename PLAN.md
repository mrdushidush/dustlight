# Fallout HD-2D — Plan

Status: **draft for the author's review** · written 2026-10-31 · nothing implemented yet.
Inputs: `PROJECT_BRIEF.md`, planning Q&A with the author (2026-10-31), and a read of
`fallout2-ce/fallout2-ce` at commit `ebb9c716` (2026-10-30).

---

## 0. Summary

| Topic | Decision / recommendation |
|---|---|
| Base | **`fallout2-ce/fallout2-ce`** (community org fork), not `alexbatalov/fallout2-ce` |
| GPU API | **Direct3D 11** (Windows-only was confirmed); vanilla SDL path stays |
| Scale | **4× masters** in the pipeline; pre-filtered 3× and 2× tiers; runtime scale is integer 2×/3×/4× |
| Baked light | **Modulate baked art** with an "original-equivalent" invariant, per-category directional strength, no de-lighting in v1 |
| Pipeline | **Plain Python + PyTorch + spandrel**, content-addressed cache, SQLite job DB. No chaiNNer |
| Repo | One local git repo (`D:\dev\fallout2-hd`) = our fork; `upstream` remote; no `origin` yet |
| Name/license | Keep working title privately; new code under the same Sustainable Use License; rename before any publication |
| Look | "Same mood, more depth" |
| New scope | **Voice track** (TTS for unvoiced dialogue + holodisks) and **Inventory track** (Fallout-styled HD inventory/loot/barter/steal/party UI) |

Phase order proposal: **P0 → P1 (go/no-go) → P2 → I-track start → P3 → …** (see §6.4). The Voice track does not depend on rendering and can be slotted anywhere after P0.

---

## 1. What the author decided in planning (2026-10-31)

- Personal project first. The pipeline may be dev-grade and tuned to the dev machine.
- Local git only; nothing pushed. Phase 0's "CI build" becomes a local one-command CI script plus a dormant GitHub Actions file.
- Paths (set in `hd.local.ini`): pristine Epic install `<pristine>` · working copy `<working_copy>` · pipeline data `<data_root>`.
- Windows only.
- the author reviews **results** (stills, GIFs, audio, frame times), not code. Art must be **fully automated** — no manual touch-up step anywhere.
- Look: original brightness and palette as baseline; add real shadows, colored local light, bounce light, darker nights.
- Phase 1 benchmark scenes: **Temple of Trials** (indoor), **Arroyo village** (outdoor day), **New Reno at night** (outdoor, many lights).
- Voice: TTS for **NPC dialogue replies** and **holodisks/notes**; offline batch generation + runtime fallback for dynamic lines; auto-cast consistent voice per NPC + per-line emotion. Original synthetic voices only — no cloning of the original cast.
- Inventory: fix all four pain points (too few visible items, no info without clicking, no sort/filter, clunky stack moves) on **all** inventory screens (main, loot, barter, steal, party gear), **Fallout-styled HD** look.

These additions conflict with nothing in brief §2: both are presentation-only (audio, UI). No script, balance or save-format change. Each has its own config toggle.

---

## 2. Corrections to brief §4 (verified at `ebb9c716`)

1. **Base repo.** Upstream `alexbatalov/fallout2-ce` last commit is 2025-02-16. The org fork `fallout2-ce/fallout2-ce` is **1,482 commits ahead, 0 behind**, pushed 2026-10-30, ~12 active contributors including sfall maintainers (NovaRain, phobos2077). Its README now lists Epic as a supported store, and claims full support for RP, Et Tu and Sonora.
2. **Dependencies.** SDL2 (`release-2.32.8`) and zlib (`v1.3.1`) are **not vendored source** — `third_party/*/CMakeLists.txt` fetches them from GitHub at configure time (FetchContent). Vendored: fpattern, lodepng, stb_vorbis.
3. **`svga.cc` presentation.** `SDL_Renderer` is forced to the **OpenGL** driver (`SDL_HINT_RENDER_DRIVER "opengl"`). Palette→RGB conversion is done on the **CPU** by `SDL_BlitSurface` into an RGB888 surface, on every `_GNW95_ShowRect` and as a full-surface blit on every palette change. `renderPresent()` only uploads and presents.
4. **Second `SDL_Renderer` user:** `movie.cc` draws movies as a separate SDL texture overlay (`movieRenderDirectOverlay`, `movieHandleRendererReset`).
5. **`renderPresent()` has 127 call sites.** Every modal loop (dialogs, PipBoy, character editor, combat, main loop) presents on its own; there is no single frame loop yet (`svga.cc` carries a TODO about migrating to an update-render cycle). Any GPU map layer must re-render from a cached scene on *every* present.
6. **Resolution/scale.** CE no longer reads `f2_res.ini`. Settings live in `fallout2.cfg` `[screen] resolution_x/resolution_y/scale`; `scale` is integer pixel duplication via `SDL_RenderSetLogicalSize`, allowed only if the logical size stays ≥ 640×480. sfall/HRP keys from `ddraw.ini` are migrated by `game_config_migration.cc`.
7. **Mouse mapping** is done in `dinput.cc` from the window size (`mouseDeviceRefreshWindowMapping`), not by SDL's logical size — so replacing `SDL_Renderer` doesn't break input.
8. **Draw order is richer than the brief lists.** `tileRefreshGame`: clear (HRP edge clipping via `mapEdgeComputeVisibleArea`) → floors → `_obj_render_pre_roof` [flat objects pass, `tileRenderEdgeBlackSquares(false)`, non-flat objects pass, `tileRenderEdgeBlackSquares(true)`] → roofs → `_obj_render_post_roof` [outlines, floating text `textObjectsRenderInRect`, tile-less objects from `gObjectListHead` at full intensity, e.g. the hex cursor] → `tile_hires_stencil_draw`. Objects are traversed through precomputed `_orderTable/_offsetTable` per hex parity.
9. **Lighting semantics.** `LIGHT_INTENSITY_MAX = 65536` displays the art **unchanged**; ambient is clamped to **[16384, 65536]** (25–100 %) and the Night Vision perk adds to it; tile intensity resets to 655. **Light only ever darkens — nothing in the original is brighter than the raw art.** An object's intensity is `max(ambient, tileIntensity(first object on its hex))` (per hex, not per object). Roofs use ambient only. `OBJECT_TRANS_WALL` and `OBJ_TYPE_INTERFACE` ignore lighting. `intensityColorTable[256][256]` is indexed by `intensity / 512`.
10. **Floors:** confirmed — intensities sampled at the tile's vertices and interpolated over the 80×36 tile via `_intensity_map`.
11. **Egg** is applied in two places: roofs (`tileRenderRoof`) and scenery/wall objects in front of the dude (`_obj_render_object`), with corner-specific rules (north/south/west corner, `WALL_TRANS_END`). It's an intensity mask (`_intensity_mask_buf_to_buf`), not a hard cut.
12. **Art API** is now typed: `FrmId`, `CritterFrmId`, `TileFrmId` classes; `buildFid` is `FrmId::buildFid` (static constexpr); `artLock(const FrmId&)`. Per-rotation offsets in the header plus per-frame `ArtFrame{x,y}` deltas, accumulated into `object->x/y` by animation code.
13. **Palette cycling — exact ranges** (`cycle.cc`): slime **229–232** (5 Hz), monitors **233–237** (10 Hz), fire_slow **238–242** (5 Hz), fire_fast **243–247** (7 Hz), shoreline **248–253** (5 Hz), plus a sixth effect the brief missed: **"bobber" alarm red at index 254** (pulsing, ~30 Hz steps). `cycle_speed_factor` scales all periods.
14. **Archive precedence:** `data/` (master_patches) > critter_patches > `mods/` (mods_order.txt) > `patchXXX.dat` > `ce.dat` > `f2_res.dat` > `critter.dat` > `master.dat`. CE needs its own `ce.dat`, built from the repo's `files/`.
15. **Existing tools we can reuse:** `tools/dat_tool.cc` (`ce-dat-tool`: list/extract/create), `tools/ce_frm2png.cc` (FRM↔PNG, indexed PNG), and a `mapper-ce` target (map editor).
16. **Speech:** every msg-based line goes through `scripts.cc _scr_get_msg_str_speech()`; MSG entries carry an **audio filename field**. Talking-head lines use lip-sync (`lips.cc`), others play as plain speech. Holodisks: `pipboy.cc` (`data/holodisk.txt` + msg); voiced Pip-Boy lines live in `sound/pipboy/`. `.ogg`/`.wav` 44.1 kHz stereo are supported.
17. **Inventory:** CE already has 2-column inventory/loot, a 4-row barter option, ctrl-click transfers with auto-balanced caps, and party loot/barter. All screens live in `inventory.cc` (6,813 lines); barter is entered from `game_dialog.cc`.
18. **Epic install facts:** in the Epic library folder (not Program Files), with English/German/French subfolders. The English folder ships **sfall 3.2** (`ddraw.dll`/`ddraw.ini`), **f2_res hi-res patch 4.1.8**, `patch000.dat`, and one loose tile in `data/art/tiles`. `f2_res.ini` has `UAC_AWARE=1` (writes settings to `%APPDATA%\Fallout2`).

---

## 3. Answers to brief §10

### 3.1 Base repo → `fallout2-ce/fallout2-ce`

- **Why:** active (daily merges), strictly a superset of upstream, more bug fixes and sfall compatibility, Epic acknowledged.
- **Cost:** high churn — e.g. the ongoing "typesafe enum" refactors touch the same files we hook (`tile.cc`, `object.cc`, `window_manager.cc`). Mitigation: thin hooks (§8), pin `ebb9c716` for Phase 0, merge `upstream/main` **monthly** with merge commits (no rebase), and keep the list of hooked files short.
- Other forks surveyed: platform ports (Vita, Switch, 3DS, Miyoo), a co-op experiment, a Godot remake attempt, `fallout2-rt-test` (a stale upstream mirror despite the name). None is a better base.
- No `fallout1-ce` counterpart exists in the org; Phase 8 will use `alexbatalov/fallout1-ce` (last push 2025-01) or whatever fork is alive then.

### 3.2 GPU API → Direct3D 11

| Option | For | Against |
|---|---|---|
| **D3D11** ✅ | Mature and simple; excellent debug layer, RenderDoc/PIX/Nsight; compute shaders (SM 5.0) are enough for radiance cascades; identical behavior on the 5060 Ti and the 3050; SDL2 gives us the HWND; HLSL hot-reload for tuning | Windows-only (accepted) |
| OpenGL 4.6 | Already what SDL uses; portable | Global-state bugs, weaker tooling; portability not needed |
| D3D12 / Vulkan | Maximum control | 3–5× more code (sync, descriptors, memory) for no visual gain in a 2D renderer |
| SDL3 GPU | Modern abstraction | Requires migrating the whole engine to SDL3 while upstream stays on SDL2 → permanent merge conflicts |
| bgfx | Portable, mature | Extra build/shader toolchain; abstraction we don't need |

The vanilla `SDL_Renderer` path remains selectable (`[hd] renderer=sdl|d3d11`). Textures use **DDS/BC7** (DirectXTex), replacing the brief's KTX2 proposal — native to D3D11.

### 3.3 Scale factor → 4× masters, pre-filtered tiers

Logical resolution = window / scale, and CE requires ≥ 640×480:

| Display | Scale | Logical view | Asset tier |
|---|---|---|---|
| **3440×1440 (the author's monitor, 21:9)** | 3× | 1146×480 (original height, wider view; 1-px letterbox) | 3× |
| 3440×1440 | 2× | 1720×720 (more map) | 2× |
| 2560×1440 | 3× | 853×480 (≈ original framing) | 3× (4→3 offline Lanczos) |
| 2560×1440 | 2× | 1280×720 (more map) | 2× |
| 1920×1080 (laptop) | 2× | 960×540 | 2× |
| 3840×2160 | 4× / 3× | 960×540 / 1280×720 | 4× / 3× |

- AI upscalers are natively 4×, so everything is mastered at 4×. The pipeline also bakes 3× and 2× tiers so the runtime always samples 1 texel : 1 pixel (crisp, no runtime filtering). Only the tier in use is loaded.
- Logical positions × integer scale keep hotspots exact at every tier.
- Per-category exception (e.g. critters mastered at 2×) only if Phase 1 shows unfixable shimmer.

### 3.4 Baked-light strategy → modulate, with an invariant

The original never brightens art (§2.9), so the raw art *is* "the scene at full light". The rule:

1. **Invariant ("original-equivalent" mode):** with all new effects off, the renderer must reproduce the original intensity field exactly. This is a test mode, not just a design goal.
2. **Default:** `final = albedo × light + emissive`. `light` is built from ambient + per-object lights using the original falloff, then:
   - normal-mapped direction modulates the light *around* the original value (normalized so a flat-facing surface keeps the original brightness);
   - shadows and GI then darken and tint;
   - lights may exceed 1.0 near sources (HDR) — this, plus colored light, is where "more depth" comes from;
   - light **color** isn't in the data, so it's derived automatically from the emitting object's art (average color of its emissive/bright pixels: torch → warm orange, neon → its own hue, monitors → cyan; fallback warm white).
3. **Per-category directional strength:** floors full (little baked shading), walls/scenery reduced (strong baked top-left light), critters low (mostly ambient + light color + gentle rim). Strengths are config values tuned in Phase 5 with side-by-sides.
4. **No de-lighting in v1.** Intrinsic-decomposition models may get a Phase 1 side experiment if time allows, but nothing depends on them.

### 3.5 Pipeline tooling → Python + PyTorch scripts

- chaiNNer is GUI-first; a fully automated, resumable pipeline is easier as code.
- Stack: Python venv, PyTorch (CUDA, Blackwell sm_120), **spandrel** (loads ESRGAN-family / OpenModelDB models), diffusers/transformers for normal and depth models, numpy, Pillow, OpenCV, imageio.
- One CLI (`fo2hd`) with stages `extract → decode → upscale → normals → height → emissive → pack`. Each stage is idempotent.
- **Content-addressed cache:** key = hash(input bytes + stage params + model file hash + stage code version) → `<data_root>\cache\<stage>\<key>`. A SQLite job DB tracks progress, so batches resume after a crash or reboot.
- Python 3.14 is installed. Risk: wheel availability on 3.14/sm_120. Fallback: a pinned 3.12 env via **uv** (needs the author's OK to install, §9).

### 3.6 Repo layout → single fork repo

```
D:\dev\fallout2-hd\                git; remote "upstream" = fallout2-ce/fallout2-ce; no "origin" yet
  src\                             upstream code, thin hooks only
  src\hd\                          our engine module: config, capture, d3d11, renderer, lighting, ui, voice
  third_party\imgui, rmlui         added when first needed
  tools\pipeline\                  Python asset pipeline
  tools\voice\                     Python TTS pipeline
  tools\scripts\                   build.ps1, deploy.ps1, ci.ps1, make_working_copy.ps1
  docs\decisions\                  ADRs (one per decision)
  PROJECT_BRIEF.md  PLAN.md
  hd.local.ini                     git-ignored: pristine path, working copy, data root
```

The two pipelines live in the same repo because they share config, the FRM/MSG readers and the cache, and are versioned with the engine that consumes their output.

### 3.7 Name and license

- "Fallout" is a Bethesda trademark: fine for a private project; **rename before any publication**.
- The engine is a derivative of Sustainable Use License code. Simplest compliant choice: license our new code under the **same SUL v1.0**, keep all notices, and add a "modified by" notice from day one (the license requires it on distribution).
- Third-party code and models are recorded in `docs/THIRD_PARTY.md` with their licenses (Dear ImGui and RmlUi: MIT; model licenses vary — non-commercial is acceptable for personal use, and each one is noted).

### 3.8 Extra decisions the new tracks need

- **UI tech:** **RmlUi** (HTML/CSS-like layout and styling, MIT) for the player-facing inventory, later the full UI pass; **Dear ImGui** only for the developer overlay (toggles, frame timings). RmlUi needs a D3D11 render interface (~500 lines, ours to write). Final choice is confirmed by an ADR in I1.
- **TTS model:** chosen by a **listening bake-off** (V1), not up front. Candidates to re-verify at that time: Chatterbox (emotion-exaggeration control), IndexTTS-2 (emotion + duration control), Orpheus (emotive tags), Kokoro-82M (very fast — runtime-fallback candidate).
- **Emotion tagging LLM:** a local model by default (free, offline, private); the Claude API is an optional switch if local quality is poor.

---

## 4. Phase 0 — Baseline (session-sized tasks)

Exit criteria (brief §7, adjusted): vanilla CE built from our repo runs from the working copy with the Epic data, a save loads, and **one local command** does a clean build plus checks.

### P0.1 Repo bootstrap and safety rails
- `git init` in `D:\dev\fallout2-hd`, add `upstream`, fetch, create `main` from `ebb9c716`, keep `PROJECT_BRIEF.md` and `PLAN.md` as our first commit on top.
- `.gitignore`: `*.dat *.frm *.pal *.msg *.map *.acm *.int *.lip *.aaf *.fon *.mve` + all image/audio/model extensions, `out/`, `hd.local.ini`. **Exempt upstream's own assets:** CE tracks its own `files/ce.dat/**` sources (a few `.frm`, `.msg`, `.int`), `files/f2_res.dat` and `sfall_testing/**` — those are CE's files, not game data, and must keep merging.
- **Pre-commit hook** that rejects those extensions and any file > 5 MB outside the exempt upstream paths (safety net for hard rule §8). Merge commits from `upstream` are checked against the same exemption list, so they pass.
- `hd.local.ini.example` with the three paths; `NOTICE` "modified by" entry; ADR-0001…0008 for the decisions in §3 that the author approves.
- **Accept:** `git check-ignore` passes for a dummy file of each banned extension; the hook blocks a test commit of `test.frm`; `git status` clean.

### P0.2 Working copy
- `tools/scripts/make_working_copy.ps1`: copy `Fallout 2 English` → `<working_copy>`, write SHA-256 of the pristine `master.dat`, `critter.dat`, `patch000.dat` and `fallout2.exe` to `hd.local.ini`'s manifest.
- Decide what to do with sfall's `ddraw.dll`/`ddraw.ini` in the working copy (CE ignores the DLL but migrates some ini keys). Default: keep it, review the migrated keys.
- **Accept:** hashes match the pristine files; the pristine folder's file list and mtimes are unchanged after the script runs (the script checks this itself).

### P0.3 Build
- `tools/scripts/build.ps1`: configure with preset `windows-x64` (VS 2022 Build Tools, MSVC 14.44 — already installed), build `RelWithDebInfo` and `Debug`, build `ce.dat` the same way the repo's CI does.
- **Accept:** `fallout2-ce.exe` and `ce.dat` produced from a clean tree; warning count recorded as a baseline.

### P0.4 Run with the Epic data
- `tools/scripts/deploy.ps1`: copy the exe and `ce.dat` into the working copy (also keep a **`fallout2-ce-vanilla.exe`** built from the pinned commit for future A/B and save-compat tests).
- Smoke test: intro movie, main menu, new character, Temple of Trials, combat, a save, quit, reload. Then `[start] map=arvillag.map` via CE's content config (migrated from sfall's `StartingMap`) to check that a talking head (Arroyo's elder) plays speech and lip-sync.
- **Accept:** the author plays ~10 minutes and confirms; a save made in our build loads in `fallout2-ce-vanilla.exe` and vice versa; screenshots in `docs/phase0/` (taken from the game, so git-ignored — a local report only).

### P0.5 Local CI
- `tools/scripts/ci.ps1`: clean configure + both configs + `tests/script_serialization_test.py` + the ignore audit (no banned file tracked) + format check (needs clang-format, §9).
- A dormant `.github/workflows/hd-ci.yml` that runs the same steps once a remote exists.
- **Accept:** one command, green, in under ~10 minutes.

---

## 5. Phase 1 — Art de-risk (session-sized tasks)

Exit criteria (brief §7): side-by-side stills, original vs HD-lit, for the three benchmark scenes, plus one critter walk cycle at 4× as a GIF. **Go/no-go on the whole project.**

### P1.1 HD module skeleton
- `src/hd/` with `hd::init()`/`hd::exit()` called from `game.cc`; the module reads its own `[hd]` section of `fallout2.cfg` through CE's existing config API (no `settings.cc` changes); debug hotkeys registered from `gameHandleKey`.
- **Accept:** with `[hd] enabled=0`, screenshots of a fixed scene are bit-identical to the P0 build; with it on, the hotkey logs to `debug.log`.

### P1.2 Draw-list recorder
- Recorder calls at the real draw sites (`tileRenderFloor`, `tileRenderRoof`, `_obj_render_object` incl. the egg branch and translucency, outlines, edge squares, floating-text rects). Recording at the draw sites captures the exact order and decisions — and the same hooks become Phase 3's draw-list emitter.
- A capture also stores: all light sources (tile, elevation, distance, intensity), the per-hex intensity array, ambient, the current palette (768 B), `intensityColorTable` (64 KB), the five blend tables, camera (center tile, offsets, window size), game time, the floor vertex intensities, and a PNG of the 8-bit iso window.
- Hotkey Ctrl+F12 → forced full refresh with recording on → `<data_root>\captures\<map>_<gametime>.json` + `.png`.
- **Accept — the replay test:** a Python script re-rasterizes the capture at 1× from the original FRMs and the dumped tables; the pixel diff against the captured PNG is **0 pixels** (documented exceptions only, e.g. text). This proves the draw list is complete before anything is built on it.

### P1.3 Scripted capture mode
- `[hd_debug] capture_map=… capture_time=… capture_tile=… capture_exit=1`: after init, load the map, set the game time, center the camera, capture, exit. No playing needed to reach New Reno at night, and it becomes the regression harness for every later phase.
- Benchmark maps: `artemple.map`, `arvillag.map`, New Reno main street (`newr1.map` — confirm from `maps.txt`).
- **Accept:** three captures produced by one command, each passing the replay test.

### P1.4 Pipeline environment, extractor, decoder
- `tools/pipeline/` venv; verify `torch.cuda` on the 5060 Ti (sm_120).
- Python DAT2 reader + FRM decoder: RGBA PNG **and** palette-index PNG per frame + JSON (fps, action frame, per-rotation offsets, per-frame w/h/x/y). It resolves the same precedence as CE (§2.14), so the pipeline sees the file the game sees.
- **Accept:** file list identical to `ce-dat-tool list`; extracted bytes identical to `ce-dat-tool extract` on a sample; decoded pixels identical to `ce-frm2png` on a sample; full-archive extraction resumes after a forced kill.

### P1.5 Sample set
- From the three captures: the set of unique FIDs (floors, walls, scenery, roofs, critters) + the player walk cycle `hmwarrab.frm` (tribal male, unarmed walk, 6 rotations).
- **Accept:** a manifest with counts per category; the compositor (P1.8) can render every capture with only the sample set plus fallback.

### P1.6 Upscaler bake-off
- 4–6 candidate 4× models (OpenModelDB pixel-art / game-texture families) via spandrel, per category.
- Alpha handling: index 0 is transparent → dilate colors into the transparent area, upscale RGB, upscale the mask separately and anti-alias it. This avoids dark/colored halos.
- Output: a local HTML contact sheet per category (nearest 4× vs each model, zoomable crops).
- **Accept:** the author picks one model per category; the pick is recorded in an ADR.

### P1.7 Normals, height, emissive
- **Heuristics first** (deterministic): floors are flat; walls are vertical planes oriented by wall type; upright objects use a billboard height (`height = anchorY − pixelY` on a vertical plane through the anchor).
- **ML detail on top:** 2–3 normal/depth models (e.g. Marigold-normals, DSINE, Depth Anything) blended with the heuristics.
- **Emissive and cycle masks** from palette indices 229–254 (§2.13; shoreline is cycling but not emissive).
- **Accept:** visual sheets per sample asset; automatic checks (floor normals within a few degrees of "up", walls vertical, emissive masks exactly match the index ranges).

### P1.8 Animation stability
- Walk cycle at 4×: (a) frame-by-frame, (b) temporally stabilized (optical-flow-guided blending of neighbor frames), (c) 2× master for critters. A flicker metric (mean difference between motion-aligned consecutive frames, relative to the original) next to the GIFs.
- **Accept:** GIFs + metric table for the author.

### P1.9 Python compositor
- Reads a capture + HD assets (nearest-upscaled original as fallback) → G-buffer at 4× (albedo, normal, height, emissive) → lighting: ambient + per-object lights with the original falloff, normal mapping, screen-space shadows (height ray-march), simple tone map. Runs on the GPU via PyTorch.
- Also renders the **original-equivalent** mode (§3.4) as a check: it must match the 1× original upscaled 4× nearest.
- **Accept:** stills for the three scenes: original 4× nearest · HD unlit · HD lit.

### P1.10 Go/no-go package
- One local report page: the stills (with a comparison slider), the walk-cycle GIF, model picks, failure cases, pipeline throughput (frames/hour per stage), and the projected time for a full-game batch.
- **Accept:** the author decides go / no-go / go-with-changes.

---

## 6. Later phases (outline)

### 6.1 Rendering phases (brief §7, refined)

| # | Phase | Main work | Exit |
|---|---|---|---|
| 2 | **GPU presentation** | D3D11 device + swap chain on the SDL window; 8-bit screen uploaded as an R8 texture + 256×1 palette texture, palette lookup in a shader (removes the CPU `SDL_BlitSurface` conversion); D3D11 movie path; resize/fullscreen; vsync/limiter; Dear ImGui dev overlay | Pixel-identical readback vs. the SDL path on a fixed screen set (main menu, map, dialog, PipBoy, world map, movie frame); frame time ≤ baseline; SDL path selectable |
| 3 | **Draw-list renderer at N×** | Recorder hooks switch to emit-only; full-frame re-emission on every present (scene traversal is cheap without rasterizing); original art as **index textures + palette lookup** so cycling and fades keep working; the five translucency modes as exact 256×256 LUTs; egg mask; outlines; floating text; HRP edge clipping; **UI-over-map compositing** via a map-coverage mask from the window manager | **1× output pixel-identical to the CPU renderer** on all regression captures; correct at 2×/3×/4×; picking unchanged |
| 4 | **HD assets** | Final §5.6 format (below); BC7 DDS tiers; per-map streaming with an LRU and a VRAM budget (4 GB laptop!; 8 GB since 2026-10-31, laptop dropped); mixing HD and fallback | Mixed scenes show no seams; missing assets fall back; VRAM within budget on both machines (dev PC only since 2026-10-31) |
| 5 | **Lighting** | Direct + ambient (original-equivalent first), light colors, shadows, radiance-cascade GI (compute), quality tiers, per-category strengths | Day/night, caves, Den at night approved by the author; every feature toggles; 60 fps at 3440×1440 all-on (dev; added 2026-10-31, the author's native resolution), 60 fps at 1080p GI-off (laptop; on hold since 2026-10-31, laptop out of scope) |
| 6 | **Effects** | Palette-cycle shaders on HD assets via cycle masks; emissives → bloom; AgX tone map, grain, vignette, grading; fades on the HD layer | Water, fire, monitors and alarms animate correctly; bloom on emissives; each effect toggles |
| 7 | **Full asset pass + UI pass** | Whole-game batch; automated capture tour of every map with diff reports; HD versions of the remaining UI (interface bar, dialogue, PipBoy, character screen, world map) built on the I-track UI layer | Whole game playable start to finish; no visual regressions in the capture tour |
| 8 | **Fallout 1** | Port `src/hd` + pipeline config to fallout1-ce | Same renderer and pipeline running Fallout 1 (needs the author's F1 copy) |

**Phase 3 tasks** (broken down 2026-10-31 at the start of P3, after reading the draw code again):

| Task | Work | Accept |
|---|---|---|
| P3.1 Emitter | The P1.2 recorder hooks gain an emit-only mode: a full-window traversal (`tileWindowRefresh` with blits suppressed) builds the complete draw list in memory. It runs at present time, only when a CE draw touched the map since the last emission. The draw list is the recorder's own record list. | Draw list complete (same records as a capture's full refresh); traversal cost measured; no change to the CPU path's output |
| P3.2 GPU map renderer, exact mode | R32_UINT index target at scale N (every per-pixel decision made at logical-pixel resolution, so N× is exactly the 1× image upscaled nearest); original frames in an R8 atlas; CE's own `intensityColorTable`, `colorMixAddTable`, blend and gray tables as LUT textures. Overwrite ops (fills, lit sprites, floors, interface, text) rasterized in draw order; destination-reading ops (five translucency modes, egg mask, outlines) as compute passes on the same target. Floor light computed in the shader from the 10 vertex intensities (the cells CE's triangle fill never writes are permanently 0, so the result depends on nothing else) | 1× GPU map = CE's iso window buffer after a full refresh, 0 px, on the benchmark scenes and the save-game walk |
| P3.3 Compositing | Presenter composites per screen pixel: map pixel if (a) no window above the map and no opaque cursor pixel covers it (ownership from the window list and the cursor shape) and (b) the 1× GPU map agrees with what CE put on the screen there; otherwise CE's own pixel. (b) makes anything that bypasses the draw list (grey options background, black fill on map load, half-done scrolls) fall back to the original pixels by itself, and counts disagreements as a live check | Final frame at 1:1 = SDL path, 0 px; N× map = nearest(1×) for N = 2, 3, 4; mismatch counter 0 in steady state |
| P3.4 Tests + perf | `[hd_debug]` map self-test (Ctrl+F10 / injected): forced CPU refresh, then (a) (b) (c) above with PNG diffs; scripted runs over the four benchmark scenes, the save walk, walking and scrolling; picking unchanged (game logic untouched; hit-testing reads art, not the screen); frame time on/off at 3440×1440 | All self-tests 0 px; frame time reported |
| P3.5 Report | Phase 3 report for the author: what changed, test results, frame times, before/after stills | the author reviews |

**Phase 4 tasks** (broken down 2026-10-31 at the start of P4). The author's answers at the start: run all of P4 and stop with a report; HD versions for **all map art except critters** (tiles, walls, scenery, items, misc ≈ 35 Mpx; the sample-set critters incl. the player are HD too; the full critter pass stays in P7); BC7 via Microsoft's **texconv** (official release binary in `<data_root>/tools`); the 4 GB laptop budget is **simulated** on the dev PC, plus a script the author can run on the laptop later. **Changed by the author (2026-10-31, end of session 2):** the laptop is out of scope for now; the VRAM budget is **8 GB** and all testing happens on the dev PC (RTX 5060 Ti 16 GB, 3440×1440), so P4.5 drops the laptop simulation and script.

| Task | Work | Accept |
|---|---|---|
| P4.0 Bulk HD masters | `fo2hd manifest tiles,walls,scenery,items,misc` (every FRM in those folders) → `fo2hd hdassets --manifest …` | All 6,865 assets built, resumable |
| P4.1 Runtime packs | `fo2hd pack`: per FRM one atlas per tier (2×/3×/4×, area-downscaled from the 4× master), all tiers sharing one layout in 1× pixels (cells on multiples of 4, so every frame starts on a BC7 block at every tier), BC7_UNORM via texconv, `pack.json` with cells, sizes and an FNV-1a hash of each frame's original indices | Round-trip PSNR of the BC7 tiers reported; packs for the sample set and the bulk set |
| P4.2 HD color pass, original art | A second pass over the same command list into an RGBA16F target at scale N, in "base" palette space (before fades and gamma). Continuous versions of CE's rules: light as a multiply (and CE's lighter half above full), translucency as `light × lerp(dest, tableColor, gray/7)` with dual-source blending, egg as a weight with the mask filtered at N×, cycle-color pixels take CE's exact result from the live palette, text/cursor/fills from the palette, outlines from the exact pass (tagged pixels). Floors, roofs and walls take their coverage from the 1× mask (no seams by construction); other art uses its HD alpha. The compositor applies the palette's current fade (per-channel gain fitted to `_cmap`) and CE's gamma table; if the live palette isn't a scaled `color.pal`, the frame falls back to exact mode. Redrawn when the draw list changes, when cycle colors change and the frame has cycle pixels, and when HD art arrives | "Original-equivalent" self-test: with every frame forced to the original art, the HD pass equals the exact pass's colors up to CE's palette quantization (PSNR/max error reported per scene), 0 holes (alpha < 1 where the exact map has a pixel) |
| P4.3 HD art store | `src/hd/hd_art.{h,cc}`: `pack.json` + tier DDS read by a loader thread (files only, no CE calls); frames uploaded on first use into a BC7 page pool (`Texture2DArray`, 2048² pages, 4 MB each), shelf-packed, LRU by page, never evicting a page the current frame uses; the pool's size is the VRAM budget (`[hd] vram_budget_mb`). Until an FRM is loaded, or if its hash/size doesn't match the art CE draws, the sprite uses the original art | HD art visible in game; self-test passes with HD on (exact checks unchanged); counters for loads, uploads, evictions, misses |
| P4.4 Mixing and seams | Fallback = original art, nearest; missing files, hash mismatches and over-budget frames all go there. Tests: holes = 0 with HD on and with a random half of the FRMs forced to fallback; tile-boundary seam metric (color step across tile edges vs. inside tiles) | 0 holes in every scene; seam metric reported |
| P4.5 Budget and perf | VRAM accounting (pool, targets, atlases, and the process total from DXGI) in the overlay and log; GPU timers on the map passes; scripted run with a tiny pool (eviction and over-budget counters); frame times at 3440×1440 with HD on | Process VRAM well inside the 8 GB budget; with a tiny pool the checks still pass (fallback, no holes); frame time reported |
| P4.6 Report | In-game stills HD vs original at 3440×1440 (back-buffer captures), mixed-scene close-ups, numbers | the author reviews |

Provisional calls (the author can overrule): outlines stay 1× (drawn by the exact pass) and cycling water/fire uses the original pixels until P6; missing art falls back to nearest (a pixel-art filter is a later option); `[hd] map_gpu` and the HD art become the working copy's default when P4 passes.

**Phase 5 tasks** (broken down 2026-10-31 at the start of P5). The author's answer at the start: run all of P5, then report; look choices (night darkness, shadow strength, light colors) are provisional, tunable settings listed in the report with side-by-sides. The design follows the P1.9 Python compositor, which the author saw at the go/no-go: light starts from the original's own per-hex light field, so the original-equivalent invariant (§3.4) holds by construction.

| Task | Work | Accept |
|---|---|---|
| P5.0 Floor seams (from P4.4) | Floor and roof tiles upscaled inside a ring of copies of themselves on the tile lattice (neighbors at (48, −12) and (32, 24)), cropped; detail normals from the same canvas. Re-bake `art/tiles`, re-pack | Seam metric's HD edge/inside ratio close to the original's; no straight lines in Arroyo sand close up |
| P5.1 Light inputs | `src/hd/light_scene.{h,cc}`: per build, the ambient level, game hour, the elevation's per-hex light (CE's unclamped `lightGetTrueTileIntensity`, uploaded when it changes) and the light sources (objects with a light radius: position, radius, intensity, mount height, color). Per atlas frame, computed on upload from the 1x silhouette (P1.7 heuristics, so original and HD art get the same shape): base normal (flat floors, wall facing from the bottom edge's slope, pillow billboards) and height above the ground line | Inputs shown in the overlay; light list matches the capture's `lights` |
| P5.2 G-buffer | The HD pass split in two: unlit albedo (same blending rules as P4.2, light removed) and attributes (normal, ground point, height, CE's own light level, category, unlit/roof flags). The P4.2 pass stays as the `original` light mode | With the new light at neutral settings the picture equals the P4 one up to the light field's smoothing; `original` mode unchanged (P4 checks pass) |
| P5.3 Direct light | Compute pass at N×: the per-hex light field, smoothed, sampled at each pixel's ground point; ambient + each light's share of the light above ambient (shared by CE's own falloff), shaped by the normal (per-category detail and shape strengths), HDR pool gain near strong lights, soft shoulder tone map; roofs ambient only; unlit pixels (interface, text, cycle colors) unchanged; darker, bluer nights | Stills of the benchmark scenes; mean brightness per scene close to the original's |
| P5.4 Light colors | Per light object, the color of bright saturated pixels of the art on and around its hex (stable while scrolling), cached; critter lights neutral; mixed toward warm white | Neon, fire and monitor lights take their own hue |
| P5.5 Shadows | Occluder heightfield (every upright pixel's height scattered to its ground point at 1x); per light a visibility map on a half-resolution ground grid inside its radius (the lowest height a point needs to see the light); soft edges; range-limited like the prototype, since CE's light field already contains wall blocking | Shadows behind critters and scenery near lamps; no shadow acne or light leaking through walls |
| P5.6 Sun and ambient occlusion | Outdoor daylight: part of the ambient comes from a sun aligned with the art's baked light (top left), with heightfield shadows; energy-conserving so a flat open ground keeps the original brightness. Heightfield ambient occlusion (contact darkening at wall bases, under objects) | Day scenes gain shadows without getting darker overall |
| P5.7 Bounce light (radiance cascades) | 2D radiance cascades on the ground plane (compute): emission = direct-lit ground albedo + emissive pixels, occluders = walls from the heightfield; indirect light added as albedo × indirect | Colored spill from neon and fire; bounce into shadows; cost measured |
| P5.8 Quality tiers and toggles | `[hd_light]` settings: mode (`original`/`new`), quality (`low`/`medium`/`high`), a switch per feature and the tuning values; Ctrl+F7 cycles new/original light; the overlay shows inputs, switches and per-pass GPU times | Every feature toggles; tiers documented |
| P5.9 Tests and perf | Self-test: `original` mode still passes P4.2; holes 0 with new light; brightness drift per scene; scroll stability (same world point keeps its light); new scenes `arroyo_night`, `den_night`; GPU timers per pass at 3440×1440 | All checks pass; 60 fps at 3440×1440 with everything on |
| P5.10 Report | Stills original / P4 / P5 per scene (day, night, caves, Den at night), one image per feature switched off, frame times, provisional calls | the author reviews |

**Phase 6 tasks** (broken down 2026-11-01 at the start of P6, after the author accepted P5). The author's answers at the start: run all of P6, then report; film grain and vignette default to **subtle, map only** (interface untouched); other look values provisional, shown as side-by-sides and GIFs. Palette cycling (cycle.cc) rotates six ranges: 229–232 slime (5 Hz), 233–237 monitors (10 Hz), 238–242 slow fire (5 Hz), 243–247 fast fire (7 Hz), 248–253 shoreline (5 Hz), 254 alarm (red pulse, 30 Hz). Today HD art shows those pixels as the original's 1× blocks.

| Task | Work | Accept |
|---|---|---|
| P6.0 Cycle scan + test scenes | Engine-side scan (`[hd_debug] cycle_scan`): every map, every elevation, floor/roof tiles and objects, pixels per cycle range in their art, with the spots that show the most; pick a scene per range (slime, monitors, fire, shoreline, alarm) into `scenes.json`; frame-sequence capture (back buffer every N ms) for GIFs | A scene for each range, or a note where the game has none |
| P6.1 HD palette cycling | On HD art, per pixel: the cycle range and position interpolated over the 1× indices (positions as angles, so neighbors across the wrap stay neighbors), weight bilinear and sharpened; color = HD albedo + weight × (live color − phase-0 color) at that position, so the HD detail stays and the colors step with CE's ticks. Original art keeps CE's exact pixels. Both light modes | At phase 0 the picture equals P5's; at other phases HD cycle pixels area-downscaled match CE's 1× colors; animation GIFs per range |
| P6.2 Emissives | Cycle ranges except the shoreline, plus the P5 glow heuristic (bright, saturated pixels of objects within 2 hexes of a lamp or fire) become emissive: unlit by the new light, an emission weight in the G-buffer, feeding bounce light and bloom. Shoreline water is lit like the floor it's on | Fire and monitors don't dim in shadow or at night; water darkens with its floor |
| P6.3 Post chain | The light pass writes HDR; a post pass on the map does bloom (emissive + bright pixels, downsample/upsample chain), tone map (P5's hue-keeping shoulder or AgX, chosen by measurement against "same mood"), grading (lift/gamma/gain, saturation, temperature; optional `.cube` LUT), vignette and grain (map rect only), then the existing fade + gamma in the compositor | Bloom on emissives; each effect toggles; brightness drift per scene reported |
| P6.4 Fades on the HD layer | The palette check fits fade as gain + offset per channel (fade to white on quit, fade to black on map change, script fades), so fades stay HD instead of dropping to the exact pass | Fade sequences: HD fallback frames 0, composite check passes |
| P6.5 Toggles, tiers, overlay | `[hd_post]` settings and switches, Ctrl+F6 effect cycling extended, quality tiers include bloom, overlay rows and GPU timers per post pass | Every effect toggles; tiers documented |
| P6.6 Tests and perf | Map tests (new light, original light, 3440); cycle checks (phase-0 equivalence, per-range match); fade check; brightness drift; perf at 3440×1440 everything on | All checks pass; 60 fps at 3440×1440 all-on |
| P6.7 Report | GIFs of each cycle range original vs HD, bloom on/off, tone map variants, grading/vignette/grain on/off, fades, numbers, provisional calls | the author reviews |

**Phase 7 tasks** (broken down 2026-11-02 at the start of P7). The author's answers at the start: begin with the parts that don't open the game (he uses the PC for daily work); the art batch runs **adaptively** (about half the GPU while he is at the PC, full speed while it is locked, paused while he plays; up to 10 GB VRAM); tests that open the game run **only while the PC is locked** and stop when he unlocks; the UI becomes an **HD skin** over the existing screens, so P7 splits into **P7a** (assets + tour) and **P7b** (UI). The RmlUi inventory redesign (I-track) stays a separate, later track.

| Task | Work | Accept |
|---|---|---|
| P7a.0 Batch speed + sharing the PC | Small frames go through the model many at a time (`Upscaler.rgb_batch`: grouped by size rounded up to 16 px, edge-padded to the group's size; 4× faster on critter frames, 42–54 dB from the one-at-a-time result); frames repeated across FRMs (9% of critter frames) built once and copied; maps and PNG writes on worker threads, overlapping the next model pass. `fo2hd --adaptive` (`governor.py`): below-normal CPU priority, half the GPU while the PC is in use, full speed while it is locked, paused while the game runs unlocked, `<data_root>\batch_mode.txt` override, VRAM capped at 8 GB. `tools/scripts/p7_batch.ps1 -Detach / -Stop` | Batched output matches the old path (PSNR); VRAM stays under the cap; the batch keeps out of the author's way |
| P7a.1 Whole-game art | Critter masters (7,119 FRMs, 224k frames, 646 Mpx; a 59-FRM review set first), UI art masters (heads, intrface, inven, skilldex, backgrnd: 1,223 FRMs, 120 Mpx, category `interface`; packed in P7b once the UI format is fixed), critter packs with a BC7 round-trip check | All built; packs verified; no FRM the maps draw is left without a pack |
| P7a.2 Map scan + roofs | `fo2hd maps`: every map file's header and tile grid → which tiles are drawn as floors and which as roofs (1,370 roofs; P4 called all but 29 floors), and the capture tour's views. Roofs get the roof-edge cleanup (P5.0): re-bake + repack | Scan agrees with the sample set's draw-derived roofs and floors; roofs rebuilt |
| P7a.3 Missing-art names | The engine writes the FRMs drawn without a pack to `hd_art_missing.txt` at exit and logs a per-folder count (the author's QA log only had the count, 154) | List after a play session or the tour |
| P7a.4 Capture tour | `tools/scripts/tour.ps1`: 652 views (the entrance and up to two of the busiest spots of each of the 221 elevations with floors, 155 map files), one game run each at 3440×1440: map self-test (exact 1×/N×/composite checks, HD holes) and stills HD vs original art. Runs only while the PC is locked; resumable | All views run; 0 failed checks; every view reviewed in the report |
| P7a.5 Regressions | `fo2hd report7` diffs each view's still against an earlier tour run (map area at 1/8 size, share of changed pixels) | Tours before and after P7b: no unexplained changes |
| P7a.6 Carry-overs | Combat benchmark (Arroyo-cave combat dipped to 48–49 fps), LRU eviction with a map-change walk (P4.5), better cycle views for den_fire and sf_docks, Klamath goo brightness (the author) | Each closed or reported |
| P7a.7 Report | `fo2hd report7`: batch progress, map scan, critter walk cycles original vs HD at 3× with the shimmer metric, the tour gallery (hover = original), numbers | the author reviews |
| P7b.0 UI recorder (coverage) | ADR-0014: art known by address (art.cc registers every loaded FRM), draw.cc's copy primitives and the text dispatch record writes into window buffers; `[hd_debug] ui_coverage_ms` logs per window the share of pixels known art explains. The tour's first locked run walks the screens once | Coverage numbers per screen |
| P7b.1 UI ops | Art writes become ops (FRM by fid or path, rotation, frame, source sub-rect, destination, transparent or not, frame hash); every window pixel knows the op that last wrote it, and an op lives while it owns a pixel. Self-test: replaying a window's live ops from the original art at 1× gives the recorded pixels | Replay self-test passes on every screen of the walk |
| P7b.2 HD UI layer | Per window an N× color target drawn from the HD page pool (the map's art store and tiers); an op whose pack is still loading is drawn when it arrives (the window's target is redrawn from its live ops). The presenter shows HD UI only where the pixel's window is on top and the recorded index equals the index CE put on the screen (the map's exactness rule), through the same fade and gamma fit as the HD map. Ctrl+F8 switches it with the map art, Ctrl+F9's tint shows what it owns, overlay lines | 1:1 self-test unchanged; walk stills HD vs original; no wrong pixels |
| P7b.3 UI packs | The interface masters (heads, intrface, inven, skilldex, backgrnd) packed by the batch; soft (HD alpha) vs hard (1× mask) edges is a pack flag, chosen from stills | Packs verified (BC7 round trip) |
| P7b.4 Coverage fixes | From the walk's numbers: copies between windows carry their HD content, scratch-buffer composition (if a screen needs it), walk entries for dialogue, barter and the world map | Each listed screen mostly HD; the rest reported |
| P7b.5 HD text | Text writes recorded (font, string, color, position); glyphs built offline from the game's fonts at N×; 1× check against CE's own glyphs | Text crisp at 3×; fallback where the check fails |
| P7b.6 Tests + report | `ui_test.ps1` (locked PC only): per screen HD vs original stills, coverage, self-tests, frame time; a second tour (P7a.5) | the author reviews |

**HD asset format (finalizes brief §5.6):** keyed by lower-case DAT path, e.g. `art/critters/hmwarrab.frm` → `<data_root>\hd\art\critters\hmwarrab\` containing `albedo`, `normal`, `height`, `emissive`, `cycle` atlases per tier (`.png` while developing, BC7 `.dds` for runtime) and `manifest.json` (scale, per-rotation/per-frame rects, scaled offsets, fps, action frame, source hash, pipeline version). Loader rule as in the brief: HD if present, otherwise GPU-upscaled original.

### 6.2 Voice track (V)

| # | Task | Exit |
|---|---|---|
| V0 | **Line inventory:** parse all dialogue `.msg` + holodisk text; statically scan compiled `.int` scripts for reply vs. option vs. float opcodes to label each msg id; mark already-voiced lines (audio field / existing speech files); map scripts → speaking critters (proto, gender, art type, name). SQLite line DB | Counts report; the author spot-checks 50 random lines for correct labels |
| V1 | **Bake-off:** 3–4 local TTS models × 30 representative lines (gender, age, mutants, ghouls, robots, emotions) | Listening page; the author picks the model; ADR |
| V2 | **Casting + emotion:** deterministic voice per speaker from proto/art/script; DSP presets for super mutants, ghouls, robots and computers; LLM tags each line's tone from its dialogue context; pronunciation lexicon for Fallout names (Hakunin, Sulik, Vault-Tec, Arroyo…) | Casting sheet + 30 tagged samples approved |
| V3 | **Batch generation:** cached by hash(text, voice, emotion, model); ASR round-trip QC (Whisper) auto-regenerates garbled lines; loudness normalized to the original speech level; `.ogg` into `<data_root>\voice\` indexed by (msg file, msg id) | Full batch done; QC failure rate < 1 %; resumable |
| V4 | **Engine integration:** hook in `_scr_get_msg_str_speech` — if a reply has no audio and voice is enabled, play the generated file through CE's existing non-lip-sync speech path; holodisk hook in `pipboy.cc` (stop on page change/close); runtime fallback for dynamic lines via a small sidecar TTS process (local pipe, disk cache); `[hd_voice]` toggles and volume | Play through Arroyo + Klamath conversations: every unvoiced reply is voiced, one consistent voice per NPC, no behavior change (state-diff test, below) |

**Voice track tasks** (planned 2026-11-03 while the author tests P7; nothing built yet, the start waits for his go and the answers below).

What the game holds (counted from the working copy's archives, 2026-11-03):

| Source | Entries | Words | Notes |
|---|---|---|---|
| Dialogue `.msg` (802 files) | 49,514 | 484,072 | NPC replies, player options, floats and descriptions mixed; median 7 words, 90% under 21, longest 143. Only the script scan (V0.2) tells which is which |
| Already voiced | 1,011 | 17,452 | 13 characters with talking heads (Sulik 244, Myron 168, Lynette 164, President 78, Marcus 70, Harold 66, Hakunin 61, Tandi 52, the Elder 40, …) plus the narrator: 3,200 files in `sound\speech` (`.acm` audio, `.lip` lip-sync). These characters also have unvoiced lines |
| Combat taunts (`combatai.msg`) | 3,537 | 18,931 | Floats critters call out in combat (`combat_taunts=1`) |
| Holodisks (`pipboy.msg` + `data\holodisk.txt`) | ~20 disks | ~7,000 (whole file) | Shown page by page in the Pip-Boy |

If NPCs speak 55–65% of the dialogue entries (V0 measures it), that is about 270–320k words, **about 30–35 hours of speech**, roughly 0.7 GB as Ogg Vorbis mono.

The engine side is smaller than §6.2 assumed: this fork already plays a message's audio in all three places (dialogue with a talking head: lip-sync; dialogue without: plain speech; outside dialogue: the float channels, per speaker, `[sound] float_speech`). V4's hook is one lookup where the audio field is empty. Generated audio is found by (msg file, msg id, hash of the text), so no `.msg` file is changed or shipped, and a mod that changes a line's text just leaves that line unvoiced.

| Task | Work | Accept | Needs |
|---|---|---|---|
| V0.1 Line reader | `tools/voice` package and `fo2hd voice-lines`: every dialogue `.msg`, `combatai.msg`, `pipboy.msg` + `holodisk.txt`; text cleanup rules (stage directions like `*sigh*`, ALL CAPS, numbers, `Vault 13`) recorded per line, not applied to the game | Line DB (SQLite, `<data_root>\voice\lines.sqlite`) with every entry | — |
| V0.2 Script scan | Decode the compiled scripts (`.int`): which msg ids each script passes to `gsay_reply`/`gsay_message` (NPC lines), `gsay_option`/`giq_option` (player lines), `float_msg` (floats), `display_msg` (message box, not spoken); ids computed at runtime (e.g. `random(100,105)`) as ranges; strings the script builds (player's name) marked dynamic | Every dialogue entry labelled or listed as unknown; counts per label | — |
| V0.3 Speakers | Script → the critters that run it (protos and map objects) → name, gender and body type from the critter art (human, super mutant, ghoul, robot, computer, child, …), talking head if any; the generic scripts (guards, townsfolk) get a speaker per critter type | Speaker per line, or "unknown" | — |
| V0.4 Report | Counts (lines, words, hours of speech per label and per town), the 50-random-line spot check page (local only: game text) | **The author checks the 50 labels** | the author |
| V1.1 Model workers | Each candidate model in its own uv venv as a worker process with one interface (text, voice, tone → wav), weights in `<data_root>\voice\models`; licenses into `docs/THIRD_PARTY.md` | Each worker speaks a test line | GPU (minutes) |
| V1.2 Bake-off | 30 representative lines (men, women, old, young, super mutants, ghouls, robots, angry, scared, drunk, long holodisk text) × 3–4 models + the fast runtime candidate; speed and VRAM measured; listening page (local only) | **The author picks the model**; ADR-0016 | the author; GPU (an hour) |
| V2.1 Voice bank | 40–80 distinct voices made with the chosen model's own means (preset speakers, voice design from a description, or seeds; never a real actor's voice), checked for distinctness (speaker-embedding distance) and stability over 20 lines each | Voice bank page | GPU (a few hours) |
| V2.2 Casting | Deterministic speaker → voice: named NPCs and party members get a voice of their own, generic critters share pools by type, gender and age; DSP presets for super mutants, ghouls, robots and computers | **Casting sheet approved** | the author |
| V2.3 Tone + names | Each NPC line's tone from its dialogue context (the line before, the option that leads to it) by an LLM, a small fixed tag set mapped to the model's controls; pronunciation lexicon for Fallout names (Hakunin, Sulik, Vault-Tec, Arroyo, Brahmin, …) | **30 tagged samples approved** | GPU or API (hours) |
| V3.1 Batch | Resumable batch like the art batch (job DB, cache by hash of text, voice, tone, model), same PC sharing: half the GPU while the author is at the PC, full speed while it is locked, paused while the game runs | Whole batch done | GPU (overnight, see estimate) |
| V3.2 QC + level | Speech recognition round trip (the text read back from the audio; word errors over a threshold regenerate with a new seed, up to 3 times), duration and silence checks; loudness matched to the game's own speech; Ogg into `<data_root>\voice\` | QC failures < 1%, listed | GPU |
| V4.1 Playback | The lookup in `_scr_get_msg_str_speech` (audio field empty, `[hd_voice] enabled=1`) through the fork's three existing paths; volume follows the speech slider; Ctrl+key toggle and overlay line | Replies, floats and taunts voiced in a test run | Locked PC |
| V4.2 Holodisks | Voice per Pip-Boy page in `pipboy.cc`, stops on page change or close | Holodisk test | Locked PC |
| V4.3 Lip-sync (optional) | Talking heads move their lips on generated lines: `.lip` data from the model's phoneme timing | A head speaks a generated line | Locked PC |
| V4.4 Dynamic lines | Lines the scripts build at runtime: a small fast TTS sidecar with a disk cache, or left silent (the author's choice, after V0 counts them) | — | — |
| V4.5 Tests + report | Scripted play through Arroyo and Klamath conversations (as the UI walk's dialogue runs): every NPC line voiced, one voice per NPC, no overlap or cut-off; state-diff (game state identical with voice on and off); listening page | **The author reviews** | Locked PC (about an hour) |

**Candidates (web research 2026-11-03; versions, speeds and licenses re-checked in V1.1 before anything is downloaded):**
- **Bake-off:**
  - **VoxCPM2** (2B, Apache): voice design from a text description, style instructions.
  - **Qwen3-TTS 1.7B** (Apache): VoiceDesign, natural-language instructions, about 1.8× real time on an RTX 4060 under Windows.
  - **IndexTTS-2.5** (strongest emotion control: an 8-value emotion vector kept apart from the voice, plus duration control). Its own license allows sharing audio only with the license and a disclaimer shipped along.
  - **Chatterbox Turbo / Multilingual V3** (MIT, 2–4 GB). Every output carries a Perth watermark, and generation runs away on long text.
- **Dropped:** Kokoro-82M (no voice design, no emotion; repo frozen) and Orpheus (dormant, Linux vLLM).
- **Runtime fallback** for dynamic lines: **Kyutai Pocket TTS** (~100M parameters, about 6× real time on two CPU cores, MIT code and CC-BY weights). It can speak with the same reference clips as the baked voices, off the GPU.
- **The cast:** all voices are designed from text descriptions ("gruff old farmer, dry"), with a 10–20 s reference clip kept per voice and an angry and a scared variant. No real person's voice, no dataset speakers.
- **Speech recognition for QC:** NVIDIA Parakeet TDT 0.6B v2 (CC-BY, very fast, word timestamps that show cut-offs). Flagged lines are re-checked with Qwen3-ASR or Whisper large-v3-turbo.
- **Tone tagging LLM:** gpt-oss-20b (Apache, 12.8 GB) or Gemma 4 26B-A4B (Apache), through llama.cpp with JSON output. It runs in its own pass, since it doesn't fit next to the TTS model.
- **Audio:** pedalboard (pitch, distortion, bitcrush, filters) for the mutant, ghoul and robot presets; ffmpeg two-pass `loudnorm`; `libvorbis`. The fork plays `.ogg` speech (`game_sound.cc`).
- **Windows:** PyTorch cu128 wheels support the 5060 Ti. Linux-only parts (flash-attn, vLLM, DeepSpeed) are avoided or run under WSL2.
- **Lessons from other voice mods:** VaultVox for Fallout 2 voices dialogue, barks and holodisks with Windows SAPI at runtime. Baldur's Gate voice mods asked for a voices-off option and switched to OGG. Bethesda-game mods: check the line list before generating, and check each line for stutter. Nexus requires AI-content tagging (since 2026-07-31) and may remove AI voice work on a rights-holder complaint. Holodisks are spoken in sentence-sized pieces (long text drifts in these models).

Rough cost, until V1 fixes the model: 12–18 working sessions of code. GPU time is mostly the batch: about 30–35 h of speech, which takes roughly 15–50 GPU hours depending on the model (one third to one and a half times the speech's length on the 5060 Ti, plus about 30% retakes), so 2–6 nights. Add 2–8 h of tone tagging on the local LLM and under an hour of speech recognition. Locked-PC game time is 2–3 h (V4).

**Questions for the author at the start of V** (from planning, 2026-11-03):

1. What gets a voice: NPC replies and holodisks (the plan), and also floats (what NPCs say over their heads), combat taunts, and the "You see …" descriptions with a narrator voice? The player's own options stay silent?
2. The 13 characters the game already voices (Sulik, Myron, Lynette, Marcus, …) have unvoiced lines too. Leave those silent, or give them a synthetic voice that won't sound like the actor?
3. Tone tagging: a local LLM (free, offline, slower, the plan's default), or the Claude API (reads the context better; sends the game's dialogue text to the API; a few dollars)?
4. If a public release is still possible: only models whose license allows sharing the audio go into the bake-off (ties into the P7 release question).
5. How far to run: V0 + V1 then stop for the listening pick (recommended, since V2 depends on the model), or V0 only first?

### 6.3 Inventory track (I)

Rules that keep it pure UX: sorting and filtering are **view-only** (scripts access items by index, so the real array order never changes); every mutation goes through the existing functions (`_move_inventory`, equip/use/drop, barter math), so AP costs, weight limits, steal rolls (one roll per move) and prices stay identical.

| # | Task | Exit |
|---|---|---|
| I0 | **UX spec + mockups** for all six screens (inventory, loot, barter, steal, party gear, use-on); a game-state dump tool (inventory, AP, caps, XP, gvars) for the state-diff test | the author approves the mockups |
| I1 | **UI layer:** RmlUi on D3D11 (needs P2); Fallout theme from pipeline-upscaled interface art + crisp fonts (SDF from upscaled game fonts or a look-alike font — derived data never committed); input routing; modal loop that mirrors the original's | A themed test panel over the game at 2×/3× |
| I2 | **Main inventory:** dense grid (far more items visible), type filter tabs, sort (type/weight/value/name), hover tooltips (damage, range, AP, ammo, weight, value) + compare to equipped, quantity slider, modifier-click moves, drag to equipment slots | Feature checklist; **state-diff test:** the same scripted operations through the old and new UI from the same save produce identical game state |
| I3 | **Loot, steal, party gear** (shared transfer component; "move all of type" = repeated normal moves, so steal still rolls per item) | Checklist + state-diff |
| I4 | **Barter** (offer tables, balance, CE's auto-balance kept) | Checklist + state-diff on prices and caps |

### 6.3a Release track (R)

**Decided by the author (2026-11-04, after P7's accept):** a **public community mod release**, done first; then the voice track; then Fallout 1 (P8). He granted an open run ("free to run as much as needed"). Still his: the name, the release upscaler (from the bake-off), and every outward-facing step (a public repo, pushing, uploading to Nexus). Those are prepared completely and then asked.

Ground rules: free and non-commercial (CE's Sustainable Use License); no original game file is shipped; only models with permissive licenses (CC0, CC-BY, MIT, Apache, BSD, WTFPL) make the release art; the name contains neither "Fallout" (it says "for Fallout 2") nor "HD-2D" (a Square Enix trademark). The author's own install keeps its UltraSharpV2 packs.

| Task | Work | Accept | Needs |
|---|---|---|---|
| R0 Plan + name | This table; 5–6 name candidates, each checked against Nexus, GitHub, Steam and trademarks | **The author picks the name** | the author |
| R1 Release upscaler | Permissive candidates from OpenModelDB: 4x-PBRify-UpscalerV4 (DAT2, CC0, by UltraSharpV2's author), 4x-Textures-GTAV-rgt-s-dither (CC-BY, P1.6 runner-up), 4x-PBRify-RPLKSRd-V3 (CC0), 4x-Nomos2-hq-dat2, 4x-TextureDAT2-otf (CC-BY), 4x-PBRify-UpscalerSIR-M-V2, 4x-HDCube (CC0), 4x-realesrgan-x4plus-anime-6b (BSD), 4x-Nomos2-hq-mosr. Every candidate goes through the **production** path (tile context, alpha, all maps) on the sample set plus talking heads, interface art and more critters; scenes composited per model with a flip viewer at 1:1; walk-cycle flicker; speed; UltraSharpV2 alongside as "what you play now". The top 2–3 also in the game itself (staging packs, locked PC) | **The author picks**; ADR-0015 | the author; GPU (1–2 h) |
| R2 Release batch | Whole game (map art, critters, UI, fonts) with the picked model into its own data root `<release_root>`; packs at 2×/3×/4×; BC7 round trip; capture tour on the release packs | Batch, packs and tour done | GPU (one night), locked night |
| R3 Release defaults | Everything on out of the box (today D3D11, GPU map and HD UI are opt-in in the code); first start picks the desktop resolution, borderless, and the scale that matches the installed pack tier, and never touches `f2_res.ini` or exclusive fullscreen; quality presets for weaker GPUs; VRAM budget from the adapter | Fresh install starts in HD at the desktop resolution | Locked PC |
| R4 Rename | Window title, exe, docs; README for players; NOTICE ("modified" notice); THIRD_PARTY review | No working-title strings left in shipped files | — |
| R5 Packaging | `release.ps1`: Release build (static CRT, no debug DLLs; pdb kept aside), one small engine zip + one art zip per tier; checksums; nothing original inside (every shipped file compared against the game's own); install = extract into the game folder, uninstall = delete | Zips built, content check passes | — |
| R6 Clean-install tests | Pristine Epic copy + the zips only → tour subset; WARP (software D3D11) self-test run as a vendor-neutral check; dependency check; Restoration Project on a copy (HD where art matches, original elsewhere) | Passes; issues listed | Locked PC |
| R7 Docs + media | Player README (install, settings, toggles, known issues, credits, licenses, AI disclosure), Nexus page text, changelog; side-by-side stills and a short clip (local until the author publishes) | the author reviews | the author |
| R8 Release candidate | Full tour on the release build from the clean install; the author plays the RC | **The author accepts the RC** | Locked night; the author |
| R9 Publish | Public source repo (scrubbed: no game data, no local paths), engine on GitHub Releases, art packs on Nexus; done by the author or with his OK at that moment | — | the author |

### 6.4 Proposed order

```
P0 ─► P1 ─► [go/no-go] ─► P2 ─► I0/I1 ─► P3 ─► I2–I4 ─► P4 ─► P5 ─► P6 ─► P7 ─► P8
          └─ V0–V4 slot in anywhere after P0 (no rendering dependency) ─┘
```

Rationale: P1 decides whether the HD half lives at all; I1 needs P2's GPU layer and P1's upscaling tools; the Voice track is independent and could even come first if the author wants something playable early. **The author chooses the slot for V.**

**Order as decided by the author at the Phase 1 go/no-go (2026-10-31):** Phase 1 = go. P3 comes straight after P2; the inventory start (I0/I1) moves to after P3; the Voice track comes after all rendering phases.

```
P0 ─► P1 ─► P2 ─► P3 ─► I0–I4 ─► P4 ─► P5 ─► P6 ─► P7 ─► V0–V4 ─► P8
```

**Changed by the author after the Phase 3 report (2026-10-31):** P4 comes before the inventory track. Where I0–I4 go after P4 is still open.

```
P0 ─► P1 ─► P2 ─► P3 ─► P4 ─► (I0–I4 ?) ─► P5 ─► P6 ─► P7 ─► V0–V4 ─► P8
```

**After the Phase 4 report (2026-10-31):** the author accepted P4. Next: the floor-seam fix (P5.0), then P5. At the start of P5 he chose "all of P5, then report". The inventory track's slot is still open.

**Phase 5 built (2026-11-01):** report waiting for the author (`fo2hd report5`); design in ADR-0012 (Proposed). Next per this order: P6.

**After the Phase 5 report (2026-11-01):** the author accepted P5 and started P6 with "all of P6, then report".

**Phase 6 built (2026-11-02):** report waiting for the author (`fo2hd report6`); design in ADR-0013 (Proposed). The author played about two hours of a new campaign on the P6 build first (Arroyo, Klamath, the Den). Next per this order: P7. The inventory track's slot is still open.

**After the Phase 6 report (2026-11-02):** the author accepted P6 (glow fine, color grade stays neutral, Klamath goo not checked yet). P7 is next and starts only on his go. The inventory track's slot is still open.

**Phase 7 started (2026-11-02):** split into P7a (whole-game art, map tour) and P7b (UI as an HD skin over the existing screens, ADR-0014). The art batch shares the PC with the author; game runs happen only while the PC is locked. The RmlUi inventory track stays separate and later; its slot is still open.

**Phase 7 built (2026-11-03):** report waiting for the author (`fo2hd report7`; STATUS.md has the exit check). Next per this order: V0–V4. The inventory track's slot is still open.

**After the Phase 7 report (2026-11-03):** the author asked for both fixes for the 12 failed views and the fix for the interface bar beside the dialogue and barter screens; the HD UI stays on until he has tested it; he tests P7 and accepts it soon. Meanwhile the voice track is planned (§6.2, "Voice track tasks"); its start waits for his go.

---

## 7. Risk register (extends brief §9)

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| 1 | Upscale quality on tiny dithered sprites | High / High | P1.6 bake-off per category; dithering-aware models; go/no-go |
| 2 | Animation shimmer | High / High | P1.8 metric + options (stabilize, 2× critters) |
| 3 | Double-lit / flat results from baked light | Med / High | §3.4 invariant + per-category strengths + side-by-sides |
| 4 | Mood drift | Med / High | Original-equivalent mode always one toggle away; every change reviewed side by side |
| 5 | Dirty-rect assumptions | Med / Med | Full-frame re-emission on present; replay test catches missing draws |
| 6 | Sfall/mod compatibility | Low (vanilla first) / Med | Vanilla only until P7; the community fork improves mod support for free |
| 7 | Upstream inactivity | **Retired** — community fork is active | — |
| 8 | **Upstream churn** conflicts with our hooks | High / Med | Thin hooks, short touch list (§8), monthly merges, replay tests after every merge |
| 9 | **127 independent `renderPresent()` loops** | Certain / Med | HD scene cached and redrawn on every present; no stale draw lists |
| 10 | Movie overlay tied to `SDL_Renderer` | Certain / Low | Ported in P2 |
| 11 | **VRAM on the 4 GB laptop** | High / High → on hold (laptop out of scope since 2026-10-31; budget 8 GB) | BC7, per-map streaming, 2× tier, budget test in P4 |
| 12 | **Pipeline compute time** (tens of thousands of frames × diffusion models) | High / Med | Throughput measured in P1.10; fast models for bulk, diffusion only where it pays; cache; batches run overnight |
| 13 | Python 3.14 / Blackwell wheel gaps | Med / Low | Pin versions; fallback 3.12 env via uv |
| 14 | Hidden render paths missing from the draw list (edge squares, stencil, outlines, hex cursor, text) | Med / Med | Replay test with 0-pixel tolerance |
| 15 | **TTS:** wrong line labels (voicing player options), wrong gender, mispronounced names, wrong emotion | Med / Med | Static script scan + spot checks; casting sheet; lexicon; ASR QC |
| 16 | **TTS ethics/licensing** | Low / Med | Synthetic voices only, no cast cloning; model licenses in `THIRD_PARTY.md` |
| 17 | **Inventory hidden gameplay coupling** (item order in scripts, steal rolls, AP, barter balance) | Med / High | View-only sort; existing functions for every mutation; state-diff tests |
| 18 | Epic data quirks (sfall 3.2 `ddraw.ini` migration changes settings silently) | Med / Low | Review migrated config in P0.2/P0.4 |
| 19 | Bitmap fonts don't scale | High / Med | SDF or look-alike font in I1 |
| 20 | Session continuity (work spread over many Claude sessions) | Med / Med | One-session tasks, ADRs, memory notes, scripted regression captures |
| 21 | Trademark in the project name | Low (private) | Rename before any publication |

---

## 8. Upstream files touched in Phases 1–3

Everything else lives in `src/hd/` and `tools/`.

| File | Phase | Why |
|---|---|---|
| `CMakeLists.txt` | 1, 2 | Add `src/hd/*` sources (option `FALLOUT_HD`, default ON); P2: link `d3d11`, `dxgi`, `d3dcompiler`, add Dear ImGui |
| `src/game.cc` | 1 | `hd::init()`/`hd::exit()` in `gameInitWithOptions`/`gameExit`; debug hotkeys in `gameHandleKey` |
| `src/main.cc` | 1 | Scripted capture mode entry (load map, set time, capture, exit) after init, before `mainLoop` |
| `src/tile.cc` | 1, 3 | P1: recorder calls in `tileRenderFloor`, `tileRenderRoof`, `tileRenderEdgeBlackSquares`, `tile_hires_stencil_draw`. P3: emit-only mode in `tileRefreshGame`, full-frame emission |
| `src/object.cc` | 1, 3 | P1: recorder calls in `_obj_render_object` (incl. egg branch, translucency, light), `objectDrawOutline`, `_obj_render_post_roof`. P3: emit-only mode in `_obj_render_pre_roof`/`_post_roof` |
| `src/text_object.cc` | 1, 3 | P1: record floating-text rects. P3: render floats into the overlay layer |
| `src/svga.cc` | 2, 3 | Renderer selection; delegate `createRenderer`, `destroyRenderer`, `renderPresent`, `handleWindowSizeChanged`, `directDrawSetPalette*`, `_GNW95_ShowRect`, `_GNW95_zero_vid_mem`, `renderFpsCounter`, window creation flags/GL hint to the HD backend. P3: HD map layer under the UI layer |
| `src/movie.cc` | 2 | D3D11 movie texture path (`movieRenderDirectOverlay`, `movieHandleRendererReset`) |
| `src/window_manager.cc` | 3 | ~~Map-coverage mask in the `_GNW_win_refresh` blits~~ As built: three accessors (`windowGetCount`, `windowGetWindowAt`, `windowRenderAllToBuffer`); the mask is computed in `src/hd/map_layer.cc` from the window list, and the self-test checks it against `windowRenderAllToBuffer` |
| `src/mouse.cc` | 3 | (added in P3) `mouseGetCursorImage` accessor, for the cursor's pixels in the mask |
| `src/map.cc` | 3 | ~~Mark the iso window as HD-composited~~ Not needed: untouched. Writers that bypass the draw list are caught by the per-pixel check against CE's screen |
| `src/palette.cc`, `src/color.cc` | 3 | ~~Expose fade state~~ Not needed: untouched. The GPU map is in palette indices; fades and cycling come with the palette |
| `src/text_object.cc`, `src/tile_hires_stencil.cc` | 3 | Emit-only skip after the P1 hooks |

Expected to stay untouched (verified): `dinput.cc` (mouse mapping already window-size based), `art.cc`, `light.cc`, `cycle.cc` — read through public APIs only.

Later tracks, for reference: Voice → `scripts.cc` (`_scr_get_msg_str_speech`), `pipboy.cc`, possibly `game_sound.cc`. Inventory → `inventory.cc` (entry points delegate to the new UI; a small internal header exposes the needed static logic functions), `game_dialog.cc` (barter entry).

---

## 9. What I need from the author

1. **Review and approve this plan** — in particular §3 (base fork, D3D11, 4× masters, modulate strategy, RmlUi) and the order in §6.4 (where to slot the Voice track).
2. **OK to install dev tools:** `uv` (Python env manager), `clang-format` (upstream's formatting check), optionally Ninja (faster builds) and RenderDoc (GPU debugging).
3. **Phase 0:** a ~10-minute play test (P0.4).
4. **Phase 1:** pick one upscaler per category from the contact sheets (P1.6), then the go/no-go (P1.10).
5. **Later:** a Fallout 1 copy for Phase 8.
