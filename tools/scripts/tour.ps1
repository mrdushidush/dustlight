# P7 capture tour (PLAN.md P7a.4): every map file, every elevation with floors, up to
# three views each (<data_root>\maps\tour.json from `fo2hd maps`; 652 views). One game
# run per view at 3440x1440, scale 3, GPU map and HD art on, in scripted capture mode:
#   - the map self-test (exact 1x/Nx/composite checks and the HD hole check, as in
#     map_test.ps1) every few seconds;
#   - two stills from the presenter's back-buffer dumps: everything on ("hd") and the
#     original art (Ctrl+F8, "orig").
# Results per view: <data_root>\reports\tour\<run>\<view>.json, <view>-hd.png,
# <view>-orig.png, <view>.debug.log. `fo2hd tour-report` turns them into a page and diffs
# the stills against an earlier run (regressions).
#
# Before the first view, once per run: the P7b UI walk (ADR-0014). It loads save slot 1
# (only loads it) and opens inventory, character screen, automap, options, Skilldex and
# Pip-Boy with injected keys; then, in runs of their own, a dialogue with the nearest critter
# that has one, and the world map. Each twice: with the HD UI ([hd] ui_hd=1; the UI recorder
# logs per window which share of its pixels known art explains and runs its replay and HD
# self-tests -> <run>\ui_coverage-<pass>.txt) and with the original art. Back-buffer dumps
# every 2 s -> <run>\ui-<pass>-NNN.png (passes hd, orig, hd-talk, orig-talk, hd-world,
# orig-world, and the screens with the other HD text styles: hd-smooth, hd-sharp); summary
# in ui_walk.json, logs ui_walk-<pass>.debug.log.
#
# Then five busy views with a 48 MB HD art pool and the HD UI on (evict-<view>: the P4.5
# eviction carry-over), then the tour views.
#
# the author uses this PC during the day, so views run only while the PC is locked (LogonUI
# runs); unlocking kills the game at once and the tour waits for the next lock. Finished
# views are skipped on a rerun. The first view of every elevation goes first, then the rest.
#
#   tour.ps1 -Detach               start hidden, wait for a lock, run; log in the run folder
#   tour.ps1 -Stop                 stop a detached tour
#   tour.ps1 -Run 20261102 -Max 5  a few views now, in this console (still only while locked)
#   tour.ps1 -NotLocked            don't wait for a lock (only with the author's go)
#   tour.ps1 -RetryFailed          also redo views that produced no checks or stills
#   tour.ps1 -Only a,b             redo just these views of the run (their results are replaced)
#   tour.ps1 -Extra '"[hd_debug]hd_tile_span=64"'   config overrides for every game run of
#                                  this call (walk, views, benchmarks); not with -Detach
#   tour.ps1 -WalkOnly             only the UI walk (ui_test.ps1 uses it)
#   tour.ps1 -Run x -NoWalk -Time 2300 -Only a,b   views alone, e.g. night shots for the store page
#
# The HD still must show the finished capture setup (map, elevation, time of day, camera):
# a view whose last dump before Ctrl+F8 comes from before it (a slow map load) runs again
# with Ctrl+F8 later.
#
# Five views in a row without any check stop the tour (something is broken, not a map).

