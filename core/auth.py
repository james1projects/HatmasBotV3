"""
Twitch OAuth Token Generator
=============================
Run this standalone to generate OAuth tokens with the correct scopes.
Opens a browser, handles the callback, saves the token.

Usage:
  python -m core.auth                — Generate bot token (HatmasBot account)
  python -m core.auth --broadcaster  — Generate broadcaster token (your channel account)
"""

import sys
import http.server
import webbrowser
import urllib.parse
import json
from pathlib import Path

from core.config import (
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET,
    TWITCH_SCOPES, TWITCH_BROADCASTER_SCOPES, BASE_DIR
)

TOKEN_DIR = BASE_DIR / "data"
BOT_TOKEN_FILE = TOKEN_DIR / "twitch_token.json"
BROADCASTER_TOKEN_FILE = TOKEN_DIR / "twitch_broadcaster_token.json"
REDIRECT_URI = "http://localhost:3000/callback"


def generate_auth_url(scopes):
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def save_token(token_data, token_file):
    token_file.parent.mkdir(exist_ok=True)
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)
    print(f"Token saved to {token_file}")


def load_token(token_file):
    if token_file.exists():
        with open(token_file) as f:
            return json.load(f)
    return None


def get_valid_token(token_file=BOT_TOKEN_FILE):
    """Get a valid token, refreshing if necessary."""
    token_data = load_token(token_file)
    if not token_data:
        return None

    import urllib.request
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token_data['access_token']}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return token_data
    except Exception:
        if "refresh_token" in token_data:
            try:
                new_data = refresh_token(token_data["refresh_token"])
                save_token(new_data, token_file)
                return new_data
            except Exception as e:
                print(f"Refresh failed: {e}")
        return None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code = None
    account_label = "HatmasBot"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            label = CallbackHandler.account_label
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <html><body style="background:#0a0b12;color:#c4a882;
                font-family:serif;display:flex;align-items:center;
                justify-content:center;height:100vh;font-size:24px;">
                <div>{label} authenticated successfully. You can close this tab.</div>
                </body></html>
            """.encode())
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authentication failed.")

    def log_message(self, format, *args):
        pass


def run_auth_flow(is_broadcaster=False):
    if is_broadcaster:
        label = "Broadcaster (your channel account)"
        scopes = TWITCH_BROADCASTER_SCOPES
        token_file = BROADCASTER_TOKEN_FILE
        CallbackHandler.account_label = "Broadcaster"
    else:
        label = "Bot (HatmasBot account)"
        scopes = TWITCH_SCOPES
        token_file = BOT_TOKEN_FILE
        CallbackHandler.account_label = "HatmasBot"

    print("=" * 50)
    print(f"  HatmasBot OAuth Token Generator")
    print(f"  Mode: {label}")
    print("=" * 50)
    print()
    print(f"Scopes: {', '.join(scopes)}")
    print()

    if is_broadcaster:
        print("IMPORTANT: Log in as your CHANNEL account (e.g., Hatmaster)")
        print("           NOT your bot account (HatmasBot)")
        print()

    auth_url = generate_auth_url(scopes)
    print("Opening browser for authentication...")
    webbrowser.open(auth_url)

    CallbackHandler.auth_code = None
    server = http.server.HTTPServer(("localhost", 3000), CallbackHandler)
    print("Waiting for callback...")
    server.handle_request()

    if CallbackHandler.auth_code:
        print("Exchanging code for token...")
        token_data = exchange_code_for_token(CallbackHandler.auth_code)
        save_token(token_data, token_file)
        print()
        print("Authentication complete!")
        print(f"Access token:  {token_data['access_token'][:20]}...")
        if "refresh_token" in token_data:
            print(f"Refresh token: {token_data['refresh_token'][:20]}...")
        print()
        print("Copy these into your config_local.py:")
        if is_broadcaster:
            print(f'  TWITCH_BROADCASTER_TOKEN = "{token_data["access_token"]}"')
            if "refresh_token" in token_data:
                print(f'  TWITCH_BROADCASTER_REFRESH_TOKEN = "{token_data["refresh_token"]}"')
        else:
            print(f'  TWITCH_BOT_TOKEN = "{token_data["access_token"]}"')
            if "refresh_token" in token_data:
                print(f'  TWITCH_BOT_REFRESH_TOKEN = "{token_data["refresh_token"]}"')
        return token_data
    else:
        print("Authentication failed - no code received")
        return None


if __name__ == "__main__":
    is_broadcaster = "--broadcaster" in sys.argv
    run_auth_flow(is_broadcaster=is_broadcaster)
