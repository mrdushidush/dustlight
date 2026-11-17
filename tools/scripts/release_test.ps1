# Clean-install test of the release zips (PLAN.md R6). Everything comes from hd.release.ini
# ($env:FO2HD_INI is set here): working_copy = the test folder (a fresh copy of the pristine
# install), data_root = the release root with its dist folder.
#
#   release_test.ps1 -Install [-Tier 3]   fresh copy of the pristine game, then only the release
#                                        zips extracted into it (engine + one tier's art), and the
#                                        save the tours load (test only; never shipped)
#   release_test.ps1 -FirstStart         one start with only what a player has: checks the first-
#                                        start setup (screen from the desktop, scale = the installed
#                                        tier, auto_screen=0 written back), the art store and the
#                                        renderer; only while the PC is locked unless -NotLocked
#
# Then the capture tour on the clean install: $env:FO2HD_INI = <repo>\hd.release.ini; tour.ps1 ...
#
#   release_test.ps1 -Install -Ini hd.release-rp.ini -RpZip <rpu_v*.zip>   the same with the
#                                        Restoration Project installed first (its own folder):
#                                        its zip extracted, then the steps of its rpu-install.bat
#                                        done here (read, not run), then Dustlight's zips
#   release_test.ps1 -Install -Ini hd.release-ettu.ini -EtTuZip <Fallout1in2.zip> -ArtDist <0.9.2 dist>
#                    -Fo1Dist <fo1 dist>    Fallout 1 as the player README says (P8.7): a fresh copy
#                                        of Fallout 2 in working_copy's parent, Et Tu's zip into it,
#                                        Et Tu's undat.bat on Fallout 1's MASTER.DAT ([fo1] install),
#                                        then the engine zip (-Dist), Fallout 2's art (-ArtDist) and
#                                        Fallout 1's art (-Fo1Dist) into Fallout1in2; no save copied.
#                                        -FirstStart then expects the art folders "hd-fo1 | hd".

param(
    [switch]$Install,
    [switch]$FirstStart,
    [int]$Tier = 3,
    [string]$Dist = '',
    [string]$ExeName = '',  # default: the exe the engine zip brought (dustlight.exe), else fallout2-ce.exe
    [switch]$NotLocked,
    [switch]$LatestExe,  # -Install: then replace the zip's exe with the latest build (tests of new code only, never for an RC)
    [string]$Ini = 'hd.release.ini',  # machine-paths file in the repo root (hd.release-rp.ini: the RP copy)
    [string]$RpZip = '',  # -Install: the Restoration Project's release zip, installed before Dustlight
    [string]$EtTuZip = '',  # -Install: Fallout Et Tu's release zip (Fallout 1); see above
    [string]$ArtDist = '',  # Fallout 2's art zips (default: -Dist)
    [string]$Fo1Dist = ''   # Et Tu: Fallout 1's art zips (<name>-<version>-art-fo1-<N>x*.zip)
)

. (Join-Path $PSScriptRoot 'common.ps1')