param(
    [string]$Run = '',
    [string]$TourFile = '',
    [int]$Max = 0,
    [int]$Time = 1200,
    [int]$FirstMs = 15000,
    [int]$OrigMs = 5000,
    [switch]$NotLocked,
    [switch]$RetryFailed,
    [string[]]$Only = @(),
    [switch]$WalkOnly,
    [switch]$NoWalk,
    [string]$WalkPasses = '',
    [string[]]$Extra = @(),
    [switch]$Detach,
    [switch]$Stop
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$data = $paths['data_root'].TrimEnd('\')
$work = $paths['working_copy'].TrimEnd('\')
if ($TourFile -eq '') { $TourFile = Join-Path $data 'maps\tour.json' }
$tourRoot = Join-Path $data 'reports\tour'
$pidFile = Join-Path $tourRoot 'tour.pid'
New-Item -ItemType Directory -Force $tourRoot | Out-Null

function Get-RunningTour {
    if (-not (Test-Path $pidFile)) { return $null }
    return Get-Process -Id ([int](Get-Content $pidFile -Raw)) -ErrorAction SilentlyContinue
}

if ($Stop) {
    $p = Get-RunningTour
    # The tour's own game (its child) goes with it; a game the author is playing stays.
    $games = @()
    if ($p) { $games = @(Get-CimInstance Win32_Process -Filter "Name='fallout2-ce.exe'" | Where-Object { $_.ParentProcessId -eq $p.Id } | ForEach-Object { $_.ProcessId }) }
    if ($p) { Invoke-Native { taskkill.exe /T /F /PID $p.Id } | Out-Null; Write-Host "Stopped tour (pid $($p.Id))." } else { Write-Host 'No tour running.' }
    # Wait until that game is gone: a deploy right after this found the exe still locked.
    foreach ($g in $games) { Wait-Process -Id $g -Timeout 30 -ErrorAction SilentlyContinue }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    return
}

if ($Run -eq '') { $Run = Get-Date -Format 'yyyyMMdd' }
$runDir = Join-Path $tourRoot $Run
New-Item -ItemType Directory -Force $runDir | Out-Null
$log = Join-Path $runDir 'tour.log'

if ($Detach) {
    if (Get-RunningTour) { throw "A tour is already running (pid $(Get-Content $pidFile -Raw))." }
    # Quoted config items don't survive the detached command line; a detached script that
    # calls tour.ps1 in-process can pass them.
    if ($Extra.Count) { throw '-Extra is not passed to a detached tour.' }
    $argList = @('-Run', $Run, '-Max', $Max, '-Time', $Time)
    if ($NotLocked) { $argList += '-NotLocked' }
    if ($RetryFailed) { $argList += '-RetryFailed' }
    if ($WalkOnly) { $argList += '-WalkOnly' }
    if ($NoWalk) { $argList += '-NoWalk' }
    if ($WalkPasses -ne '') { $argList += @('-WalkPasses', $WalkPasses) }
    if ($Only.Count) { $argList += @('-Only', ($Only -join ',')) }
    $id = Start-Detached $PSCommandPath $argList
    Set-Content -LiteralPath $pidFile -Value $id -Encoding ascii
    Write-Host "Tour started (pid $id); log: $log"
    return
}

function Write-Log([string]$line) {
    # Retried: another process reading the log can hold it for a moment, and a failed write
    # used to end the tour silently (2026-11-02 15:43).
    $text = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $line
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -LiteralPath $log -Value $text -Encoding utf8 -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 300 }
    }
    [IO.File]::AppendAllText((Join-Path $runDir 'tour.crash.txt'), "$text (tour.log not writable)`r`n")
}

function Test-Locked { return $NotLocked -or [bool](Get-Process LogonUI -ErrorAction SilentlyContinue) }

# The art batch's mode file (tools/pipeline/fo2hd/governor.py): `pause` holds the batch.
$batchModeFile = Join-Path $data 'batch_mode.txt'
function Set-BatchMode([string]$mode) {
    $old = if (Test-Path $batchModeFile) { (Get-Content -Raw $batchModeFile).Trim() } else { $null }
    Set-Content -LiteralPath $batchModeFile -Value $mode -Encoding ascii
    Write-Log "batch mode: $mode (was $(if ($old) { $old } else { 'auto' }))"
    return $old
}
function Restore-BatchMode($old) {
    if ($old) { Set-Content -LiteralPath $batchModeFile -Value $old -Encoding ascii } else { Remove-Item -LiteralPath $batchModeFile -ErrorAction SilentlyContinue }
    Write-Log "batch mode: $(if ($old) { $old } else { 'auto' }) again"
}

# Scratch capture dir: the game writes its capture there and its reports next to it
# (<capture_dir>\..\reports\present for back-buffer dumps, ...\map for self-test failures).
$tmp = Join-Path $data 'tourtmp'
$captureDir = Join-Path $tmp 'captures'
$dumps = Join-Path $tmp 'reports\present'
$failDir = Join-Path $tmp 'reports\map'
New-Item -ItemType Directory -Force $captureDir, $dumps, $failDir | Out-Null

# PowerShell 5.1 emits a JSON array as one object; ForEach-Object unrolls it.
$views = @(Get-Content -Raw $TourFile | ConvertFrom-Json | ForEach-Object { $_ })
$views = @($views | Sort-Object @{ Expression = { [int]($_.name -replace '^.*_', '') } }, @{ Expression = { $_.name } })
# P7a.6, the P4.5 carry-over: busy views with a 48 MB HD art pool (12 pages), so pages are
# evicted and packs reloaded all the time, and the HD UI on, so map and UI share the pool
# (hd_art.h HdArtClient). They go first, as views named evict-<view>.
$evictNames = @('newr1_e0_1', 'denbus1_e0_1', 'sfchina_e0_1', 'ncr1_e0_1', 'vctycocl_e0_1')
$evictViews = @($views | Where-Object { $evictNames -contains $_.name } | ForEach-Object {
        [pscustomobject]@{ name = "evict-$($_.name)"; map = $_.map; elevation = $_.elevation; tile = $_.tile
            extra = @('"[hd]vram_budget_mb=48"', '"[hd]ui_hd=1"') } })
# P7a.6: the P6 cycle views that showed no fire barrel (den_fire) and no water (sf_docks): the
# camera was clamped away from their tiles at the map edge. [hd_debug] capture_cycle centers
# on the reachable view with the most of that palette-cycle range instead (cycle_scan.h).
$cycleViews = @(
    [pscustomobject]@{ name = 'cycle-den_fire'; map = 'denbus1.map'; elevation = 0; tile = -1
        extra = @('"[hd_debug]capture_cycle=fast_fire"', '"[hd_debug]capture_time=2300"') },
    [pscustomobject]@{ name = 'cycle-sf_docks'; map = 'sfdock.map'; elevation = 0; tile = -1
        extra = @('"[hd_debug]capture_cycle=shoreline"', '"[hd_debug]capture_time=1200"') }
)
# P7a.6: the combat benchmark (the author's Arroyo-cave fights dipped to 48-49 fps). Scripted play
# ([hd_debug] capture_play=1): the real game loop on arcaves.map, fps every 120 frames, a clean
# quit 65 s after game start (the map is up after ~5 s; the keys count from game start too).
# "combat" enters combat (a = 97) and ends turns (space = 32) over and over; "idle" stands
# still on the same map for comparison.
$combatKeys = (@(12000, 28000, 44000) | ForEach-Object { "$($_):97" }) + (@(16, 20, 24, 32, 36, 40, 48, 52, 56) | ForEach-Object { "$($_ * 1000):32" })
# No back-buffer dumps or map self-tests here: each stalls the loop and would spoil the fps.
$playArgs = @('"[hd_debug]capture_play=1"', '"[hd_debug]quit_after_ms=65000"', '"[hd_debug]present_report_frames=120"', '"[hd]ui_hd=1"',
    '"[hd_debug]present_selftest_ms=0"', '"[hd_debug]map_selftest_ms=0"')
$benchViews = @(
    [pscustomobject]@{ name = 'bench-idle-arcaves'; map = 'arcaves.map'; elevation = 0; tile = -1
        extra = $playArgs + @('"[hd_debug]inject_keys="') },
    [pscustomobject]@{ name = 'bench-combat-arcaves'; map = 'arcaves.map'; elevation = 0; tile = -1
        extra = $playArgs + @("`"[hd_debug]inject_keys=$($combatKeys -join ',')`"") },
    # The same with CE's own renderer (no GPU map, no HD UI) through the same presenter: a dip
    # here too is CE's combat start, not the HD layers.
    [pscustomobject]@{ name = 'bench-combat-arcaves-orig'; map = 'arcaves.map'; elevation = 0; tile = -1
        extra = $playArgs + @("`"[hd_debug]inject_keys=$($combatKeys -join ',')`"", '"[hd]map_gpu=0"', '"[hd]ui_hd=0"') }
)
$views = @($evictViews) + @($cycleViews) + @($benchViews) + @($views)
$exe = Join-Path $work 'fallout2-ce.exe'

# $f8Ms: when Ctrl+F8 (kb.h KEY_CTRL_F8 = 357: original art) comes, counted from game start
# like every injected key; the capture settles until $f8Ms + $OrigMs after its setup.
# (PowerShell names ignore case: never call it $firstMs, that IS the $FirstMs parameter.)
function Invoke-View($v, [int]$f8Ms) {
    $settleMs = $f8Ms + $OrigMs
    $keys = "$($f8Ms):357"
    Get-ChildItem $dumps, $failDir, $captureDir -File -ErrorAction SilentlyContinue | Remove-Item
    # The presenter self-test every 2 s, not 1: with the HD UI on (the author's config since
    # 2026-11-03) one takes 0.9-1.2 s at 3440x1440, and its interval counts from its start,
    # so at 1 s it ran on every frame of the fades after a map load (70 frames ~20 s instead
    # of ~1 s: tour 20261103b's random-encounter maps missed the 15 s Ctrl+F8 and ran twice).
    $gameArgs = @(
        '"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"',
        '"[hd_debug]present_selftest_ms=2000"', '"[hd_debug]map_selftest_ms=3000"', "`"[hd]capture_dir=$captureDir`"",
        "`"[hd_debug]capture_map=$($v.map)`"", "`"[hd_debug]capture_name=tour`"", "`"[hd_debug]capture_settle_ms=$settleMs`"",
        "`"[hd_debug]capture_elevation=$($v.elevation)`"", "`"[hd_debug]capture_tile=$($v.tile)`"", "`"[hd_debug]capture_time=$Time`"",
        '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"',
        "`"[hd_debug]inject_keys=$keys`""
    )
    if ($v.PSObject.Properties['extra']) { $gameArgs += $v.extra }
    $gameArgs += $Extra
    # Benchmarks: the art batch runs at full speed while the PC is locked and would take the
    # GPU from the game; the governor's mode file pauses it (read every 5 s, after the current
    # unit of work), and the old mode comes back afterwards.
    $bench = $v.name -like 'bench-*'
    if ($bench) { $oldMode = Set-BatchMode 'pause'; Start-Sleep -Seconds 20 }
    try {
        $env:DEBUGACTIVE = 'log'
        $started = Get-Date
        $proc = Start-Process -FilePath $exe -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
        $deadline = $started.AddMilliseconds($settleMs + 90000)
        while (-not $proc.HasExited) {
            if (-not (Test-Locked)) {
                $proc.Kill(); $proc.WaitForExit(5000) | Out-Null
                return 'unlocked'
            }
            if ((Get-Date) -gt $deadline) {
                $proc.Kill(); $proc.WaitForExit(5000) | Out-Null
                break
            }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        if ($bench) { Restore-BatchMode $oldMode }
    }
    $seconds = ((Get-Date) - $started).TotalSeconds

    $viewLog = Join-Path $runDir "$($v.name).debug.log"
    Copy-Item (Join-Path $work 'debug.log') $viewLog -Force
    $lines = @(Get-Content $viewLog | Where-Object { $_ -match 'HD: ' })
    $tests = @($lines | Where-Object { $_ -match "self-test '[^']+'( \dx| mask| hd)?: (PASS|FAIL)" -and $_ -notmatch 'present self-test' })
    $fails = @($tests | Where-Object { $_ -match ': FAIL' })
    $broken = @($lines | Where-Object { $_ -match 'HD: shader |GPU map layer failed|lighting failed|post effects failed|can.t load' })
    $hd = @($tests | Where-Object { $_ -match "self-test '[^']+' hd: " })

    # Stills: the last back-buffer dump before the Ctrl+F8 key and the last one after it. The
    # map self-test logs its 1:1 composites under the same 'auto-NNN' labels from its own,
    # slower counter (its files go elsewhere), so the newest dump is the highest number, not
    # the last line: the first tour run took a black startup frame as the HD still.
    # The map self-test runs only once the capture is set up (map loaded, elevation, time of
    # day, camera: hd.cc runScriptedCapture's settle loop), so the HD still must be a dump
    # numbered above every dump before the first map self-test. A slow load (the first tour
    # shared the GPU with the art batch; New Reno's big maps) put Ctrl+F8 before that: the HD
    # still showed the map at its entrance or before the time of day was set.
    $state = 0
    $last = @{}
    $beforeSetup = -1
    $setupSeen = $false
    foreach ($line in $lines) {
        if ($state -eq 0 -and -not $setupSeen -and $line -match "map self-test '[^']+' 1x: ") {
            $setupSeen = $true
            if ($last.ContainsKey(0)) { $beforeSetup = $last[0] }
        }
        # Only the presenter's own dumps: the map self-test's present check logs the map's
        # label and writes no dump (a run with fewer presenter dumps picked auto-012 from it).
        if ($line -match "present self-test 'auto-(\d+)': (PASS|FAIL)" -and (Test-Path (Join-Path $dumps ('auto-{0:D3}.png' -f [int]$Matches[1])))) {
            $n = [int]$Matches[1]
            if (-not $last.ContainsKey($state) -or $n -gt $last[$state]) { $last[$state] = $n }
        }
        if ($line -match 'HD: injecting key 357') { $state = 1 }
    }
    $hdAfterSetup = $setupSeen -and $last.ContainsKey(0) -and $last[0] -gt $beforeSetup
    foreach ($k in @($last.Keys)) { $last[$k] = 'auto-{0:D3}' -f $last[$k] }
    $stills = [ordered]@{}
    foreach ($s in @(@(0, 'hd'), @(1, 'orig'))) {
        if ($last.ContainsKey($s[0])) {
            Copy-Item (Join-Path $dumps "$($last[$s[0]]).png") (Join-Path $runDir "$($v.name)-$($s[1]).png") -Force
            $stills[$s[1]] = "$($v.name)-$($s[1]).png"
        }
    }
    $failImages = @(Get-ChildItem $failDir -Filter '*.png' -ErrorAction SilentlyContinue | Select-Object -First 4 | ForEach-Object {
        $dst = "$($v.name)-fail-$($_.Name)"; Copy-Item $_.FullName (Join-Path $runDir $dst) -Force; $dst })
    $missing = $lines | Where-Object { $_ -match 'art without packs: (.*)$' } | Select-Object -Last 1
    # P7a.3: the names behind that count (the game writes them at exit; an older file is stale).
    $missingFile = Join-Path $work 'hd_art_missing.txt'
    if ($missing -and (Test-Path $missingFile) -and (Get-Item $missingFile).LastWriteTime -ge $started) {
        Copy-Item $missingFile (Join-Path $runDir "$($v.name).art_missing.txt") -Force
    }
    $result = [ordered]@{
        name = $v.name; map = $v.map; elevation = $v.elevation; tile = $v.tile; time = $Time
        seconds = [math]::Round($seconds, 1)
        exit_code = if ($proc.HasExited) { $proc.ExitCode } else { $null }
        checks = $tests.Count + $broken.Count
        failed = $fails.Count + $broken.Count
        fails = @(@($fails) + @($broken) | Select-Object -First 6 | ForEach-Object { $_ -replace '^.*?HD: ', '' })
        hd_last = if ($hd.Count -gt 0) { $hd[-1] -replace '^.*? hd: ', '' } else { $null }
        stills = $stills
        hd_still_after_setup = $hdAfterSetup
        first_ms = $f8Ms
        fail_images = $failImages
        art_missing = if ($missing) { $missing -replace '^.*art without packs: ', '' } else { $null }
        # fps per "present cost" window (every 600 frames; bench views: every 120)
        fps = @($lines | ForEach-Object { if ($_ -match 'HD: present cost .*\(([\d.]+) fps\)') { [double]$Matches[1] } })
        # ... and each window's longest frame (ms), to tell one hitch from many slow frames
        longest_ms = @($lines | ForEach-Object { if ($_ -match 'HD: present cost .*longest frame ([\d.]+) ms') { [double]$Matches[1] } })
        gpu = @($lines | Where-Object { $_ -match 'HD: map GPU time|HD: VRAM|quit_after_ms' } | ForEach-Object { $_ -replace '^.*?HD: ', '' })
    }
    $result | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 (Join-Path $runDir "$($v.name).json")
    return $result
}

# A game the tour didn't start is the author's: he may lock the PC with his game still open, and
# a second game on the same working copy would share its config, logs and saves.
function Get-OwnGames { @(Get-CimInstance Win32_Process -Filter "Name='fallout2-ce.exe'" | Where-Object { $_.ParentProcessId -eq $PID }) }
function Test-ForeignGame { [bool](Get-CimInstance Win32_Process -Filter "Name='fallout2-ce.exe'" | Where-Object { $_.ParentProcessId -ne $PID }) }

function Wait-Locked {
    if (-not (Test-Locked) -or (Test-ForeignGame)) {
        Write-Log 'waiting for the PC to be locked (and no game of the author''s open)'
        while (-not (Test-Locked) -or (Test-ForeignGame)) { Start-Sleep -Seconds 30 }
        Write-Log 'locked: running'
    }
}

function Invoke-UiWalk {
    $uiDir = Join-Path $tmp 'reports\ui'
    # kb.h: i=105 inventory, c=99 character, Tab=9 automap, o=111 options, s=115 Skilldex,
    # p=112 Pip-Boy (ui_walk lends one), Esc=27 closes each. With [hd_debug] ui_walk=1, Ctrl+F2=351 asks for a
    # dialogue with the nearest critter that has one and Ctrl+F1=350 opens the world map; the
    # game can't leave those by key, so each gets runs of its own. Then F1=315 help (any key
    # closes it), F4=318 save and F5=319 load screens (Esc cancels; never F6/F7, the quick
    # save would overwrite a slot), and options again with p=112 for the preferences.
    $screens = @('9000:105', '13000:27', '15000:99', '19000:27', '21000:9', '25000:27', '27000:111', '31000:27',
        '33000:115', '37000:27', '39000:112', '44000:27',
        '46000:315', '50000:27', '52000:318', '57000:27', '59000:319', '64000:27', '66000:111', '68000:112', '72000:27', '74000:27') -join ','
    # Each walk twice with the same keys: the HD UI (P7b.2) with the recorder's coverage and
    # replay self-test and the HD UI self-test, then the original art; back-buffer dumps every
    # 2 s pair up by number.
    $hdArgs = @('"[hd]ui_hd=1"', '"[hd_debug]ui_coverage_ms=1500"')
    # The original: CE's art and CE's text (HD text doesn't follow hd_force_original).
    $origArgs = @('"[hd]ui_hd=1"', '"[hd_debug]hd_force_original=1"', '"[hd]ui_text=0"')
    # Ctrl+F1 and Ctrl+F2 are developer keys: a clean install ignores them, and the pass would
    # still report 0 failed without ever showing its screen.
    $walkKeys = @('"[hd]enabled=1"')
    $passes = @(
        @{ name = 'hd'; keys = $screens; seconds = 80; args = $hdArgs },
        @{ name = 'orig'; keys = $screens; seconds = 80; args = $origArgs },
        # Slot 1 stands in the Temple with nobody to talk to: the dialogue runs play Arroyo village.
        # The village plays vsuit.mve on entry (11-22 s); injected keys wait it out, so they come
        # that much later. b=98 in a dialogue asks to barter (or gets the critter's refusal).
        @{ name = 'hd-talk'; keys = '12000:351,22000:98'; seconds = 64; args = $hdArgs + $walkKeys; play = 'arvillag.map'; reach = 'HD: ui walk: dialogue with' },
        @{ name = 'orig-talk'; keys = '12000:351,22000:98'; seconds = 64; args = $origArgs + $walkKeys; play = 'arvillag.map'; reach = 'HD: ui walk: dialogue with' },
        @{ name = 'hd-world'; keys = '9000:350'; seconds = 26; args = $hdArgs + $walkKeys; reach = 'HD: ui walk: world map' },
        @{ name = 'orig-world'; keys = '9000:350'; seconds = 26; args = $origArgs + $walkKeys; reach = 'HD: ui walk: world map' },
        # HD text (P7b.5): the screens again with the other two glyph styles the author liked.
        @{ name = 'hd-smooth'; keys = $screens; seconds = 80; args = $hdArgs + @('"[hd]ui_text_style=smooth"') },
        @{ name = 'hd-sharp'; keys = $screens; seconds = 80; args = $hdArgs + @('"[hd]ui_text_style=sharp"') }
    )
    # -WalkPasses hd-talk,orig-talk: only those (a quick check of one screen).
    if ($WalkPasses -ne '') { $passes = @($passes | Where-Object { ($WalkPasses -split '[, ]+') -contains $_.name }) }
    $summary = [ordered]@{}
    foreach ($pass in $passes) {
        Remove-Item (Join-Path $uiDir 'ui_coverage.txt') -ErrorAction SilentlyContinue
        Get-ChildItem $dumps -File -ErrorAction SilentlyContinue | Remove-Item
        # Slot 1, or scripted play on a map ([hd_debug] capture_play, the real game loop).
        $start = if ($pass.play) { @("`"[hd_debug]capture_map=$($pass.play)`"", '"[hd_debug]capture_play=1"', '"[hd_debug]capture_time=1200"') } else { @('--dev-load-game=1') }
        $gameArgs = @($start) + @('"[ui]skip_opening_movies=2"', '"[hd]renderer=d3d11"', '"[hd]map_gpu=1"',
            "`"[hd]capture_dir=$captureDir`"", '"[hd_debug]present_selftest_ms=2000"', '"[hd_debug]ui_walk=1"', "`"[hd_debug]inject_keys=$($pass.keys)`"",
            # a clean exit before the deadline, so the exit lines (HD text counts, ui layer stats) reach the log
            "`"[hd_debug]quit_after_ms=$(($pass.seconds - 4) * 1000)`"",
            '"[screen]resolution_x=3440"', '"[screen]resolution_y=1440"', '"[screen]scale=3"', '"[screen]windowed=1"') + $pass.args + $Extra
        $env:DEBUGACTIVE = 'log'
        $proc = Start-Process -FilePath $exe -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
        $deadline = (Get-Date).AddSeconds($pass.seconds)
        while (-not $proc.HasExited -and (Get-Date) -lt $deadline) {
            if (-not (Test-Locked)) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null; return $false }
            Start-Sleep -Milliseconds 500
        }
        if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
        $walkLog = Join-Path $runDir "ui_walk-$($pass.name).debug.log"
        Copy-Item (Join-Path $work 'debug.log') $walkLog -Force
        $coverage = Join-Path $uiDir 'ui_coverage.txt'
        if (Test-Path $coverage) { Copy-Item $coverage (Join-Path $runDir "ui_coverage-$($pass.name).txt") -Force }
        $stills = @(Get-ChildItem $dumps -Filter 'auto-*.png' -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object {
            $dst = "ui-$($pass.name)-$($_.BaseName -replace '^auto-', '').png"; Copy-Item $_.FullName (Join-Path $runDir $dst) -Force; $dst })
        $lines = @(Get-Content $walkLog | Where-Object { $_ -match 'HD: ' })
        $ticks = @($lines | Where-Object { $_ -match 'HD: ui coverage' })
        $replay = @($lines | Where-Object { $_ -match 'HD: ui replay self-test' })
        $hdTests = @($lines | Where-Object { $_ -match 'HD: ui hd self-test \d+: (PASS|FAIL)' })
        $broken = @($lines | Where-Object { $_ -match 'HD: ui layer|HD: shader |failed to start|can.t load' })
        $text = @($lines | Where-Object { $_ -match 'HD: ui text|HD: ui composite|HD: ui reach|HD: ui layer: (can|CE)' } | Select-Object -Last 8)
        # $null: this pass has no screen of its own to reach.
        $reached = if ($pass.reach) { [bool]($lines | Where-Object { $_.Contains($pass.reach) }) } else { $null }
        $summary[$pass.name] = [ordered]@{
            coverage_ticks = $ticks.Count
            coverage_last = if ($ticks.Count -gt 0) { $ticks[-1] -replace '^.*HD: ', '' } else { $null }
            replay_runs = $replay.Count
            replay_failed = @($replay | Where-Object { $_ -match ': FAIL' }).Count
            replay_first_fail = @($replay | Where-Object { $_ -match ': FAIL' } | Select-Object -First 1 | ForEach-Object { $_ -replace '^.*HD: ', '' })
            hd_runs = $hdTests.Count
            hd_failed = @($hdTests | Where-Object { $_ -match ': FAIL' }).Count
            hd_lines = @($hdTests | Select-Object -Last 3 | ForEach-Object { $_ -replace '^.*HD: ', '' })
            ui_layer = @($broken | ForEach-Object { $_ -replace '^.*HD: ', '' })
            ui_text = @($text | ForEach-Object { $_ -replace '^.*HD: ', '' })
            stills = $stills
            reached = $reached
        }
        Write-Log ("ui walk {0}: {1} coverage samples, {2} replay self-tests ({3} failed), {4} HD self-tests ({5} failed), {6} stills{7}{8}" -f $pass.name, $ticks.Count,
            $replay.Count, $summary[$pass.name].replay_failed, $hdTests.Count, $summary[$pass.name].hd_failed, $stills.Count,
            $(if ($reached -eq $false) { ", DID NOT REACH ITS SCREEN" }),
            $(if ($ticks.Count -gt 0) { '; ' + ($ticks[-1] -replace '^.*HD: ', '') }))
    }
    $summary | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 (Join-Path $runDir 'ui_walk.json')
    return $true
}

Write-Log "tour start (pid $PID): $($views.Count) views, run $Run$(if ($NotLocked) { ', not waiting for a lock' })"
$done = 0
$empty = 0
try {
    while (-not $NoWalk -and -not (Test-Path (Join-Path $runDir 'ui_walk.json'))) {
        Wait-Locked
        if (-not (Invoke-UiWalk)) { Write-Log 'ui walk: stopped, PC unlocked' }
    }
    if ($WalkOnly) { $views = @() }
    # -Only 'a,b' arrives as one string from a detached start: split it here.
    $onlyNames = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($onlyNames.Count) {
        $unknown = @($onlyNames | Where-Object { @($views.name) -notcontains $_ })
        if ($unknown.Count) { throw "-Only: no such views: $($unknown -join ', ')" }
        $views = @($views | Where-Object { $onlyNames -contains $_.name })
        Write-Log "only these $($views.Count) views (results replaced): $($onlyNames -join ', ')"
    }
    foreach ($v in $views) {
        $resultFile = Join-Path $runDir "$($v.name).json"
        if ((Test-Path $resultFile) -and -not $onlyNames.Count) {
            if (-not $RetryFailed) { continue }
            $old = Get-Content -Raw $resultFile | ConvertFrom-Json
            if ($old.checks -gt 0 -and $old.stills.hd) { continue }
        }
        if ($Max -gt 0 -and $done -ge $Max) { break }
        $f8Ms = $FirstMs
        while ($true) {
            Wait-Locked
            $r = Invoke-View $v $f8Ms
            if ($r -eq 'unlocked') { Write-Log "$($v.name): stopped, PC unlocked"; continue }
            # An HD still from before the capture setup: again, Ctrl+F8 15 s later (twice at most).
            if ($v.name -notlike 'bench-*' -and -not $r.hd_still_after_setup -and $f8Ms -lt $FirstMs + 30000) {
                $f8Ms += 15000
                # Drop this attempt's result: a tour restarted before the retry finishes would
                # otherwise take it as done (mbase12_e0_0 in 20261103b).
                Remove-Item -LiteralPath $resultFile -ErrorAction SilentlyContinue
                Write-Log "$($v.name): HD still from before the capture setup; again with Ctrl+F8 at $($f8Ms / 1000) s"
                continue
            }
            break
        }
        $done++
        $empty = if ($r.checks -eq 0) { $empty + 1 } else { 0 }
        if ($empty -ge 5) { throw 'five views in a row without a single check; see the last .debug.log' }
        Write-Log ('{0}: {1} checks, {2} failed, stills {3}, {4:N0} s{5}' -f $r.name, $r.checks, $r.failed, (@($r.stills.Keys) -join '+'), $r.seconds,
            $(if ($r.failed -gt 0) { ' | ' + ($r.fails -join ' | ') } else { '' }))
    }
    Write-Log "tour finished: $done views this time"
} catch {
    [IO.File]::AppendAllText((Join-Path $runDir 'tour.crash.txt'), ('{0:yyyy-MM-dd HH:mm:ss} {1}{2}{3}{2}' -f (Get-Date), $_, "`r`n", $_.ScriptStackTrace))
    Write-Log "tour failed: $_"
    throw
} finally {
    # Only the tour's own game, never the author's.
    Get-OwnGames | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -eq $PID) { Remove-Item $pidFile }
}
