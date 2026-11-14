# Register (or remove) the logon autostart task for the dashboard server.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#
# The task itself lives in the Windows Task Scheduler, not in this repo, so a
# fresh clone (or a new machine) needs to run this once. Re-running is safe:
# -Force overwrites the previous definition.
#
# Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI when the
# file has no BOM, and mis-decoded bytes can break parsing.

param(
    [switch]$Remove,
    [string]$TaskName = "DeepSeek Trading Terminal Dashboard",
    [int]$DelaySeconds = 30
)

$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $PSScriptRoot
$user    = "$env:USERDOMAIN\$env:USERNAME"
$launcher = Join-Path $root "scripts\autostart-dashboard.ps1"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) { "task not found: $TaskName"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "removed: $TaskName"
    exit 0
}

if (-not (Test-Path $launcher)) { "missing launcher: $launcher"; exit 1 }

$act = New-ScheduledTaskAction -Execute "powershell.exe" `
       -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcher + '"') `
       -WorkingDirectory $root

$trg = New-ScheduledTaskTrigger -AtLogOn -User $user
# Give the shell, network and disk a moment after logon: server.py reads a dozen
# JSON/CSV files and builds the wind/news views on startup.
$trg.Delay = "PT${DelaySeconds}S"

# IgnoreNew: a second instance would silently bind 8766 and hold stale data.
# ExecutionTimeLimit zero: the server is meant to run all day, not time out.
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

$prn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set `
    -Principal $prn -Force `
    -Description "Logon autostart for the local dashboard (http://127.0.0.1:8765/). Skips if already running." | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
"registered: " + $t.TaskName
"  trigger : " + ($t.Triggers | ForEach-Object { "AtLogOn " + $_.UserId + " delay=" + $_.Delay })
"  action  : " + ($t.Actions  | ForEach-Object { $_.Execute + " " + $_.Arguments })
