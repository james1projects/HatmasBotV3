"""
YouTube Live-Badge Plugin
==========================
Listens for the `stream_live` / `stream_offline` events fired by
`plugins/stream_status.py` (which polls Twitch's /helix/streams every
60s). When you go live, applies LIVE badges to your last N YouTube
thumbnails. When you go offline, reverts them.

Architecture
------------
This plugin is intentionally THIN — all the heavy lifting (OAuth,
YouTube API calls, badge composition, state tracking) lives in
`tools/youtube_live_badge.py`. The plugin just shells out to that
script as a subprocess on the appropriate transitions, captures
stdout/stderr to the bot console, and logs success/failure.

Why subprocess instead of importing the module directly?
- Total isolation. If the OAuth flow has a hiccup or the YouTube API
  errors, the bot itself stays healthy.
- Easier to debug independently — you can run the same command
  manually from a terminal.
- The Stream Deck wrappers (go_live.bat / go_offline.bat) already
  invoke the same subprocess command, so any fix in one place fixes
  both paths.

Feature toggle
--------------
The plugin checks `bot.feature_enabled('youtube_live_badge')` before
acting. To disable temporarily without removing the plugin, toggle
that feature off in the dashboard.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "youtube_live_badge.py"
FEATURE_KEY = "youtube_live_badge"


class YouTubeLiveBadgePlugin:
    """Auto-applies and reverts YouTube LIVE thumbnail badges based on
    Twitch live-status events. Hooks into the existing OverlayManager
    event bus that StreamStatusPlugin already drives."""

    def __init__(self):
        self.bot = None
        self._logger = logging.getLogger("YouTubeLiveBadge")
        self._inflight: Optional[asyncio.Task] = None  # active apply/revert task

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    def setup(self, bot):
        # NOTE: setup() is called synchronously by core/bot.py:register_plugin,
        # so this MUST NOT be async — an async setup would return an
        # un-awaited coroutine and the body would silently never run.
        self.bot = bot
        # Make sure the feature toggle exists (defaults to enabled)
        if hasattr(bot, "features") and FEATURE_KEY not in bot.features:
            bot.features[FEATURE_KEY] = True

    async def on_ready(self):
        # Subscribe to the OverlayManager event bus once everything's wired up.
        try:
            overlay = self._get_overlay_manager()
            if overlay is None:
                self._logger.warning(
                    "[YouTubeLiveBadge] No overlay_manager on bot — auto-fire disabled. "
                    "Use go_live.bat / go_offline.bat manually."
                )
                return
            overlay.add_event_listener(self._on_overlay_event)
            print(
                "[YouTubeLiveBadge] Listening for stream_live / stream_offline events. "
                f"(Feature toggle: '{FEATURE_KEY}')"
            )
        except Exception as exc:
            self._logger.error(f"[YouTubeLiveBadge] Failed to subscribe: {exc}")

    async def cleanup(self):
        # Best-effort unsubscribe so a hot-reload doesn't leave dead listeners.
        try:
            overlay = self._get_overlay_manager()
            if overlay is not None:
                overlay.remove_event_listener(self._on_overlay_event)
        except Exception:
            pass
        # Don't cancel an in-flight apply/revert — let it finish so YouTube
        # doesn't end up half-badged.
        if self._inflight and not self._inflight.done():
            try:
                await asyncio.wait_for(self._inflight, timeout=30)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _on_overlay_event(self, event_name: str, data: Any):
        """Filter for stream_live / stream_offline; fire-and-forget the
        appropriate subprocess so the event bus isn't blocked while we
        wait for YouTube uploads."""
        if event_name not in ("stream_live", "stream_offline"):
            return

        # Feature toggle check
        if not self._feature_enabled():
            print(f"[YouTubeLiveBadge] '{FEATURE_KEY}' disabled, skipping {event_name}.")
            return

        # If a previous run is still going, wait for it (prevents
        # overlapping apply+revert calls fighting over the same files).
        if self._inflight and not self._inflight.done():
            try:
                await self._inflight
            except Exception:
                pass

        if event_name == "stream_live":
            self._inflight = asyncio.create_task(self._run_subcommand("apply"))
        elif event_name == "stream_offline":
            self._inflight = asyncio.create_task(self._run_subcommand("revert"))

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def _run_subcommand(self, subcmd: str):
        """Shell out to tools/youtube_live_badge.py <subcmd>."""
        if not TOOL_PATH.exists():
            self._logger.error(f"[YouTubeLiveBadge] Tool not found at {TOOL_PATH}")
            return

        print(f"[YouTubeLiveBadge] Stream {('went LIVE' if subcmd == 'apply' else 'went OFFLINE')} "
              f"— running `{subcmd}`...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(TOOL_PATH),
                subcmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(REPO_ROOT),
            )
            stdout, stderr = await proc.communicate()

            # Echo subprocess output to the bot console (with a tag so it's
            # easy to grep for in logs).
            for line in stdout.decode(errors="replace").splitlines():
                if line.strip():
                    print(f"  [YouTubeLiveBadge] {line}")
            for line in stderr.decode(errors="replace").splitlines():
                if line.strip():
                    print(f"  [YouTubeLiveBadge:err] {line}")

            if proc.returncode == 0:
                print(f"[YouTubeLiveBadge] `{subcmd}` completed cleanly.")
            else:
                self._logger.warning(
                    f"[YouTubeLiveBadge] `{subcmd}` exited with code {proc.returncode}"
                )
        except FileNotFoundError as exc:
            self._logger.error(f"[YouTubeLiveBadge] Could not invoke python: {exc}")
        except Exception as exc:
            self._logger.exception(f"[YouTubeLiveBadge] Subcommand `{subcmd}` raised: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_overlay_manager(self):
        """Return the bot's overlay_manager or None if not wired up."""
        # The bot may expose it directly OR via webserver.
        for path in ("overlay_manager", "webserver.overlay_manager"):
            obj = self.bot
            for attr in path.split("."):
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if obj is not None:
                return obj
        return None

    def _feature_enabled(self) -> bool:
        try:
            return bool(self.bot.features.get(FEATURE_KEY, True))
        except Exception:
            return True
