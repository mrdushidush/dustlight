# P7b.6 UI test: the tour's UI walk on its own (tour.ps1 -WalkOnly), into a run folder of its
# own: per screen HD vs original stills, coverage per window, replay / HD / text self-tests.
# Frame times with the HD UI on come from the tour's bench views (bench-*-arcaves). Opens the
# game, so it runs only while the PC is locked (it waits for a lock), like the tour.
#
#   ui_test.ps1                 run now in this console (waits for a lock)
#   ui_test.ps1 -Detach         start hidden; log <data_root>\reports\tour\<run>\tour.log
#   ui_test.ps1 -Passes hd-talk,orig-talk   only those walk passes
#   then: fo2hd report7 --tour-run <run>   (the UI section reads <run>\ui_walk.json)

param(
    [string]$Run = '',
    [string]$Passes = '',
    [switch]$Detach
)

. (Join-Path $PSScriptRoot 'common.ps1')

if ($Run -eq '') { $Run = 'ui-' + (Get-Date -Format 'yyyyMMdd-HHmm') }
$paths = Get-HdPaths
$pidFile = Join-Path $paths['data_root'].TrimEnd('\') 'reports\tour\tour.pid'
if ((Test-Path $pidFile) -and (Get-Process -Id ([int](Get-Content $pidFile -Raw)) -ErrorAction SilentlyContinue)) {
    # One game at a time on the working copy: config, logs and saves are shared.
    throw "A tour is running (pid $(Get-Content $pidFile -Raw)); stop it first (tour.ps1 -Stop)."
}
$tour = Join-Path $PSScriptRoot 'tour.ps1'
$walkArgs = @{ Run = $Run; WalkOnly = $true }
if ($Passes -ne '') { $walkArgs.WalkPasses = $Passes }
if ($Detach) { & $tour @walkArgs -Detach } else { & $tour @walkArgs }
