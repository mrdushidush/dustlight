# Other copies of Fallout 2 a player may have (PLAN.md R6), after the full run: the French and
# German folders of the Epic install, and a copy without the high-resolution patch and sfall
# files (a stand-in for a store copy that doesn't ship them; it isn't a real Steam or GOG copy).
# Each gets its own clean install from the zips, the first start, the GPU map self-tests, the
# UI walk and a few views. Game windows open only while the PC is locked: an unlock kills the
# game and the step runs again at the next lock. Nothing is published.
#
#   release_variants.ps1 -Dist <dist folder> -Detach [-AfterPid <chain pid>] [-Variants french,german,nohires]
#   release_variants.ps1 -Dist <dist folder> -Detach -Variants gog -GogFolder <untouched GOG install>
#   release_variants.ps1 -Dist <dist folder> -Stop
# Results: <release data root>\reports\release_variants\<variant>\ (results.json, step logs, map
# images), tours <variant>-<dist folder name>. The game folders are only read.

param(
    [Parameter(Mandatory = $true)][string]$Dist,
    [string]$Variants = 'french,german,nohires',
    [string]$GogFolder = '',
    [int]$Tier = 3,
    [int]$AfterPid = 0,    # wait for this process first (only one game window at a time)
    [switch]$Detach,
    [switch]$Stop
)

. (Join-Path $PSScriptRoot 'common.ps1')

$repo = $script:RepoRoot
$env:FO2HD_INI = Join-Path $repo 'hd.release.ini'
$data = (Get-HdPaths)['data_root'].TrimEnd('\')
$epic = (Get-HdPaths)['pristine'].TrimEnd('\')
$epicRoot = Split-Path $epic -Parent
$outDir = Join-Path $data 'reports\release_variants'
New-Item -ItemType Directory -Force $outDir | Out-Null
$log = Join-Path $outDir 'release_variants.log'
$pidFile = Join-Path $outDir 'release_variants.pid'
$version = Split-Path $Dist -Leaf
$views = 'arvillag_e0_0', 'newr2_e0_0', 'vctycocl_e0_1', 'denbus1_e0_2'

function Write-Log([string]$line) {
    $text = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $line
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -LiteralPath $log -Value $text -Encoding utf8 -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 300 }
    }
}
function Test-Locked { return [bool](Get-Process LogonUI -ErrorAction SilentlyContinue) }

if ($Stop) {
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile -Raw)
        Invoke-Native { taskkill.exe /T /F /PID $id } | Out-Null
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Write-Log 'stopped (-Stop)'
    }
    return
}
if ($Detach) {
    # A second run may queue behind the first with -AfterPid <the first's pid>.
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -ne $AfterPid -and (Get-Process -Id ([int](Get-Content $pidFile -Raw)) -ErrorAction SilentlyContinue)) {
        throw "Already running (pid $(Get-Content $pidFile -Raw)). Use -AfterPid to queue behind it, or -Stop first."
    }
    $passOn = @('-Dist', $Dist, '-Variants', $Variants, '-Tier', "$Tier", '-AfterPid', "$AfterPid")
    if ($GogFolder -ne '') { $passOn += @('-GogFolder', $GogFolder) }
    $id = Start-Detached $PSCommandPath $passOn
    Set-Content -LiteralPath $pidFile -Value $id -Encoding ascii
    Write-Host "Variants started (pid $id); log: $log"
    return
}

(Get-Process -Id $PID).PriorityClass = 'BelowNormal'

