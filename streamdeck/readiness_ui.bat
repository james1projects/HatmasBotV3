@echo off
REM ============================================================
REM Stream Deck: open the HatmasBot Readiness panel.
REM
REM Starts tools\readiness_ui.py (standalone server, works even
REM when the bot is down) and opens it as an app window. Every
REM non-green check gets a fix button next to it: start the bot,
REM launch OBS / MixItUp / SMITE 2, restart cloudflared, re-auth
REM tokens, open the recordings folder.
REM
REM The console window stays open while the panel runs; close it
REM (or Ctrl+C) to stop the server. The old console report is
REM still available as check_stream.bat.
REM ============================================================

setlocal
pushd "%~dp0.."

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py tools\readiness_ui.py %*
) else (
    python tools\readiness_ui.py %*
)

popd
endlocal
