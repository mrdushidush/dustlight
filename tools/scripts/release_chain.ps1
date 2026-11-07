# Everything after the release batch, unattended (PLAN.md R2, R5, R6, R8), so a locked night
# isn't lost waiting for someone to start the next step: the zips, the clean-install checks,
# the full capture tour on the clean install, the store page's stills and clip, then the
# zips' other art tiers (release_tiers.ps1).
# Game windows open only while the PC is locked (release_checks.ps1 and tour.ps1 wait for
# that themselves; unlocking kills the game and the step goes on at the next lock). Nothing
# here publishes anything.
#
#   release_chain.ps1 -Detach [-Credits <file>] [-HdPack <dir>] [-Tier 3] [-RpZip <zip>]
#   release_chain.ps1 -Stop
# The art needs no batch when it already exists:
#   release_chain.ps1 -Detach -NoWait -HdPack <data_root>\hdpack -RpZip <zip>
# Log: <release data root>\reports\release_chain\release_chain.log
#
# A test of the chain itself, on what the batch has made so far:
#   release_chain.ps1 -NoWait -Version 0.0.1-chaincheck -Tiers 2 -Tier 2 -Steps firststart -TourMax 3

param(
    [string]$Credits = 'docs\player\art_credits_ultrasharpv2.json',
    [string]$HdPack = '',                 # the art the zips hold; default: <release data root>\hdpack
    [int]$Tier = 3,                       # the tier the checks and the tour install
    [string]$Tiers = '2,3,4',             # the tiers the zips hold
    [string]$RpZip = '',
    [string]$Version = '',                # default: HD_RELEASE_VERSION in src/hd/release_info.h
    [string]$Steps = 'firststart,hw,warp,rp',
    [string]$Run = '',                    # the tour's run folder; default release-<Version>
    [int]$TourMax = 0,
    [switch]$NoWait,                      # don't wait for the batch
    [switch]$Detach,
    [switch]$Stop
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$env:FO2HD_INI = Join-Path $script:RepoRoot 'hd.release.ini'
$data = (Get-HdPaths)['data_root'].TrimEnd('\')
$outDir = Join-Path $data 'reports\release_chain'
New-Item -ItemType Directory -Force $outDir | Out-Null
$log = Join-Path $outDir 'release_chain.log'
$pidFile = Join-Path $outDir 'release_chain.pid'

function Write-Log([string]$line) {
    $text = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $line
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -LiteralPath $log -Value $text -Encoding utf8 -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 300 }
    }
}

if ($Stop) {
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile -Raw)
        # /T: the whole tree (release_checks, tour, their game).
        Invoke-Native { taskkill.exe /T /F /PID $id } | Out-Null
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Write-Log 'stopped (-Stop)'
    }
    return
}

if ($Detach) {
    if ((Test-Path $pidFile) -and (Get-Process -Id ([int](Get-Content $pidFile -Raw)) -ErrorAction SilentlyContinue)) {
        throw "A chain is already running (pid $(Get-Content $pidFile -Raw)). Use -Stop first."
    }
    $passOn = @('-Credits', $Credits, '-Tier', "$Tier", '-Tiers', $Tiers, '-Steps', $Steps, '-TourMax', "$TourMax")
    if ($HdPack -ne '') { $passOn += @('-HdPack', $HdPack) }
    if ($RpZip -ne '') { $passOn += @('-RpZip', $RpZip) }
    if ($Version -ne '') { $passOn += @('-Version', $Version) }
    if ($Run -ne '') { $passOn += @('-Run', $Run) }
    if ($NoWait) { $passOn += '-NoWait' }
    $id = Start-Detached $PSCommandPath $passOn
    Set-Content -LiteralPath $pidFile -Value $id -Encoding ascii
    Write-Host "Release chain started (pid $id); log: $log"
    return
}

# Zips and tests shouldn't crowd the PC if the author comes back; children inherit this.
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'

