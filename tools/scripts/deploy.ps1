# Copies the built game and ce.dat into the working copy (PLAN.md P0.4).
#
#   deploy.ps1                        RelWithDebInfo -> fallout2-ce.exe
#   deploy.ps1 -Config Debug
#   deploy.ps1 -AlsoAsVanilla         also install as fallout2-ce-vanilla.exe; use only for a
#                                     build of unmodified upstream (the A/B and save-compat reference)
#
# On first deploy, seeds a windowed [screen] section into the working copy's fallout2.cfg.
# Without it, CE would migrate the Epic f2_res.ini (WINDOWED=0, 1024x768) and switch the
# monitor to exclusive fullscreen.

param(
    [ValidateSet('RelWithDebInfo', 'Debug', 'Release')]
    [string]$Config = 'RelWithDebInfo',
    [switch]$AlsoAsVanilla
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$pristine = $paths['pristine'].TrimEnd('\')

if ($work -ieq $pristine -or $work.StartsWith($pristine + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to deploy: working_copy ($work) is the pristine install or inside it."
}
if (-not (Test-Path -LiteralPath (Join-Path $work 'master.dat'))) {
    throw "No working copy at $work. Run make_working_copy.ps1 first."
}

$binDir = Join-Path $script:RepoRoot "out\build\windows-x64\$Config"
$exe = Join-Path $binDir 'fallout2-ce.exe'
$ceDat = Join-Path $script:RepoRoot 'out\dist\ce.dat'
foreach ($f in $exe, $ceDat) {
    if (-not (Test-Path $f)) { throw "Missing $f. Run build.ps1 -Config $Config first." }
}

Write-Step "Deploying $Config build to $work"
Copy-Item $exe (Join-Path $work 'fallout2-ce.exe') -Force
$pdb = Join-Path $binDir 'fallout2-ce.pdb'
if (Test-Path $pdb) { Copy-Item $pdb (Join-Path $work 'fallout2-ce.pdb') -Force }
Copy-Item $ceDat (Join-Path $work 'ce.dat') -Force
if ($AlsoAsVanilla) {
    Copy-Item $exe (Join-Path $work 'fallout2-ce-vanilla.exe') -Force
    Write-Host '    also installed as fallout2-ce-vanilla.exe'
}

$cfg = Join-Path $work 'fallout2.cfg'
$cfgText = if (Test-Path $cfg) { Get-Content -Raw $cfg } else { '' }
if ($cfgText -notmatch '(?m)^\s*resolution_x\s*=') {
    if ($cfgText -match '(?m)^\s*\[screen\]') {
        Write-Warning 'fallout2.cfg has a [screen] section without resolution_x; not seeding it.'
    } else {
        Write-Step 'Seeding windowed [screen] settings (1280x960 window, scale 2 = original 640x480 view)'
        $block = "`r`n[screen]`r`nresolution_x=1280`r`nresolution_y=960`r`nscale=2`r`nwindowed=1`r`n"
        Add-Content -LiteralPath $cfg -Value $block -Encoding ASCII -NoNewline
    }
}

$cfgText = Get-Content -Raw -LiteralPath $cfg
if ($cfgText -notmatch '(?m)^\s*\[hd\]') {
    # HD module on (only adds the Ctrl+F12 capture hotkey in Phase 1); captures go to the data root.
    $captureDir = Join-Path $paths['data_root'] 'captures'
    Write-Step "Seeding [hd] settings (captures to $captureDir)"
    $block = "`r`n[hd]`r`nenabled=1`r`ncapture_dir=$captureDir`r`n"
    Add-Content -LiteralPath $cfg -Value $block -Encoding ASCII -NoNewline
}

# Phase 4 (provisional call, PLAN.md P4): the working copy plays with the GPU map and the HD
# art by default. Only added if [hd] has no map_gpu key yet, so a hand edit sticks.
$cfgText = Get-Content -Raw -LiteralPath $cfg
if ($cfgText -notmatch '(?m)^\s*map_gpu\s*=') {
    # A clean install has its own art in hd\, the engine's default. Pointing it at the data root's
    # packs would quietly test other art (2026-11-06: a night of runs on the release root's PBRify packs).
    $artLine = ''
    if (-not (Test-Path (Join-Path $work 'hd\art'))) {
        $artDir = Join-Path $paths['data_root'] 'hdpack'
        $artLine = "hd_art_dir=$artDir`$1`n"
    }
    Write-Step "Seeding [hd] renderer=d3d11, map_gpu=1$(if ($artLine) { ", hd_art_dir=$artDir" })"
    # $1 = the file's line ending's CR (or nothing), so CRLF and LF files both stay consistent.
    $cfgText = $cfgText -replace '(?m)^\[hd\][ \t]*(\r?)$', "[hd]`$1`nrenderer=d3d11`$1`nmap_gpu=1`$1`n$artLine".TrimEnd("`n")
    Set-Content -LiteralPath $cfg -Value $cfgText -Encoding ASCII -NoNewline
}

# Release track (PLAN.md R3): the engine's first-start setup ([hd] auto_screen, default 1) would
# set the working copy's screen from the desktop once. The working copy keeps its own [screen]
# settings, so it is switched off here before the game ever runs this build.
$cfgText = Get-Content -Raw -LiteralPath $cfg
if ($cfgText -notmatch '(?m)^\s*auto_screen\s*=') {
    Write-Step 'Seeding [hd] auto_screen=0 (the working copy keeps its [screen] settings)'
    $cfgText = $cfgText -replace '(?m)^\[hd\][ \t]*(\r?)$', "[hd]`$1`nauto_screen=0`$1"
    Set-Content -LiteralPath $cfg -Value $cfgText -Encoding ASCII -NoNewline
}

Write-Step 'Deploy OK'
exit 0
