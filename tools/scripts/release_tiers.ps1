# The zips' other art tiers in the game (PLAN.md R6), after the full run on the main tier: each
# gets a clean install, the first start, the GPU map self-tests at its real scale, the UI walk
# and eight busy views. Game windows open only while the PC is locked. Nothing is published.
#
#   release_tiers.ps1 -Dist <dist folder> [-Tiers 4,2] [-MainTier 3]
#
# The screen decides what a first start can show: on this 1440 px screen, a 4x-only install
# finds no scale that fits, so "scale = installed tier" is expected to fail there. That result is
# what a player who picked too big a pack gets. The rest of the run plays 4x at scale 4 in a
# 3440x1920 window, taller than the screen.
# Results: <release data root>\reports\release_checks-tier<N>, tours tier<N>-<dist folder name>.
# Each tier reinstalls the clean copy, so this runs last.

param(
    [Parameter(Mandatory = $true)][string]$Dist,
    [string]$Tiers = '4,2',
    [int]$MainTier = 3
)

. (Join-Path $PSScriptRoot 'common.ps1')

$env:FO2HD_INI = Join-Path $script:RepoRoot 'hd.release.ini'
$data = (Get-HdPaths)['data_root'].TrimEnd('\')
$checks = Join-Path $data 'reports\release_checks'
$version = Split-Path $Dist -Leaf
$views = 'arvillag_e0_0', 'newr2_e0_0', 'vctycocl_e0_1', 'denbus1_e0_2', 'ncr2_e0_1', 'modgard_e0_2', 'sftanker_e1_2', 'vault13_e0_1'

# Each run below rewrites the checks' folder: keep the main tier's results first.
$mainKeep = "$checks-tier$MainTier"
if (Test-Path $checks) {
    Remove-Item $mainKeep -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item $checks $mainKeep -Recurse
}

foreach ($tier in @($Tiers -split ',' | ForEach-Object { [int]$_ } | Where-Object { $_ -ne $MainTier })) {
    Write-Step "Tier $tier"
    & (Join-Path $PSScriptRoot 'release_checks.ps1') -Tier $tier -ZipExe -Dist $Dist -Steps 'firststart', 'hw'
    $keep = "$checks-tier$tier"
    Remove-Item $keep -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item $checks $keep -Recurse

    $height = [math]::Max(1440, 480 * $tier)
    $extra = @('"[screen]resolution_x=3440"', "`"[screen]resolution_y=$height`"", "`"[screen]scale=$tier`"")
    $run = "tier$tier-$version"
    & (Join-Path $PSScriptRoot 'tour.ps1') -Run $run -WalkOnly -WalkPasses 'hd,hd-talk,hd-world' -Extra $extra
    & (Join-Path $PSScriptRoot 'tour.ps1') -Run $run -NoWalk -Only $views -Extra $extra
}
Write-Step 'Other tiers done'
