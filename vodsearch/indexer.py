"""
Indexer: discover recordings -> decode audio -> transcribe -> store.

Incremental by design. A recording is re-indexed only when it's new,
changed on disk (size/mtime), was indexed with a different model, or
`--force` is given. Rows whose file vanished (the sorter moved it) are
dropped so the site never links a clip to a missing source.

Per recording:
  1. ffprobe: duration + audio layout.
  2. Read the `<stem>.events.json` sidecar (kills/deaths from the
     offline detector) if present.
  3. Decode every configured voice track in one ffmpeg pass.
  4. Gate each track on `speech_fraction` so silent Discord/Misc
     tracks cost nothing.
  5. Transcribe the live ones, store segments labelled by speaker.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import audio as audio_mod
from .store import Store, now_iso
from .transcribe import Transcriber

DEFAULT_TRACKS: Dict[int, str] = {1: "hatmaster", 2: "friends", 3: "friends"}
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")
Logger = Callable[[str], None]


@dataclass
class IndexOptions:
    recordings_dir: Path
    db_path: Path
    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    language: Optional[str] = "en"
    batch_size: int = 16
    tracks: Dict[int, str] = field(default_factory=lambda: dict(DEFAULT_TRACKS))
    min_duration_s: float = 8.0
    min_speech_frac: float = 0.02
    speech_threshold_db: float = -45.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    force: bool = False
    limit: Optional[int] = None
    include_root: bool = False
    skip_dirs: Tuple[str, ...] = (".tiktok_bg", "processed", "replays")
    dry_run: bool = False
    prune_missing: bool = True


def discover(recordings_dir: Path, include_root: bool = False,
             skip_dirs: Tuple[str, ...] = (".tiktok_bg",)) -> List[Path]:
    """All .mp4 files under `recordings_dir`, newest first. Root-level
    files are unsorted drop-folder recordings and are skipped unless
    `include_root` (the sorter will move them into a god folder later
    and they'd be re-indexed under the new path anyway)."""
    root = Path(recordings_dir)
    if not root.is_dir():
        return []
    skip = {s.lower() for s in skip_dirs}
    found: List[Path] = []
    for p in root.rglob("*.mp4"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts[:-1]
        if any(part.lower() in skip or part.startswith(".") for part in rel_parts):
            continue
        if not rel_parts and not include_root:
            continue
        found.append(p)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def sidecar_path(mp4: Path) -> Path:
    return mp4.parent / (mp4.stem + ".events.json")


def load_sidecar(mp4: Path) -> Tuple[List[str], List[dict]]:
    sc = sidecar_path(mp4)
    if not sc.exists():
        return [], []
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    gods = [g for g in (data.get("gods_seen") or []) if isinstance(g, str)]
    events = [e for e in (data.get("events") or []) if isinstance(e, dict)]
    return gods, events


def recorded_at_for(path: Path, duration_s: float, mtime: float) -> str:
    """Start time of the recording. OBS names files by start time; after
    the sorter renames them we fall back to mtime (end of recording)
    minus the duration."""
    m = _DATE_RE.search(path.name)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, s).isoformat()
        except ValueError:
            pass
    end = datetime.fromtimestamp(mtime)
    start = end - timedelta(seconds=float(duration_s or 0))
    return start.replace(microsecond=0).isoformat()


def god_for(path: Path, gods_seen: List[str], recordings_dir: Path) -> Optional[str]:
    if len(gods_seen) == 1:
        return gods_seen[0]
    try:
        rel = path.relative_to(recordings_dir)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) > 1:
        folder = rel.parts[0]
        if folder.lower() not in ("mixed", "unknown"):
            return folder
    return None


