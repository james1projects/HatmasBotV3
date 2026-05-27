@echo off
REM ============================================================
REM Stream Deck-friendly pre-stream readiness check.
REM
REM Runs ~12 checks against everything HatmasBot depends on,
REM prints a colored report, and waits long enough for you to
REM read it before closing.
REM
REM Drop the path to this .bat into a Stream Deck "System: Open"
REM button. Press it ~30 sec before going live. If the verdict
REM is green you're clear; if anything's red, fix the listed
REM hint and re-press.
REM
REM Exit codes (only matters if you wire this into automation):
REM   0 = all checks passed (or only WARN)
REM   1 = at least one FAIL — do not stream
REM   2 = readiness checker itself errored (rare)
REM ============================================================

setlocal
pushd "%~dp0"

echo.
echo ====================================
echo    HATMASBOT STREAM READINESS
echo ====================================
echo.

python tools\check_stream_ready.py
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Pause for 8 seconds so you can read the report...
    timeout /t 8 >nul
) else (
    REM On FAIL or ERROR, give you longer to read + fix
    echo Pause for 30 seconds so you can read the report and act on hints...
    timeout /t 30 >nul
)

popd
endlocal
exit /b %RC%
