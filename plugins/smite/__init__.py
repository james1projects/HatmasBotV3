"""
plugins.smite — Smite 2 tracker.gg integration for HatmasBot.

Was a single 1,988-line plugins/smite.py before P1-7 of the cleanup
sweep. Now split into a package of concern-specific modules
(state, tracker_client, history, match_state, obs_portrait, title,
predictions, twitch_api, commands) composed onto a single SmitePlugin
class via mixin inheritance.

External imports stay the same as the monolith — main.py and
tools/replay_economy.py both `from plugins.smite import SmitePlugin`
and get back the same class they always did.

Internal modules import each other relatively (`from .match_state
import ...`).
"""

from __future__ import annotations

from .plugin import SmitePlugin


__all__ = ["SmitePlugin"]