def index_recording(store: Store, transcriber: Transcriber, path: Path,
                    opts: IndexOptions, log: Logger = print) -> dict:
    """Index one recording. Returns a summary dict; raises on hard
    failure (caller records the error against the row)."""
    st = path.stat()
    if not opts.force and store.is_current(path, st.st_size, st.st_mtime, opts.model):
        return {"path": str(path), "skipped": "current"}

    pr = audio_mod.probe(path, opts.ffprobe)
    gods_seen, events = load_sidecar(path)
    god = god_for(path, gods_seen, opts.recordings_dir)
    rel_folder = None
    try:
        rel = path.relative_to(opts.recordings_dir)
        rel_folder = rel.parts[0] if len(rel.parts) > 1 else ""
    except ValueError:
        pass
    meta = dict(folder=rel_folder, stem=path.stem, god=god, gods_seen=gods_seen,
                duration_s=pr.duration_s, size_bytes=st.st_size, mtime=st.st_mtime,
                recorded_at=recorded_at_for(path, pr.duration_s, st.st_mtime),
                model=opts.model, status="indexing", error=None)
    if opts.dry_run:
        return {"path": str(path), "dry_run": True, "god": god,
                "duration_s": pr.duration_s, "events": len(events),
                "tracks": [t for t in opts.tracks if t < len(pr.audio_tracks)]}

    rec_id = store.upsert_recording(path, **meta)
    store.replace_events(rec_id, events, god)

    if pr.duration_s < opts.min_duration_s:
        store.replace_segments(rec_id, [])
        store.replace_tracks(rec_id, [])
        store.upsert_recording(path, status="skipped", error="too short",
                               indexed_at=now_iso())
        return {"path": str(path), "skipped": "too short", "duration_s": pr.duration_s}

    wanted = [t for t in opts.tracks if t < len(pr.audio_tracks)]
    store.replace_segments(rec_id, [])            # clear all tracks' old rows
    track_rows: List[dict] = []
    total_segments = 0
    t0 = time.time()
    decoded = audio_mod.decode_tracks(path, wanted, opts.ffmpeg) if wanted else {}
    decode_s = time.time() - t0
    kept: List[int] = []
    for t in wanted:
        samples = decoded.get(t)
        if samples is None:
            continue
        frac = audio_mod.speech_fraction(samples, threshold_db=opts.speech_threshold_db)
        row = {"track_index": t, "speaker": opts.tracks[t],
               "rms_db": audio_mod.rms_db(samples), "speech_frac": frac,
               "transcribed": False, "segments": 0}
        if frac < opts.min_speech_frac:
            track_rows.append(row)
            continue
        # OBS profiles have shipped the mic on two tracks at once (seen
        # 2026-07: track 2 byte-identical to track 1). Transcribing both
        # doubles every search hit, so drop exact duplicates.
        dup = next((k for k in kept if audio_mod.same_audio(decoded[k], samples)), None)
        if dup is not None:
            row["speaker"] = f"dup:{dup}"
            track_rows.append(row)
            log(f"    track {t}: identical to track {dup}, skipped")
            continue
        kept.append(t)
        t1 = time.time()
        segs = transcriber.transcribe(samples)
        n = store.replace_segments(rec_id, [
            dict(track_index=t, speaker=opts.tracks[t], start_s=s.start, end_s=s.end,
                 text=s.text, no_speech_prob=s.no_speech_prob, avg_logprob=s.avg_logprob)
            for s in segs], track_index=t)
        row.update(transcribed=True, segments=n)
        total_segments += n
        track_rows.append(row)
        log(f"    track {t} ({opts.tracks[t]}): speech {frac:.0%}, {n} segments, "
            f"{time.time() - t1:.0f}s")
    store.replace_tracks(rec_id, track_rows)
    store.upsert_recording(path, status="done", error=None, indexed_at=now_iso())
    return {"path": str(path), "recording_id": rec_id, "god": god,
            "duration_s": pr.duration_s, "segments": total_segments,
            "events": len(events), "decode_s": decode_s,
            "elapsed_s": time.time() - t0}


