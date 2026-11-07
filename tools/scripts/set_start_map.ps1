# Sets or clears the map a new game starts on, in the working copy's content config
# overlay (data\config\game#patch.cfg, [start] map=...). CE reads it in main.cc
# (the sfall StartingMap feature). Command-line overrides can't set it: they only
# reach fallout2.cfg, not the content config.
#
#   set_start_map.ps1 -Map arvillag.map
#   set_start_map.ps1 -Clear

param(
    [Parameter(ParameterSetName = 'Set', Mandatory = $true)][string]$Map,
    [Parameter(ParameterSetName = 'Clear', Mandatory = $true)][switch]$Clear
)

. (Join-Path $PSScriptRoot 'common.ps1')

$work = (Get-HdPaths)['working_copy'].TrimEnd('\')
$cfg = Join-Path $work 'data\config\game#patch.cfg'
$lines = if (Test-Path -LiteralPath $cfg) { @(Get-Content -LiteralPath $cfg) } else { @() }

# Drop any existing [start] map= line, remembering where [start] is.
$result = New-Object System.Collections.Generic.List[string]
$section = ''
$startIndex = -1
foreach ($line in $lines) {
    if ($line -match '^\s*\[(.+)\]\s*$') {
        $section = $Matches[1].Trim()
        $result.Add($line)
        if ($section -ieq 'start') { $startIndex = $result.Count }
        continue
    }
    if ($section -ieq 'start' -and $line -match '^\s*map\s*=') { continue }
    $result.Add($line)
}

if (-not $Clear) {
    if ($startIndex -ge 0) {
        $result.Insert($startIndex, "map=$Map")
    } else {
        $result.Add('')
        $result.Add('[start]')
        $result.Add("map=$Map")
    }
}

New-Item -ItemType Directory -Force (Split-Path -LiteralPath $cfg) | Out-Null
[IO.File]::WriteAllLines($cfg, $result.ToArray())
if ($Clear) {
    Write-Step 'New games start on the default map (artemple.map)'
} else {
    Write-Step "New games start on $Map"
}
exit 0
