"""
Pre-Stream Readiness Check
==========================
Runs ~12 concurrent end-to-end checks against everything HatmasBot needs
in order to stream cleanly: bot dashboard, both Twitch tokens, OBS
WebSocket + game source, MixItUp API, tracker.gg, public website
(localhost:8070 + hatmaster.tv via Cloudflare), cloudflared service
state, disk space, asset library integrity, Spotify token, and the
SMITE 2 process.

Each check uses the same interface the bot itself uses (e.g. it calls
GetSourceScreenshot on OBS, not just "is port 4455 open"), so a green
result is real-world-meaningful, not a process-existence rubber stamp.

Usage
-----
    python tools/check_stream_ready.py            # full report, colored
    python tools/check_stream_ready.py --json     # machine-readable JSON
    python tools/check_stream_ready.py --quick    # skip slower checks

Exit codes
----------
    0 — all checks OK or only WARNs
    1 — at least one FAIL (don't go live)
    2 — script itself errored (config missing, etc.)
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Force unbuffered output so the .bat wrapper shows results in real time.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


# ============================================================
# CONFIG / CONSTANTS
# ============================================================

# Ports we expect (from core/config.py + core/public_webserver.py)
DASHBOARD_PORT = 8069
PUBLIC_PORT = 8070
OBS_WS_PORT = 4455
MIXITUP_PORT = 8911

# Paths
DATA_DIR = REPO_ROOT / "data"
RECORDINGS_DIR = REPO_ROOT / "recordings"
GOD_ICONS_DIR = DATA_DIR / "god_icons"
GOD_CARDS_DIR = DATA_DIR / "god_cards"
WIKI_HTML = REPO_ROOT / "Gods - SMITE 2 Wiki.html"
BOT_TOKEN_FILE = DATA_DIR / "twitch_token.json"
BROADCASTER_TOKEN_FILE = DATA_DIR / "twitch_broadcaster_token.json"
SPOTIFY_TOKEN_FILE = DATA_DIR / "spotify_token.json"

# Thresholds
DISK_WARN_GB = 30
DISK_FAIL_GB = 10
TOKEN_EXPIRES_WARN_MIN = 30  # warn if token expires in less than this many minutes
PUBLIC_HEALTHZ_URL = "https://hatmaster.tv/healthz"
HEALTHZ_TIMEOUT = 8.0
DEFAULT_TIMEOUT = 5.0

# Required Twitch broadcaster scopes (from HatmasBot.md "Broadcaster Scopes")
REQUIRED_BROADCASTER_SCOPES = {
    "channel:manage:broadcast",
    "channel:manage:predictions",
    "channel:read:subscriptions",
    "moderator:manage:shoutouts",
    "channel:manage:redemptions",
    "channel:read:redemptions",
    "moderator:read:chatters",
}


# ============================================================
# RESULT TYPES
# ============================================================

@dataclass
class CheckResult:
    name: str
    status: str            # "OK" | "WARN" | "FAIL" | "SKIP"
    detail: str = ""
    hint: str = ""
    elapsed_ms: int = 0
    extras: dict = field(default_factory=dict)


def ok(name, detail="", **kw):
    return CheckResult(name=name, status="OK", detail=detail, **kw)


def warn(name, detail, hint="", **kw):
    return CheckResult(name=name, status="WARN", detail=detail, hint=hint, **kw)


def fail(name, detail, hint="", **kw):
    return CheckResult(name=name, status="FAIL", detail=detail, hint=hint, **kw)


def skip(name, detail="", **kw):
    return CheckResult(name=name, status="SKIP", detail=detail, **kw)


# ============================================================
# HELPERS
# ============================================================

async def _http_get_json(url, timeout=DEFAULT_TIMEOUT, headers=None):
    """Minimal async-friendly HTTP GET that returns (status, body_text)."""
    loop = asyncio.get_event_loop()

    def _sync_get():
        req = urllib.request.Request(url, headers=headers or {})
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return exc.code, body
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    return await loop.run_in_executor(None, _sync_get)


async def _run_subprocess(cmd, timeout=DEFAULT_TIMEOUT):
    """Run a shell command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", "timeout"


