# ============================================================
# install_bot_task.ps1 — register HatmasBot to start at logon.
#
# Creates a Task Scheduler entry that launches run_bot.bat (the
# crash-restarting supervisor) in a visible console window every
# time you log on. Run once:
#
#   powershell -ExecutionPolicy Bypass -File tools\install_bot_task.ps1
#
# Undo:
#   Unregister-ScheduledTask -TaskName "HatmasBot" -Confirm:$false
#
# Note: the trigger is at LOGON, not boot — the bot needs your
# interactive session (OBS websocket, game window) anyway. If the
# PC reboots to the lock screen, the bot starts after you sign in.
# ============================================================

$repo = Split-Path -Parent $PSScriptRoot   # tools\ -> repo root
$bat = Join-Path $repo "run_bot.bat"
$taskName = "HatmasBot"

if (-not (Test-Path $bat)) {
    Write-Error "run_bot.bat not found at $bat"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit Zero matters: the Task Scheduler default kills
# tasks after 72 hours, which would stop the bot mid-week.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $trigger -Settings $settings -Force

Write-Host ""
Write-Host "Scheduled task '$taskName' installed (starts at logon)."
Write-Host "Start it now without logging off:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Or just run run_bot.bat directly."
