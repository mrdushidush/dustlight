# All captures for the Phase 6 report (PLAN.md P6.7), at 3440x1440, scale 3:
#
#   cycle scenes (scenes_cycle.json)  stills + frame sequences: HD (everything on) and the
#                                     original game (Ctrl+F8)
#   benchmark scenes (scenes.json)    stills: everything on (p6), one post effect off at a
#   and slime_caves                   time (Ctrl+F5: no-bloom, no-grade, no-vignette,
#                                     no-grain), post effects off (post-off), the original
#                                     game (orig); then a run with the AgX tone map (agx)
#   fades                             arroyo_day: frame sequence of the game's fades to
#                                     white and black and back (Ctrl+F3, fade_test=1)
#
#   phase6_stills.ps1                 -> <data_root>\reports\phase6\img and seq
#   phase6_stills.ps1 -Only cycle     (or: post, agx, grade, fade)

param(
    [ValidateSet('all', 'cycle', 'post', 'agx', 'grade', 'fade')]
    [string]$Only = 'all',
    [string[]]$Extra = @()
)

. (Join-Path $PSScriptRoot 'common.ps1')

$capture = Join-Path $PSScriptRoot 'phase6_capture.ps1'
$paths = Get-HdPaths
$tmp = Join-Path $paths['data_root'] 'reports\phase6'
New-Item -ItemType Directory -Force $tmp | Out-Null

# The benchmark scenes plus the slime cave (many stacked lights, glowing goo).
# PowerShell 5.1's ConvertFrom-Json emits a JSON array as one object; ForEach-Object
# unrolls it (else the list holds one nested array and every scene name is empty).
$bench = @(Get-Content -Raw (Join-Path $PSScriptRoot 'scenes.json') | ConvertFrom-Json | ForEach-Object { $_ })
$cycle = @(Get-Content -Raw (Join-Path $PSScriptRoot 'scenes_cycle.json') | ConvertFrom-Json | ForEach-Object { $_ })
$postScenes = Join-Path $tmp 'scenes_post.json'
ConvertTo-Json -InputObject (@($bench) + @($cycle | Where-Object { $_.name -in 'slime_caves', 'vault_computers' })) | Set-Content -Encoding utf8 $postScenes

if ($Only -in 'all', 'cycle') {
    & $capture -Seq -States 'hd', 'orig:357' -Extra $Extra
}
if ($Only -in 'all', 'post') {
    # kb.h: KEY_CTRL_F5 = 354 (post effects: all on -> no bloom -> no grade -> no vignette
    # -> no grain -> all off), KEY_CTRL_F8 = 357 (original art).
    & $capture -ScenesFile $postScenes -States 'p6', 'no-bloom:354', 'no-grade:354', 'no-vignette:354', 'no-grain:354', 'post-off:354', 'orig:357' -Extra $Extra
}
if ($Only -in 'all', 'agx') {
    & $capture -ScenesFile $postScenes -States 'agx' -Extra (@('"[hd_light]tonemap_curve=agx"') + $Extra)
}
if ($Only -in 'all', 'grade') {
    # The grade is neutral by default; an example of what it can do ("warm wasteland").
    & $capture -ScenesFile $postScenes -Scene arroyo_day, temple_outdoor, den_night -States 'grade-warm' -Extra (@('"[hd_post]grade_temperature=0.35"', '"[hd_post]grade_saturation=1.12"', '"[hd_post]grade_lift=0.02,0.015,0"', '"[hd_post]grade_gamma=1.03,1.0,0.97"') + $Extra)
}
if ($Only -in 'all', 'fade') {
    $fadeScenes = Join-Path $tmp 'scenes_fade.json'
    ConvertTo-Json -InputObject @($bench | Where-Object { $_.name -eq 'arroyo_day' }) | Set-Content -Encoding utf8 $fadeScenes
    # Ctrl+F3 (352) starts a frame sequence and runs the four fades (~2.8 s).
    & $capture -ScenesFile $fadeScenes -Seq -SeqCount 100 -SeqStep 2 -States 'hd', 'fade:352' -Extra (@('"[hd_debug]fade_test=1"') + $Extra)
}
exit 0