$inherited = $env:FO2HD_INI
$env:FO2HD_INI = $null
$mainPaths = Get-HdPaths
$env:FO2HD_INI = $inherited
function Get-ReleaseExeName([string]$folder) {
    if (Test-Path (Join-Path $folder 'dustlight.exe')) { return 'dustlight.exe' }
    return 'fallout2-ce.exe'
}
$env:FO2HD_INI = Join-Path $script:RepoRoot $Ini
$paths = Get-HdPaths
$pristine = $paths['pristine'].TrimEnd('\')
$test = $paths['working_copy'].TrimEnd('\')
$data = $paths['data_root'].TrimEnd('\')
if ($Dist -eq '') { $Dist = Join-Path $data 'dist' }
if ($test -eq $mainPaths['working_copy'].TrimEnd('\') -or $test -eq $pristine) {
    throw "hd.release.ini's working_copy ($test) must be its own folder, not the author's working copy or the pristine install."
}
if ($ArtDist -eq '') { $ArtDist = $Dist }
# Et Tu: Fallout 2's copy is working_copy's parent (Et Tu reads ..\master.dat); never the author's Fallout 1 folder.
$root = $test
if ($EtTuZip -ne '') {
    if ((Split-Path $test -Leaf) -ne 'Fallout1in2') { throw "Et Tu: working_copy ($test) must end in \Fallout1in2." }
    if ($Fo1Dist -eq '') { throw 'Et Tu: -Fo1Dist (the Fallout 1 art zips) is needed.' }
    $root = Split-Path $test -Parent
    $fo1Test = (Get-Content (Join-Path $script:RepoRoot 'hd.fo1.ini') | Where-Object { $_ -match '^\s*working_copy\s*=' }) -replace '^\s*working_copy\s*=\s*', ''
    if ($root -eq $pristine -or $root -eq (Split-Path $fo1Test.Trim().TrimEnd('\') -Parent) -or (Test-Path (Join-Path $root 'Play-Fallout1-HD.bat'))) {
        throw "Et Tu: $root is the pristine install or the author's Fallout 1 folder."
    }
    $fo1Install = (Get-Content $env:FO2HD_INI | Where-Object { $_ -match '^\s*install\s*=' }) -replace '^\s*install\s*=\s*', ''
    $fo1Master = Join-Path $fo1Install.Trim() 'MASTER.DAT'
    if (-not (Test-Path $fo1Master)) { throw "Et Tu: no Fallout 1 MASTER.DAT at $fo1Master ([fo1] install in $env:FO2HD_INI)." }
}

function Test-Locked { return $NotLocked -or (Test-SessionLocked) }

if ($Install) {
    if (Get-Process -Name 'fallout2-ce', 'dustlight' -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($test, [StringComparison]::OrdinalIgnoreCase) }) {
        throw "The game is running from $test."
    }
    Write-Step "Fresh copy of $pristine -> $root"
    Invoke-Native { robocopy.exe $pristine $root /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 } | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    if ($EtTuZip -ne '') {
        # Et Tu's README: unpack into the Fallout 2 folder, then undat Fallout 1's MASTER.DAT into Fallout1in2.
        Write-Step "Fallout Et Tu: extracting $(Split-Path $EtTuZip -Leaf)"
        Invoke-Native { tar.exe -xf $EtTuZip -C $root } | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "tar failed on $EtTuZip ($LASTEXITCODE)" }
        if (-not (Test-Path (Join-Path $test 'undat.bat'))) { throw "No $test\undat.bat after extracting Et Tu." }
        Write-Step "Et Tu's undat.bat on $fo1Master"
        $bat = Join-Path $test 'undat.bat'
        $undat = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"`"$bat`" `"$fo1Master`"`"" -WorkingDirectory $test -NoNewWindow -Wait -PassThru
        if ($undat.ExitCode -ne 0) { throw "undat.bat failed ($($undat.ExitCode))" }
    }
    if ($RpZip -ne '') {
        Write-Step "Restoration Project: extracting $(Split-Path $RpZip -Leaf)"
        Invoke-Native { tar.exe -xf $RpZip -C $test } | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "tar failed on $RpZip ($LASTEXITCODE)" }
        # rpu-install.bat (v2.4.34), step by step: old patches and data to a backup, keeping saves,
        # text, the .gcd files and music; mods_order.txt into mods\; the sfall script into data\scripts.
        $backup = Join-Path $test 'backup\rpu'
        New-Item -ItemType Directory -Force $backup | Out-Null
        foreach ($f in 'patch000.dat', 'f2_res.dat') { if (Test-Path (Join-Path $test $f)) { Move-Item (Join-Path $test $f) $backup -Force } }
        Move-Item (Join-Path $test 'mods_order.txt') (Join-Path $test 'mods\mods_order.txt') -Force
        Move-Item (Join-Path $test 'data') (Join-Path $backup 'data') -Force
        New-Item -ItemType Directory -Force (Join-Path $test 'data\sound'), (Join-Path $test 'data\scripts') | Out-Null
        foreach ($keep in 'savegame', 'sound\music') {
            $from = Join-Path $backup "data\$keep"
            if (Test-Path $from) { Move-Item $from (Join-Path $test "data\$keep") -Force }
        }
        Get-ChildItem (Join-Path $backup 'data') -File | Where-Object { $_.Extension -in '.txt', '.gcd' } | Move-Item -Destination (Join-Path $test 'data') -Force
        Move-Item (Join-Path $test 'gl_ammomod.int') (Join-Path $test 'data\scripts\gl_ammomod.int') -Force
        Remove-Item (Join-Path $test 'rpu-install.*') -ErrorAction SilentlyContinue
        Write-Step 'Restoration Project installed (rpu-install.bat steps)'
    }
    $engine = @(Get-ChildItem $Dist -Filter '*-engine.zip')
    $art = @(Get-ChildItem $ArtDist -Filter "*-art-$($Tier)x*.zip")
    if ($engine.Count -ne 1 -or $art.Count -lt 1) { throw "Need one engine zip in $Dist and the $($Tier)x art zip(s) in $ArtDist." }
    $zips = $engine + $art
    if ($EtTuZip -ne '') {
        $fo1Art = @(Get-ChildItem $Fo1Dist -Filter "*-art-fo1-$($Tier)x*.zip")
        if ($fo1Art.Count -lt 1) { throw "No Fallout 1 $($Tier)x art zip(s) in $Fo1Dist." }
        $zips += $fo1Art
    }
    foreach ($zip in $zips) {
        Write-Step "Extracting $($zip.Name)"
        # Windows' own tar (bsdtar) reads zip64 archives quickly.
        Invoke-Native { tar.exe -xf $zip.FullName -C $test } | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "tar failed on $($zip.Name) ($LASTEXITCODE)" }
    }
    # The tours load slot 1 (the author's campaign save); a test-only copy.
    $save = Join-Path $mainPaths['working_copy'] 'data\savegame\SLOT01'
    if ($EtTuZip -eq '' -and (Test-Path $save)) {
        $target = Join-Path $test 'data\savegame\SLOT01'
        New-Item -ItemType Directory -Force $target | Out-Null
        Copy-Item (Join-Path $save '*') $target -Recurse -Force
        Write-Step 'Copied the save slot 1 the tours load (test only)'
    }
    $markers = @(Join-Path $test "hd\pack_$($Tier)x.json")
    if ($EtTuZip -ne '') { $markers += Join-Path $test "hd-fo1\pack_$($Tier)x.json" }
    foreach ($marker in $markers) { if (-not (Test-Path $marker)) { throw "No $marker after extracting." } }
    if ($ExeName -eq '') { $ExeName = Get-ReleaseExeName $test }
    if ($LatestExe) {
        $built = Join-Path $script:RepoRoot 'out\build\windows-x64\RelWithDebInfo\fallout2-ce.exe'
        Copy-Item $built (Join-Path $test $ExeName) -Force
        Write-Step "Replaced $ExeName with the latest build (test of new code, not the zip's exe)"
    }
    if ($ExeName -ne 'fallout2-ce.exe') {
        # Test only: the capture scripts (map_test, tour) start fallout2-ce.exe; a copy of the
        # release exe under that name lets them run on the clean install unchanged.
        Copy-Item (Join-Path $test $ExeName) (Join-Path $test 'fallout2-ce.exe') -Force
        Write-Step "Test-only copy: $ExeName -> fallout2-ce.exe (for map_test/tour)"
    }
    Write-Step "Installed: $(($zips | ForEach-Object Name) -join ', ')"
}

if ($FirstStart) {
    if ($ExeName -eq '') { $ExeName = Get-ReleaseExeName $test }
    $exe = Join-Path $test $ExeName
    if (-not (Test-Path $exe)) { throw "No $exe; run -Install first." }
    if (-not (Test-Locked)) { throw 'The PC is not locked (the game window would open on the author''s desktop); lock it or pass -NotLocked.' }
    $cfg = Join-Path $test 'fallout2.cfg'
    $before = if (Test-Path $cfg) { Get-Content -Raw $cfg } else { '' }
    # The installed f2_res.ini (the pristine one, or the Restoration Project's own) must come
    # through the first start unchanged: compared with itself before the run, not with pristine.
    $resIni = Join-Path $test 'f2_res.ini'
    $resBefore = if (Test-Path $resIni) { (Get-FileHash $resIni -Algorithm SHA256).Hash } else { 'missing' }
    $env:DEBUGACTIVE = 'log'
    Remove-Item (Join-Path $test 'debug.log') -ErrorAction SilentlyContinue
    # Only what a player has; the one test-only key ends the run (it works with [hd] enabled=0).
    $proc = Start-Process -FilePath $exe -ArgumentList @('"[hd_debug]quit_after_ms=25000"') -WorkingDirectory $test -PassThru
    if (-not $proc.WaitForExit(90000)) { $proc.Kill(); throw 'The game did not quit within 90 s.' }
    $log = Get-Content (Join-Path $test 'debug.log')
    $after = Get-Content -Raw $cfg
    $results = [ordered]@{}
    $first = $log | Where-Object { $_ -match '^HD: first start: ' } | Select-Object -First 1
    $results['first start ran'] = [bool]$first
    $results["scale = installed tier ($($Tier)x)"] = [bool]($first -match "scale $Tier \(matches the installed HD art")
    $results['auto_screen=0 written'] = $after -match '(?m)^auto_screen=0'
    $results['borderless written'] = $after -match '(?m)^windowed=2'
    # Et Tu with the Fallout 1 art zip: both folders, Fallout 1's first, with no setting (first_run.cc hdArtDirs).
    $etTu = @(Get-ChildItem (Join-Path $test 'hd-fo1') -Filter 'pack_*x.json' -ErrorAction SilentlyContinue).Count -gt 0
    if ($etTu) {
        $results['art store up: hd-fo1 | hd'] = [bool]($log | Where-Object { $_ -match '^HD: art store up: hd-fo1 \| hd,' })
        # Et Tu's own settings (Fallout 2's data one folder up) must come through the first start.
        foreach ($key in 'master_dat', 'critter_dat') {
            $was = [regex]::Match($before, "(?m)^$key=.*$").Value
            $results["Et Tu's $key kept"] = $was -ne '' -and $after -match "(?m)^$([regex]::Escape($was))\s*$"
        }
    } else {
        $results['art store up'] = [bool]($log | Where-Object { $_ -match '^HD: art store up: hd,' })
    }
    $results['D3D11 presenter'] = [bool]($log | Where-Object { $_ -match 'HD: D3D11 presenter up: ' })
    $results['no shader or layer failures'] = -not ($log | Where-Object { $_ -match 'HD: shader |GPU map layer failed|lighting failed|post effects failed' })
    $resAfter = if (Test-Path $resIni) { (Get-FileHash $resIni -Algorithm SHA256).Hash } else { 'missing' }
    $results['f2_res.ini left alone'] = $resAfter -eq $resBefore
    Write-Host ''
    Write-Host "first start: $first"
    foreach ($k in $results.Keys) { Write-Host ('  {0,-42} {1}' -f $k, $(if ($results[$k]) { 'PASS' } else { 'FAIL' })) }
    Copy-Item (Join-Path $test 'debug.log') (Join-Path $data 'first_start.debug.log') -Force
    Set-Content -LiteralPath (Join-Path $data 'first_start.cfg.before') -Value $before -Encoding ascii
    if ($results.Values -contains $false) { exit 1 }
}
