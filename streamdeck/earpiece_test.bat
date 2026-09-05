@echo off
REM ============================================================================
REM earpiece_test.bat  (Stream Deck: EARPIECE)
REM
REM Speaks a short line through the co-caster's private earpiece channel
REM (COCASTER_EAR_DEVICE in core/config.py, default "Headphones") so the
REM audio routing can be checked before a stream. Works even while the
REM "cocaster" feature toggle is off. Needs the bot running (dashboard on
REM localhost:8069).
REM
REM Pass a word to run a REAL chat summary instead of the canned line:
REM     earpiece_test.bat now
REM ============================================================================
setlocal
set URL=http://localhost:8069/api/cocaster/test
if /I "%~1"=="now" set URL=%URL%?now=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing '%URL%' -TimeoutSec 30).Content } catch { 'earpiece test failed: ' + $_.Exception.Message }"
endlocal
