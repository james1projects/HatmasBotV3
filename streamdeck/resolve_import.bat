@echo off
rem Stream Deck launcher for resolve_import.py.
rem Point a Stream Deck "System > Open" action at this file.
"C:\Users\james\AppData\Local\Programs\Python\Python314\python.exe" "C:\Projects\HatmasBot\tools\resolve_import.py" %*
if errorlevel 1 (
    echo.
    echo Import failed - see the error above.
    pause
)