# One step = one child PowerShell, so an unlock can kill it with its game (as release_checks.ps1).
function Invoke-Step([string]$dir, [string]$test, [string]$name, [string[]]$arguments) {
    while ($true) {
        while (-not (Test-Locked)) { Start-Sleep -Seconds 10 }
        Write-Log "  step ${name}: start"
        $stepLog = Join-Path $dir "$name.txt"
        $child = Start-Process powershell.exe -WindowStyle Hidden -PassThru -RedirectStandardOutput $stepLog -RedirectStandardError "$stepLog.err" -ArgumentList (@(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File') + $arguments)
        $null = $child.Handle  # keeps the process handle, so ExitCode is there after it exits
        $unlocked = $false
        while (-not $child.HasExited) {
            if (-not (Test-Locked)) {
                $unlocked = $true
                Invoke-Native { taskkill.exe /T /F /PID $child.Id } | Out-Null
                Get-CimInstance Win32_Process -Filter "Name='fallout2-ce.exe' OR Name='dustlight.exe'" |
                    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($test, [StringComparison]::OrdinalIgnoreCase) } |
                    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if ($unlocked) { Write-Log "  step ${name}: unlocked, killed; again at the next lock"; continue }
        Write-Log "  step ${name}: exit $($child.ExitCode)"
        return $child.ExitCode
    }
}

# A game folder without the high-resolution patch and sfall: never touches the Epic install.
function Get-NoHiresPristine {
    $folder = Join-Path (Split-Path $epicRoot -Parent) 'fo2-nohires-pristine'
    Invoke-Native { robocopy.exe $epic $folder /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 /XF 'f2_res*' 'ddraw.*' 'sfall-readme.txt' 'fallout2HR.exe' 'Fallout2_High_Resolution_Patch_*' } | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    return $folder
}

function Get-Pristine([string]$variant) {
    switch ($variant) {
        'french' { return Join-Path $epicRoot 'Fallout 2 French' }
        'german' { return Join-Path $epicRoot 'Fallout 2 German' }
        'nohires' { return Get-NoHiresPristine }
        'gog' {
            if (-not (Test-Path (Join-Path $GogFolder 'master.dat'))) { throw "No master.dat in -GogFolder '$GogFolder'." }
            return $GogFolder.TrimEnd('\')
        }
        default { throw "Unknown variant $variant" }
    }
}

$results = [ordered]@{}
try {
    if ($AfterPid -gt 0) {
        Write-Log "waiting for pid $AfterPid"
        while (Get-Process -Id $AfterPid -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 30 }
    }
    Write-Log "variants: $Variants, tier $Tier, $Dist"
    foreach ($variant in @($Variants -split ',')) {
        $dir = Join-Path $outDir $variant
        New-Item -ItemType Directory -Force $dir | Out-Null
        $test = Join-Path (Split-Path $epicRoot -Parent) "fo2-variant-$variant"
        $iniName = "hd.release-$variant.ini"
        $pristine = Get-Pristine $variant
        # release_test.ps1 takes a file in the repo root (ignored by git: machine paths).
        Set-Content -LiteralPath (Join-Path $repo $iniName) -Encoding ascii -Value @(
            '[paths]', "pristine=$pristine", "working_copy=$test", "data_root=$data")
        Write-Log "$variant`: $pristine -> $test"

        $scripts = $PSScriptRoot
        $r = [ordered]@{}
        $r['install'] = Invoke-Step $dir $test 'install' @("`"$scripts\release_test.ps1`"", '-Install', '-Tier', "$Tier", '-Ini', $iniName, '-Dist', "`"$Dist`"")
        if ($r['install'] -eq 0) {
            $r['firststart'] = Invoke-Step $dir $test 'firststart' @("`"$scripts\release_test.ps1`"", '-FirstStart', '-Tier', "$Tier", '-Ini', $iniName)
            # Children inherit FO2HD_INI: the self-tests and tour run on this copy.
            $saved = $env:FO2HD_INI
            $env:FO2HD_INI = Join-Path $repo $iniName
            try {
                $r['scenes'] = Invoke-Step $dir $test 'scenes' @("`"$scripts\map_test.ps1`"", '-Only', 'scenes', '-Label', $variant)
                $map = Join-Path $data 'reports\map'
                if (Test-Path $map) {
                    $keep = Join-Path $dir 'map'
                    Remove-Item $keep -Recurse -Force -ErrorAction SilentlyContinue
                    Copy-Item $map $keep -Recurse
                }
                $run = "$variant-$version"
                $r['walk'] = Invoke-Step $dir $test 'walk' @("`"$scripts\tour.ps1`"", '-Run', $run, '-WalkOnly', '-WalkPasses', 'hd,hd-talk,hd-world')
                $r['views'] = Invoke-Step $dir $test 'views' @("`"$scripts\tour.ps1`"", '-Run', $run, '-NoWalk', '-Only', ($views -join ','))
            } finally { $env:FO2HD_INI = $saved }
        }
        $results[$variant] = $r
        Write-Log "$variant`: $(($r.Keys | ForEach-Object { "$_=$($r[$_])" }) -join ', ')"
        $r | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $dir 'results.json') -Encoding utf8
    }
    Write-Log 'variants finished'
} catch {
    Write-Log "variants failed: $_"
    throw
} finally {
    if ((Test-Path $pidFile) -and [int](Get-Content $pidFile -Raw) -eq $PID) { Remove-Item $pidFile }
}
