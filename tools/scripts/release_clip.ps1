# The store page's short clip (PLAN.md R7): the scenes in clip_scenes.json (fire, neon, screens,
# water, slime; each with the 1920 px wide part of the screen where they are), 1080p cut pixel
# for pixel at the 3x art tier, one MP4 without the hex cursor. On the clean install and
# release art of hd.release.ini; game windows only while the PC is locked. Game-derived:
# local until the author publishes it.
#
#   release_clip.ps1                  -> <release data root>\reports\release-media\clip.mp4
#   release_clip.ps1 -Scene den_fire -Seconds 3      a short test

param(
    [string[]]$Scene = @(),
    [int]$Seconds = 5
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$env:FO2HD_INI = Join-Path $script:RepoRoot 'hd.release.ini'
$data = (Get-HdPaths)['data_root'].TrimEnd('\')
# The art installed from the zips, so the clip shows what ships, wherever the zips got it from.
$work = (Get-HdPaths)['working_copy'].TrimEnd('\')
$installedArt = Join-Path $work 'hd'
if (-not (Test-Path (Join-Path $installedArt 'art'))) { throw "No installed art in $installedArt." }
$seqRoot = Join-Path $data 'reports\phase6\seq'
$outDir = Join-Path $data 'reports\release-media'
New-Item -ItemType Directory -Force $outDir | Out-Null
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'ffmpeg not found.' }

# The game runs at 60 fps; every second frame gives a 30 fps clip. The sequence stays in memory
# until it ends (6 MB a frame here), so what it shows keeps the game's real speed.
$frames = $Seconds * 30
$scenesFile = Join-Path $PSScriptRoot 'clip_scenes.json'
$names = @($Scene | ForEach-Object { $_ -split ',' } | Where-Object { $_ })

$parts = @()
foreach ($s in (Get-Content -Raw $scenesFile | ConvertFrom-Json)) {
    $name = $s.name
    if ($names.Count -gt 0 -and $names -notcontains $name) { continue }
    while (-not (Get-Process LogonUI -ErrorAction SilentlyContinue)) { Start-Sleep -Seconds 10 }
    # [hd] enabled=1: a clean install doesn't take the developer hotkeys, and Ctrl+F4 starts a sequence.
    & (Join-Path $PSScriptRoot 'phase6_capture.ps1') -Scene $name -States 'hd' -Seq -SeqCount $frames -SeqStep 2 -SeqRect "$($s.x),31,1920,1080" `
        -ScenesFile $scenesFile -ArtDir $installedArt -Extra @('"[hd]enabled=1"', '"[hd_debug]capture_no_cursor=1"')
    $seq = Join-Path $seqRoot "$name-hd"
    if (-not (Test-Path $seq)) { throw "${name}: no frame sequence came out (see still6-$name.debug.log)" }
    $pngs = @(Get-ChildItem $seq -Filter '*.png' | Sort-Object Name)
    if ($pngs.Count -lt $frames) { throw "${name}: $($pngs.Count) frames of $frames" }
    $list = Join-Path $outDir "clip-$name.txt"
    Set-Content -LiteralPath $list -Encoding ascii -Value @($pngs | ForEach-Object { "file '$($_.FullName -replace '\\', '/')'" })
    $part = Join-Path $outDir "clip-$name.mp4"
    Invoke-Native { ffmpeg -y -hide_banner -loglevel error -r 30 -f concat -safe 0 -i $list -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -r 30 $part } | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "${name}: ffmpeg failed ($LASTEXITCODE)" }
    $parts += $part
    Remove-Item -LiteralPath $list
    # About 1 GB of PNGs per scene: the MP4 is what stays.
    Remove-Item -LiteralPath $seq -Recurse -Force
}
if ($parts.Count -eq 0) { throw 'No scene to capture.' }

$joined = Join-Path $outDir 'clip-parts.txt'
Set-Content -LiteralPath $joined -Encoding ascii -Value @($parts | ForEach-Object { "file '$($_ -replace '\\', '/')'" })
$clip = Join-Path $outDir 'clip.mp4'
Invoke-Native { ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i $joined -c copy -movflags +faststart $clip } | Out-Null
if ($LASTEXITCODE -ne 0) { throw "joining the clip failed ($LASTEXITCODE)" }
Remove-Item -LiteralPath $joined
$parts | ForEach-Object { Remove-Item -LiteralPath $_ }
Write-Step "Clip: $clip ($([math]::Round((Get-Item $clip).Length / 1MB, 1)) MB, $($parts.Count) scenes)"
