r"""
tools/vod_index.py — "Ask the VOD" indexer with HatmasBot's defaults.

Thin shim over the standalone `vodsearch` package: fills in the repo's
paths and model choice from core/config.py so the Stream Deck button
(streamdeck/process_recordings.bat, after the sorter) and the CLI need
no flags.

    python tools\vod_index.py                 # == index (incremental)
    python tools\vod_index.py index --force   # re-transcribe everything
    python tools\vod_index.py search "nice trap" --god Ymir --event kill
    python tools\vod_index.py browse --event multikill
    python tools\vod_index.py clip --segment 123
    python tools\vod_index.py stats --errors
    python toolsod_index.py publish --god Ymir      # make recordings public (default: private)
    python toolsod_index.py unpublish --all
    python toolsod_index.py embed                   # semantic-search vectors (local Ollama)
    python toolsod_index.py relabel                 # streamer/friends labels from speech levels
    python toolsod_index.py events                  # re-read detector sidecars only

Any `vodsearch.cli` flag still works; the defaults here only fill gaps.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import config  # noqa: E402
from vodsearch.cli import main  # noqa: E402


def defaults() -> dict:
    return {
        "recordings_dir": Path(config.VOD_RECORDINGS_DIR),
        "db_path": Path(config.VOD_DB_PATH),
        "clips_dir": Path(config.VOD_CLIPS_DIR),
        "model": config.VOD_WHISPER_MODEL,
        "initial_prompt": getattr(config, "VOD_WHISPER_PROMPT", None),
        "embed_host": getattr(config, "VOD_EMBED_HOST", "http://localhost:11434"),
        "embed_model": getattr(config, "VOD_EMBED_MODEL", "nomic-embed-text"),
        "tracks": config.VOD_TRACKS,
        "audio_tracks": ",".join(str(t) for t in config.VOD_CLIP_AUDIO_TRACKS),
        "clip_height": config.VOD_CLIP_HEIGHT,
        "encoder": config.VOD_CLIP_ENCODER,
        "ffmpeg": config.VOD_FFMPEG,
        "ffprobe": config.VOD_FFPROBE,
    }


if __name__ == "__main__":
    argv = sys.argv[1:] or ["index"]
    sys.exit(main(argv, defaults()))