# One script in its own PowerShell, its output in a file next to the log.
function Invoke-ChainStep([string]$name, [string]$file, [string[]]$arguments) {
    Write-Log "step ${name}: start"
    $stepLog = Join-Path $outDir "$name.txt"
    $child = Start-Process powershell.exe -WindowStyle Hidden -PassThru -RedirectStandardOutput $stepLog -RedirectStandardError "$stepLog.err" -ArgumentList (@(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$file`"") + $arguments)
    $null = $child.Handle  # keeps the process handle, so ExitCode is there after it exits
    $child.WaitForExit()
    Write-Log "step ${name}: exit $($child.ExitCode)"
    return $child.ExitCode
}

function Invoke-Media([string[]]$mediaArgs) {
    Push-Location (Join-Path $script:RepoRoot 'tools\pipeline')
    try {
        Invoke-Native { & uv run fo2hd --data-root $data release-media @mediaArgs } |
            Where-Object { $_ -notmatch 'meshgrid|UserWarning' } | ForEach-Object { Write-Log "    $_" }
        if ($LASTEXITCODE -ne 0) { throw "release-media failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

try {
    Write-Log "chain start (pid $PID): credits $Credits, art $(if ($HdPack -ne '') { $HdPack } else { '<data root>\hdpack' }), tiers $Tiers, tier $Tier, steps $Steps"
    if (-not $NoWait) {
        $batchPid = Join-Path $data 'bulk\p7_batch.pid'
        Write-Log 'waiting for the batch'
        while ((Test-Path $batchPid) -and (Get-Process -Id ([int](Get-Content $batchPid -Raw)) -ErrorAction SilentlyContinue)) { Start-Sleep -Seconds 60 }
        # A stopped or failed batch leaves its art unfinished: those zips would lack it.
        $last = @(Get-Content -LiteralPath (Join-Path $data 'bulk\p7_batch.log') -Tail 1)[0]
        if ($last -notmatch 'batch finished') { throw "the batch didn't finish (last log line: $last)" }
        Write-Log 'batch finished'
    }

    if ($Version -eq '') {
        $info = Get-Content -Raw (Join-Path $script:RepoRoot 'src\hd\release_info.h')
        $Version = [regex]::Match($info, '#define HD_RELEASE_VERSION "([^"]+)"').Groups[1].Value
        if ($Version -eq '') { throw 'No version (src/hd/release_info.h).' }
    }
    $dist = Join-Path $data "dist\$Version"
    if ($Run -eq '') { $Run = "release-$Version" }
    $creditsFile = if ([IO.Path]::IsPathRooted($Credits)) { $Credits } else { Join-Path $script:RepoRoot $Credits }

    # release.json is written last: with it, the zips of this version are complete.
    if (Test-Path (Join-Path $dist 'release.json')) {
        Write-Log "zips already built: $dist"
    } else {
        $zipArgs = @('-Credits', "`"$creditsFile`"", '-Version', $Version, '-Tiers', $Tiers)
        if ($HdPack -ne '') { $zipArgs += @('-HdPack', "`"$HdPack`"") }
        $code = Invoke-ChainStep 'zips' (Join-Path $PSScriptRoot 'release.ps1') $zipArgs
        if ($code -ne 0) { throw "the zips failed (exit $code), see zips.txt" }
    }

    $checkArgs = @('-Tier', "$Tier", '-Dist', "`"$dist`"", '-Steps', $Steps, '-ZipExe')
    if ($RpZip -ne '') { $checkArgs += @('-RpZip', "`"$RpZip`"") }
    Invoke-ChainStep 'checks' (Join-Path $PSScriptRoot 'release_checks.ps1') $checkArgs | Out-Null
    # The tour needs the install and its first start. A failed self-test elsewhere (WARP's
    # exact pass, say) doesn't stop it: it goes in the report with the rest.
    $results = Get-Content -Raw (Join-Path $data 'reports\release_checks\results.json') | ConvertFrom-Json
    Write-Log "checks: $(($results.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', ')"
    if ($results.install -ne 0 -or $results.firststart -ne 0) { throw 'the clean install or its first start failed: no tour' }

    $tourArgs = @('-Run', $Run)
    if ($TourMax -gt 0) { $tourArgs += @('-Max', "$TourMax") }
    $code = Invoke-ChainStep 'tour' (Join-Path $PSScriptRoot 'tour.ps1') $tourArgs
    if ($code -ne 0) { throw "the tour stopped (exit $code)" }

    # Store page media: the views chosen by eye (media_views.json) and the clip. A problem here
    # doesn't touch the zips or the tour, so it only goes in the log.
    try {
        $viewsFile = Join-Path $PSScriptRoot 'media_views.json'
        $mediaNames = @(Get-Content -Raw $viewsFile | ConvertFrom-Json | ForEach-Object { $_ } | ForEach-Object { $_.name })
        $mediaRun = "$Run-media"
        $code = Invoke-ChainStep 'tour-media' (Join-Path $PSScriptRoot 'tour.ps1') @('-Run', $mediaRun, '-NoWalk', '-TourFile', "`"$viewsFile`"", '-Only', ($mediaNames -join ','))
        if ($code -ne 0) { throw "the media views stopped (exit $code)" }
        $code = Invoke-ChainStep 'clip' (Join-Path $PSScriptRoot 'release_clip.ps1') @()
        if ($code -ne 0) { Write-Log "the clip failed (exit $code), see clip.txt" }
        Invoke-Media @('--run', (Join-Path $data "reports\tour\$mediaRun"), '--views', $viewsFile)
    } catch {
        Write-Log "media failed: $_"
    }
    # Last: each tier reinstalls the clean copy. Their results stay in their own folders.
    $otherTiers = @($Tiers -split ',' | Where-Object { [int]$_ -ne $Tier })
    if ($otherTiers.Count -gt 0 -and $TourMax -eq 0) {
        Invoke-ChainStep 'other-tiers' (Join-Path $PSScriptRoot 'release_tiers.ps1') @('-Dist', "`"$dist`"", '-Tiers', ($otherTiers -join ','), '-MainTier', "$Tier") | Out-Null
    }
    Write-Log "chain finished: zips $dist, tour run $Run, media $(Join-Path $data 'reports\release-media')"
} catch {
    Write-Log "chain failed: $_"
    throw
} finally {
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -eq $PID) { Remove-Item $pidFile }
}
