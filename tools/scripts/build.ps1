# Configures and builds fallout2-ce plus ce.dat (PLAN.md P0.3).
#
#   build.ps1                         RelWithDebInfo
#   build.ps1 -Config Debug,RelWithDebInfo
#   build.ps1 -Clean                  delete the build tree first (re-downloads SDL2/zlib)
#
# Writes the unique compiler warnings of each config to out/warnings-<config>.txt
# ("path code" per line, no line numbers) so ci.ps1 can compare them to the baseline.
# Incremental builds only report warnings for recompiled files.

param(
    [ValidateSet('RelWithDebInfo', 'Debug', 'Release')]
    [string[]]$Config = @('RelWithDebInfo'),
    [switch]$Clean
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath

$buildDir = Join-Path $script:RepoRoot 'out\build\windows-x64'
$distDir = Join-Path $script:RepoRoot 'out\dist'

if ($Clean -and (Test-Path $buildDir)) {
    Write-Step "Removing $buildDir"
    Remove-Item -Recurse -Force $buildDir
}

Push-Location $script:RepoRoot
try {
    if (-not (Test-Path (Join-Path $buildDir 'CMakeCache.txt'))) {
        Write-Step 'Configuring (preset windows-x64)'
        $configureLog = Invoke-Native { cmake --preset windows-x64 }
        if ($LASTEXITCODE -ne 0) {
            $configureLog | Select-Object -Last 30 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
            throw "CMake configure failed ($LASTEXITCODE)"
        }
    }

    foreach ($c in $Config) {
        Write-Step "Building $c"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $log = Invoke-Native { cmake --build $buildDir --config $c --parallel -- /nologo /v:minimal }
        $exit = $LASTEXITCODE
        $logPath = Join-Path $script:RepoRoot "out\build-$c.log"
        $log | Set-Content -Encoding UTF8 $logPath
        if ($exit -ne 0) {
            $log | Select-String -Pattern ': (fatal )?error ' | Select-Object -First 20 | ForEach-Object { Write-Host $_.Line -ForegroundColor Red }
            throw "Build $c failed ($exit). Full log: $logPath"
        }

        $prefix = [regex]::Escape($script:RepoRoot + '\')
        $warnings = $log |
            Select-String -Pattern '^(?<file>.+?)\(\d+(,\d+)?\): warning (?<code>C\d+)' |
            ForEach-Object { ('{0} {1}' -f ($_.Matches[0].Groups['file'].Value -replace "^$prefix", '' -replace '\\', '/'), $_.Matches[0].Groups['code'].Value) } |
            Sort-Object -Unique
        # Only a full rebuild reports every warning; ci.ps1 builds with -Clean.
        [IO.File]::WriteAllLines((Join-Path $script:RepoRoot "out\warnings-$c.txt"), [string[]]@($warnings))
        Write-Host ('    done in {0}s, {1} unique warnings' -f [int]$sw.Elapsed.TotalSeconds, @($warnings).Count)
    }

    Write-Step 'Creating ce.dat'
    New-Item -ItemType Directory -Force $distDir | Out-Null
    $datTool = Join-Path $buildDir "$($Config[0])\ce-dat-tool.exe"
    Invoke-Native { & $datTool create (Join-Path $script:RepoRoot 'files\ce.dat') (Join-Path $distDir 'ce.dat') } | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ce-dat-tool create failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}

Write-Step 'Build OK'
exit 0
