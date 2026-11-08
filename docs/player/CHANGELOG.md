# {name} changelog

## {version}

An engine-only update: replace the engine files; your art pack from 0.9.0-beta stays as it is.

- The game writes `debug.log` in its folder at every start, so a bug report can include it
  without any setup. `[debug] mode=none` in `fallout2.cfg` turns it off.
- No more faint dashed lines along some floor tiles' edges (seen on the Den's roads, for
  example): the edges of HD floor tiles now keep to the colors of the original pixels around
  them. `[hd] edge_clamp=0` brings back 0.9.0's look.
- `[hd_light] field_max` set to 1 or less now limits the light where many lamps overlap, as
  intended; it used to switch the limit off.

## 0.9.0-beta

The first public version, a beta: so far it has only been tested on one PC.

- HD art for the whole of Fallout 2 at 2x, 3x or 4x: floors, walls, scenery, items, every
  critter and animation, talking heads, the interface and the fonts. Characters and creatures
  use no AI: they're the original pixels, enlarged. The rest is AI-upscaled and, for the world,
  mixed back with the original pixels.
- New lighting: colored light, soft shadows, bounced light, darker nights, and glow on
  fires, screens and the glowing goo.
- A little film grain and vignette on the map.
- Ctrl+F8 switches HD and original art, Ctrl+F7 new and original light, live in the game.
- Starts in borderless fullscreen at the desktop resolution, with the scale of the
  installed art pack.
