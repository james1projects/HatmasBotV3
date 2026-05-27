@echo off
REM ============================================================
REM Stream Deck "going offline" trigger.
REM
REM Reverts every video that was LIVE-badged during this stream
REM by re-uploading the cached originals from
REM data/youtube_thumbnails/<id>.png.
REM
REM Idempotent — running it when nothing is currently badged
REM just prints "nothing to revert" and exits cleanly.
REM ============================================================

setlocal
pushd "%~dp0"

echo.
echo =============================
echo  YOUTUBE LIVE BADGE - REVERT
echo =============================
echo.

python tools\youtube_live_badge.py revert
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Done. Originals restored.
    timeout /t 5 >nul
) else (
    echo Errored with code %RC% — see output above. Re-run go_offline.bat to retry.
    timeout /t 20 >nul
)

popd
endlocal
exit /b %RC%
