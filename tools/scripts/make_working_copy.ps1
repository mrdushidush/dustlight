# Creates the game working copy from the pristine install and verifies it (PLAN.md P0.2).
#
# - Never writes to the pristine folder, and proves it by comparing a file snapshot
#   (path, size, timestamp) taken before and after.
# - If the working copy already exists, only verifies hashes (saves are never touched).
# - Writes hd.local.manifest.json with the SHA-256 of the key data files.
# - Creates the data_root folder layout.

param(
    # Copy missing files into an existing working copy (never overwrites existing files).
    [switch]$Repair
)

. (Join-Path $PSScriptRoot 'common.ps1')

$paths = Get-HdPaths
$pristine = $paths['pristine'].TrimEnd('\')
$work = $paths['working_copy'].TrimEnd('\')
$dataRoot = $paths['data_root'].TrimEnd('\')
$keyFiles = 'master.dat', 'critter.dat', 'patch000.dat', 'fallout2.exe'

foreach ($file in $keyFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $pristine $file))) {
        throw "Pristine install looks wrong: $file not found in $pristine"
    }
}

Write-Step "Snapshot of pristine install: $pristine"
$before = Get-FolderSnapshot $pristine

$exists = Test-Path -LiteralPath $work
if (-not $exists -or $Repair) {
    if ($exists) {
        Write-Step "Copying missing files into $work"
    } else {
        Write-Step "Copying to $work"
    }
    # /XC /XN /XO: never overwrite files that already exist in the destination.
    robocopy $pristine $work /E /COPY:DAT /DCOPY:T /XC /XN /XO /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    # robocopy: 0-7 are success codes (1 = files copied), 8+ are failures.
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
    $global:LASTEXITCODE = 0
} else {
    Write-Step "Working copy exists; verifying only (use -Repair to add missing files)"
}

Write-Step 'Hashing key files'
$hashes = [ordered]@{}
$mismatch = $false
foreach ($file in $keyFiles) {
    $p = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $pristine $file)).Hash
    $w = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $work $file)).Hash
    $hashes[$file] = $p
    $state = if ($p -eq $w) { 'ok' } else { $mismatch = $true; 'DIFFERS' }
    Write-Host ('    {0,-14} {1}  {2}' -f $file, $p.Substring(0, 16), $state)
}

Write-Step 'Checking pristine install is unchanged'
$after = Get-FolderSnapshot $pristine
$diff = Compare-Object $before $after
if ($diff) {
    $diff | Format-Table -AutoSize | Out-String | Write-Host
    throw 'Pristine install changed while this script ran.'
}
Write-Host "    $($before.Count) files, unchanged"

$manifest = [ordered]@{
    written = (Get-Date).ToString('s')
    pristine = $pristine
    working_copy = $work
    sha256 = $hashes
}
$manifest | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $script:RepoRoot 'hd.local.manifest.json')

Write-Step "Data root layout: $dataRoot"
foreach ($dir in 'captures', 'cache', 'extract', 'hd', 'voice', 'reports') {
    New-Item -ItemType Directory -Force (Join-Path $dataRoot $dir) | Out-Null
}

if ($mismatch) {
    throw 'Working copy data files differ from the pristine install.'
}
Write-Step 'Working copy OK'
exit 0
