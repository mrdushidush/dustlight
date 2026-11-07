# P7 art batch (PLAN.md P7a.1): HD masters for every critter and the UI art, then the
# critters' runtime packs; last, the frames that needed tiles get a second pass with more VRAM. Runs for hours on the PC the author uses every day, so the pipeline
# runs with --adaptive (fo2hd/governor.py): low CPU priority, about half the GPU while the
# PC is in use, full speed while it is locked, paused while the game runs.
# <data_root>\batch_mode.txt (auto | full | shared | pause) overrides that while it exists.
# Resumable at any point: finished FRMs are recorded in jobs.sqlite / jobs_pack.sqlite.
#
#   p7_batch.ps1             run in this console
#   p7_batch.ps1 -Detach     start hidden in the background and return; log: <data_root>\bulk\p7_batch.log
#   p7_batch.ps1 -Stop       stop a detached batch (rerun to continue later)

#   p7_batch.ps1 -FollowPack <manifest>   (started by the batch itself) pack passes over the
#                            manifest while its masters are still being built, until the batch
#                            writes bulk\follow_stop.flag
#
# Release art (PLAN.md R2): -DataRoot <release_root> -Model <model file> builds the whole game
# with that model into its own data root (masters, packs, fonts), leaving the author's data root and
# its UltraSharpV2 art alone. The manifests still come from the main data root's bulk folder.
#   p7_batch.ps1 -DataRoot <release_root> -Model <data_root>\models\r1\x.pth -Detach

