# Stills for the Phase 4 report (PLAN.md P4.6): the benchmark scenes at 3440x1440, scale 3,
# GPU map on, HD art on; Ctrl+F8 (injected) switches to the original art halfway, so each
# scene gives one HD and one original back buffer of the same view. A second run per scene
# with [hd_debug] hd_fallback_percent=50 gives the mixed still (half the art files fall
# back to the original art). The presenter's self-test dumps the back buffer every second;
# the last dump before the switch (HD) and the last one at the end (original) are kept.
#
#   phase4_stills.ps1                 -> <data_root>\reports\phase4\img\<scene>-{hd,orig,mix}.png
#   phase4_stills.ps1 -Scene arroyo_day -NoMix
#
# Runs windowed (3438x1440 window) so it works with a locked desktop too.

param(
    [string[]]$Scene = @(),
    [switch]$NoMix,
    [int]$SwitchMs = 28000,
    [int]$SettleMs = 40000,
    [string]$ScenesFile = (Join-Path $PSScriptRoot 'scenes.json'),
    [string]$ArtDir = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
if ($ArtDir -eq '') { $ArtDir = Join-Path $paths['data_root'] 'hdpack' }
$captureDir = Join-Path $paths['data_root'] 'captures'
$dumps = Join-Path $paths['data_root'] 'reports\present'
$outDir = Join-Path $paths['data_root'] 'reports\phase4\img'
New-Item -ItemType Directory -Force $dumps, $outDir | Out-Null

function Invoke-Still($s, [string]$mode) {
    Get-ChildItem $dumps -Filter 'auto-*.png' -ErrorAction SilentlyContinue | Remove-Item
    $gameArgs = @(
        '"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', "`"[hd]hd_art_dir=$ArtDir`"",
        '"[hd_debug]present_selftest_ms=1000"', "`"[hd]capture_dir=$captureDir`"",
        "`"[hd_debug]capture_map=$($s.map)`"", "`"[hd_debug]capture_name=still_$($s.name)`"", "`"[hd_debug]capture_settle_ms=$SettleMs`"",
        '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"'
    )
    foreach ($key in 'elevation', 'tile', 'dude_tile', 'time') {
        $value = $s.$key
        if ($null -ne $value) { $gameArgs += "`"[hd_debug]capture_$key=$value`"" }
    }
    if ($mode -eq 'mix') {
        $gameArgs += '"[hd_debug]hd_fallback_percent=50"'
    } else {
        $gameArgs += "`"[hd_debug]inject_keys=$($SwitchMs):357`"" # KEY_CTRL_F8: HD art off
    }
    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
    if (-not $proc.WaitForExit(($SettleMs + 60000))) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
    $log = Join-Path $dumps "still-$($s.name)-$mode.debug.log"
    Copy-Item (Join-Path $work 'debug.log') $log -Force

    # Walk the log in order: the key press starts the 'orig' state.
    $state = if ($mode -eq 'mix') { 'mix' } else { 'hd' }
    $last = @{}
    foreach ($line in (Get-Content $log)) {
        if ($line -match "present self-test '(auto-\d+)': (PASS|FAIL)") { $last[$state] = $Matches[1] }
        if ($line -match 'Ctrl\+F8 pressed') { $state = 'orig' }
    }
    foreach ($name in $last.Keys) {
        $file = Join-Path $dumps "$($last[$name]).png"
        if (-not (Test-Path $file)) { throw "missing $file" }
        Copy-Item $file (Join-Path $outDir "$($s.name)-$name.png") -Force
        Write-Host "    $($s.name)-$name <- $($last[$name]).png"
    }
    $want = if ($mode -eq 'mix') { @('mix') } else { @('hd', 'orig') }
    foreach ($name in $want) { if (-not $last.ContainsKey($name)) { throw "no back buffer for $($s.name) '$name'; see $log" } }
    Select-String -Path $log -Pattern 'HD: (art store stats|VRAM)' | ForEach-Object { Write-Host "    $($_.Line)" }
}

foreach ($s in (Get-Content -Raw $ScenesFile | ConvertFrom-Json)) {
    if ($Scene.Count -gt 0 -and $Scene -notcontains $s.name) { continue }
    Write-Step "Phase 4 stills: $($s.name) at 3440x1440, scale 3"
    Invoke-Still $s 'switch'
    if (-not $NoMix) { Invoke-Still $s 'mix' }
}
Write-Step "Stills in $outDir"
exit 0
