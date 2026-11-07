# Release checks that open the game window (PLAN.md R6), on the clean install from
# hd.release.ini, only while the PC is locked: unlocking kills the test game at once and the
# step runs again at the next lock.
#
#   1. first start: a fresh install of the zips, one start with only what a player has
#      (release_test.ps1 -Install, then -FirstStart)
#   3. rp (if -RpZip): the same first start on a second clean copy with the Restoration
#      Project installed first (hd.release-rp.ini), then the map self-tests there
#   hw: the same map self-tests on the GPU in the same window as the WARP run (1720x960,
#      scale 2: an 860 px wide map, a width the earlier tests never used), so a WARP failure
#      can be told from a window-size one
#   2. WARP: the GPU map self-tests (map_test.ps1 -Only scenes) on Microsoft's software
#      rasterizer instead of the GPU ([hd_debug] d3d_warp=1): a vendor-neutral check
#
#   release_checks.ps1 -Detach [-Tier 2] [-Dist <dir>]   wait for a lock in the background, run, log
#   release_checks.ps1 -Stop
#   -ZipExe: test the exe in the zips (a release candidate) instead of the latest build
# Log and results: <release data root>\reports\release_checks\

param(
    [int]$Tier = 2,
    [string]$Dist = '',
    [string[]]$Steps = @('firststart', 'warp'),
    [int]$WarpSceneSeconds = 300,
    [string]$RpZip = '',
    [switch]$ZipExe,
    [switch]$Detach,
    [switch]$Stop
)

. (Join-Path $PSScriptRoot 'common.ps1')

$env:FO2HD_INI = Join-Path $script:RepoRoot 'hd.release.ini'
$paths = Get-HdPaths
$data = $paths['data_root'].TrimEnd('\')
$test = $paths['working_copy'].TrimEnd('\')
$outDir = Join-Path $data 'reports\release_checks'
New-Item -ItemType Directory -Force $outDir | Out-Null
$log = Join-Path $outDir 'release_checks.log'
$pidFile = Join-Path $outDir 'release_checks.pid'

function Write-Log([string]$line) {
    $text = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $line
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -LiteralPath $log -Value $text -Encoding utf8 -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 300 }
    }
}
function Test-Locked { return [bool](Get-Process LogonUI -ErrorAction SilentlyContinue) }
function Stop-TestGames {
    Get-CimInstance Win32_Process -Filter "Name='fallout2-ce.exe' OR Name='dustlight.exe'" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($test, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Stop) {
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile -Raw)
        Invoke-Native { taskkill.exe /T /F /PID $id } | Out-Null
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
    Stop-TestGames
    Write-Log 'stopped (-Stop)'
    return
}
if ($Detach) {
    $passOn = @('-Tier', "$Tier", '-WarpSceneSeconds', "$WarpSceneSeconds", '-Steps', ($Steps -join ','))
    if ($Dist -ne '') { $passOn += @('-Dist', $Dist) }
    if ($RpZip -ne '') { $passOn += @('-RpZip', $RpZip) }
    if ($ZipExe) { $passOn += '-ZipExe' }
    $id = Start-Detached $PSCommandPath $passOn
    Set-Content -LiteralPath $pidFile -Value $id -Encoding ascii
    Write-Host "Release checks started (pid $id); log: $log"
    return
}
if ($Steps.Count -eq 1 -and $Steps[0] -match ',') { $Steps = $Steps[0] -split ',' }

# Sets [section] key=value in an INI file (the clean copy's fallout2.cfg; it is disposable).
function Set-IniKeys([string]$file, [string]$section, [hashtable]$values) {
    $lines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $file)
    $start = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq "[$section]") { $start = $i; break } }
    if ($start -lt 0) { $lines.Add(''); $lines.Add("[$section]"); $start = $lines.Count - 1 }
    $end = $lines.Count
    for ($i = $start + 1; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim().StartsWith('[')) { $end = $i; break } }
    foreach ($key in $values.Keys) {
        $found = $false
        for ($i = $start + 1; $i -lt $end; $i++) {
            if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $lines[$i] = "$key=$($values[$key])"; $found = $true; break }
        }
        if (-not $found) { $lines.Insert($start + 1, "$key=$($values[$key])"); $end++ }
    }
    Set-Content -LiteralPath $file -Value $lines -Encoding ascii
}