# -CategoryModels 'critter=<file>;interface=<file>': a model per art category (fonts follow
# interface), the rest from -Model.
# -From '<step name>': start at that step (the ones before it are done), e.g. -From 'hdassets critters'.
param([switch]$Detach, [switch]$Stop, [string]$FollowPack = '', [string]$DataRoot = '', [string]$Model = '', [string]$CategoryModels = '', [string]$From = '')

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$mainData = (Get-HdPaths)['data_root'].TrimEnd('\')
$data = if ($DataRoot -ne '') { $DataRoot.TrimEnd('\') } else { $mainData }
$bulk = Join-Path $data 'bulk'
$release = $DataRoot -ne ''
if ($release -and $Model -eq '') { throw '-DataRoot needs -Model (the release art is never made with the default model).' }
# Global fo2hd options for every call: the data root and the model.
$fo2hdGlobal = @()
if ($release) { $fo2hdGlobal += @('--data-root', $data) }
if ($Model -ne '') { $fo2hdGlobal += @('--default-model', $Model) }
foreach ($pair in ($CategoryModels -split ';' | Where-Object { $_ -ne '' })) { $fo2hdGlobal += @('--category-model', $pair) }
$passOn = @()
if ($release) { $passOn += @('-DataRoot', $data) }
if ($Model -ne '') { $passOn += @('-Model', $Model) }
if ($CategoryModels -ne '') { $passOn += @('-CategoryModels', $CategoryModels) }
$detachOn = @($passOn)
if ($From -ne '') { $detachOn += @('-From', $From) }
$pidFile = Join-Path $bulk 'p7_batch.pid'
$log = Join-Path $bulk 'p7_batch.log'

function Write-Log([string]$line) {
    # Retried: a reader holding the log (tail -F) made writes fail (tour.ps1, 2026-11-02).
    $text = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $line
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -LiteralPath $log -Value $text -Encoding utf8 -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 300 }
    }
}

function Get-RunningBatch {
    if (-not (Test-Path $pidFile)) { return $null }
    $id = [int](Get-Content $pidFile -Raw)
    return Get-Process -Id $id -ErrorAction SilentlyContinue
}

if ($Stop) {
    $p = Get-RunningBatch
    if (-not $p) { Write-Host 'No batch running.'; return }
    # /T: the whole tree (uv, python, texconv).
    Invoke-Native { taskkill.exe /T /F /PID $p.Id } | Out-Null
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Write-Log 'stopped (p7_batch.ps1 -Stop)'
    Write-Host "Stopped batch (pid $($p.Id))."
    return
}

if ($Detach) {
    if (Get-RunningBatch) { throw "A batch is already running (pid $(Get-Content $pidFile -Raw)). Use -Stop first." }
    New-Item -ItemType Directory -Force $bulk | Out-Null
    $id = Start-Detached $PSCommandPath $detachOn
    Set-Content -LiteralPath $pidFile -Value $id -Encoding ascii
    Write-Host "Batch started (pid $id); log: $log"
    return
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw 'uv not found (see tools/pipeline/README.md).' }
$env:UV_LINK_MODE = 'copy'
$env:PYTHONUNBUFFERED = '1'
$pipeline = Join-Path $script:RepoRoot 'tools\pipeline'
$followFlag = Join-Path $bulk 'follow_stop.flag'

# The pack follower: packing an FRM needs only its finished master (meta.json is written
# after all of its frames), so the critter packs are built while the GPU still makes masters
# for the rest instead of ~3.5 h afterwards. Passes until the batch says its masters are done;
# the batch's own pack step then packs what is left (finished packs are skipped).
if ($FollowPack -ne '') {
    Push-Location $pipeline
    try {
        while (-not (Test-Path $followFlag)) {
            Write-Log '    [pack follower] pass'
            Invoke-Native { & $uv run fo2hd @fo2hdGlobal pack --adaptive --manifest $FollowPack } |
                Where-Object { $_ -match 'skipped|missing_master|packed|Error|error|Traceback' } |
                ForEach-Object { Write-Log "    [pack follower] $_" }
            if ($LASTEXITCODE -ne 0) { Write-Log "    [pack follower] pass failed (exit $LASTEXITCODE)" }
            for ($i = 0; $i -lt 60 -and -not (Test-Path $followFlag); $i++) { Start-Sleep -Seconds 10 }
        }
    } finally { Pop-Location }
    Write-Log '    [pack follower] done'
    return
}
$mainBulk = Join-Path $mainData 'bulk'
$mapArt = Join-Path $mainBulk 'manifest_tiles_walls_scenery_items_misc.json'
$critters = Join-Path $mainBulk 'manifest_critters.json'
$ui = Join-Path $mainBulk 'manifest_ui.json'

# Map art first: only the tiles the map scan reclassified as roofs (fo2hd maps) are
# rebuilt and repacked; everything else in that manifest is already done and skipped.
$steps = @(
    @{ name = 'hdassets map art (roofs)'; args = @('hdassets', '--adaptive', '--manifest', $mapArt) },
    @{ name = 'pack map art (roofs)'; args = @('pack', '--adaptive', '--manifest', $mapArt) },
    # UI art before the critters: P7b's HD skin needs it first; the critters are batch-only work.
    @{ name = 'hdassets ui'; args = @('hdassets', '--adaptive', '--manifest', $ui) },
    # Packs keep the HD alpha; hard (1x mask) vs soft edges is pack.json's flag, chosen at runtime.
    @{ name = 'pack ui'; args = @('pack', '--adaptive', '--manifest', $ui, '--check', '20') },
    @{ name = 'hdassets critters'; args = @('hdassets', '--adaptive', '--manifest', $critters); follow = $critters },
    @{ name = 'pack critters'; args = @('pack', '--adaptive', '--manifest', $critters, '--check', '20') },
    # Last: frames that needed tiles under the 8 GB cap (bulk\tiled.jsonl) again in one piece
    # with 15 GB, only while the PC is locked (the author, 2026-11-02), then repacked.
    @{ name = 'retry tiled frames'; args = @('retry-tiled') }
)
if ($release) {
    # A fresh data root has no font atlases yet (P7b.5 made them by hand); upscaler glyphs on the CPU.
    $steps += @{ name = 'fonts'; args = @('fonts') }
    # texconv lives in the main data root's tools folder; pack looks for it under its own root.
    $tools = Join-Path $data 'tools'
    if (-not (Test-Path $tools)) {
        New-Item -ItemType Directory -Force $data | Out-Null
        Invoke-Native { cmd.exe /c mklink /J "$tools" "$(Join-Path $mainData 'tools')" } | Out-Null
    }
}

if ($From -ne '') {
    $at = [array]::FindIndex([object[]]$steps, [Predicate[object]] { param($s) $s.name -eq $From })
    if ($at -lt 0) { throw "-From '$From': no such step ($(($steps | ForEach-Object { $_.name }) -join ', '))." }
    $steps = @($steps[$at..($steps.Count - 1)])
}

Write-Log "batch start (pid $PID)$(if ($From -ne '') { ", from '$From'" })"
Push-Location $pipeline
try {
    foreach ($step in $steps) {
        Write-Log "step: $($step.name)"
        $started = Get-Date
        $stepArgs = $step.args
        $follower = $null
        if ($step.follow) {
            Remove-Item $followFlag -ErrorAction SilentlyContinue
            # A child of this process, so -Stop's taskkill /T takes it along.
            # One expression in parentheses: in a command's arguments, `@(...) + (...)` passes
            # a stray '+' (the release batch's first critter step failed on it, 2026-11-04).
            $followArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-FollowPack', "`"$($step.follow)`"") +
                @($passOn | ForEach-Object { "`"$_`"" })
            $follower = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList $followArgs
            Write-Log "    pack follower started (pid $($follower.Id))"
        }
        Invoke-Native { & $uv run fo2hd @fo2hdGlobal @stepArgs } |
            Where-Object { $_ -notmatch 'meshgrid|UserWarning' } |
            ForEach-Object { Write-Log $_ }
        $code = $LASTEXITCODE
        if ($follower) {
            # Its current pass finishes first: two packers on the same FRM would collide.
            Set-Content -LiteralPath $followFlag -Value 'stop' -Encoding ascii
            $follower.WaitForExit()
            Write-Log '    pack follower finished'
        }
        Write-Log ('step done: {0} (exit {1}, {2:N1} h)' -f $step.name, $code, ((Get-Date) - $started).TotalHours)
        if ($code -ne 0) { throw "$($step.name) failed (exit $code)" }
    }
    Write-Log 'batch finished'
} catch {
    Write-Log "batch failed: $_"
    throw
} finally {
    Pop-Location
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -eq $PID) { Remove-Item $pidFile }
}
