"""
tools/readiness_ui.py — the readiness check as a real window.

Standalone aiohttp app (NOT part of the bot's webserver on 8069 — the
whole point is that this works when the bot is down). Imports the
probes from tools/check_stream_ready.py, renders them as a live panel,
and puts a fix button next to every non-green row:

    Bot down            -> Start bot        (tools/bot_ctl.ps1 restart)
    Twitch token 401    -> Start bot (auto-refresh) / Re-auth console
    OBS unreachable     -> Launch OBS
    MixItUp unreachable -> Launch MixItUp
    hatmaster.tv down   -> Restart cloudflared (UAC prompt)
    Low disk            -> Open recordings folder
    SMITE 2 not running -> Launch via Steam

Usage:
    py tools/readiness_ui.py              # start server + open app window
    py tools/readiness_ui.py --no-open    # server only
    py tools/readiness_ui.py --port 8073

Launched by streamdeck/readiness_ui.bat. Opens as an Edge --app window
(no browser chrome) when Edge is present, else the default browser.
Ctrl+C in the console (or closing it) stops the server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_stream_ready as csr  # noqa: E402

from aiohttp import web  # noqa: E402

DEFAULT_PORT = 8073
PORT_TRIES = 6

OBS_EXE = Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
MIXITUP_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "MixItUp" / "MixItUp.exe"
SMITE2_STEAM_URL = "steam://rungameid/2437170"


# ============================================================
# FIX ACTIONS
# ============================================================

def _bot_ctl(action: str) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(REPO_ROOT / "tools" / "bot_ctl.ps1"), "-Action", action],
        capture_output=True, text=True, timeout=30,
    )
    return (out.stdout or out.stderr or "").strip() or f"bot_ctl {action} done"


def fix_start_bot() -> str:
    return _bot_ctl("restart")


def fix_stop_bot() -> str:
    return _bot_ctl("stop")


def fix_launch_obs() -> str:
    if not OBS_EXE.exists():
        return f"OBS not found at {OBS_EXE}"
    subprocess.Popen([str(OBS_EXE)], cwd=str(OBS_EXE.parent))
    return "OBS launching"


def fix_launch_mixitup() -> str:
    if not MIXITUP_EXE.exists():
        return f"MixItUp not found at {MIXITUP_EXE}"
    subprocess.Popen([str(MIXITUP_EXE)], cwd=str(MIXITUP_EXE.parent))
    return "MixItUp launching (enable Developer API on 8911 if the check stays red)"


def fix_restart_cloudflared() -> str:
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Process powershell -Verb RunAs -ArgumentList "
         "'-NoProfile','-Command','Restart-Service cloudflared'"],
    )
    return "UAC prompt opened — approve it to restart the cloudflared service"


def fix_reauth_bot() -> str:
    subprocess.Popen([sys.executable, "-m", "core.auth"], cwd=str(REPO_ROOT),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return "Auth console opened — follow the browser flow"


def fix_reauth_broadcaster() -> str:
    subprocess.Popen([sys.executable, "-m", "core.auth", "--broadcaster"],
                     cwd=str(REPO_ROOT),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return "Broadcaster auth console opened — follow the browser flow"


def fix_launch_smite() -> str:
    os.startfile(SMITE2_STEAM_URL)
    return "Asked Steam to launch SMITE 2"


def fix_open_recordings() -> str:
    os.startfile(str(csr.RECORDINGS_DIR))
    return "Recordings folder opened"


FIX_ACTIONS = {
    "start_bot": ("Start bot", fix_start_bot),
    "stop_bot": ("Stop bot", fix_stop_bot),
    "launch_obs": ("Launch OBS", fix_launch_obs),
    "launch_mixitup": ("Launch MixItUp", fix_launch_mixitup),
    "restart_cloudflared": ("Restart cloudflared", fix_restart_cloudflared),
    "reauth_bot": ("Re-auth bot token", fix_reauth_bot),
    "reauth_broadcaster": ("Re-auth broadcaster", fix_reauth_broadcaster),
    "launch_smite": ("Launch SMITE 2", fix_launch_smite),
    "open_recordings": ("Open recordings", fix_open_recordings),
}

# check slug -> fix action ids offered when that check is not OK
SLUG_FIXES = {
    "bot_dashboard": ["start_bot"],
    "bot_health": ["start_bot"],
    "bot_token": ["start_bot", "reauth_bot"],
    "broadcaster_token": ["start_bot", "reauth_broadcaster"],
    "obs_websocket": ["launch_obs"],
    "mixitup": ["launch_mixitup"],
    "public_local": ["start_bot"],
    "web_login": ["start_bot"],
    "public_external": ["restart_cloudflared"],
    "cloudflared": ["restart_cloudflared"],
    "disk_space": ["open_recordings"],
    "spotify_token": ["start_bot"],
    "smite_process": ["launch_smite"],
}


# ============================================================
# CHECK RUNNER (slug-tagged)
# ============================================================

async def run_checks_tagged(quick: bool = False) -> dict:
    started = time.monotonic()
    slugs, tasks = [], []
    for slug, factory, slowish in csr.ALL_CHECKS:
        if quick and slowish:
            continue
        slugs.append(slug)
        tasks.append(asyncio.create_task(factory()))

    rows = []
    for slug, task in zip(slugs, tasks):
        try:
            r = await task
        except Exception as exc:
            r = csr.fail(slug, f"check raised {type(exc).__name__}: {exc}")
        fixes = SLUG_FIXES.get(slug, []) if r.status in ("FAIL", "WARN") else []
        rows.append({
            "slug": slug,
            "name": r.name,
            "status": r.status,
            "detail": r.detail,
            "hint": r.hint,
            "fixes": [{"id": f, "label": FIX_ACTIONS[f][0]} for f in fixes],
        })

    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    if counts["FAIL"]:
        verdict, tone = f"{counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['OK']} OK — DO NOT STREAM YET", "fail"
    elif counts["WARN"]:
        verdict, tone = f"{counts['WARN']} WARN, {counts['OK']} OK — safe to stream, fix warnings when you can", "warn"
    else:
        verdict, tone = f"ALL {counts['OK']} CHECKS PASSED — READY TO STREAM", "ok"

    return {
        "rows": rows,
        "verdict": verdict,
        "tone": tone,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "checked_at": time.strftime("%H:%M:%S"),
    }


# ============================================================
# WEB APP
# ============================================================

HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>HatmasBot Readiness</title>
<style>
  :root {
    --bg: #202C39; --panel: rgba(223,160,110,0.05);
    --border: rgba(223,160,110,0.18);
    --text: #FFFFFF; --text-dim: #AFC2C9; --text-muted: #8FA6AF;
    --ok: #DFA06E; --warn: #AFC2C9; --fail: #DF2935; --steel: #7D98A1;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", Inter, Arial, sans-serif; font-size: 14px;
    padding: 20px 24px; min-width: 640px;
  }
  h1 {
    font-family: Bahnschrift, "Arial Narrow", sans-serif;
    font-size: 26px; letter-spacing: 2px; text-transform: uppercase;
    font-weight: 600;
  }
  .topbar { display: flex; align-items: baseline; gap: 14px; margin-bottom: 14px; }
  .topbar .stamp { color: var(--text-muted); font-size: 12px; }
  .topbar button { margin-left: auto; }
  #verdict {
    padding: 10px 14px; margin-bottom: 14px; font-weight: 600;
    letter-spacing: 0.5px; border: 1px solid var(--border);
  }
  #verdict.ok   { color: var(--ok);   border-color: var(--ok); }
  #verdict.warn { color: var(--warn); border-color: var(--warn); }
  #verdict.fail { color: var(--fail); border-color: var(--fail); }
  .row {
    display: flex; align-items: flex-start; gap: 12px;
    padding: 9px 12px; border: 1px solid var(--border);
    border-top: none; background: var(--panel);
  }
  .row:first-of-type { border-top: 1px solid var(--border); }
  .chip {
    min-width: 52px; text-align: center; font-size: 11px; font-weight: 700;
    letter-spacing: 1px; padding: 3px 0; flex-shrink: 0;
  }
  .chip.OK   { color: var(--ok); }
  .chip.WARN { color: var(--warn); }
  .chip.FAIL { color: var(--fail); }
  .chip.SKIP { color: var(--text-muted); }
  .body { flex: 1; min-width: 0; }
  .name { font-weight: 600; }
  .detail { color: var(--text-dim); font-size: 13px; margin-top: 1px; }
  .hint { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
  .fixes { display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
  button {
    background: transparent; color: var(--ok); border: 1px solid var(--ok);
    padding: 5px 12px; font: inherit; font-size: 12px; font-weight: 600;
    letter-spacing: 0.5px; cursor: pointer;
  }
  button:hover { background: rgba(223,160,110,0.15); }
  button:disabled { opacity: 0.4; cursor: default; }
  button.steel { color: var(--steel); border-color: var(--steel); }
  button.steel:hover { background: rgba(125,152,161,0.15); }
  #toast {
    position: fixed; bottom: 18px; left: 24px; right: 24px;
    padding: 10px 14px; background: var(--bg);
    border: 1px solid var(--steel); color: var(--text-dim);
    font-size: 13px; display: none;
  }
  #loading { color: var(--text-muted); padding: 20px 0; }
</style>
</head>
<body>
  <div class="topbar">
    <h1>HatmasBot Readiness</h1>
    <span class="stamp" id="stamp"></span>
    <button class="steel" id="recheck" onclick="runChecks()">Re-check</button>
  </div>
  <div id="verdict" style="display:none"></div>
  <div id="rows"><div id="loading">Running checks...</div></div>
  <div id="toast"></div>
<script>
let busy = false;
let recheckTimer = null;

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

async function runChecks() {
  if (busy) return;
  busy = true;
  document.getElementById("recheck").disabled = true;
  try {
    const res = await fetch("/api/checks");
    const data = await res.json();
    render(data);
  } catch (e) {
    document.getElementById("rows").innerHTML =
      '<div id="loading">Check run failed: ' + esc(String(e)) + "</div>";
  }
  busy = false;
  document.getElementById("recheck").disabled = false;
}

function render(data) {
  const v = document.getElementById("verdict");
  v.style.display = "block";
  v.className = data.tone;
  v.textContent = data.verdict;
  document.getElementById("stamp").textContent =
    "checked " + data.checked_at + " in " + data.elapsed_ms + "ms";
  const rows = document.getElementById("rows");
  rows.innerHTML = "";
  for (const r of data.rows) {
    const div = document.createElement("div");
    div.className = "row";
    let fixHtml = "";
    for (const f of r.fixes) {
      fixHtml += '<button onclick="runFix(\\'' + f.id + '\\', this)">' + esc(f.label) + "</button>";
    }
    div.innerHTML =
      '<span class="chip ' + r.status + '">' + r.status + "</span>" +
      '<div class="body"><div class="name">' + esc(r.name) + "</div>" +
      '<div class="detail">' + esc(r.detail) + "</div>" +
      (r.hint && r.status !== "OK" ? '<div class="hint">' + esc(r.hint) + "</div>" : "") +
      "</div>" +
      '<div class="fixes">' + fixHtml + "</div>";
    rows.appendChild(div);
  }
}

async function runFix(id, btn) {
  btn.disabled = true;
  toast("Running: " + btn.textContent + "...");
  try {
    const res = await fetch("/api/fix", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: id}),
    });
    const data = await res.json();
    toast(data.message || "Done");
  } catch (e) {
    toast("Fix failed: " + e);
  }
  btn.disabled = false;
  clearTimeout(recheckTimer);
  recheckTimer = setTimeout(runChecks, 6000);
}

let toastTimer = null;
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = "none"; }, 8000);
}

runChecks();
</script>
</body>
</html>
"""


