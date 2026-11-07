# Launches the deployed game from the working copy, checks it starts and stays
# responsive, screenshots the main menu and closes the game (PLAN.md P0.4).
# Needs an interactive desktop. Intro movies are skipped with a command-line config
# override, which CE does not write back to fallout2.cfg.
#
# Outputs go to <data_root>\reports\smoke\<timestamp>\ (screenshots are game-derived,
# so they never go into the repo): menu.png, debug.log.

param(
    [string]$Exe = 'fallout2-ce.exe',
    [string[]]$GameArgs = @('[ui]skip_opening_movies=1'),
    [int]$StartupTimeoutSeconds = 30,
    [int]$MenuWaitSeconds = 6
)

. (Join-Path $PSScriptRoot 'common.ps1')

Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FoWin {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
}
'@

$paths = Get-HdPaths
$work = $paths['working_copy'].TrimEnd('\')
$outDir = Join-Path $paths['data_root'] ("reports\smoke\" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $outDir | Out-Null

function Save-ClientScreenshot([IntPtr]$hwnd, [string]$path) {
    $rect = New-Object FoWin+RECT
    [FoWin]::GetClientRect($hwnd, [ref]$rect) | Out-Null
    $origin = New-Object FoWin+POINT
    [FoWin]::ClientToScreen($hwnd, [ref]$origin) | Out-Null
    $w = $rect.Right - $rect.Left
    $h = $rect.Bottom - $rect.Top
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($origin.X, $origin.Y, 0, 0, (New-Object System.Drawing.Size $w, $h))
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    return "${w}x${h}"
}

Write-Step "Launching $Exe from $work"
$env:DEBUGACTIVE = 'log'
$proc = Start-Process -FilePath (Join-Path $work $Exe) -ArgumentList $GameArgs -WorkingDirectory $work -PassThru

$deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
while ($proc.MainWindowHandle -eq 0 -and -not $proc.HasExited -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    $proc.Refresh()
}
if ($proc.HasExited) { throw "Game exited during startup (exit code $($proc.ExitCode)). See $work\debug.log" }
if ($proc.MainWindowHandle -eq 0) { $proc.Kill(); throw 'No game window within the startup timeout.' }
Write-Host "    window up after $([int]((Get-Date) - $proc.StartTime).TotalSeconds)s: '$($proc.MainWindowTitle)'"

Start-Sleep -Seconds $MenuWaitSeconds

$proc.Refresh()
if ($proc.HasExited) { throw "Game exited unexpectedly (exit code $($proc.ExitCode))." }
$responding = $proc.Responding
# The screenshot copies from the screen, so the window must be on top.
[FoWin]::BringWindowToTop($proc.MainWindowHandle) | Out-Null
[FoWin]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 500
try {
    $size = Save-ClientScreenshot $proc.MainWindowHandle (Join-Path $outDir 'menu.png')
} catch {
    # A locked desktop (nobody at the machine) has no screen to copy from.
    $size = "unknown (screenshot skipped: $($_.Exception.Message.Trim()))"
}
Write-Host "    responding: $responding, client area: $size"

Write-Step 'Closing the game'
$proc.Kill()
$proc.WaitForExit(5000) | Out-Null

$log = Join-Path $work 'debug.log'
if (Test-Path $log) {
    Copy-Item $log (Join-Path $outDir 'debug.log')
    $errors = Select-String -Path $log -Pattern 'error|fail|can''t|cannot' -SimpleMatch:$false | Select-Object -First 15
    Write-Host "    debug.log: $((Get-Content $log).Count) lines, $(@($errors).Count) error-looking lines (first 15 shown)"
    $errors | ForEach-Object { Write-Host "      $($_.Line)" }
}

Write-Step "Smoke output: $outDir"
if (-not $responding) { throw 'Game window was not responding.' }
exit 0
