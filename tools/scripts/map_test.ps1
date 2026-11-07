# Phase 3 acceptance: the GPU map renderer draws exactly what CE draws (PLAN.md P3.4).
#
# Runs the deployed game with [hd] renderer=d3d11 map_gpu=1 and [hd_debug] map_selftest_ms,
# so every few seconds the game refreshes the map on the CPU and the map self-test checks:
#   1x   GPU map == CE's map window buffer, 0 px
#   Nx   GPU map at 2x/3x/4x == the 1x map upscaled nearest
#   comp the composited frame at 1:1 == the SDL path's conversion of the screen
# Runs: the benchmark scenes (scripted capture mode, scenes.json) and a save-game walk
# through the screens that cover the map (inventory, character, automap, options, ...).
# In the scenes, [hd_debug] map_stress puts every translucency mode and outline type on
# objects in view, floats a text, and keeps scrolling and moving critters (-NoStress: off).
# Failure images go to <data_root>\reports\map\.
#
#   map_test.ps1                     everything
#   map_test.ps1 -Only scenes        just the benchmark scenes (or: walk)
#   map_test.ps1 -Extra '"[screen]scale=3"', ...   config overrides (window size, scale)
#   map_test.ps1 -Label 3440         results also go to <data_root>\reports\map\summary-3440.json
#
# Works with a locked desktop (no real input, no screen grabs).

