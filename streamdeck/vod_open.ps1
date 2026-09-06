# streamdeck\vod_open.ps1 -- open an Ask the VOD page in the default browser.
#
# Called by vod_review.bat / vod_search.bat (Stream Deck buttons). Picks the
# server that is actually up:
#   1. the bot's public server on localhost:8070 (serves /vod and /vod/review
#      to the local browser whether or not the web_vod toggle is on);
#   2. otherwise the standalone dev host (tools\vod_devserver.py) on
#      localhost:8078, started here once if nothing is listening, then left
#      running quietly.
#
#   powershell -NoProfile -File streamdeck\vod_open.ps1 -Page /vod/review
param(
    [string]$Page = "/vod/review",
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$python = "C:\Users\james\AppData\Local\Programs\Python\Python314\python.exe"

function Test-Port([int]$port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $port)
        $c.Close()
        return $true
    } catch { return $false }
}

if (Test-Port 8070) {
    Start-Process ("http://localhost:8070" + $Page)
    exit 0
}

if (-not (Test-Port 8078)) {
    Start-Process -FilePath $python `
        -ArgumentList "tools\vod_devserver.py", "--no-open", "--port", "8078" `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(25)
    while (-not (Test-Port 8078) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
}

if (Test-Port 8078) {
    Start-Process ("http://localhost:8078" + $Page)
    exit 0
}

Write-Host "The VOD dev host did not start. Run this by hand to see the error:"
Write-Host "  $python tools\vod_devserver.py"
Start-Sleep -Seconds 8
exit 1
