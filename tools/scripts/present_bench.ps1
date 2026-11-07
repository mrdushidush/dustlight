# Presentation cost of the SDL path, the D3D11 path, and the D3D11 path with the GPU map
# (PLAN.md Phase 2 "frame time <= baseline", Phase 3 P3.4).
#
# Runs the same scenario per variant and reads the "present cost" lines
# (src/hd/present_stats.cc) from debug.log. The cost is the CPU time spent getting
# frames to the screen: SDL's 8-bit -> RGB blits and present; D3D11's upload, draw and
# present, plus (GPU map) the draw-list emission, mask and map draw.
#
#   present_bench.ps1                         save walk: load a save, stay on the map, walk the screens
#   present_bench.ps1 -Scenario stress        newreno_night with [hd_debug] map_stress (constant redraws)
#   present_bench.ps1 -Variants d3d11,map,hd  only some variants (sdl, d3d11, map = exact GPU map, hd = HD art on,
#                                             new light and post effects; hdnopost = the same without post effects
#                                             (the Phase 5 look); hdorig = HD art with the original light, the Phase 4 look)
#   present_bench.ps1 -Extra '"[screen]resolution_x=3440"', ...   extra config overrides
#   present_bench.ps1 -Exe fallout2-ce-p5.exe -Label p5build   another build (an exe in the working copy);
#                                             results go to bench-p5build-<scenario>* instead of bench-<scenario>*

param(
    [ValidateSet('walk', 'stress')]
    [string]$Scenario = 'walk',
    [string[]]$Variants = @('sdl', 'd3d11', 'map'),
    [int]$Slot = 1,
    [int]$Seconds = 45,
    [int]$Repeat = 2,
    [string[]]$Extra = @(),
    [string]$Exe = 'fallout2-ce.exe',
    [string]$Label = ''
)
$name = if ($Label) { "$Label-$Scenario" } else { $Scenario }

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$outDir = Join-Path $paths['data_root'] 'reports\present'
New-Item -ItemType Directory -Force $outDir | Out-Null

$variantArgs = @{
    sdl = @('"[hd]renderer=sdl"')
    d3d11 = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=0"')
    map = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', '"[hd]hd_art=0"')
    hd = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', '"[hd]hd_art=1"', "`"[hd]hd_art_dir=$(Join-Path $paths['data_root'] 'hdpack')`"", '"[hd_light]mode=new"')
    hdorig = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', '"[hd]hd_art=1"', "`"[hd]hd_art_dir=$(Join-Path $paths['data_root'] 'hdpack')`"", '"[hd_light]mode=original"')
    hdnopost = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=1"', '"[hd]hd_art=1"', "`"[hd]hd_art_dir=$(Join-Path $paths['data_root'] 'hdpack')`"", '"[hd_light]mode=new"', '"[hd_post]enabled=0"')
}

if ($Scenario -eq 'walk') {
    $walk = @('20000:105', '23000:27', '25000:99', '28000:27', '30000:9', '33000:27', '35000:115', '38000:27') -join ','
    $scenarioArgs = @("--dev-load-game=$Slot", "`"[hd_debug]inject_keys=$walk`"")
    $waitForExit = $false
} else {
    $captureDir = Join-Path $paths['data_root'] 'captures'
    $scenarioArgs = @("`"[hd]capture_dir=$captureDir`"", '"[hd_debug]capture_map=newr1.map"', '"[hd_debug]capture_time=2300"', '"[hd_debug]capture_name=bench_stress"', "`"[hd_debug]capture_settle_ms=$($Seconds * 1000)`"", '"[hd_debug]map_stress=1"')
    $waitForExit = $true
}

$results = @()
foreach ($i in 1..$Repeat) {
    foreach ($variant in $Variants) {
        Write-Step "Benchmark $name run $i, $variant ($Exe)"
        $gameArgs = @('"[ui]skip_opening_movies=2"') + $variantArgs[$variant] + $scenarioArgs + $Extra
        $env:DEBUGACTIVE = 'log'
        $proc = Start-Process -FilePath (Join-Path $work $Exe) -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
        if ($waitForExit) {
            if (-not $proc.WaitForExit(($Seconds + 60) * 1000)) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
        } else {
            Start-Sleep -Seconds $Seconds
            if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }
        }
        $log = Join-Path $work 'debug.log'
        Copy-Item $log (Join-Path $outDir "bench-$name-$variant-$i.debug.log") -Force
        $lines = @(Select-String -Path $log -Pattern 'HD: present cost' | ForEach-Object { $_.Line })
        # One run without numbers (a slow load, a hung movie) shouldn't throw away the others.
        if ($lines.Count -eq 0) { Write-Warning "no present-cost lines for $variant, run $i (skipped)"; continue }
        $last = $lines[-1]
        if ($last -notmatch '\(([a-z0-9]+)\): ([\d.]+) ms/frame over the last \d+ frames \(([\d.]+) fps\), ([\d.]+) ms/frame over (\d+) frames') { throw "unparsed: $last" }
        $expected = if ($variant -eq 'sdl') { 'sdl' } else { 'd3d11' }
        if ($Matches[1] -ne $expected) { throw "asked for $variant, ran $($Matches[1])" }
        if ($variant -in 'map', 'hd', 'hdorig' -and -not (Select-String -Path $log -Pattern 'GPU map layer on' -Quiet)) { throw 'the GPU map layer did not start' }
        if ($variant -in 'hd', 'hdorig' -and -not (Select-String -Path $log -Pattern 'GPU map layer on, HD art' -Quiet)) { throw 'HD art did not start' }
        Select-String -Path $log -Pattern 'HD: (VRAM|art store stats|map GPU time|lighting GPU)' | ForEach-Object { Write-Host "    $($_.Line)" }
        $results += [pscustomobject]@{ Run = $i; Variant = $variant; MsPerFrame = [double]$Matches[4]; Frames = [int]$Matches[5]; Fps = [double]$Matches[3] }
        Write-Host ("    {0:N3} ms/frame presentation CPU time over {1} frames, {2} fps" -f [double]$Matches[4], $Matches[5], $Matches[3])
    }
}

Write-Host ''
$summary = $results | Group-Object Variant | ForEach-Object {
    [pscustomobject]@{ Variant = $_.Name; MsPerFrame = ($_.Group | Measure-Object MsPerFrame -Average).Average; Fps = ($_.Group | Measure-Object Fps -Average).Average }
}
$summary | ForEach-Object { Write-Host ("  {0,-6} {1:N3} ms/frame, {2:N1} fps (average of {3} runs)" -f $_.Variant, $_.MsPerFrame, $_.Fps, $Repeat) }
$results | Export-Csv -NoTypeInformation (Join-Path $outDir "bench-$name.csv")
$sdl = ($summary | Where-Object Variant -eq 'sdl').MsPerFrame
foreach ($v in 'd3d11', 'map', 'hdorig', 'hd') {
    $ms = ($summary | Where-Object Variant -eq $v).MsPerFrame
    if ($sdl -and $ms) { Write-Step ("{0} costs {1:P0} of the SDL path's presentation time" -f $v, ($ms / $sdl)) }
}
exit 0
