@echo off
REM ============================================================
REM Stream Deck-friendly launcher for the Thumbnail Studio.
REM
REM Starts tools\thumbnail_studio.py (aiohttp on 127.0.0.1:8071
REM by default, falls through to the next free port up to 8089)
REM and opens the studio in your default browser. Build YouTube
REM thumbnails interactively: pick gods + items from icon
REM dropdowns, render, then save-and-open in Paint.NET for final
REM touch-ups.
REM
REM Drop this file's path into a Stream Deck "System: Open"
REM button. The console window stays open while the server runs;
REM press Ctrl+C in it (or close the window) to stop.
REM
REM CLI flags pass straight through (e.g. --port 9000, --no-open).
REM ============================================================

setlocal
pushd "%~dp0"

echo.
echo ====================================
echo    HATMASBOT THUMBNAIL STUDIO
echo ====================================
echo.
echo Starting server. Ctrl+C here (or close the window) to stop.
echo.

python tools\thumbnail_studio.py %*
set "RC=%ERRORLEVEL%"

popd
endlocal
exit /b %RC%