# One step = one child PowerShell, so an unlock can kill it with its game.
function Invoke-Step([string]$name, [string[]]$arguments) {
    while ($true) {
        while (-not (Test-Locked)) { Start-Sleep -Seconds 10 }
        Write-Log "step ${name}: start"
        $stepLog = Join-Path $outDir "$name.txt"
        $child = Start-Process powershell.exe -WindowStyle Hidden -PassThru -RedirectStandardOutput $stepLog -RedirectStandardError "$stepLog.err" -ArgumentList (@(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File') + $arguments)
        $null = $child.Handle  # keeps the process handle, so ExitCode is there after it exits
        $unlocked = $false
        while (-not $child.HasExited) {
            if (-not (Test-Locked)) {
                $unlocked = $true
                Invoke-Native { taskkill.exe /T /F /PID $child.Id } | Out-Null
                Stop-TestGames
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if ($unlocked) { Write-Log "step ${name}: unlocked, killed; again at the next lock"; continue }
        Write-Log "step ${name}: exit $($child.ExitCode)"
        Get-Content $stepLog -ErrorAction SilentlyContinue | Where-Object { $_ -match 'PASS|FAIL|first start|checks|failed|summary' } | ForEach-Object { Write-Log "    $_" }
        return $child.ExitCode
    }
}

Write-Log "release checks: steps $($Steps -join ', '), tier $Tier"
$scripts = $PSScriptRoot
$exeChoice = if ($ZipExe) { @() } else { @('-LatestExe') }
$results = [ordered]@{}
try {
    foreach ($step in $Steps) {
        switch ($step) {
            'firststart' {
                $install = @("`"$scripts\release_test.ps1`"", '-Install') + $exeChoice + @('-Tier', "$Tier")
                if ($Dist -ne '') { $install += @('-Dist', "`"$Dist`"") }
                # -Install needs no lock but resets the folder, so it runs right before the start.
                $installCode = Invoke-Step 'install' $install
                $results['install'] = $installCode
                if ($installCode -eq 0) {
                    $results['firststart'] = Invoke-Step 'firststart' @("`"$scripts\release_test.ps1`"", '-FirstStart', '-Tier', "$Tier")
                }
            }
            'rp' {
                if ($RpZip -eq '') { Write-Log 'step rp: no -RpZip, skipped'; continue }
                $rpIni = 'hd.release-rp.ini'
                $install = @("`"$scripts\release_test.ps1`"", '-Install') + $exeChoice + @('-Tier', "$Tier", '-Ini', $rpIni, '-RpZip', "`"$RpZip`"")
                if ($Dist -ne '') { $install += @('-Dist', "`"$Dist`"") }
                $results['rp install'] = Invoke-Step 'rp-install' $install
                if ($results['rp install'] -eq 0) {
                    $results['rp firststart'] = Invoke-Step 'rp-firststart' @("`"$scripts\release_test.ps1`"", '-FirstStart', '-Tier', "$Tier", '-Ini', $rpIni)
                    # The map self-tests on the RP copy: the child inherits FO2HD_INI.
                    $saved = $env:FO2HD_INI
                    $env:FO2HD_INI = Join-Path $script:RepoRoot $rpIni
                    try {
                        $results['rp scenes'] = Invoke-Step 'rp-scenes' @("`"$scripts\map_test.ps1`"", '-Only', 'scenes', '-Label', 'rp')
                    } finally { $env:FO2HD_INI = $saved }
                }
            }
            { $_ -in 'warp', 'hw' } {
                # The clean install's settings, in a window (logical 860x480 at scale 2, 1146x480
                # at 3), WARP or the GPU; written into the clean copy's own fallout2.cfg (disposable).
                $cfg = Join-Path $test 'fallout2.cfg'
                $width = if ($Tier -eq 3) { 3438 } elseif ($Tier -eq 4) { 3440 } else { 1720 }
                $height = 480 * $Tier
                Set-IniKeys $cfg 'hd_debug' @{ d3d_warp = [int]($step -eq 'warp') }
                Set-IniKeys $cfg 'screen' @{ scale = $Tier; windowed = 1; resolution_x = $width; resolution_y = $height }
                Set-IniKeys $cfg 'hd' @{ auto_screen = 0 }
                try {
                    $seconds = if ($step -eq 'warp') { $WarpSceneSeconds } else { 90 }
                    $results[$step] = Invoke-Step $step @("`"$scripts\map_test.ps1`"", '-Only', 'scenes', '-Label', $step, '-SceneSeconds', "$seconds")
                    # map_test clears its image folder per run: keep this run's images
                    $keep = Join-Path $outDir "map-$step"
                    Remove-Item $keep -Recurse -Force -ErrorAction SilentlyContinue
                    Copy-Item (Join-Path $data 'reports\map') $keep -Recurse
                } finally {
                    Set-IniKeys $cfg 'hd_debug' @{ d3d_warp = 0 }
                }
            }
        }
    }
} finally {
    $results | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outDir 'results.json') -Encoding utf8
    Write-Log "release checks done: $(($results.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', ')"
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -eq $PID) { Remove-Item $pidFile }
}
