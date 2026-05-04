"""
Spotify OAuth Token Generator
===============================
Run standalone to authenticate with Spotify.

Usage: python spotify_auth.py
"""

import http.server
import webbrowser
import urllib.parse
import urllib.request
import json
import base64
from pathlib import Path

from core.config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI, SPOTIFY_SCOPES, DATA_DIR
)

TOKEN_FILE = DATA_DIR / "spotify_token.json"


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="background:#0a0b12;color:#c4a882;
                font-family:serif;display:flex;align-items:center;
                justify-content:center;height:100vh;font-size:24px;">
                <div>Spotify connected to HatmasBot! You can close this tab.</div>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            error = params.get("error", ["Unknown error"])[0]
            self.wfile.write(f"Authentication failed: {error}".encode())

    def log_message(self, format, *args):
        pass


def run():
    print("=" * 50)
    print("  HatmasBot Spotify Authentication")
    print("=" * 50)
    print()

    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": " ".join(SPOTIFY_SCOPES),
        "show_dialog": "true",
    }
    auth_url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"

    print("Opening browser for Spotify authentication...")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler)
    print("Waiting for callback...")
    server.handle_request()

    if not CallbackHandler.auth_code:
        print("Authentication failed - no code received")
        return

    print("Exchanging code for token...")

    auth_str = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": CallbackHandler.auth_code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={"Authorization": f"Basic {auth_str}"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode())

        import time
        save_data = {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expiry": time.time() + token_data.get("expires_in", 3600) - 60,
        }

        TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump(save_data, f, indent=2)

        print()
        print("Spotify authentication complete!")
        print(f"Token saved to {TOKEN_FILE}")
        print(f"Access token: {token_data['access_token'][:20]}...")
        if token_data.get("refresh_token"):
            print("Refresh token saved (will auto-refresh)")

    except Exception as e:
        print(f"Token exchange failed: {e}")


if __name__ == "__main__":
    run()
