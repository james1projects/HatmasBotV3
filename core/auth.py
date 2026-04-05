"""
Twitch OAuth Token Generator
=============================
Run this standalone to generate OAuth tokens with the correct scopes.
Opens a browser, handles the callback, saves the token.

Usage: python -m core.auth
"""

import http.server
import webbrowser
import urllib.parse
import json
from pathlib import Path

from core.config import (
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET,
    TWITCH_SCOPES, BASE_DIR
)

TOKEN_FILE = BASE_DIR / "data" / "twitch_token.json"
REDIRECT_URI = "http://localhost:3000/callback"


def generate_auth_url():
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(TWITCH_SCOPES),
    }
    return f"https://id.twitch.tv/oauth2/authorize?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code):
    import urllib.request
    data = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=data,
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def refresh_token(refresh_tok):
    import urllib.request
    data = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
    }).encode()

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=data,
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def save_token(token_data):
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    print(f"Token saved to {TOKEN_FILE}")


def load_token():
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


def get_valid_token():
    """Get a valid token, refreshing if necessary."""
    token_data = load_token()
    if not token_data:
        return None

    # Try to validate the token
    import urllib.request
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token_data['access_token']}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return token_data
    except Exception:
        # Token expired, try refresh
        if "refresh_token" in token_data:
            try:
                new_data = refresh_token(token_data["refresh_token"])
                save_token(new_data)
                return new_data
            except Exception as e:
                print(f"Refresh failed: {e}")
        return None


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
                <div>HatmasBot authenticated successfully. You can close this tab.</div>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authentication failed.")

    def log_message(self, format, *args):
        pass  # Suppress server logs


def run_auth_flow():
    print("=" * 50)
    print("  HatmasBot OAuth Token Generator")
    print("=" * 50)
    print()
    print(f"Scopes: {', '.join(TWITCH_SCOPES)}")
    print()

    auth_url = generate_auth_url()
    print("Opening browser for authentication...")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", 3000), CallbackHandler)
    print("Waiting for callback...")
    server.handle_request()

    if CallbackHandler.auth_code:
        print("Exchanging code for token...")
        token_data = exchange_code_for_token(CallbackHandler.auth_code)
        save_token(token_data)
        print()
        print("Authentication complete!")
        print(f"Access token: {token_data['access_token'][:20]}...")
        if "refresh_token" in token_data:
            print("Refresh token saved (will auto-refresh)")
        return token_data
    else:
        print("Authentication failed - no code received")
        return None


if __name__ == "__main__":
    run_auth_flow()
