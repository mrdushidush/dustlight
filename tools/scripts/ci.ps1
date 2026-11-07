# Local CI (PLAN.md P0.5). One command, prints a summary table, exits 1 on any failure.
#
#   ci.ps1                    hooks, tracked-file audit, format, clean build of both
#                             configs, warnings vs baseline, upstream tests
#   ci.ps1 -Smoke             also deploy and run the smoke test (needs a desktop)
#   ci.ps1 -NoClean           incremental build (warning check is skipped: it needs a full rebuild)
#   ci.ps1 -UpdateBaseline    accept the current warnings as tools/scripts/warnings-baseline.txt

param(
    [switch]$Smoke,
    [switch]$NoClean,
    [switch]$UpdateBaseline
)

. (Join-Path $PSScriptRoot 'common.ps1')
Update-ProcessPath
$ErrorActionPreference = 'Continue'

$results = [ordered]@{}
function Invoke-Check([string]$name, [scriptblock]$body) {
    Write-Step $name
    try {
        $note = & $body
        $results[$name] = if ($note -like 'SKIPPED*') { $note } else { "PASS $note".Trim() }
    } catch {
        $results[$name] = "FAIL: $($_.Exception.Message)"
        Write-Host "    $($_.Exception.Message)" -ForegroundColor Red
    }
}

Push-Location $script:RepoRoot
try {
    Invoke-Check 'git hooks installed' {
        $hooks = git config core.hooksPath
        if ($hooks -ne 'tools/git-hooks') {
            throw "core.hooksPath is '$hooks'. Run: git config core.hooksPath tools/git-hooks"
        }
    }

    Invoke-Check 'no game data tracked' {
        # Same rules as the commit hook; read them from it so the two never drift apart.
        $hook = Get-Content -Raw 'tools/git-hooks/check-staged.sh'
        $banned = [regex]::Match($hook, "banned='([^']+)'").Groups[1].Value
        $exempt = [regex]::Match($hook, "exempt='([^']+)'").Groups[1].Value
        if (-not $banned -or -not $exempt) { throw 'Could not read the rules from check-staged.sh' }

        $bad = git ls-files | Where-Object { $_ -notmatch $exempt -and $_ -match $banned }
        $big = git ls-tree -r -l HEAD | ForEach-Object {
            $parts = $_ -split '\s+', 5
            if ($parts[3] -ne '-' -and [long]$parts[3] -gt 5MB -and $parts[4] -notmatch $exempt) { $parts[4] }
        }
        $all = @($bad) + @($big) | Where-Object { $_ }
        if ($all.Count -gt 0) { throw "tracked game data or large files: $($all -join ', ')" }
    }

    Invoke-Check 'clang-format 18' {
        $cf = Get-Command clang-format -ErrorAction SilentlyContinue
        if (-not $cf) { throw 'clang-format not found (uv tool install clang-format==18.1.8)' }
        $version = & $cf.Source --version
        if ($version -notmatch 'version 18\.') { throw "upstream formats with clang-format 18, found: $version" }
        $files = git ls-files 'src/*.cc' 'src/*.h'
        $out = & $cf.Source --dry-run --Werror $files 2>&1
        if ($LASTEXITCODE -ne 0) {
            $offenders = $out | Select-String -Pattern '^(.+?):\d+:\d+: error' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique
            throw "not formatted: $($offenders -join ', ')"
        }
        "($(@($files).Count) files)"
    }

    $buildOk = $false
    Invoke-Check 'build RelWithDebInfo + Debug' {
        $buildArgs = @{ Config = @('RelWithDebInfo', 'Debug') }
        if (-not $NoClean) { $buildArgs['Clean'] = $true }
        & (Join-Path $PSScriptRoot 'build.ps1') @buildArgs
        if (-not $?) { throw 'build.ps1 failed' }
        $script:buildOk = $true
    }

    Invoke-Check 'compiler warnings vs baseline' {
        if ($NoClean) { return 'SKIPPED (needs a clean build)' }
        if (-not $script:buildOk) { throw 'build failed' }
        $current = Get-Content 'out/warnings-RelWithDebInfo.txt', 'out/warnings-Debug.txt' | Where-Object { $_ } | Sort-Object -Unique
        $baselinePath = 'tools/scripts/warnings-baseline.txt'
        if ($UpdateBaseline) {
            [IO.File]::WriteAllLines((Join-Path $script:RepoRoot $baselinePath), [string[]]@($current))
            return "(baseline updated: $(@($current).Count) warnings)"
        }
        $baseline = Get-Content $baselinePath | Where-Object { $_ -and -not $_.StartsWith('#') }
        $new = $current | Where-Object { $baseline -notcontains $_ }
        $gone = $baseline | Where-Object { $current -notcontains $_ }
        if ($new) { throw "new warnings: $($new -join '; ')" }
        if ($gone) { return "($(@($gone).Count) baseline warnings no longer occur; consider -UpdateBaseline)" }
        "($(@($current).Count) known warnings)"
    }

    Invoke-Check 'upstream test: script serialization' {
        # The test compiles with GCC/Clang flags (-fsanitize=address,undefined); MSVC can't run it.
        # GitHub's Windows machines have such a compiler, but the compiled test can't start there
        # (exit 0xC0000135, a missing DLL), which says nothing about the code it tests.
        if ($env:GITHUB_ACTIONS) { return 'SKIPPED (the compiled test cannot start on GitHub Windows machines)' }
        $cxx = @('clang++', 'g++') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
        if (-not $cxx) { return 'SKIPPED (needs clang++ or g++; not installed)' }
        $env:CXX = $cxx
        python tests/script_serialization_test.py
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
    }

    if ($Smoke) {
        Invoke-Check 'deploy + smoke test' {
            & (Join-Path $PSScriptRoot 'deploy.ps1')
            if (-not $?) { throw 'deploy.ps1 failed' }
            & (Join-Path $PSScriptRoot 'smoke.ps1')
            if (-not $?) { throw 'smoke.ps1 failed' }
        }
    }
} finally {
    Pop-Location
}

Write-Host ''
Write-Host 'CI summary' -ForegroundColor Cyan
$failed = $false
foreach ($k in $results.Keys) {
    $v = $results[$k]
    $color = if ($v.StartsWith('PASS')) { 'Green' } elseif ($v.StartsWith('SKIPPED')) { 'Yellow' } else { $failed = $true; 'Red' }
    Write-Host ('  {0,-36} {1}' -f $k, $v) -ForegroundColor $color
}
if ($failed) { exit 1 }
exit 0
