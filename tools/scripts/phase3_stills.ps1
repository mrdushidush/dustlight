# Stills for the Phase 3 report (PLAN.md P3.5): the save game at 3440x1440, scale 3, with
# the GPU map, in four states: the map as it looks (identical to the original), the tinted
# debug view (every pixel the GPU map supplies tinted cyan), inventory open and the options
# menu open (tinted). The presenter's self-test dumps the back buffer every second; the
# last dump before each state change is kept.
#
#   phase3_stills.ps1                 -> <data_root>\reports\phase3\img\{map,tint,inventory,options}.png
#
# Runs windowed (3438x1440 window) so it works with a locked desktop too.

param(
    [int]$Slot = 1
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$dumps = Join-Path $paths['data_root'] 'reports\present'
$outDir = Join-Path $paths['data_root'] 'reports\phase3\img'
New-Item -ItemType Directory -Force $dumps, $outDir | Out-Null
Get-ChildItem $dumps -Filter 'auto-*.png' -ErrorAction SilentlyContinue | Remove-Item

# kb.h: Ctrl+F9=358, i=105, Esc=27, o=111.
$keys = @('10000:358', '15000:105', '19000:27', '22000:111', '26000:27') -join ','
$gameArgs = @(
    "--dev-load-game=$Slot", '"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"',
    '"[hd_debug]present_selftest_ms=1000"', "`"[hd_debug]inject_keys=$keys`"",
    '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"'
)
Write-Step 'Phase 3 stills at 3440x1440, scale 3'
$env:DEBUGACTIVE = 'log'
$proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
Start-Sleep -Seconds 40
if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
$log = Join-Path $dumps 'stills.debug.log'
Copy-Item (Join-Path $work 'debug.log') $log -Force

# Walk the log in order: each key starts a state; remember the last dump of each state.
$state = $null
$last = @{}
foreach ($line in (Get-Content $log)) {
    if ($line -match "present self-test '(auto-\d+)': PASS .*GPU map composited") {
        if ($null -eq $state) { $state = 'map' }
        $last[$state] = $Matches[1]
    }
    if ($line -match 'injecting key (\d+)') {
        $state = switch ($Matches[1]) { '358' { 'tint' } '105' { 'inventory' } '111' { 'options' } default { 'between' } }
    }
}
foreach ($name in 'map', 'tint', 'inventory', 'options') {
    if (-not $last.ContainsKey($name)) { throw "no back buffer for state '$name'; see $log" }
    Copy-Item (Join-Path $dumps "$($last[$name]).png") (Join-Path $outDir "$name.png") -Force
    Write-Host "    $name <- $($last[$name]).png"
}
Write-Step "Stills in $outDir"
exit 0
