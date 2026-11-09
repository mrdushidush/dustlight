# {name} {version}

**{name} is a free mod for Fallout 2: the original game with HD art and new lighting.**
It runs on Fallout 2 Community Edition (CE), the open-source rebuild of the engine. The
rules, story, saves and the original's look all stay the same; it's the same game, made
sharper.

- **HD art for the whole game:** floors, walls, scenery, items, every critter and
  animation, talking heads, the interface, and the game's fonts. The art was made from the
  original sprites in an automated pipeline, with {model_short}. **Characters and creatures
  use no AI:** every pixel is a copy of an original one, painted shadows included. Everything
  else is AI-upscaled, and floors, roofs, walls and scenery are mixed back with the original
  pixels so less invented detail shows. Nothing was painted over by hand and no content was
  added, but an upscaler does guess at fine detail the original pixels don't hold, like the
  grain of a surface or skin. Each HD frame
  records which original frame it came from; if the game's frame is a different one (a mod
  changed it), the original frame is shown.
- **New lighting:** colored light from lamps, fires and monitors; soft shadows; light
  that bounces off walls and floors; darker nights; glow on fires, screens and the glowing
  goo. Press **Ctrl+F7** to compare with the original light at any time.
- **Subtle effects:** a little film grain and vignette on the map (the interface is left
  alone).
- **Toggle it all:** **Ctrl+F8** switches between HD and the original art, live.

## What you need

- **Your own copy of Fallout 2** (GOG, Steam or Epic). {name} contains no game files.
- **Windows 10 or 11, 64-bit**, and a **DirectX 11 graphics card** with at least 4 GB of
  video memory.
- The **art pack for your screen's height**. Download the engine and **one** art pack:

| Your screen's height | For example | Art pack | Download |
|---|---|---|---|
| 960 to 1439 pixels | 1920×1080, 2560×1080 | **2x** | {size_2x} |
| 1440 to 1919 pixels | 2560×1440, 3440×1440 | **3x** | {size_3x} |
| 1920 pixels or more | 3840×2160 | **4x** | {size_4x} |

A pack for a taller screen than yours shows no HD art, only the original art, so go by the
table. A smaller pack works on a taller screen, showing more of the map at a smaller size.
Screens under 960 pixels tall can't show the HD art.

The 3x and 4x packs come in parts; download all parts of your pack.

## Install

1. Extract `{name}-{version}-engine.zip` into your Fallout 2 folder (the one with
   `master.dat`), e.g. `C:\GOG Games\Fallout 2`.
2. Extract every part of your art pack into the same folder. The art goes into a new
   `hd` folder.
3. Start **`{exe}`** from that folder.

On its first start, {name} switches to borderless fullscreen at your desktop resolution,
with the scale that matches your art pack. Saves work exactly as in Fallout 2 CE: your
existing saves load, and {name} changes nothing about what is saved.

**Updating** from an earlier {name}: extract the new engine zip into the same folder and let
it replace the files there. Your art pack, settings and saves stay; a new art pack is only
needed when the changelog says so.

Already use Fallout 2 CE? {name} is CE with the HD renderer added, so it replaces
`fallout2-ce.exe` in its role; your CE settings in `fallout2.cfg` are kept, except the
screen settings on the first start.

## Settings

Everything is in `fallout2.cfg` in the game folder.

| Setting | What it does |
|---|---|
| `[screen] scale=3` | The game's zoom. Match it to your art pack (2, 3 or 4); with another scale, the original art is shown. |
| `[screen] resolution_x`, `resolution_y`, `windowed` | Window size; `windowed=2` is borderless fullscreen, `1` a window. |
| `[hd_light] quality=high` | `medium` turns off bounced light, `low` also shadows and sunlight. Try these on a slower graphics card. |
| `[hd_light] mode=original` | The original lighting on the HD art. |
| `[hd_light] sun_path=0` | The sun stays where it is at noon all day, as in 0.9.0. |
| `[hd_light] round_body=0` | Trees, cacti, bushes, rocks and barrels without their rounder look (default 0.5; up to about 1.5 for more). |
| `[hd_post] enabled=0` | No film grain or vignette. |
| `[hd] ui_hd=0` | The original interface art. |
| `[hd] ui_text_style=sharp` | HD text style: `model` (default), `sharp` or `smooth`. |
| `[hd] renderer=sdl` | Turns {name} off: plain CE. |

## Mods

Mods that change scripts, maps or text work as they do with CE. Where a mod changes the
art, its art is shown as it is, in the original resolution, next to the HD art for
everything else. The Restoration Project is supported this way.

## Uninstall

Delete `{exe}` and the `hd` folder. Delete `ce.dat` too, unless you use Fallout 2 CE.

## Known limits

{known_issues}

## Credits and licenses

- **Fallout 2 Community Edition** by Alexander Batalov and the fallout2-ce contributors,
  under the Sustainable Use License (see `hd\docs\LICENSE.md`). {name} is a modified
  version of it (see `hd\docs\NOTICE.md`).
{model_credits}
- Other components and their licenses: `hd\docs\THIRD_PARTY.md`.

{name} is free and non-commercial. It is an unofficial fan project, not affiliated with or
endorsed by Bethesda Softworks, ZeniMax Media or Interplay. Fallout is a trademark of
ZeniMax Media Inc.