param(
    [ValidateSet('all', 'scenes', 'walk')]
    [string]$Only = 'all',
    [int]$Slot = 1,
    [int]$SelfTestMs = 1500,
    [int]$SettleMs = 12000,
    [switch]$NoStress,
    [string]$ScenesFile = (Join-Path $PSScriptRoot 'scenes.json'),
    [string[]]$Scene = @(),
    [string[]]$Extra = @(),
    [string]$Label = 'default', # names <data_root>\reports\map\summary-<label>.json
    [int]$SceneSeconds = 90 # per scene; WARP ([hd_debug]d3d_warp=1, PLAN.md R6) needs more
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$outDir = Join-Path $paths['data_root'] 'reports\map'
New-Item -ItemType Directory -Force $outDir | Out-Null
Get-ChildItem $outDir -Filter '*.png' -ErrorAction SilentlyContinue | Remove-Item

$common = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', "`"[hd_debug]map_selftest_ms=$SelfTestMs`"", '"[ui]skip_opening_movies=2"') + $Extra
$runs = @()

if ($Only -in 'all', 'scenes') {
    # The game writes reports to <capture_dir>/../reports, so capture into the main folder.
    $captureDir = Join-Path $paths['data_root'] 'captures'
    foreach ($s in (Get-Content -Raw $ScenesFile | ConvertFrom-Json)) {
        if ($Scene.Count -gt 0 -and $Scene -notcontains $s.name) { continue }
        $sceneArgs = @("`"[hd]capture_dir=$captureDir`"", "`"[hd_debug]capture_map=$($s.map)`"", "`"[hd_debug]capture_name=maptest_$($s.name)`"", "`"[hd_debug]capture_settle_ms=$SettleMs`"", "`"[hd_debug]map_stress=$([int](-not $NoStress))`"")
        foreach ($key in 'elevation', 'tile', 'dude_tile', 'time') {
            $value = $s.$key
            if ($null -ne $value) { $sceneArgs += "`"[hd_debug]capture_$key=$value`"" }
        }
        $runs += @{ Name = $s.name; Seconds = $SceneSeconds; WaitForExit = $true; Args = $sceneArgs }
    }
}

if ($Only -in 'all', 'walk') {
    # kb.h key codes: i=105, Esc=27, c=99, Tab=9, o=111, s=115.
    $walk = @(
        '9000:105', '13000:27',   # inventory
        '15000:99', '19000:27',   # character screen
        '21000:9', '25000:27',    # automap
        '27000:111', '31000:27',  # options (grey map behind)
        '33000:115', '37000:27'   # skilldex
    ) -join ','
    $runs += @{ Name = 'walk'; Seconds = 44; WaitForExit = $false; Args = @("--dev-load-game=$Slot", "`"[hd_debug]inject_keys=$walk`"") }
}

$total = 0
$failed = 0
$coverage = @(0, 0, 0, 0, 0, 0, 0, 0) # 1x checks whose frame used each op
$summaryRuns = @()
foreach ($run in $runs) {
    Write-Step "GPU map test: $($run.Name)"
    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList ($common + $run.Args) -WorkingDirectory $work -PassThru
    if ($run.WaitForExit) {
        if (-not $proc.WaitForExit($run.Seconds * 1000)) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
    } else {
        Start-Sleep -Seconds $run.Seconds
        if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
    }

    $log = Join-Path $outDir "$($run.Name).debug.log"
    Copy-Item (Join-Path $work 'debug.log') $log -Force
    $lines = Select-String -Path $log -Pattern 'HD: (map|present self-test|GPU map)' | ForEach-Object { $_.Line }
    if (-not ($lines | Where-Object { $_ -match 'GPU map layer on' })) { throw "$($run.Name): the GPU map layer did not start; see $log" }
    $tests = @($lines | Where-Object { $_ -match "self-test '[^']+'( \dx| mask| hd)?: (PASS|FAIL)" })
    $fails = @($tests | Where-Object { $_ -match "self-test '[^']+'( \dx| mask| hd)?: FAIL" })
    $hdTests = @($tests | Where-Object { $_ -match "self-test '[^']+' hd: " })
    foreach ($line in $tests) {
        if ($line -match 'ops fill (\d+) lit (\d+) floor (\d+) iface (\d+) text (\d+) trans (\d+) egg (\d+) outline (\d+)') {
            for ($k = 0; $k -lt 8; $k++) { if ([int]$Matches[$k + 1] -gt 0) { $coverage[$k]++ } }
        }
    }
    # A shader that doesn't compile turns parts off quietly; that's a failure too.
    $broken = @($lines | Where-Object { $_ -match 'HD: shader |GPU map layer failed|lighting failed|post effects failed' })
    if ($broken.Count -gt 0) { $fails = @($fails) + $broken; $tests = @($tests) + $broken }
    $total += $tests.Count
    $failed += $fails.Count
    $first = $tests | Where-Object { $_ -match ' 1x: PASS' } | Select-Object -Last 1
    $stress = $lines | Where-Object { $_ -match 'map stress:' } | Select-Object -First 1
    if ($stress) { Write-Host "    $($stress -replace '^.*map stress: ', 'stress: ')" }
    Write-Host "    $($tests.Count) checks, $($fails.Count) failed"
    if ($first) { Write-Host "    $($first -replace '^.*?1x: ', '1x: ')" }
    $lastHd = $hdTests | Select-Object -Last 1
    if ($lastHd) { Write-Host "    $($hdTests.Count) hd checks; last: $($lastHd -replace '^.*? hd: ', 'hd: ')" }
    $seams = @($lines | Where-Object { $_ -match "self-test '[^']+' seams: " })
    if ($seams.Count -gt 0) { Write-Host "    $($seams[-1] -replace '^.*? seams: ', 'seams: ')" }
    $live = @($lines | ForEach-Object { if ($_ -match 'live fallback before the refresh: (-?\d+) px') { [int]$Matches[1] } })
    if ($live.Count -gt 0) { Write-Host "    live fallback before each forced refresh: max $(($live | Measure-Object -Maximum).Maximum) px, $(@($live | Where-Object { $_ -gt 0 }).Count) of $($live.Count) samples non-zero" }
    $fails | Select-Object -First 6 | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    $presenter = $lines | Where-Object { $_ -match 'presenter up: (.*)$' } | Select-Object -First 1
    $summaryRuns += [ordered]@{
        name = $run.Name
        checks = $tests.Count
        failed = $fails.Count
        live_max = if ($live.Count -gt 0) { ($live | Measure-Object -Maximum).Maximum } else { $null }
        live_nonzero = @($live | Where-Object { $_ -gt 0 }).Count
        live_samples = $live.Count
        stress = if ($stress) { $stress -replace '^.*map stress: ', '' } else { $null }
        last_1x = if ($first) { $first -replace '^.*?1x: ', '' } else { $null }
        hd_checks = $hdTests.Count
        hd = @($hdTests | ForEach-Object { $_ -replace '^.*?self-test ', '' })
        seams = @($seams | ForEach-Object { $_ -replace '^.*?self-test ', '' })
    }
    Get-ChildItem $outDir -Filter 'auto-*.png' | ForEach-Object { Rename-Item $_.FullName ("$($run.Name)-" + $_.Name) -Force }
}

$names = 'fill', 'lit', 'floor', 'interface', 'text', 'translucent', 'egg', 'outline'
Write-Step ('Exact 1x checks per draw op: ' + (($names | ForEach-Object -Begin { $k = 0 } -Process { "$_ $($coverage[$k])"; $k++ }) -join ', '))
$opCoverage = [ordered]@{}
for ($k = 0; $k -lt 8; $k++) { $opCoverage[$names[$k]] = $coverage[$k] }
$summary = [ordered]@{ label = $Label; extra = $Extra; when = (Get-Date -Format s); checks = $total; failed = $failed; coverage = $opCoverage; runs = $summaryRuns }
$summary | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 (Join-Path $outDir "summary-$Label.json")
if ($total -eq 0) { throw 'no self-tests ran' }
if ($failed -gt 0) { throw "$failed of $total checks failed; images in $outDir" }
Write-Step "GPU map test OK: $total checks, 0 px different"
exit 0