def refresh_events(store: Store, recordings_dir: Path, log: Logger = print) -> dict:
    """Re-read every indexed recording's .events.json and rewrite its
    event rows (tiers, kinds, notes) without touching transcripts. Use
    after the detector re-scans, or when the tier rules change."""
    counters = {"recordings": 0, "events": 0, "missing_sidecar": 0}
    for rec in store.list_recordings():
        path = Path(rec["path"])
        if not path.exists():
            continue
        gods_seen, events = load_sidecar(path)
        if not events and not sidecar_path(path).exists():
            counters["missing_sidecar"] += 1
            continue
        god = rec.get("god") or god_for(path, gods_seen, recordings_dir)
        counters["events"] += store.replace_events(int(rec["id"]), events, god)
        counters["recordings"] += 1
    log(f"[vodsearch] events refreshed: {counters['recordings']} recordings, "
        f"{counters['events']} events, {counters['missing_sidecar']} without a sidecar")
    return counters


def prune_missing(store: Store, log: Logger = print) -> int:
    gone = 0
    for rec in store.list_recordings():
        if not Path(rec["path"]).exists():
            store.delete_recording(int(rec["id"]))
            gone += 1
            log(f"  pruned missing: {rec['path']}")
    return gone


def fmt_hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def run(opts: IndexOptions, log: Logger = print) -> dict:
    """Index everything under opts.recordings_dir. Returns counters."""
    files = discover(opts.recordings_dir, opts.include_root, opts.skip_dirs)
    if opts.limit:
        files = files[: int(opts.limit)]
    counters = {"found": len(files), "indexed": 0, "skipped": 0, "errors": 0,
                "segments": 0, "audio_s": 0.0, "pruned": 0}
    log(f"[vodsearch] {len(files)} recording(s) under {opts.recordings_dir}"
        f" -> {opts.db_path} (model {opts.model}, {opts.device})")
    if opts.dry_run:
        log("[vodsearch] dry run: nothing will be transcribed or written")
    store = Store(opts.db_path)
    transcriber = Transcriber(opts.model, opts.device, opts.compute_type,
                              language=opts.language, batch_size=opts.batch_size)
    t_run = time.time()
    try:
        if opts.prune_missing and not opts.dry_run:
            counters["pruned"] = prune_missing(store, log)
        for i, path in enumerate(files, 1):
            label = f"[{i}/{len(files)}] {path.relative_to(opts.recordings_dir)}"
            try:
                res = index_recording(store, transcriber, path, opts, log)
            except KeyboardInterrupt:
                rec = store.find_recording(path)
                if rec:
                    store.set_status(int(rec["id"]), "error", "interrupted")
                log(f"{label}: interrupted")
                raise
            except Exception as e:  # keep going; one bad file must not stop the run
                counters["errors"] += 1
                rec = store.find_recording(path)
                if rec:
                    store.set_status(int(rec["id"]), "error", f"{type(e).__name__}: {e}"[:500])
                log(f"{label}: ERROR {type(e).__name__}: {e}")
                continue
            if res.get("skipped"):
                counters["skipped"] += 1
                if res["skipped"] != "current":
                    log(f"{label}: skipped ({res['skipped']})")
                continue
            if res.get("dry_run"):
                log(f"{label}: would index god={res['god']} "
                    f"{fmt_hms(res['duration_s'])} tracks={res['tracks']} events={res['events']}")
                continue
            counters["indexed"] += 1
            counters["segments"] += int(res.get("segments") or 0)
            counters["audio_s"] += float(res.get("duration_s") or 0)
            speed = (res["duration_s"] / res["elapsed_s"]) if res.get("elapsed_s") else 0
            log(f"{label}: {res['god'] or '?'} {fmt_hms(res['duration_s'])}, "
                f"{res['segments']} segments, {res['events']} events, "
                f"{res['elapsed_s']:.0f}s ({speed:.1f}x realtime)")
    finally:
        store.close()
    counters["elapsed_s"] = time.time() - t_run
    log(f"[vodsearch] done: {counters['indexed']} indexed, {counters['skipped']} skipped, "
        f"{counters['errors']} errors, {counters['pruned']} pruned; "
        f"{fmt_hms(counters['audio_s'])} of audio in {fmt_hms(counters['elapsed_s'])}")
    return counters
