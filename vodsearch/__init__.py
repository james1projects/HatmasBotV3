"""
vodsearch — "Ask the VOD": searchable stream archive.
=======================================================

Turns a folder of OBS recordings into a full-text-searchable index of
*moments*: every sentence the streamer said (GPU transcription of the
mic track), plus every kill/death the offline detector already found
(`<name>.events.json` sidecars), all timestamped against the source
video so any hit can be cut into a short clip on demand.

Standalone by design: nothing in this package imports HatmasBot. The
bot wires it in through `tools/vod_index.py` (CLI defaults from
core/config.py) and `core/public_webserver.py` (the /vod page + API).
Lift the package out and it works for any streamer with recordings.

Modules
-------
  cuda.py        Windows-only CUDA DLL preload for ctranslate2.
  audio.py       ffmpeg probing + multi-track decode to 16 kHz mono.
  transcribe.py  faster-whisper wrapper with hallucination filtering.
  store.py       SQLite schema (FTS5) + search/browse/stats queries.
  clips.py       ffmpeg clip rendering (NVENC, H.264 720p, mixed audio).
  indexer.py     Discover -> decode -> transcribe -> store, incremental.
  cli.py         `index` / `search` / `clip` / `stats` subcommands.
"""

__version__ = "0.1.0"
