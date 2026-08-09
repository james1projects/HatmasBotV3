# ============================================================
# bot_ctl.ps1 — start / restart / stop HatmasBot from a button.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action status
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action restart
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action stop
#
# restart:
#   - Supervisor running  -> kill the main.py child; the supervisor
#     relaunches it in ~5s (its normal crash-recovery path).
#   - Nothing running     -> launch run_bot.bat in a new console.
#   - Bare main.py (no supervisor) -> kill it, then launch run_bot.bat
#     so the restart comes back supervised.
# stop:
#   - Kill supervisor first (so it can't relaunch), then the bot.
#
# The kill is hard (Stop-Process -Force). That is acceptable here:
# every state write is atomic (core/atomic_io.py) and economy.db is
# SQLite WAL — the supervisor exists precisely because the bot must
# survive unexpected termination. For a fully graceful stop, type
# "quit" in the bot console instead.
# ============================================================

param(
    [ValidateSet("status", "restart", "stop")]
    [string]$Action = "status"
)

$repo = Split-Path -Parent $PSScriptRoot
$runBat = Join-Path $repo "run_bot.bat"

function Get-BotProcs {
    $all = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'py%'" |
        Where-Object { $_.Name -match '^(py|python|pythonw)(3[\d.]*)?\.exe$' })
    [pscustomobject]@{
        Supervisors = @($all | Where-Object { $_.CommandLine -like "*supervisor.py*" })
        Bots        = @($all | Where-Object { $_.CommandLine -match 'main\.py"?\s*$' })
    }
}

function Start-BotConsole {
    Write-Host "Launching run_bot.bat in a new console..."
    Start-Process -FilePath $runBat -WorkingDirectory $repo
}

$procs = Get-BotProcs

switch ($Action) {
    "status" {
        if ($procs.Supervisors.Count -gt 0) {
            Write-Host ("Supervisor running (PID {0})" -f (($procs.Supervisors | ForEach-Object ProcessId) -join ", "))
        } else {
            Write-Host "Supervisor: not running"
        }
        if ($procs.Bots.Count -gt 0) {
            Write-Host ("Bot running (PID {0})" -f (($procs.Bots | ForEach-Object ProcessId) -join ", "))
        } else {
            Write-Host "Bot: not running"
        }
    }

    "restart" {
        if ($procs.Bots.Count -eq 0 -and $procs.Supervisors.Count -eq 0) {
            Write-Host "Bot is not running."
            Start-BotConsole
            break
        }
        if ($procs.Bots.Count -gt 0) {
            foreach ($p in $procs.Bots) {
                Write-Host "Stopping bot (PID $($p.ProcessId))..."
                try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
            }
        }
        if ($procs.Supervisors.Count -gt 0) {
            Write-Host "Supervisor is running - it will relaunch the bot in ~5s."
        } else {
            Write-Host "No supervisor found - starting fresh under run_bot.bat."
            Start-BotConsole
        }
    }

    "stop" {
        if ($procs.Bots.Count -eq 0 -and $procs.Supervisors.Count -eq 0) {
            Write-Host "Nothing to stop - bot is not running."
            break
        }
        foreach ($p in $procs.Supervisors) {
            Write-Host "Stopping supervisor (PID $($p.ProcessId))..."
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
        }
        foreach ($p in $procs.Bots) {
            Write-Host "Stopping bot (PID $($p.ProcessId))..."
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
        }
        Write-Host "Stopped. (Reminder: typing 'quit' in the bot console is the graceful path.)"
    }
}
