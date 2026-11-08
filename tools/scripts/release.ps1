# Builds what players download (PLAN.md R5): the engine zip and the per-tier art zips, from
# the art in -HdPack (default: the hdpack of the release data root in hd.release.ini).
#
#   release.ps1 -Credits docs\player\art_credits_ultrasharpv2.json -HdPack <data_root>\hdpack
#                                            (name, version: src/hd/release_info.h)
#   release.ps1 ... -Tiers 2 -SkipBuild      one tier, the exe as last built
#   release.ps1 ... -DocsOnly                only the filled docs, in <release data root>\reports\player-docs
#   release.ps1 ... -Tiers none -Field 'size_2x=about 1.5 GB',...   engine-only update (art of an earlier release)
#
# Output: <release data root>\dist\<Version>\ (zips, nexus.md, SHA256SUMS.txt, release.json) and
# the exe's pdb in <release data root>\symbols\<Version>\ for crash reports (never shipped).
# The build refuses art from a model the credits file doesn't name.

param(
    [string]$Name = '',     # default: HD_RELEASE_NAME in src/hd/release_info.h
    [string]$Version = '',  # default: HD_RELEASE_VERSION there
    [Parameter(Mandatory = $true)][string]$Credits,
    [string]$HdPack = '',   # default: <release data root>\hdpack
    [string]$ExeName = '',
    [string]$Tiers = '2,3,4',
    [string]$SourceUrl = '(to be published)',
    [switch]$SkipBuild,
    [switch]$DocsOnly,
    # More {key} values for the docs, e.g. an engine-only update (-Tiers none) states the
    # earlier release's pack sizes: -Field 'size_2x=about 1.5 GB','size_3x=about 3.2 GB'
    [string[]]$Field = @()
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$env:FO2HD_INI = Join-Path $script:RepoRoot 'hd.release.ini'
$info = Get-Content -Raw (Join-Path $script:RepoRoot 'src\hd\release_info.h')
if ($Name -eq '') { $Name = [regex]::Match($info, '#define HD_RELEASE_NAME "([^"]+)"').Groups[1].Value }
if ($Version -eq '') { $Version = [regex]::Match($info, '#define HD_RELEASE_VERSION "([^"]+)"').Groups[1].Value }
if ($Name -eq '' -or $Version -eq '') { throw 'No name/version (src/hd/release_info.h).' }
$numbers = [regex]::Match($info, '#define HD_RELEASE_VERSION_NUMBERS ([0-9, ]+)').Groups[1].Value -replace ' ', ''
$fromText = [regex]::Match($Version, '^(\d+)\.(\d+)\.(\d+)')
if ($numbers -ne "$($fromText.Groups[1].Value),$($fromText.Groups[2].Value),$($fromText.Groups[3].Value),0") {
    throw "HD_RELEASE_VERSION_NUMBERS ($numbers) doesn't match $Version (src/hd/release_info.h)."
}
$data = (Get-HdPaths)['data_root'].TrimEnd('\')
if ($HdPack -eq '') { $HdPack = Join-Path $data 'hdpack' }
if (-not (Test-Path (Join-Path $HdPack 'art'))) { throw "No art in $HdPack." }
if ($ExeName -eq '') { $ExeName = "$($Name.ToLower()).exe" }
$Credits = (Resolve-Path $Credits).Path
$out = if ($DocsOnly) { Join-Path $data 'reports\player-docs' } else { Join-Path $data "dist\$Version" }
if ((Test-Path $out) -and -not $DocsOnly) { throw "$out exists; a version is built once (bump -Version)." }

if (-not $SkipBuild -and -not $DocsOnly) {
    & (Join-Path $PSScriptRoot 'build.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'build failed' }
}
$bin = Join-Path $script:RepoRoot 'out\build\windows-x64\RelWithDebInfo'
$exe = Join-Path $bin 'fallout2-ce.exe'
$ceDat = Join-Path $script:RepoRoot 'out\dist\ce.dat'

$r = $script:RepoRoot
$docs = @(
    "$r\docs\player\README.md=$Name-README.txt",
    "$r\LICENSE.md=hd/docs/LICENSE.md",
    "$r\NOTICE.md=hd/docs/NOTICE.md",
    "$r\docs\player\THIRD_PARTY.md=hd/docs/THIRD_PARTY.md",
    "$r\docs\player\CHANGELOG.md=hd/docs/CHANGELOG.md"
)
foreach ($lic in Get-ChildItem (Join-Path $r 'third_party') -Recurse -Depth 1 -File | Where-Object { $_.Name -match '^LICENSE' }) {
    $docs += "$($lic.FullName)=hd/docs/licenses/$($lic.Directory.Name).txt"
}
$docArgs = @(); foreach ($d in $docs) { $docArgs += @('--doc', $d) }
$fields = @('--field', "known_issues=@$r\docs\player\known_issues.md", '--field', "source_url=$SourceUrl")
foreach ($f in $Field) { $fields += @('--field', $f) }
$extra = @('--art-credits', $Credits, '--page', "$r\docs\player\nexus.md")
if ($DocsOnly) { $extra += '--docs-only' }

Push-Location (Join-Path $r 'tools\pipeline')
try {
    Invoke-Native { & uv run fo2hd release --name $Name --version $Version --exe $exe --exe-name $ExeName --ce-dat $ceDat `
        --hdpack $HdPack --out $out --tiers $Tiers @extra @docArgs @fields } |
        Where-Object { $_ -notmatch 'meshgrid|UserWarning' } | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "fo2hd release failed ($LASTEXITCODE)" }
} finally { Pop-Location }
if ($DocsOnly) { Write-Step "Docs -> $out"; return }

$symbols = Join-Path $data "symbols\$Version"
New-Item -ItemType Directory -Force $symbols | Out-Null
Copy-Item $exe, (Join-Path $bin 'fallout2-ce.pdb') $symbols
Write-Step "Release $Name $Version -> $out (pdb kept in $symbols)"
