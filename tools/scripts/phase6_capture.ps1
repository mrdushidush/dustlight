# Stills and frame sequences for Phase 6 (PLAN.md P6.0, P6.7): one game run per scene at
# 3440x1440, scale 3, GPU map and HD art on. Injected keys walk the run through states;
# in each state the last back buffer the presenter's self-test dumps (every second) is
# kept as the still, and with -Seq a frame sequence (Ctrl+F4) is taken for a GIF.
#
#   -States 'hd', 'orig:357'          state name, then the key codes that enter it (kb.h:
#                                     355 Ctrl+F6 next light effect, 356 Ctrl+F7 new/original
#                                     light, 357 Ctrl+F8 HD/original art, 354 Ctrl+F5 post);
#                                     the first state is the start (no key)
#
#   phase6_capture.ps1                -> <data_root>\reports\phase6\img\<scene>-<state>.png
#   phase6_capture.ps1 -Seq           ... and <data_root>\reports\phase6\seq\<scene>-<state>\NNN.png
#   phase6_capture.ps1 -Scene den_fire -Extra '"[hd_post]bloom=0"' -Tag nobloom
#
# Runs windowed (3438x1440 window) so it works with a locked desktop too.

param(
    [string[]]$Scene = @(),
    [string[]]$States = @('hd', 'orig:357'),
    [switch]$Seq,
    [int]$SeqCount = 60,
    [int]$SeqStep = 2,
    [string]$SeqRect = '1119,211,1200,720',
    [int]$FirstMs = 26000,
    [string[]]$Extra = @(),
    [string]$Tag = '', # suffix for the output names (tuning runs)
    [string]$ScenesFile = (Join-Path $PSScriptRoot 'scenes_cycle.json'),
    [string]$ArtDir = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
if ($ArtDir -eq '') { $ArtDir = Join-Path $paths['data_root'] 'hdpack' }
$captureDir = Join-Path $paths['data_root'] 'captures'
$dumps = Join-Path $paths['data_root'] 'reports\present'
$imgDir = Join-Path $paths['data_root'] 'reports\phase6\img'
$seqDir = Join-Path $paths['data_root'] 'reports\phase6\seq'
New-Item -ItemType Directory -Force $dumps, $imgDir, $seqDir | Out-Null

# kb.h: KEY_CTRL_F4 = 353 starts a frame sequence.
$stepMs = if ($Seq) { 4000 + [int]($SeqCount * $SeqStep * 1000 / 60) } else { 4000 }
$names = @()
$keys = @()
$t = $FirstMs
for ($i = 0; $i -lt $States.Count; $i++) {
    $parts = $States[$i].Split(':')
    $names += $parts[0]
    foreach ($k in @($parts | Select-Object -Skip 1)) { if ($i -gt 0 -and $k -ne '') { $keys += "$($t):$k"; $t += 100 } }
    if ($Seq) { $keys += "$($t + 2500):353" }
    $t += $stepMs
}
$settleMs = $t + 20000 # slack: map loading and sequence writes delay the injected keys

function Invoke-Capture($s) {
    Get-ChildItem $dumps -Filter 'auto-*.png' -ErrorAction SilentlyContinue | Remove-Item
    Get-ChildItem $dumps -Filter 'seq*' -ErrorAction SilentlyContinue | Remove-Item
    $gameArgs = @(
        '"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', "`"[hd]hd_art_dir=$ArtDir`"",
        '"[hd_debug]present_selftest_ms=1000"', "`"[hd]capture_dir=$captureDir`"",
        "`"[hd_debug]capture_map=$($s.map)`"", "`"[hd_debug]capture_name=still6_$($s.name)`"", "`"[hd_debug]capture_settle_ms=$settleMs`"",
        '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"',
        "`"[hd_debug]inject_keys=$($keys -join ',')`""
    )
    if ($Seq) { $gameArgs += @("`"[hd_debug]frame_seq_count=$SeqCount`"", "`"[hd_debug]frame_seq_step=$SeqStep`"", "`"[hd_debug]frame_seq_rect=$SeqRect`"") }
    $gameArgs += $Extra
    foreach ($key in 'elevation', 'tile', 'dude_tile', 'time') {
        $value = $s.$key
        if ($null -ne $value) { $gameArgs += "`"[hd_debug]capture_$key=$value`"" }
    }
    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
    if (-not $proc.WaitForExit(($settleMs + 90000))) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
    $suffix = if ($Tag -ne '') { "-$Tag" } else { '' }
    $log = Join-Path $dumps "still6-$($s.name)$suffix.debug.log"
    Copy-Item (Join-Path $work 'debug.log') $log -Force
    $broken = Select-String -Path $log -Pattern 'HD: shader |GPU map layer failed|lighting failed' | Select-Object -First 3
    if ($broken) { throw "$($s.name): HD renderer didn't start: $($broken.Line -join ' | ')" }

    # Walk the log in order: each injected state key (not Ctrl+F4) moves to the next state
    # once all keys of that state are in.
    $stateKeys = @($States | ForEach-Object { @($_.Split(':') | Select-Object -Skip 1 | Where-Object { $_ -ne '' }).Count })
    $state = 0
    $pressed = 0
    $last = @{}
    $seqState = @{}
    foreach ($line in (Get-Content $log)) {
        if ($line -match "present self-test '(auto-\d+)': (PASS|FAIL)") { $last[$state] = $Matches[1] }
        if ($line -match 'HD: injecting key (\d+)' -and $Matches[1] -ne '353') {
            $pressed++
            if ($state + 1 -lt $States.Count -and $pressed -ge $stateKeys[$state + 1]) { $state++; $pressed = 0 }
        }
        if ($line -match 'HD: frame sequence (\d+): (\d+) frames') {
            $seqState[[int]$Matches[1]] = @{ State = $state }
        }
    }
    for ($i = 0; $i -lt $names.Count; $i++) {
        $name = "$($s.name)-$($names[$i])$suffix"
        if (-not $last.ContainsKey($i)) { throw "no back buffer for $name; see $log" }
        Copy-Item (Join-Path $dumps "$($last[$i]).png") (Join-Path $imgDir "$name.png") -Force
        Write-Host "    $name <- $($last[$i]).png"
    }
    foreach ($k in $seqState.Keys) {
        $name = "$($s.name)-$($names[$seqState[$k].State])$suffix"
        $dir = Join-Path $seqDir $name
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
        New-Item -ItemType Directory -Force $dir | Out-Null
        Get-ChildItem $dumps -Filter "seq$k-*.png" | ForEach-Object { Move-Item $_.FullName (Join-Path $dir ($_.Name -replace "^seq$k-", '')) }
        Move-Item (Join-Path $dumps "seq$k-times.txt") (Join-Path $dir 'times.txt')
        Write-Host "    seq $name ($((Get-ChildItem $dir -Filter '*.png').Count) frames)"
    }
    Select-String -Path $log -Pattern 'HD: (lighting|VRAM|map GPU time|post)' | ForEach-Object { Write-Host "    $($_.Line)" }
}

foreach ($s in (Get-Content -Raw $ScenesFile | ConvertFrom-Json)) {
    if ($Scene.Count -gt 0 -and $Scene -notcontains $s.name) { continue }
    Write-Step "Phase 6 capture: $($s.name) at 3440x1440, scale 3"
    Invoke-Capture $s
}
Write-Step "Stills in $imgDir$(if ($Seq) { ", sequences in $seqDir" })"
exit 0
