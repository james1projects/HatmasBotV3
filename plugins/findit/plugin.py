"""
FindIt Plugin (early development)
=================================
"Ctrl+F for real life" — point a phone camera at the world, type what
you're looking for, get a box + beep when it's spotted. Lives at
hatmaster.tv/FindIt; the phone page and its WebSocket are served by the
public webserver (core/public_webserver.py), which proxies frames to a
GPU detection worker (plugins/findit/worker.py) managed here.

The worker is a CHILD PROCESS in its own venv (.venv-findit — torch +
ultralytics stay out of the bot's environment, and a crash there can
never take the bot down). Lifecycle:

    page opened  -> ensure_worker() -> spawn worker, poll /healthz
    toggle off   -> reconciler kills the worker (frees ~1.5 GB VRAM)
    idle         -> no WebSocket clients for FINDIT_IDLE_TIMEOUT seconds
                    -> reconciler kills the worker
    bot shutdown -> cleanup() kills the worker

The whole feature sits behind the "findit" toggle in DEFAULT_FEATURES
(default OFF, flip it live from the control panel's features card).
Off = hatmaster.tv/FindIt and /ws/findit 404, and no worker process
runs. No chat commands — the entire surface is HTTP.
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from core.config import (
    BASE_DIR, DATA_DIR,
    FINDIT_PYTHON, FINDIT_WORKER_PORT, FINDIT_MODEL, FINDIT_SIM_THRESHOLD,
    FINDIT_IDLE_TIMEOUT, FINDIT_STARTUP_TIMEOUT,
)

WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"
WORKER_DIR = DATA_DIR / "findit"   # cwd for the worker: weights download here


class FindItPlugin:
    def __init__(self):
        self.bot = None
        self._proc = None
        self._log_task = None
        self._reconcile_task = None
        self._start_lock = asyncio.Lock()
        # Connection accounting for idle shutdown. The public webserver
        # calls note_connect()/note_disconnect() from the WS proxy.
        self.active_clients = 0
        self._last_activity = 0.0

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        state = "ON" if self.enabled() else "OFF (enable via control panel)"
        print(f"[FindIt] Ready — feature toggle is {state}; page at /FindIt "
              f"(worker on demand, port {FINDIT_WORKER_PORT})")

    async def on_ready(self):
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def cleanup(self):
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.stop_worker("bot shutdown")

    # === PUBLIC SURFACE (used by core/public_webserver.py) ===

    def enabled(self):
        return bool(self.bot and self.bot.is_feature_enabled("findit"))

    def worker_running(self):
        return self._proc is not None and self._proc.returncode is None

    def backend_url(self):
        return f"http://127.0.0.1:{FINDIT_WORKER_PORT}"

    def note_connect(self):
        self.active_clients += 1
        self._last_activity = time.monotonic()

    def note_disconnect(self):
        self.active_clients = max(0, self.active_clients - 1)
        self._last_activity = time.monotonic()

    async def ensure_worker(self):
        """Start the worker if needed and wait until /healthz reports
        ready (model loaded + warm). Raises RuntimeError on failure."""
        async with self._start_lock:
            self._last_activity = time.monotonic()
            if not self.worker_running():
                await self._spawn()
            await self._wait_ready()

    # === WORKER LIFECYCLE ===

    async def _spawn(self):
        python = FINDIT_PYTHON or sys.executable
        if not Path(python).exists():
            raise RuntimeError(
                f"FindIt worker python not found at {python} — create the "
                f".venv-findit environment (see HATMASBOT.md FindIt section)")
        WORKER_DIR.mkdir(parents=True, exist_ok=True)
        try:
            # CREATE_NEW_PROCESS_GROUP isolates the worker from the bot's
            # Ctrl+C so we control its shutdown explicitly (same pattern
            # as core/cloudflared.py).
            self._proc = await asyncio.create_subprocess_exec(
                python, str(WORKER_SCRIPT),
                "--port", str(FINDIT_WORKER_PORT),
                "--model", FINDIT_MODEL,
                "--sim-threshold", str(FINDIT_SIM_THRESHOLD),
                "--items", str(WORKER_DIR / "items.json"),
                cwd=str(WORKER_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as e:
            self._proc = None
            raise RuntimeError(f"FindIt worker failed to launch: {e}")
        print(f"[FindIt] Worker starting (pid {self._proc.pid}) — "
              f"loading model onto GPU...")
        self._log_task = asyncio.create_task(self._pump_logs())

    async def _pump_logs(self):
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    print(f"[FindIt] {line}")
        except (asyncio.CancelledError, Exception):
            pass

    async def _wait_ready(self):
        """Poll /healthz until the model is warm. First-ever start can be
        slow (weight download); normal starts take ~10-20s."""
        deadline = time.monotonic() + FINDIT_STARTUP_TIMEOUT
        url = self.backend_url() + "/healthz"
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if not self.worker_running():
                    raise RuntimeError(
                        "FindIt worker exited during startup — check the "
                        "[FindIt] lines in the bot console")
                try:
                    async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(1.0)
        raise RuntimeError(
            f"FindIt worker not ready after {FINDIT_STARTUP_TIMEOUT}s")

    async def stop_worker(self, reason=""):
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            print(f"[FindIt] Stopping worker{' — ' + reason if reason else ''}")
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[FindIt] Error stopping worker: {e}")
        if self._log_task:
            self._log_task.cancel()
            try:
                await self._log_task
            except (asyncio.CancelledError, Exception):
                pass
            self._log_task = None

    async def _reconcile_loop(self):
        """Every 5s: kill the worker if the feature was toggled off, or if
        no phone has been connected for FINDIT_IDLE_TIMEOUT seconds. The
        worker holds ~1.5 GB of VRAM — don't keep it around for nothing."""
        while True:
            await asyncio.sleep(5)
            try:
                if not self.worker_running():
                    continue
                if not self.enabled():
                    await self.stop_worker("feature toggled off")
                elif (self.active_clients == 0
                        and self._last_activity
                        and time.monotonic() - self._last_activity
                        > FINDIT_IDLE_TIMEOUT):
                    await self.stop_worker(
                        f"idle for {FINDIT_IDLE_TIMEOUT}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[FindIt] reconcile error: {e}")
