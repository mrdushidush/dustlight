#ifndef FALLOUT_HD_FIRST_RUN_H_
#define FALLOUT_HD_FIRST_RUN_H_

#include <string>
#include <vector>

#include "config.h"

namespace fallout::hd {

// [hd] hd_art_dir: the HD art folders, searched in this order (PLAN.md P8.2).
// One folder, or several separated by '|' (';' would start an INI comment):
// Fallout Et Tu runs with "hd-fo1|hd", Fallout 1's own art before Fallout
// 2's. Each pack, the fonts and each sprite's painted shadow come from the
// first folder that has them. Default (not set or empty): "hd-fo1|hd" when
// hd-fo1\pack_<N>x.json exists (the Fallout 1 art zip, P8.7), else "hd";
// trailing slashes are dropped, empty entries skipped.
std::vector<std::string> hdArtDirs(Config* config);

// First start of a release install (PLAN.md R3).
//
// With [hd] auto_screen=1 (the default) the game's screen settings are set
// once, before the window opens: borderless fullscreen at the desktop
// resolution of the primary display, and the scale of the largest installed
// HD art tier that still gives the game its 640x480 minimum (e.g. 3x on
// 2560x1440 or 3440x1440, 2x on 1920x1080, 4x on 3840x2160). Then the key is
// set to 0, so later changes to [screen] stick. Installed tiers are found by
// the marker file each art pack ships (<hd_art_dir>/pack_<N>x.json) or, for
// packs built locally, by the interface bar's art at that tier.
//
// Runs on the loaded fallout2.cfg, after CE's own f2_res.ini migration and
// before command-line overrides (so a test's "[screen]..." still wins for its
// run). Returns true if it changed `config`; the caller writes the file.
bool firstRunScreenSetup(Config* config);

// What firstRunScreenSetup decided, for the debug log once it is open
// (empty if it didn't run).
const char* firstRunReport();

// Which HD art tiers (2..4) are installed, as a bit mask (bit N = tier N).
// With several art folders, the tiers every folder that holds art has (a
// folder without any is left out); if they have none in common, any
// folder's tiers.
int installedArtTiers(Config* config);

} // namespace fallout::hd

#endif /* FALLOUT_HD_FIRST_RUN_H_ */
