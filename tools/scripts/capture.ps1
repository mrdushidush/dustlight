# Scripted map captures (PLAN.md P1.3): starts the deployed game once per scene in
# [hd_debug] capture mode, which loads the map, sets time/camera, captures and quits.
# Then runs the replay test on the new captures.
#
#   capture.ps1                       the benchmark scenes (tools/scripts/scenes.json)
#   capture.ps1 -Scene arroyo_day     one scene by name
#   capture.ps1 -NoReplay             skip the replay test
#
# Captures go to <data_root>\captures\<scene>.json/.png. Works with a locked desktop:
# nothing here reads the screen or sends input.

param(
    [string[]]$Scene = @(),
    [string]$ScenesFile = (Join-Path $PSScriptRoot 'scenes.json'),
    [string]$Exe = 'fallout2-ce.exe',
    [int]$TimeoutSeconds = 120,
    [switch]$NoReplay
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$captureDir = Join-Path $paths['data_root'] 'captures'
New-Item -ItemType Directory -Force $captureDir | Out-Null

$scenes = Get-Content -Raw $ScenesFile | ConvertFrom-Json
if ($Scene.Count -gt 0) {
    $scenes = @($scenes | Where-Object { $Scene -contains $_.name })
    if ($scenes.Count -eq 0) { throw "No scene named $($Scene -join ', ') in $ScenesFile" }
}

$written = @()
foreach ($s in $scenes) {
    Write-Step "Capturing $($s.name): $($s.map)"
    $gameArgs = @(
        '"[ui]skip_opening_movies=2"',
        "`"[hd]capture_dir=$captureDir`"",
        "`"[hd_debug]capture_map=$($s.map)`"",
        "`"[hd_debug]capture_name=$($s.name)`""
    )
    foreach ($key in 'elevation', 'tile', 'dude_tile', 'time', 'settle_ms') {
        $value = $s.$key
        if ($null -ne $value) { $gameArgs += "`"[hd_debug]capture_$key=$value`"" }
    }

    $json = Join-Path $captureDir "$($s.name).json"
    Remove-Item -ErrorAction SilentlyContinue $json, ([IO.Path]::ChangeExtension($json, '.png'))

    $env:DEBUGACTIVE = 'log'
    $proc = Start-Process -FilePath (Join-Path $work $Exe) -ArgumentList $gameArgs -WorkingDirectory $work -PassThru
    if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
        $proc.Kill()
        throw "$($s.name): game did not finish within $TimeoutSeconds s"
    }
    if ($proc.ExitCode -ne 0) { throw "$($s.name): game exited with $($proc.ExitCode); see $work\debug.log" }
    if (-not (Test-Path $json)) { throw "$($s.name): no capture written; see $work\debug.log" }
    Copy-Item (Join-Path $work 'debug.log') (Join-Path $captureDir "$($s.name).debug.log") -Force
    Write-Host "    $json ($([int]((Get-Item $json).Length / 1KB)) KB)"
    $written += $json
}

if (-not $NoReplay) {
    Write-Step 'Replay test'
    Push-Location (Join-Path $script:RepoRoot 'tools\pipeline')
    try {
        $out = Invoke-Native { uv run --quiet fo2hd replay @written }
        $out | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { throw 'replay test failed' }
    } finally {
        Pop-Location
    }
}

Write-Step "Captures OK: $captureDir"
exit 0
