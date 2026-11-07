# Stills for the Phase 5 report (PLAN.md P5.10): the benchmark scenes at 3440x1440, scale 3,
# GPU map and HD art on, new light. Injected keys walk one run per scene through
#   p5          the new light, every effect on
#   no-<fx>     Ctrl+F6, one effect off at a time (normals, colors, shadows, pool, tonemap,
#               sun, ao, night, gi), then all on again
#   p4          Ctrl+F7: the original light on the HD art (the Phase 4 look)
#   orig        Ctrl+F8: the original art, the exact renderer (the original game)
# The presenter's self-test dumps the back buffer every second; the last dump in each state
# is kept.
#
#   phase5_stills.ps1                 -> <data_root>\reports\phase5\img\<scene>-<state>.png
#   phase5_stills.ps1 -Scene arroyo_day -NoEffects
#   phase5_stills.ps1 -Extra '"[hd_light]shadow_strength=0.7"' -Tag shadow07
#
# Runs windowed (3438x1440 window) so it works with a locked desktop too.

param(
    [string[]]$Scene = @(),
    [switch]$NoEffects,
    [int]$FirstMs = 28000,
    [int]$StepMs = 4000,
    [string[]]$Extra = @(),
    [string]$Tag = '', # suffix for the image names (tuning runs)
    [string]$ScenesFile = (Join-Path $PSScriptRoot 'scenes.json'),
    [string]$ArtDir = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
if ($ArtDir -eq '') { $ArtDir = Join-Path $paths['data_root'] 'hdpack' }
$captureDir = Join-Path $paths['data_root'] 'captures'
$dumps = Join-Path $paths['data_root'] 'reports\present'
$outDir = Join-Path $paths['data_root'] 'reports\phase5\img'
New-Item -ItemType Directory -Force $dumps, $outDir | Out-Null

# kb.h: KEY_CTRL_F6 = 355, KEY_CTRL_F7 = 356, KEY_CTRL_F8 = 357.
$effects = @('normals', 'colors', 'shadows', 'pool', 'tonemap', 'sun', 'ao', 'night', 'gi')
$keys = @()
$want = @('p5')
$t = $FirstMs
if (-not $NoEffects) {
    foreach ($fx in $effects) { $keys += "$($t):355"; $want += "no-$fx"; $t += $StepMs }
    $keys += "$($t):355"; $t += $StepMs # all on again
}
$keys += "$($t):356"; $want += 'p4'; $t += $StepMs
$keys += "$($t):357"; $want += 'orig'; $t += $StepMs
$settleMs = $t + 4000

function Invoke-Stills($s) {
    Get-ChildItem $dumps -Filter 'auto-*.png' -ErrorAction SilentlyContinue | Remove-Item
    $gameArgs = @(
        '"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', "`"[hd]hd_art_dir=$ArtDir`"",
        '"[hd_debug]present_selftest_ms=1000"', "`"[hd]capture_dir=$captureDir`"",
        "`"[hd_debug]capture_map=$($s.map)`"", "`"[hd_debug]capture_name=still5_$($s.name)`"", "`"[hd_debug]capture_settle_ms=$settleMs`"",
        '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"',
        '"[hd_light]mode=new"', "`"[hd_debug]inject_keys=$($keys -join ',')`""
    ) + $Extra
    foreach ($key in 'elevation', 'tile', 'dude_tile', 'time') {
        $value = $s.$key
        if ($null -ne $value) { $gameArgs += "`"[hd_debug]capture_$key=$value`"" }
    }
    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
    if (-not $proc.WaitForExit(($settleMs + 60000))) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
    $suffix = if ($Tag -ne '') { "-$Tag" } else { '' }
    $log = Join-Path $dumps "still5-$($s.name)$suffix.debug.log"
    Copy-Item (Join-Path $work 'debug.log') $log -Force

    # Walk the log in order: each key press starts a state.
    $state = 'p5'
    $last = @{}
    foreach ($line in (Get-Content $log)) {
        if ($line -match "present self-test '(auto-\d+)': (PASS|FAIL)") { $last[$state] = $Matches[1] }
        if ($line -match 'Ctrl\+F6 pressed, light: no (\w+)') { $state = "no-$($Matches[1])" }
        if ($line -match 'Ctrl\+F6 pressed, light: all effects') { $state = 'p5-again' }
        if ($line -match 'Ctrl\+F7 pressed') { $state = 'p4' }
        if ($line -match 'Ctrl\+F8 pressed') { $state = 'orig' }
    }
    foreach ($name in $want) {
        if (-not $last.ContainsKey($name)) { throw "no back buffer for $($s.name) '$name'; see $log" }
        $file = Join-Path $dumps "$($last[$name]).png"
        if (-not (Test-Path $file)) { throw "missing $file" }
        Copy-Item $file (Join-Path $outDir "$($s.name)-$name$suffix.png") -Force
        Write-Host "    $($s.name)-$name$suffix <- $($last[$name]).png"
    }
    Select-String -Path $log -Pattern 'HD: (lighting|VRAM|map GPU time)' | ForEach-Object { Write-Host "    $($_.Line)" }
}

foreach ($s in (Get-Content -Raw $ScenesFile | ConvertFrom-Json)) {
    if ($Scene.Count -gt 0 -and $Scene -notcontains $s.name) { continue }
    Write-Step "Phase 5 stills: $($s.name) at 3440x1440, scale 3"
    Invoke-Stills $s
}
Write-Step "Stills in $outDir"
exit 0
