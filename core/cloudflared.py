"""
Cloudflare Tunnel manager
=========================
Launches `cloudflared tunnel run <name>` as a child process tied to the
bot's lifetime, so `python main.py` brings the public site (hatmaster.tv)
up automatically and takes it down on shutdown — no more starting the
tunnel by hand in a separate terminal.

Why a child process instead of the Windows service?
- The documented production setup is the auto-starting `cloudflared`
  Windows service (see HatmasBot.md → "Cloudflare Tunnel"). This module
  is the convenience path for when that service isn't installed/running
  and you'd rather have the tunnel's lifetime match the bot's.
- Running this alongside a live service is harmless — Cloudflare treats
  each `cloudflared` process as a tunnel *replica* and load-balances
  across them. But it's redundant; if your service is healthy, set
  CLOUDFLARED_ENABLED = False in config_local.py and let the service do
  the job.

Fails soft: if cloudflared can't be found or won't start, we print a
one-line warning and the bot keeps running (the site just stays
unreachable until the tunnel is up by other means).
"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class CloudflaredTunnel:
    """Manages a `cloudflared tunnel run <name>` child process."""

    def __init__(self, tunnel_name: str, exe_path: str = ""):
        self.tunnel_name = tunnel_name
        # Explicit config path wins; otherwise search PATH, then fall back
        # to the standard Windows MSI install locations.
        self.exe_path = exe_path or self._find_cloudflared()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._log_task: Optional[asyncio.Task] = None

    @staticmethod
    def _find_cloudflared() -> str:
        """Locate cloudflared.exe: PATH first, then MSI defaults."""
        found = shutil.which("cloudflared")
        if found:
            return found
        candidates = [
            Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
            Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return ""

    async def start(self) -> None:
        """Spawn the tunnel. No-op (with a warning) if cloudflared is
        missing or the process can't be launched."""
        if not self.exe_path:
            print("[Cloudflared] cloudflared.exe not found on PATH or in "
                  "the standard install locations — skipping tunnel launch. "
                  "Install it or set CLOUDFLARED_PATH in config_local.py.")
            return

        try:
            # CREATE_NEW_PROCESS_GROUP isolates cloudflared from the bot's
            # Ctrl+C so we control its shutdown explicitly in stop().
            self._proc = await asyncio.create_subprocess_exec(
                self.exe_path, "tunnel", "run", self.tunnel_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as e:
            print(f"[Cloudflared] Failed to launch tunnel '{self.tunnel_name}': {e}")
            self._proc = None
            return

        print(f"[Cloudflared] Tunnel '{self.tunnel_name}' starting "
              f"(pid {self._proc.pid}) → hatmaster.tv")
        self._log_task = asyncio.create_task(self._pump_logs())

    async def _pump_logs(self) -> None:
        """Echo interesting cloudflared output to the bot console.

        cloudflared is chatty; we surface connection registrations and
        anything error-shaped, and drop routine noise so the bot console
        stays readable."""
        assert self._proc is not None and self._proc.stdout is not None
        keep = ("Registered tunnel connection", "Starting tunnel",
                "ERR", "error", "failed", "Retrying", "unregistered")
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line and any(k in line for k in keep):
                    print(f"[Cloudflared] {line}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def stop(self) -> None:
        """Terminate the tunnel process and stop pumping its logs."""
        if self._proc is None:
            return
        if self._proc.returncode is None:
            print("[Cloudflared] Stopping tunnel...")
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    print("[Cloudflared] Tunnel didn't exit in time — killing.")
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass  # already gone
            except Exception as e:
                print(f"[Cloudflared] Error during shutdown: {e}")

        if self._log_task:
            self._log_task.cancel()
            try:
                await self._log_task
            except (asyncio.CancelledError, Exception):
                pass
        self._proc = None
