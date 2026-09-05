"""plugins/cocaster — the co-caster (private earpiece + on-stream lines).

See plugin.py for the design; persona.md for the on-stream voice;
chatlog.py for the local chat log it feeds on.
"""

from .plugin import CoCasterPlugin

__all__ = ["CoCasterPlugin"]