async def handle_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def handle_ping(request):
    return web.json_response({"app": "hatmasbot-readiness"})


async def handle_checks(request):
    quick = request.query.get("quick") == "1"
    return web.json_response(await run_checks_tagged(quick=quick))


async def handle_fix(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "bad request"}, status=400)
    action = body.get("action")
    if action not in FIX_ACTIONS:
        return web.json_response({"ok": False, "message": f"unknown action: {action}"}, status=400)
    label, fn = FIX_ACTIONS[action]
    try:
        message = await asyncio.to_thread(fn)
    except Exception as exc:
        return web.json_response({"ok": False, "message": f"{label} failed: {exc}"}, status=500)
    print(f"[ReadinessUI] fix '{action}': {message}")
    return web.json_response({"ok": True, "message": message})


def open_app_window(url: str) -> None:
    for edge in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if edge.exists():
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=860,760"])
            return
    import webbrowser
    webbrowser.open(url)


async def main() -> int:
    parser = argparse.ArgumentParser(description="HatmasBot readiness panel.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true",
                        help="don't open the app window")
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_get("/api/checks", handle_checks)
    app.router.add_post("/api/fix", handle_fix)

    runner = web.AppRunner(app)
    await runner.setup()
    port = None
    for candidate in range(args.port, args.port + PORT_TRIES):
        try:
            site = web.TCPSite(runner, "127.0.0.1", candidate)
            await site.start()
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        print(f"[ReadinessUI] no free port in {args.port}..{args.port + PORT_TRIES - 1}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"[ReadinessUI] serving {url}  (Ctrl+C to stop)")
    if not args.no_open:
        open_app_window(url)

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
