@echo off
REM ============================================================================
REM bingo_end.bat  (Stream Deck: BINGO END)
REM
REM Closes the open Stream Bingo round with no winner (the round closes by
REM itself the moment someone hits bingo). Needs the bot running.
REM ============================================================================
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Method Post 'http://localhost:8069/api/bingo/end' -TimeoutSec 15).Content } catch { 'bingo end failed: ' + $_.Exception.Message }"
