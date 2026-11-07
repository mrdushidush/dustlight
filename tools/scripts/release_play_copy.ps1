# A folder to play a release candidate in, as a player would have it (PLAN.md R8): a copy of an
# untouched game folder plus only the zips. It isn't started, so the first run is a true first
# start, and it holds no test save or test exe. The game folder is only read.
#
#   release_play_copy.ps1 -Pristine <game folder> -Out <new folder> -Dist <dist folder> [-Tier 3]

param(
    [Parameter(Mandatory = $true)][string]$Pristine,
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][string]$Dist,
    [int]$Tier = 3
)

. (Join-Path $PSScriptRoot 'common.ps1')

if (Test-Path $Out) { throw "$Out exists: choose a new folder (nothing is overwritten)." }
if (-not (Test-Path (Join-Path $Pristine 'master.dat'))) { throw "No master.dat in $Pristine." }
$zips = @(Get-ChildItem $Dist -Filter '*-engine.zip') + @(Get-ChildItem $Dist -Filter "*-art-$($Tier)x*.zip")
if ($zips.Count -lt 2) { throw "No engine zip and $($Tier)x art zip(s) in $Dist." }

Write-Step "Copying $Pristine -> $Out"
Invoke-Native { robocopy.exe $Pristine $Out /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 } | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
foreach ($zip in $zips) {
    Write-Step "Extracting $($zip.Name)"
    Invoke-Native { tar.exe -xf $zip.FullName -C $Out } | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "tar failed on $($zip.Name) ($LASTEXITCODE)" }
}
$exe = Join-Path $Out 'dustlight.exe'
if (-not (Test-Path (Join-Path $Out "hd\pack_$($Tier)x.json"))) { throw "No art of tier $Tier after extracting." }
if (-not (Test-Path $exe)) { throw "No $exe after extracting." }
Write-Step "Ready: $exe (sha256 $((Get-FileHash $exe -Algorithm SHA256).Hash.Substring(0, 16).ToLower()))"
