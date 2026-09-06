"""plugins/bingo — Stream Bingo: viewer cards marked by the bot and the deck.

pool.py   square pool, card generation, bingo math (pure)
store.py  rounds / cards / calls in data/bingo.db
plugin.py the plugin: detector + economy hooks, Hats, chat, overlay events
"""

from .plugin import BingoPlugin

__all__ = ["BingoPlugin"]
