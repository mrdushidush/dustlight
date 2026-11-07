# Phase 2 acceptance: the D3D11 presenter shows exactly what the SDL path shows.
#
# Runs the deployed game with [hd] renderer=d3d11 and [hd_debug] present_selftest_ms,
# so the presenter periodically renders the 8-bit screen at 1:1, reads it back and
# compares it with SDL's own 8-bit -> RGB conversion of the same pixels. Injected keys
# walk through the screens; a second run plays the intro movies (movie overlay path).
# Back buffers are saved to <data_root>\reports\present\ for inspection.
#
#   present_test.ps1                 both runs
#   present_test.ps1 -Slot 1         save slot for the screen walk (--dev-load-game)
#
# Works with a locked desktop (no real input, no screen grabs).

param(
    [int]$Slot = 1,
    [int]$SelfTestMs = 1200
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$outDir = Join-Path $paths['data_root'] 'reports\present'
New-Item -ItemType Directory -Force $outDir | Out-Null
Get-ChildItem $outDir -Filter '*auto-*.png' -ErrorAction SilentlyContinue | Remove-Item

# kb.h key codes: i=105, Esc=27, p=112, c=99, Tab=9, o=111, s=115.
$walk = @(
    '7000:105', '10000:27',   # inventory
    '12000:112', '15000:27',  # Pip-Boy
    '17000:99', '20000:27',   # character screen
    '22000:9', '25000:27',    # automap
    '27000:111', '30000:27',  # options
    '32000:115', '35000:27'   # skilldex
) -join ','

$runs = @(
    @{ Name = 'screens'; Seconds = 38; Args = @("--dev-load-game=$Slot", '"[ui]skip_opening_movies=2"', "`"[hd_debug]inject_keys=$walk`"") },
    @{ Name = 'movies'; Seconds = 16; Args = @('"[ui]skip_opening_movies=0"') }
)

$total = 0
$failed = 0
foreach ($run in $runs) {
    Write-Step "D3D11 present test: $($run.Name)"
    $gameArgs = @('"[hd]renderer=d3d11"', '"[hd]map_gpu=0"', "`"[hd_debug]present_selftest_ms=$SelfTestMs`"") + $run.Args
    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work 'fallout2-ce.exe') -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
    Start-Sleep -Seconds $run.Seconds
    if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null }

    $log = Join-Path $work 'debug.log'
    Copy-Item $log (Join-Path $outDir "$($run.Name).debug.log") -Force
    $lines = Select-String -Path $log -Pattern 'HD: (D3D11|present)' | ForEach-Object { $_.Line }
    $up = $lines | Where-Object { $_ -match 'presenter up' }
    if (-not $up) { throw "$($run.Name): the D3D11 presenter did not start; see $outDir\$($run.Name).debug.log" }
    Write-Host "    $up"
    $tests = @($lines | Where-Object { $_ -match 'self-test' })
    $fails = @($tests | Where-Object { $_ -match 'FAIL' })
    $total += $tests.Count
    $failed += $fails.Count
    Write-Host "    $($tests.Count) self-tests, $($fails.Count) failed"
    $fails | Select-Object -First 5 | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    Get-ChildItem $outDir -Filter 'auto-*.png' | ForEach-Object { Rename-Item $_.FullName ("$($run.Name)-" + $_.Name) -Force }
}

Write-Step "Back buffers: $outDir"
if ($total -eq 0) { throw 'no self-tests ran' }
if ($failed -gt 0) { throw "$failed of $total self-tests failed" }
Write-Step "Present test OK: $total self-tests, 0 px different"
exit 0
