# {name} changelog

## {version}

New world art and a new engine: install both. (The 0.9.0-beta art packs also work with this
engine, without the floor and roof fixes below.)

- Floors and roofs: no more straight lines along some tile joins (roads in Broken Hills and
  New Reno, for example), and ribbed and corrugated metal roofs show their single tiles much
  less. Built ground (roads, pavement, bricks) is cleaner; sand and dirt keep their grain.
- The sun moves through the day: in the morning and evening shadows are longer and turn;
  at noon they fall as before. `[hd_light] sun_path=0` keeps the noon sun all day.
- Trees, cacti, bushes, rocks and barrels look a little rounder in the sun.
  `[hd_light] round_body=0` turns it off.
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
