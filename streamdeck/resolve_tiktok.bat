@echo off
rem Stream Deck launcher for resolve_tiktok.py (vertical TikTok build).
rem Point a Stream Deck "System > Open" action at this file.
"C:\Users\james\AppData\Local\Programs\Python\Python314\python.exe" "C:\Projects\HatmasBot\tools\resolve_tiktok.py" %*
if errorlevel 1 (
    echo.
    echo TikTok build failed - see the error above.
    pause
)
