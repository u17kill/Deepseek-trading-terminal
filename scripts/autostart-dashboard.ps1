# Silent launcher for the dashboard server (called by Task Scheduler at logon).
# Double-clicking it also works; it refuses to start a second instance.
#
# Why this exists, as opposed to start-dashboard.bat:
#   1) No console window and no browser tab at logon -- nobody is watching at 09:00.
#   2) It probes port 8765 first. server.py walks 8765..8776 for a free port, so
#      without the probe a second instance would quietly land on 8766, holding a
#      stale in-memory copy of the data -- you would then hit refresh on the old
#      process and wonder why the numbers never change.
#   3) Console output goes to data/server.stdout.log (covered by the /data/* ignore
#      rule). Business-level logging is server.py's own data/server.log.
#
# Why python.exe and not pythonw.exe:
#   Under pythonw, sys.stdout is None and server.py calls print() during startup,
#   which raises AttributeError and kills the process before it binds a port.
#   python.exe plus CreateNoWindow is the combination that actually works.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI when
# there is no BOM, so non-ASCII comments get mis-decoded and can break parsing.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-Port([int]$Port){
    $c = New-Object Net.Sockets.TcpClient
    try   { $c.Connect("127.0.0.1", $Port); return $true }
    catch { return $false }
    finally { try { $c.Dispose() } catch {} }
}

$out = Join-Path $root "data\server.stdout.log"

# UTF8 without BOM: Out-File -Encoding utf8 on 5.1 prepends a BOM, which then
# shows up as a stray character on the first line of the log.
function Write-LauncherLog([string]$Msg){
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [autostart] $Msg`r`n"
    [System.IO.File]::AppendAllText($out, $line, (New-Object System.Text.UTF8Encoding($false)))
}

# Resolve an interpreter that can actually run this project. Two traps here:
#   - PATH order puts the Microsoft Store alias (WindowsApps\python.exe, a 0-byte
#     forwarding stub) first, and it answers every call with "Python was not found";
#   - the plain system install (Python 3.12) has no numpy/pandas, so it imports
#     fine but dies on `import indicator_core`. Being "a python" is not enough.
# So: drop the stub, then require `import numpy, pandas` to succeed. The probe
# also covers the versioned interpreter that ships with WorkBuddy, which is the
# only one on this machine with the scientific stack installed.
function Resolve-Python {
    $cands = @()
    Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $cands += $_.Source }
    $cands += @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    # Versioned private interpreters, newest first: path changes on every upgrade,
    # so glob it rather than hard-coding a version number.
    $pattern = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions\*\python.exe"
    $cands += @(Get-ChildItem $pattern -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending |
                ForEach-Object { $_.FullName })

    $probe = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # a failed probe is not an error, it is a verdict
    try {
        foreach ($c in ($cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)) {
            if ($c -like "*\WindowsApps\*") { continue }
            $null = & $c -c "import numpy, pandas" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { return $c }
        }
    } finally {
        $ErrorActionPreference = $probe
    }
    return $null
}

# Already running: do nothing, exit 0 so Task Scheduler does not report a failure.
if (Test-Port 8765) { exit 0 }

$py = Resolve-Python
if (-not $py) {
    Write-LauncherLog "no usable python.exe found, dashboard not started"
    exit 1
}

Write-LauncherLog ("starting " + $py)

# Launch through scripts\serve-daemon.py, which redirects its own stdout/stderr
# to the log and then runs server.py via runpy. Going through cmd for the
# redirection instead would mean fighting cmd's quote rules for no benefit.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName               = $py
$psi.Arguments              = '"scripts\serve-daemon.py"'
$psi.WorkingDirectory       = $root
$psi.UseShellExecute        = $false
$psi.CreateNoWindow         = $true
$psi.WindowStyle            = "Hidden"
[System.Diagnostics.Process]::Start($psi) | Out-Null

exit 0
