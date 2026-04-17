import asyncio
import json
from pathlib import Path
from typing import Dict, Set, Optional, Any


class OverlayManager:
    """Central overlay management system for HatmasBot.

    Receives events from plugins, checks JSON rules config, and sends
    show/hide/update commands to overlay HTML pages via websocket.
    """

    def __init__(self, web_server):
        """Initialize the OverlayManager.

        Args:
            web_server: The webserver instance
        """
        self.web_server = web_server
        self._ws_clients: Dict[str, Set] = {}
        self._timers: Dict[str, asyncio.Task] = {}
        self._visible: Dict[str, bool] = {}
        self._last_show_data: Dict[str, Any] = {}  # Cache last show data for reconnects
        self._pending_show_delays: Dict[str, asyncio.Task] = {}
        self._send_lock = asyncio.Lock()  # Serialize all WebSocket sends
        self.rules: Dict[str, Any] = {}

        self.reload_rules()

    def reload_rules(self) -> None:
        """Reload rules from the JSON config file."""
        rules_path = Path(__file__).parent / "overlay_rules.json"

        try:
            if rules_path.exists():
                with open(rules_path, "r") as f:
                    self.rules = json.load(f)
            else:
                self.rules = {}
        except Exception as e:
            print(f"[Overlay] Error loading rules: {e}")
            self.rules = {}

    def register_ws(self, overlay_name: str, ws) -> None:
        """Register a websocket connection for an overlay.

        Args:
            overlay_name: Name of the overlay
            ws: Websocket connection object
        """
        if overlay_name not in self._ws_clients:
            self._ws_clients[overlay_name] = set()
        self._ws_clients[overlay_name].add(ws)

    def unregister_ws(self, overlay_name: str, ws) -> None:
        """Unregister a websocket connection for an overlay.

        Args:
            overlay_name: Name of the overlay
            ws: Websocket connection object
        """
        if overlay_name in self._ws_clients:
            self._ws_clients[overlay_name].discard(ws)
            if not self._ws_clients[overlay_name]:
                del self._ws_clients[overlay_name]

    async def _send(self, overlay_name: str, action: str, data: Optional[Any] = None) -> None:
        """Send a command to all websocket clients for an overlay.

        Uses a lock to prevent concurrent WebSocket writes, which can
        corrupt the connection and silently disconnect clients.

        Args:
            overlay_name: Name of the overlay
            action: Action type ("show", "hide", or "update")
            data: Optional data to include in the message
        """
        message = {
            "overlay": overlay_name,
            "action": action,
        }
        if data is not None:
            message["data"] = data

        if overlay_name not in self._ws_clients:
            return

        message_json = json.dumps(message)
        disconnected = set()

        async with self._send_lock:
            for ws in self._ws_clients.get(overlay_name, set()):
                try:
                    await asyncio.wait_for(ws.send_str(message_json), timeout=2.0)
                except Exception:
                    disconnected.add(ws)

        # Clean up disconnected clients outside the lock
        for ws in disconnected:
            self.unregister_ws(overlay_name, ws)

    def _cancel_timers(self, overlay_name: str) -> None:
        """Cancel all pending timers for an overlay.

        Args:
            overlay_name: Name of the overlay
        """
        # Cancel hide timer
        if overlay_name in self._timers:
            self._timers[overlay_name].cancel()
            del self._timers[overlay_name]

        # Cancel pending show delay
        if overlay_name in self._pending_show_delays:
            self._pending_show_delays[overlay_name].cancel()
            del self._pending_show_delays[overlay_name]

    async def _start_hide_timer(self, overlay_name: str, seconds: float) -> None:
        """Start an auto-hide timer for an overlay.

        Args:
            overlay_name: Name of the overlay
            seconds: Seconds to wait before hiding
        """
        # Cancel existing hide timer
        if overlay_name in self._timers:
            self._timers[overlay_name].cancel()

        async def hide_task():
            try:
                await asyncio.sleep(seconds)
                await self._send(overlay_name, "hide")
                self._visible[overlay_name] = False
                self._last_show_data.pop(overlay_name, None)
                print(f"[Overlay] hide: {overlay_name} (auto {int(seconds)}s)")
            except asyncio.CancelledError:
                pass

        self._timers[overlay_name] = asyncio.create_task(hide_task())

    async def _execute_delayed_show(self, overlay_name: str, delay: float, event_name: str, data: Optional[Any] = None) -> None:
        """Execute a delayed show action.

        Args:
            overlay_name: Name of the overlay
            delay: Seconds to wait before showing
            event_name: Name of the triggering event
            data: Optional data to include with the show command
        """
        try:
            await asyncio.sleep(delay)

            # Check if this task is still the current pending one
            if self._pending_show_delays.get(overlay_name) is not asyncio.current_task():
                return

            if overlay_name in self._pending_show_delays:
                del self._pending_show_delays[overlay_name]

            # Send show command
            await self._send(overlay_name, "show", data)
            self._visible[overlay_name] = True
            print(f"[Overlay] show: {overlay_name} ({event_name})")

            # Start auto-hide timer if configured
            overlay_config = self.rules.get(overlay_name, {})
            hide_after = overlay_config.get("hide_after")
            if hide_after:
                await self._start_hide_timer(overlay_name, hide_after)

        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """Gracefully shut down the overlay manager.

        Cancels all pending timers and closes all websocket connections
        so the event loop can exit cleanly.
        """
        # Cancel all hide timers and pending show delays
        for name in list(self._timers):
            self._timers[name].cancel()
        self._timers.clear()

        for name in list(self._pending_show_delays):
            self._pending_show_delays[name].cancel()
        self._pending_show_delays.clear()

        # Close all websocket connections
        for overlay_name, clients in list(self._ws_clients.items()):
            for ws in list(clients):
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except Exception:
                    pass
        self._ws_clients.clear()
        self._visible.clear()
        self._last_show_data.clear()

    async def emit(self, event_name: str, data: Optional[Any] = None) -> None:
        """Process an event and trigger overlay actions.

        Args:
            event_name: Name of the event
            data: Optional data associated with the event
        """
        for overlay_name, overlay_config in self.rules.items():
            # Check show_on triggers
            show_on = overlay_config.get("show_on", [])
            show_delay = None
            should_show = False

            for trigger in show_on:
                if isinstance(trigger, str):
                    if trigger == event_name:
                        should_show = True
                        break
                elif isinstance(trigger, dict):
                    if trigger.get("event") == event_name:
                        should_show = True
                        show_delay = trigger.get("delay", 0)
                        break

            if should_show:
                # Cancel any pending delayed show for this overlay
                self._cancel_timers(overlay_name)

                if show_delay and show_delay > 0:
                    # Schedule delayed show
                    task = asyncio.create_task(
                        self._execute_delayed_show(overlay_name, show_delay, event_name, data)
                    )
                    self._pending_show_delays[overlay_name] = task
                else:
                    # Immediate show (include data so overlay can render immediately)
                    await self._send(overlay_name, "show", data)
                    self._visible[overlay_name] = True
                    self._last_show_data[overlay_name] = data
                    print(f"[Overlay] show: {overlay_name} ({event_name})")

                    # Start auto-hide timer if configured
                    hide_after = overlay_config.get("hide_after")
                    if hide_after:
                        await self._start_hide_timer(overlay_name, hide_after)

            # Check hide_on triggers
            hide_on = overlay_config.get("hide_on", [])
            if event_name in hide_on:
                self._cancel_timers(overlay_name)
                await self._send(overlay_name, "hide")
                self._visible[overlay_name] = False
                self._last_show_data.pop(overlay_name, None)
                print(f"[Overlay] hide: {overlay_name} ({event_name})")

            # Check keep_alive_on and update_on triggers (only if overlay is visible)
            if self._visible.get(overlay_name, False):
                keep_alive_on = overlay_config.get("keep_alive_on", [])
                update_on = overlay_config.get("update_on", [])

                if event_name in keep_alive_on:
                    # Reset the auto-hide timer
                    hide_after = overlay_config.get("hide_after")
                    if hide_after:
                        await self._start_hide_timer(overlay_name, hide_after)

                # Only send updates for events this overlay subscribes to
                # via show_on, keep_alive_on, or update_on
                if data is not None and not should_show:
                    if event_name in keep_alive_on or event_name in update_on:
                        await self._send(overlay_name, "update", data)

