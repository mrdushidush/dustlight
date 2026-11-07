# Nexus Mods page (draft)

Paste into the mod page's description (Nexus uses BBCode; headings become [size] tags).
Placeholders in braces are filled at release time. Fields for the upload form are at the end.

---

## {name}: HD art and new lighting for Fallout 2

**Same game, sharper.** {name} brings the whole of Fallout 2 to HD and lights it anew,
on the Fallout 2 Community Edition engine. Rules, story, saves and the original's look stay
the same. This is a beta, version {version}: see the known limits below.

**What changes**
- **HD art everywhere:** every floor, wall, piece of scenery, item, critter and animation,
  the talking heads, the interface and the fonts, at 2x, 3x or 4x the original resolution.
- **New light:** colored light from lamps, fires and screens, soft shadows, light bouncing
  off walls, darker nights, and glow on fires and monitors.
- **Compare any time:** Ctrl+F8 switches HD and original art, Ctrl+F7 new and original
  light, live in the game.

**What stays**
- The game itself: same rules, AP, prices, scripts, maps and saves (saves work as in CE).
- The original's mood: the palette, the brightness of day scenes, the pixel-exact framing.
  Art a mod changed, and a few effects, stay as they are in the original resolution.

**How the art was made (AI disclosure)**
The HD art was made from Fallout 2's own sprites in an automated pipeline, with
{model_short}. No hand-painting, no new content, no art from other games.
**Characters and creatures use no AI:** every pixel is a copy of an original one, painted
shadows included. Everything else is AI-upscaled from its original, with floors, roofs, walls
and scenery mixed back with the original pixels so less invented detail shows. An upscaler
still guesses at fine detail the original pixels don't hold, like the grain of a surface or
skin, so that detail is the model's and not the original artists'. Each HD frame records which original frame it came from; if the game's
frame is a different one (a mod changed it), the original frame is shown. The lighting is
computed by the renderer, not painted. The screenshots and videos on this page are captures
from the game, showing that art.

**AI and the code:** much of the new renderer's code was written with an AI coding assistant
(Claude) under the author's direction, and this page was drafted with it too. Decisions and
reviews of the results are the author's.

**Requirements**
- Your own Fallout 2 (GOG, Steam or Epic), Windows 10/11 64-bit, a DirectX 11 GPU with
  4 GB of video memory or more, and a screen at least 960 pixels tall for the HD art.

**Downloads:** the **engine** (small) and **one art pack** for your screen's height:
- 1080 pixels tall (1920×1080, 2560×1080): **2x** ({size_2x})
- 1440 pixels tall (2560×1440, 3440×1440): **3x** ({size_3x})
- 2160 pixels tall (4K): **4x** ({size_4x})

A pack for a taller screen than yours shows no HD art, only the original art.

**Install:** extract the engine and every part of your art pack into the Fallout 2
folder, then start `{exe}`. Full instructions and settings are in the included README.

**Mods:** script, map and text mods work as with CE (the Restoration Project too); art a
mod changes is shown in its original resolution.

**Known limits**
{known_issues}

**Problems?** Please report them on this page's Bugs tab or at {source_url}/issues, not to
the Fallout 2 CE project.

**Credits**
- **Fallout 2 Community Edition** by Alexander Batalov and contributors (Sustainable Use
  License): the engine. https://github.com/fallout2-ce/fallout2-ce , originally
  https://github.com/alexbatalov/fallout2-ce
{model_credits}

Source code: {source_url}.

Free and non-commercial. Not affiliated with Bethesda, ZeniMax or Interplay.

---

## Upload form

- Category: Visuals and Graphics (or Overhauls)
- Tags: HD, upscaled, lighting, graphics
- AI classification (Nexus's rules in force since 2026-11-03, help article 180; new uploads fill
  every field at once, and the form's own wording decides): **AI-Generated Content** ("AI produced
  the majority of the code, ... or other assets"), and **AI Media** (this page's text) if the form
  takes more than one. **Not AI Assisted for the code:** that tag needs evidence of commit history
  and of understanding the code, which moderators may ask the author to explain. The page names
  "AI texture upscaling" under AI Assisted too, so add that tag only if the form takes several and
  it is offered for the art alone.
- AI training and forks permissions: leave at "Ask Me" (the default)
- Requirements: the game; no other mods
- Permissions: free; no asset reuse in paid content; keep the credits
- Donation points: off (keeps the release non-commercial, as CE's license asks and as Kim2091's
  permission requires)
- Publish last: upload every file with the page unpublished. Nexus doesn't allow public
  placeholders.