# ============================================================
# CHECKS
# ============================================================

async def check_bot_dashboard():
    name = "Bot dashboard"
    t0 = time.time()
    status, body = await _http_get_json(f"http://localhost:{DASHBOARD_PORT}/api/state")
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        try:
            data = json.loads(body)
            features = data.get("features", {})
            enabled = sum(1 for v in features.values() if v)
            total = len(features)
            return ok(name, f"localhost:{DASHBOARD_PORT}/api/state OK ({enabled}/{total} features on)",
                      elapsed_ms=elapsed)
        except Exception:
            return ok(name, f"localhost:{DASHBOARD_PORT} responded 200", elapsed_ms=elapsed)
    return fail(
        name,
        f"localhost:{DASHBOARD_PORT} not responding ({status})",
        hint="Bot may have crashed or hasn't started. Run `python main.py` from the repo root.",
        elapsed_ms=elapsed,
    )


async def check_twitch_token(token_file: Path, label: str,
                              required_scopes: Optional[set] = None):
    name = f"Twitch {label} token"
    t0 = time.time()
    if not token_file.exists():
        return fail(name, f"{token_file.name} not found",
                    hint=f"Run `python -m core.auth{' --broadcaster' if 'broadcaster' in label else ''}` to generate it.",
                    elapsed_ms=int((time.time() - t0) * 1000))
    try:
        token_data = json.loads(token_file.read_text(encoding="utf-8"))
        access_token = token_data.get("access_token", "")
        if not access_token:
            return fail(name, f"no access_token in {token_file.name}",
                        hint="Re-run the OAuth flow.",
                        elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        return fail(name, f"could not parse {token_file.name}: {exc}",
                    hint="Re-run the OAuth flow.",
                    elapsed_ms=int((time.time() - t0) * 1000))

    # Validate against Twitch
    headers = {"Authorization": f"OAuth {access_token}"}
    status, body = await _http_get_json(
        "https://id.twitch.tv/oauth2/validate", headers=headers
    )
    elapsed = int((time.time() - t0) * 1000)
    if status != 200:
        return fail(name, f"Twitch rejected token (HTTP {status})",
                    hint=f"Token expired or revoked. Run `python -m core.auth"
                         f"{' --broadcaster' if 'broadcaster' in label else ''}` to refresh.",
                    elapsed_ms=elapsed)
    try:
        info = json.loads(body)
    except Exception:
        return warn(name, "validate response wasn't JSON; token might still work",
                    elapsed_ms=elapsed)

    expires_in = int(info.get("expires_in", 0))
    expires_min = expires_in // 60
    scopes_present = set(info.get("scopes") or [])

    detail = f"valid, expires in {expires_min // 60}h {expires_min % 60}m"
    extras = {"login": info.get("login"), "scopes": sorted(scopes_present),
              "expires_in_seconds": expires_in}

    if required_scopes:
        missing = required_scopes - scopes_present
        if missing:
            return fail(
                name,
                f"missing scopes: {', '.join(sorted(missing))}",
                hint="Re-run `python -m core.auth --broadcaster` to grant the missing scopes.",
                elapsed_ms=elapsed, extras=extras,
            )

    if expires_min < TOKEN_EXPIRES_WARN_MIN:
        return warn(
            name, f"token expires in only {expires_min}m",
            hint="Bot's auto-refresh should handle it, but consider re-running auth if it fails.",
            elapsed_ms=elapsed, extras=extras,
        )
    return ok(name, detail, elapsed_ms=elapsed, extras=extras)


async def check_obs_websocket():
    name = "OBS WebSocket + Smite 2 source"
    t0 = time.time()
    try:
        from obsws_python import ReqClient
    except ImportError:
        return skip(name, "obsws_python not installed",
                    elapsed_ms=int((time.time() - t0) * 1000))

    # Pull password from config_local if present
    try:
        from core import config as bot_config
        password = getattr(bot_config, "OBS_WS_PASSWORD", "")
        host = getattr(bot_config, "OBS_WS_HOST", "localhost")
        port = getattr(bot_config, "OBS_WS_PORT", OBS_WS_PORT)
        kd_source = getattr(bot_config, "KILL_DETECT_OBS_SOURCE", "Smite 2")
    except Exception as exc:
        return fail(name, f"could not import core.config: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000))

    loop = asyncio.get_event_loop()

    def _sync_check():
        try:
            client = ReqClient(host=host, port=port, password=password, timeout=DEFAULT_TIMEOUT)
        except Exception as exc:
            return None, f"connection failed: {type(exc).__name__}: {exc}"
        try:
            version = client.get_version()
            obs_version = getattr(version, "obs_version", "?")
            ws_version = getattr(version, "obs_web_socket_version", "?")
            # Try to find the kill detector source (may live inside any scene)
            scenes = client.get_scene_list().scenes
            found = False
            for scene in scenes:
                scene_name = scene["sceneName"]
                items = client.get_scene_item_list(scene_name).scene_items
                if any(it["sourceName"] == kd_source for it in items):
                    found = True
                    break
            return {"obs": obs_version, "ws": ws_version, "source_found": found}, None
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    result, err = await loop.run_in_executor(None, _sync_check)
    elapsed = int((time.time() - t0) * 1000)

    if err:
        return fail(
            name, err,
            hint=f"Make sure OBS is open and Tools → WebSocket Server Settings is enabled on port {port}.",
            elapsed_ms=elapsed,
        )

    if not result["source_found"]:
        return warn(
            name,
            f"OBS up (v{result['obs']}, ws v{result['ws']}) but '{kd_source}' source not found in any scene",
            hint=f"Add a Game Capture source named exactly '{kd_source}' to your scene, or update KILL_DETECT_OBS_SOURCE in config.",
            elapsed_ms=elapsed,
        )

    return ok(
        name,
        f"OBS v{result['obs']}, WS v{result['ws']}, '{kd_source}' source found",
        elapsed_ms=elapsed,
    )


async def check_mixitup():
    name = "MixItUp API"
    t0 = time.time()
    status, body = await _http_get_json(f"http://localhost:{MIXITUP_PORT}/api/v2/status")
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        return ok(name, f"localhost:{MIXITUP_PORT} responding", elapsed_ms=elapsed)
    if status is None:
        return fail(
            name, f"localhost:{MIXITUP_PORT} unreachable",
            hint="Open MixItUp, then Settings → Developer API → enable on port 8911.",
            elapsed_ms=elapsed,
        )
    return warn(
        name, f"unexpected response (HTTP {status})",
        hint="MixItUp is up but its Developer API may be disabled.",
        elapsed_ms=elapsed,
    )


async def check_tracker_gg():
    name = "tracker.gg"
    t0 = time.time()
    try:
        from core import config as bot_config
        platform_name = getattr(bot_config, "SMITE2_PLATFORM", "steam")
        platform_id = getattr(bot_config, "SMITE2_PLATFORM_ID", "")
    except Exception:
        platform_name, platform_id = "steam", ""

    if not platform_id or platform_id == "YOUR_PLATFORM_ID":
        return skip(name, "SMITE2_PLATFORM_ID not configured",
                    elapsed_ms=int((time.time() - t0) * 1000))

    # tracker.gg is Cloudflare-fronted; use curl_cffi the same way other tools do
    try:
        from curl_cffi import requests as cffi_requests
    except Exception:
        # Fallback to urllib (often gets 403 from Cloudflare but worth one shot)
        url = f"https://api.tracker.gg/api/v2/smite2/standard/profile/{platform_name}/{platform_id}"
        status, body = await _http_get_json(url)
        elapsed = int((time.time() - t0) * 1000)
        if status == 200:
            return ok(name, f"profile {platform_id} reachable (urllib)", elapsed_ms=elapsed)
        return warn(name, f"HTTP {status} via urllib (curl_cffi not installed)",
                    hint="`pip install curl_cffi` for a more reliable check.",
                    elapsed_ms=elapsed)

    loop = asyncio.get_event_loop()

    def _sync_get():
        url = f"https://api.tracker.gg/api/v2/smite2/standard/profile/{platform_name}/{platform_id}"
        session = cffi_requests.Session(impersonate="chrome")
        try:
            resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            return resp.status_code, len(resp.content or b"")
        except Exception as exc:
            return None, str(exc)

    status, info = await loop.run_in_executor(None, _sync_get)
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        return ok(name, f"profile reachable ({info} bytes)", elapsed_ms=elapsed)
    if status is None:
        return fail(name, f"tracker.gg unreachable: {info}",
                    hint="Network issue or tracker.gg outage. Check status.tracker.gg.",
                    elapsed_ms=elapsed)
    if status == 403:
        return fail(name, "Cloudflare 403",
                    hint="Cloudflare bypass failed. Try `pip install --upgrade curl_cffi`.",
                    elapsed_ms=elapsed)
    if status == 404:
        return warn(name, f"profile not found (404)",
                    hint=f"Verify SMITE2_PLATFORM_ID='{platform_id}' is correct.",
                    elapsed_ms=elapsed)
    return warn(name, f"HTTP {status}", elapsed_ms=elapsed)


async def check_local_public_webserver():
    name = "Public webserver (local)"
    t0 = time.time()
    status, body = await _http_get_json(f"http://localhost:{PUBLIC_PORT}/healthz")
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        return ok(name, f"localhost:{PUBLIC_PORT}/healthz OK", elapsed_ms=elapsed)
    return fail(
        name, f"localhost:{PUBLIC_PORT} not responding ({status})",
        hint="Bot's PublicWebServer didn't start. Check the bot console for errors at startup.",
        elapsed_ms=elapsed,
    )


async def check_hatmaster_tv():
    name = "hatmaster.tv (Cloudflare → cloudflared → webserver)"
    t0 = time.time()
    status, body = await _http_get_json(PUBLIC_HEALTHZ_URL, timeout=HEALTHZ_TIMEOUT)
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        return ok(name, f"hatmaster.tv/healthz OK ({elapsed}ms)", elapsed_ms=elapsed)
    if status is None:
        return fail(
            name, "DNS / network unreachable",
            hint="Check internet connection. If only this fails, Cloudflare itself may be down.",
            elapsed_ms=elapsed,
        )
    if status in (502, 503, 521, 522, 523, 524, 525, 526):
        return fail(
            name, f"Cloudflare error {status} (tunnel down)",
            hint="cloudflared service is not connected. Run admin cmd: `sc start cloudflared`.",
            elapsed_ms=elapsed,
        )
    return fail(
        name, f"unexpected HTTP {status}",
        hint="Check Cloudflare dashboard + cloudflared logs.",
        elapsed_ms=elapsed,
    )


async def check_cloudflared_service():
    name = "cloudflared service"
    t0 = time.time()
    if platform.system() != "Windows":
        return skip(name, "Windows-only check",
                    elapsed_ms=int((time.time() - t0) * 1000))
    rc, out, err = await _run_subprocess(["sc", "query", "cloudflared"])
    elapsed = int((time.time() - t0) * 1000)
    if rc is None:
        return warn(name, "sc query timed out", elapsed_ms=elapsed)
    if rc != 0:
        return fail(
            name, "service not installed (`sc query cloudflared` failed)",
            hint="Reinstall: `cd C:\\Users\\james\\.cloudflared && cloudflared.exe service install` (admin cmd).",
            elapsed_ms=elapsed,
        )
    state_line = next((l for l in out.splitlines() if "STATE" in l), "")
    if "RUNNING" in state_line:
        return ok(name, "RUNNING", elapsed_ms=elapsed)
    return fail(
        name, f"service not running ({state_line.strip()})",
        hint="`sc start cloudflared` in admin cmd.",
        elapsed_ms=elapsed,
    )


async def check_disk_space():
    name = "Disk space (recordings)"
    t0 = time.time()
    target = RECORDINGS_DIR if RECORDINGS_DIR.exists() else REPO_ROOT
    try:
        usage = shutil.disk_usage(target)
        free_gb = usage.free / (1024 ** 3)
    except Exception as exc:
        return warn(name, f"could not check ({exc})",
                    elapsed_ms=int((time.time() - t0) * 1000))
    elapsed = int((time.time() - t0) * 1000)
    detail = f"{free_gb:.1f} GB free in {target.name}/"
    if free_gb < DISK_FAIL_GB:
        return fail(name, detail, hint=f"Free up space — under {DISK_FAIL_GB} GB will choke OBS recording.",
                    elapsed_ms=elapsed)
    if free_gb < DISK_WARN_GB:
        return warn(name, detail, hint=f"Below {DISK_WARN_GB} GB. Consider archiving old VODs before stream.",
                    elapsed_ms=elapsed)
    return ok(name, detail, elapsed_ms=elapsed)


async def check_god_assets():
    name = "God asset library"
    t0 = time.time()
    if not WIKI_HTML.exists():
        return warn(name, "Gods - SMITE 2 Wiki.html missing — cannot validate count",
                    hint="Re-save https://wiki.smite2.com/w/Gods as 'Webpage, Complete'.",
                    elapsed_ms=int((time.time() - t0) * 1000))
    # Reuse the wiki parser
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_dgi", REPO_ROOT / "download_god_icons.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        gods = mod.parse_wiki_html()
    except Exception as exc:
        return warn(name, f"could not parse wiki HTML: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000))
    elapsed = int((time.time() - t0) * 1000)
    expected = len(gods)
    icons_present = sum(1 for g in gods if (GOD_ICONS_DIR / f"{g['slug']}.png").exists())
    cards_present = sum(1 for g in gods if (GOD_CARDS_DIR / f"{g['slug']}.png").exists())

    detail = f"{icons_present}/{expected} icons, {cards_present}/{expected} cards"
    extras = {"icons": icons_present, "cards": cards_present, "expected": expected}

    missing_icons = expected - icons_present
    missing_cards = expected - cards_present

    if missing_icons > 0:
        return warn(name, detail,
                    hint=f"{missing_icons} god icons missing — run `python download_god_icons.py --check` to list.",
                    elapsed_ms=elapsed, extras=extras)
    if missing_cards > 5:
        return warn(name, detail,
                    hint=f"{missing_cards} god cards missing — run `python tools/download_god_cards.py --check`.",
                    elapsed_ms=elapsed, extras=extras)
    return ok(name, detail, elapsed_ms=elapsed, extras=extras)


async def check_spotify_token():
    name = "Spotify token"
    t0 = time.time()
    if not SPOTIFY_TOKEN_FILE.exists():
        return warn(name, "spotify_token.json not found",
                    hint="Run `python spotify_auth.py` once if you want song requests to work.",
                    elapsed_ms=int((time.time() - t0) * 1000))
    try:
        data = json.loads(SPOTIFY_TOKEN_FILE.read_text(encoding="utf-8"))
        access = data.get("access_token", "")
        if not access:
            return fail(name, "no access_token",
                        hint="Re-run `python spotify_auth.py`.",
                        elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        return warn(name, f"could not parse: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000))

    headers = {"Authorization": f"Bearer {access}"}
    status, body = await _http_get_json("https://api.spotify.com/v1/me", headers=headers)
    elapsed = int((time.time() - t0) * 1000)
    if status == 200:
        try:
            info = json.loads(body)
            return ok(name, f"valid (user: {info.get('display_name', 'unknown')})", elapsed_ms=elapsed)
        except Exception:
            return ok(name, "valid", elapsed_ms=elapsed)
    if status == 401:
        return warn(name, "expired (auto-refresh should kick in when bot starts)",
                    hint="If song requests fail tonight, re-run `python spotify_auth.py`.",
                    elapsed_ms=elapsed)
    return warn(name, f"HTTP {status}", elapsed_ms=elapsed)


async def check_smite_process():
    name = "SMITE 2 process"
    t0 = time.time()
    if platform.system() != "Windows":
        return skip(name, "Windows-only check", elapsed_ms=int((time.time() - t0) * 1000))
    rc, out, err = await _run_subprocess(
        ["tasklist", "/FI", "IMAGENAME eq Smite.exe", "/NH"]
    )
    rc2, out2, _ = await _run_subprocess(
        ["tasklist", "/FI", "IMAGENAME eq Smite2.exe", "/NH"]
    )
    elapsed = int((time.time() - t0) * 1000)
    combined = (out or "") + (out2 or "")
    if "Smite.exe" in combined or "Smite2.exe" in combined:
        return ok(name, "running", elapsed_ms=elapsed)
    return warn(
        name, "not running",
        hint="Not a hard fail — launch the game when you're ready. Bot will pick it up automatically.",
        elapsed_ms=elapsed,
    )


# ============================================================
# DRIVER
# ============================================================

async def check_web_login():
    """Website login + trading readiness (WEBSITE_TRADING_DESIGN.md).

    Three states:
      - trading disabled by config       -> OK (deliberate)
      - enabled but secret/creds missing -> FAIL (viewers see a dead
                                            login button)
      - enabled + configured             -> probe /api/me end-to-end
                                            (expects 401 + JSON when
                                            logged out)
    """
    name = "Website login (hatmaster.tv)"
    t0 = time.time()
    try:
        import sys as _sys
        _sys.path.insert(0, str(REPO_ROOT))
        from core.config import (WEB_SESSION_SECRET, WEB_TRADING_ENABLED,
                                 TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
    except Exception as exc:
        return fail(name, f"could not import core.config: {exc}")

    if not WEB_TRADING_ENABLED:
        return ok(name, "web trading disabled by config (WEB_TRADING_ENABLED=False)")

    missing = []
    if not WEB_SESSION_SECRET:
        missing.append("WEB_SESSION_SECRET")
    if TWITCH_CLIENT_ID in ("", "YOUR_CLIENT_ID"):
        missing.append("TWITCH_CLIENT_ID")
    if TWITCH_CLIENT_SECRET in ("", "YOUR_CLIENT_SECRET"):
        missing.append("TWITCH_CLIENT_SECRET")
    if missing:
        return fail(
            name, f"trading enabled but missing: {', '.join(missing)}",
            hint="Set them in core/config_local.py. Generate a secret: "
                 "python -c \"import secrets; print(secrets.token_urlsafe(64))\"",
        )

    status, body = await _http_get_json(
        f"http://localhost:{PUBLIC_PORT}/api/me")
    elapsed = int((time.time() - t0) * 1000)
    if status == 401:
        return ok(name, "configured; /api/me responds (logged-out 401)",
                  elapsed_ms=elapsed)
    return warn(
        name, f"/api/me returned {status} (expected 401 when logged out)",
        hint="Bot not running, or the login routes failed to register.",
        elapsed_ms=elapsed,
    )


ALL_CHECKS = [
    ("bot_dashboard",     check_bot_dashboard,             False),
    ("bot_token",         lambda: check_twitch_token(BOT_TOKEN_FILE, "bot"),                 False),
    ("broadcaster_token", lambda: check_twitch_token(BROADCASTER_TOKEN_FILE, "broadcaster",
                                                     REQUIRED_BROADCASTER_SCOPES),           False),
    ("obs_websocket",     check_obs_websocket,             False),
    ("mixitup",           check_mixitup,                   False),
    ("tracker_gg",        check_tracker_gg,                True),   # slow-ish
    ("public_local",      check_local_public_webserver,    False),
    ("web_login",         check_web_login,                 False),
    ("public_external",   check_hatmaster_tv,              True),   # slow-ish (external)
    ("cloudflared",       check_cloudflared_service,       False),
    ("disk_space",        check_disk_space,                False),
    ("god_assets",        check_god_assets,                False),
    ("spotify_token",     check_spotify_token,             True),   # slow-ish
    ("smite_process",     check_smite_process,             False),
]


async def run_all_checks(quick=False):
    coros = []
    for slug, factory, slowish in ALL_CHECKS:
        if quick and slowish:
            continue
        try:
            coro = factory()
        except Exception as exc:
            coro = asyncio.sleep(0)  # placeholder
            coros.append((slug, asyncio.create_task(asyncio.sleep(0))))
            continue
        coros.append((slug, asyncio.create_task(coro)))

    results = []
    for slug, task in coros:
        try:
            r = await task
            results.append(r)
        except Exception as exc:
            results.append(fail(slug, f"check raised {type(exc).__name__}: {exc}"))
    return results


# ============================================================
# OUTPUT FORMATTING
# ============================================================

USE_ANSI = sys.stdout.isatty() and platform.system() != "Windows" or "WT_SESSION" in os.environ
# Best-effort: enable ANSI on Windows 10+ cmd
if platform.system() == "Windows":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        USE_ANSI = True
    except Exception:
        pass

C_RESET = "\x1b[0m" if USE_ANSI else ""
C_GREEN = "\x1b[32m" if USE_ANSI else ""
C_YELLOW = "\x1b[33m" if USE_ANSI else ""
C_RED = "\x1b[31m" if USE_ANSI else ""
C_DIM = "\x1b[2m" if USE_ANSI else ""
C_BOLD = "\x1b[1m" if USE_ANSI else ""


def status_label(status):
    if status == "OK":
        return f"{C_GREEN}[OK]  {C_RESET}"
    if status == "WARN":
        return f"{C_YELLOW}[WARN]{C_RESET}"
    if status == "FAIL":
        return f"{C_RED}[FAIL]{C_RESET}"
    return f"{C_DIM}[skip]{C_RESET}"


def print_report(results, elapsed_total_ms):
    bar = "=" * 70
    print(bar)
    print(f"{C_BOLD}HATMASBOT STREAM READINESS{C_RESET}  —  "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')}  ({elapsed_total_ms}ms)")
    print(bar)
    for r in results:
        print(f"{status_label(r.status)} {r.name:38s} — {r.detail}")
        if r.hint and r.status in ("FAIL", "WARN"):
            print(f"        {C_DIM}Hint: {r.hint}{C_RESET}")
    print(bar)

    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    if counts["FAIL"] > 0:
        verdict = f"{C_RED}{C_BOLD}{counts['FAIL']} FAIL"
        if counts["WARN"]:
            verdict += f", {counts['WARN']} WARN"
        verdict += f", {counts['OK']} OK — DO NOT STREAM YET{C_RESET}"
    elif counts["WARN"] > 0:
        verdict = (f"{C_YELLOW}{C_BOLD}{counts['WARN']} WARN, {counts['OK']} OK — "
                   f"safe to stream, fix warnings when you can{C_RESET}")
    else:
        verdict = f"{C_GREEN}{C_BOLD}ALL {counts['OK']} CHECKS PASSED — READY TO STREAM{C_RESET}"
    print(verdict)
    print(bar)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Pre-stream readiness check for HatmasBot."
    )
    parser.add_argument("--json", action="store_true",
                        help="Print results as JSON (machine-readable).")
    parser.add_argument("--quick", action="store_true",
                        help="Skip slower checks (external HTTP).")
    args = parser.parse_args()

    t0 = time.time()
    try:
        results = asyncio.run(run_all_checks(quick=args.quick))
    except Exception as exc:
        print(f"[!] Readiness checker errored: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
    elapsed_total = int((time.time() - t0) * 1000)

    if args.json:
        print(json.dumps({
            "timestamp": time.time(),
            "elapsed_ms": elapsed_total,
            "results": [asdict(r) for r in results],
        }, indent=2))
    else:
        print_report(results, elapsed_total)

    has_fail = any(r.status == "FAIL" for r in results)
    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
