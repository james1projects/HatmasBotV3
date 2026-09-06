@echo off
REM ============================================================================
REM bingo_call.bat <square_id>   (Stream Deck: one key per square you call)
REM
REM Marks a manual bingo square on every viewer card via the dashboard:
REM     bingo_call.bat no_mana
REM     bingo_call.bat blames_jungle
REM Square ids are the "id" fields in data\bingo\pool.json (the dashboard's
REM /bingo page lists them). Needs the bot running (localhost:8069).
REM Other bingo buttons: bingo_start.bat, bingo_end.bat.
REM ============================================================================
setlocal
if "%~1"=="" (
    echo usage: bingo_call.bat ^<square_id^>
    timeout /t 4 /nobreak > nul
    exit /b 2
)
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Method Post 'http://localhost:8069/api/bingo/fire?event=%~1' -TimeoutSec 15).Content } catch { 'bingo call failed: ' + $_.Exception.Message }"
endlocal
