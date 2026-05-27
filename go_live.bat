@echo off
REM ============================================================
REM Stream Deck "going live" trigger.
REM
REM Applies the LIVE NOW badge to your last 8 YouTube thumbnails.
REM Cached originals live in data/youtube_thumbnails/<id>.png so
REM revert is instant once we've badged a video once.
REM
REM First run after `pip install google-auth-oauthlib
REM google-api-python-client` will pop a browser for OAuth consent
REM (one-time). Subsequent runs are headless.
REM
REM Pair with go_offline.bat at end of stream to revert.
REM ============================================================

setlocal
pushd "%~dp0"

echo.
echo ============================
echo  YOUTUBE LIVE BADGE - APPLY
echo ============================
echo.

python tools\youtube_live_badge.py apply
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Done. LIVE badges applied. Press go_offline.bat at end of stream.
    timeout /t 5 >nul
) else (
    echo Errored with code %RC% — see output above.
    timeout /t 20 >nul
)

popd
endlocal
exit /b %RC%
