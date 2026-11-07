# Shared helpers for tools/scripts/*.ps1 (Windows PowerShell 5.1 compatible).

$ErrorActionPreference = 'Stop'

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

# Tools installed by winget/uv may not be on this process's PATH yet.
function Update-ProcessPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
        [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Reads hd.local.ini into a hashtable of sections -> hashtable of key/value.
# $env:FO2HD_INI points every script at another one (PLAN.md R6: the release's clean-install
# copy and data root, hd.release.ini), without touching the author's working copy.
function Read-HdConfig {
    $path = if ($env:FO2HD_INI) { $env:FO2HD_INI } else { Join-Path $script:RepoRoot 'hd.local.ini' }
    if (-not (Test-Path $path)) {
        throw "Missing $path. Copy hd.local.ini.example to hd.local.ini and set the paths."
    }

    $config = @{}
    $section = ''
    foreach ($line in Get-Content $path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith(';') -or $trimmed.StartsWith('#')) {
            continue
        }
        if ($trimmed -match '^\[(.+)\]$') {
            $section = $Matches[1]
            $config[$section] = @{}
            continue
        }
        $eq = $trimmed.IndexOf('=')
        if ($eq -gt 0 -and $section -ne '') {
            $config[$section][$trimmed.Substring(0, $eq).Trim()] = $trimmed.Substring($eq + 1).Trim()
        }
    }
    return $config
}

function Get-HdPaths {
    $paths = (Read-HdConfig)['paths']
    foreach ($key in 'pristine', 'working_copy', 'data_root') {
        if (-not $paths -or -not $paths[$key]) {
            throw "hd.local.ini: [paths] $key is not set."
        }
    }
    return $paths
}

# File list with sizes and timestamps, used to prove a folder was not modified.
function Get-FolderSnapshot([string]$root) {
    Get-ChildItem -LiteralPath $root -Recurse -File -Force |
        ForEach-Object { '{0}|{1}|{2}' -f $_.FullName.Substring($root.Length), $_.Length, $_.LastWriteTimeUtc.Ticks } |
        Sort-Object
}

# Runs a native command and returns its output (stdout and stderr) as strings.
# Windows PowerShell turns native stderr into error records when output is redirected,
# and 'Stop' would make harmless warnings fatal; callers check $LASTEXITCODE instead.
function Invoke-Native([scriptblock]$command) {
    $ErrorActionPreference = 'Continue'
    & $command 2>&1 | ForEach-Object { "$_" }
}

function Write-Step([string]$message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

# Starts a script as a hidden PowerShell process that doesn't belong to the caller (created
# through WMI, so it isn't in the calling shell's job and outlives it); returns its pid.
# p7_batch.ps1 / tour.ps1 -Detach.
function Start-Detached([string]$script, [string[]]$arguments = @()) {
    $quoted = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script) + $arguments | ForEach-Object {
        if ("$_" -match '[\s"]') { '"' + ("$_" -replace '"', '\"') + '"' } else { "$_" } }
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = 'powershell.exe ' + ($quoted -join ' '); CurrentDirectory = (Split-Path $script); ProcessStartupInformation = $startup }
    if ($result.ReturnValue -ne 0) { throw "Win32_Process.Create failed ($($result.ReturnValue))" }
    return [int]$result.ProcessId
}
