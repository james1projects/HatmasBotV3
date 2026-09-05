"""
vodsearch command line.

    python -m vodsearch.cli index  --recordings <dir> --db <file> [--model large-v3] [--force] [--limit N] [--dry-run]
    python -m vodsearch.cli search "<words>" [--god Ymir] [--event kill|multikill|death|any] [--limit 20] [--semantic]
    python -m vodsearch.cli embed                  # vectors for semantic search (local Ollama, incremental)
    python -m vodsearch.cli browse [--god Ymir] [--event kill]
    python -m vodsearch.cli clip   --segment ID | --event ID | --recording ID --start S [--end E]  [--out DIR]
    python -m vodsearch.cli stats
    python -m vodsearch.cli events                 # re-read sidecars only (tier rules changed / detector re-scanned)
    python -m vodsearch.cli publish   --god Ymir | --recording 12 | --folder Sylvanus | --all
    python -m vodsearch.cli unpublish --all        # everything private again (the default state)

HatmasBot's `tools/vod_index.py` calls `main(argv, defaults=...)` with
the repo's config values so none of the paths need typing on the
Stream Deck. Standalone use just passes flags.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import clips as clips_mod
from .indexer import (DEFAULT_TRACKS, IndexOptions, embed_pending, fmt_hms, refresh_events,
                      run as run_index)
from .transcribe import DEFAULT_INITIAL_PROMPT
from .store import Store


def parse_tracks(spec: str) -> Dict[int, str]:
    """'1:hatmaster,2:discord,3:misc' -> {1: 'hatmaster', ...}. A bare
    number gets the label 'track<N>'."""
    out: Dict[int, str] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            idx, label = part.split(":", 1)
            out[int(idx)] = label.strip() or f"track{int(idx)}"
        else:
            out[int(part)] = f"track{int(part)}"
    return out


def build_parser(defaults: Optional[dict] = None) -> argparse.ArgumentParser:
    d = dict(defaults or {})
    db_default = d.get("db_path")
    rec_default = d.get("recordings_dir")
    p = argparse.ArgumentParser(prog="vodsearch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_db(sp):
        sp.add_argument("--db", type=Path, default=db_default, required=db_default is None,
                        help="SQLite index file")

    ix = sub.add_parser("index", help="transcribe + index recordings (incremental)")
    ix.add_argument("--recordings", type=Path, default=rec_default, required=rec_default is None)
    add_db(ix)
    ix.add_argument("--model", default=d.get("model", "large-v3"))
    ix.add_argument("--device", default=d.get("device", "cuda"))
    ix.add_argument("--compute-type", default=d.get("compute_type", "float16"))
    ix.add_argument("--language", default=d.get("language", "en"))
    ix.add_argument("--batch-size", type=int, default=int(d.get("batch_size", 16)))
    ix.add_argument("--prompt", default=d.get("initial_prompt", None),
                    help="Whisper initial prompt (punctuation style); '' disables; default is a neutral one")
    ix.add_argument("--tracks", default=d.get("tracks", "1:hatmaster,2:friends,3:friends"),
                    help="audio tracks to transcribe as idx:speaker, e.g. 1:hatmaster,2:discord")
    ix.add_argument("--min-speech", type=float, default=float(d.get("min_speech_frac", 0.02)),
                    help="skip a track when fewer than this fraction of 0.5s windows have signal")
    ix.add_argument("--min-duration", type=float, default=float(d.get("min_duration_s", 8)))
    ix.add_argument("--ffmpeg", default=d.get("ffmpeg", "ffmpeg"))
    ix.add_argument("--ffprobe", default=d.get("ffprobe", "ffprobe"))
    ix.add_argument("--force", action="store_true", help="re-index even if current")
    ix.add_argument("--limit", type=int, default=None, help="only the N newest recordings")
    ix.add_argument("--include-root", action="store_true",
                    help="also index unsorted files in the recordings root")
    ix.add_argument("--no-prune", action="store_true", help="keep rows whose file is gone")
    ix.add_argument("--no-embed", action="store_true", help="skip semantic embeddings after indexing")
    ix.add_argument("--embed-host", default=d.get("embed_host", "http://localhost:11434"))
    ix.add_argument("--embed-model", default=d.get("embed_model", "nomic-embed-text"))
    ix.add_argument("--dry-run", action="store_true")

    se = sub.add_parser("search", help="full-text search over the transcript")
    se.add_argument("query", nargs="+")
    add_db(se)
    se.add_argument("--god")
    se.add_argument("--event", choices=["any", "kill", "multikill", "death", "assist"])
    se.add_argument("--speaker")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--semantic", action="store_true", help="rank by meaning (needs embeddings)")
    se.add_argument("--embed-host", default=d.get("embed_host", "http://localhost:11434"))
    se.add_argument("--embed-model", default=d.get("embed_model", "nomic-embed-text"))

    em = sub.add_parser("embed", help="embed transcript lines for semantic search (incremental)")
    add_db(em)
    em.add_argument("--embed-host", default=d.get("embed_host", "http://localhost:11434"))
    em.add_argument("--embed-model", default=d.get("embed_model", "nomic-embed-text"))

    br = sub.add_parser("browse", help="newest detector events with nearby speech")
    add_db(br)
    br.add_argument("--god")
    br.add_argument("--event", choices=["any", "kill", "multikill", "death", "assist"], default="any")
    br.add_argument("--limit", type=int, default=20)

    cl = sub.add_parser("clip", help="render a clip for a segment / event / time span")
    add_db(cl)
    cl.add_argument("--segment", type=int)
    cl.add_argument("--event", type=int)
    cl.add_argument("--recording", type=int)
    cl.add_argument("--start", type=float)
    cl.add_argument("--end", type=float)
    cl.add_argument("--pre", type=float, default=clips_mod.DEFAULT_PRE_S)
    cl.add_argument("--post", type=float, default=clips_mod.DEFAULT_POST_S)
    cl.add_argument("--out", type=Path, default=d.get("clips_dir", Path("clips")))
    cl.add_argument("--height", type=int, default=int(d.get("clip_height", 720)))
    cl.add_argument("--encoder", default=d.get("encoder", "h264_nvenc"))
    cl.add_argument("--audio-tracks", default=d.get("audio_tracks", "0,1"),
                    help="comma-separated audio track indexes to mix into the clip")
    cl.add_argument("--ffmpeg", default=d.get("ffmpeg", "ffmpeg"))

    ev = sub.add_parser("events", help="re-read every .events.json sidecar (tiers/kinds), no transcription")
    ev.add_argument("--recordings", type=Path, default=rec_default, required=rec_default is None)
    add_db(ev)

    for name, help_text in (("publish", "mark recordings PUBLIC (visible on the site)"),
                            ("unpublish", "mark recordings PRIVATE (the default)")):
        vp = sub.add_parser(name, help=help_text)
        add_db(vp)
        vp.add_argument("--recording", type=int, action="append", default=[], help="recording id (repeatable)")
        vp.add_argument("--god", help="every done recording of this god")
        vp.add_argument("--folder", help="every done recording in this folder (e.g. Ymir, mixed, unknown)")
        vp.add_argument("--all", action="store_true", help="every done recording")

    stt = sub.add_parser("stats", help="index statistics")
    add_db(stt)
    stt.add_argument("--errors", action="store_true", help="list recordings in error state")
    return p


def _print_moments(result: dict) -> None:
    moments = result.get("moments") or []
    print(f"{result.get('total', len(moments))} result(s) [{result.get('mode')}]")
    for m in moments:
        ev = ",".join(e["kind"] for e in m.get("events") or []) or "-"
        if m.get("score") is not None:
            ev += f" {m['score']:.2f}"
        when = (m.get("recorded_at") or "")[:10]
        seg = m.get("segment_id")
        text = (m.get("text") or "").replace("\n", " ")
        print(f"  seg {seg!s:>6}  {m.get('god') or '?':<14} {when}  {fmt_hms(m['start_s']):>7}"
              f"  [{ev}]  {text[:110]}")


def cmd_index(a) -> int:
    opts = IndexOptions(
        recordings_dir=Path(a.recordings), db_path=Path(a.db), model=a.model,
        device=a.device, compute_type=a.compute_type, language=a.language or None,
        batch_size=a.batch_size, tracks=parse_tracks(a.tracks) or dict(DEFAULT_TRACKS),
        initial_prompt=(DEFAULT_INITIAL_PROMPT if a.prompt is None else (a.prompt or None)),
        min_duration_s=a.min_duration, min_speech_frac=a.min_speech,
        ffmpeg=a.ffmpeg, ffprobe=a.ffprobe, force=a.force, limit=a.limit,
        include_root=a.include_root, dry_run=a.dry_run, prune_missing=not a.no_prune,
        embed=not a.no_embed, embed_host=a.embed_host, embed_model=a.embed_model)
    counters = run_index(opts)
    return 1 if counters["errors"] and not counters["indexed"] else 0


def cmd_search(a) -> int:
    q = " ".join(a.query)
    with Store(a.db) as store:
        if a.semantic:
            from .embed import OllamaEmbedder, top_k
            emb = OllamaEmbedder(a.embed_host, a.embed_model)
            ids, mat = store.load_embeddings(emb.model)
            if ids.size == 0:
                print("no embeddings yet: run `embed` first", file=sys.stderr)
                return 2
            scored = top_k(emb.embed_query(q), ids, mat, k=a.limit * 4)
            moments = store.moments_for_segments(scored, god=a.god, event=a.event,
                                                 speaker=a.speaker, limit=a.limit)
            res = {"mode": "semantic", "total": len(moments), "moments": moments}
        else:
            res = store.search(q, god=a.god, event=a.event, speaker=a.speaker, limit=a.limit)
    _print_moments(res)
    return 0


def cmd_embed(a) -> int:
    from .embed import OllamaEmbedder
    emb = OllamaEmbedder(a.embed_host, a.embed_model)
    if not emb.available():
        print(f"Ollama not reachable at {a.embed_host} (model {a.embed_model})", file=sys.stderr)
        return 2
    with Store(a.db) as store:
        n = embed_pending(store, emb)
        print(f"{n} embedded, {store.embedding_count(emb.model)} total")
    return 0


def cmd_browse(a) -> int:
    with Store(a.db) as store:
        res = store.browse(god=a.god, event=a.event, limit=a.limit)
    _print_moments(res)
    return 0


def cmd_clip(a) -> int:
    with Store(a.db) as store:
        if a.segment:
            seg = store.get_segment(a.segment)
            if not seg:
                print(f"no segment {a.segment}", file=sys.stderr)
                return 2
            rec_id, src, dur = int(seg["recording_id"]), seg["path"], float(seg["duration_s"] or 0)
            start, end = clips_mod.clip_window(float(seg["start_s"]), float(seg["end_s"]), dur, a.pre, a.post)
        elif a.event:
            ev = store.get_event(a.event)
            if not ev:
                print(f"no event {a.event}", file=sys.stderr)
                return 2
            rec_id, src, dur = int(ev["recording_id"]), ev["path"], float(ev["duration_s"] or 0)
            pre = float(ev.get("pre_s") or 0) or a.pre
            post = float(ev.get("post_s") or 0) or a.post
            start, end = clips_mod.clip_window(float(ev["ts_s"]), None, dur, pre, post)
        elif a.recording and a.start is not None:
            rec = store.get_recording(a.recording)
            if not rec:
                print(f"no recording {a.recording}", file=sys.stderr)
                return 2
            rec_id, src, dur = int(rec["id"]), rec["path"], float(rec["duration_s"] or 0)
            start, end = clips_mod.clip_window(a.start, a.end, dur, a.pre, a.post)
        else:
            print("clip needs --segment, --event, or --recording + --start", file=sys.stderr)
            return 2
    tracks = [int(t) for t in str(a.audio_tracks).split(",") if t.strip()]
    out = Path(a.out) / clips_mod.clip_name(rec_id, start, end, a.height, tracks)
    path = clips_mod.render_clip(a.ffmpeg, src, start, end, out, tracks, a.height, a.encoder)
    print(f"{path}  ({fmt_hms(start)} - {fmt_hms(end)})")
    return 0


def cmd_events(a) -> int:
    with Store(a.db) as store:
        refresh_events(store, Path(a.recordings))
    return 0


def cmd_visibility(a) -> int:
    vis = "public" if a.cmd == "publish" else "private"
    with Store(a.db) as store:
        ids = list(a.recording or [])
        if a.god:
            ids += store.recording_ids(god=a.god)
        if a.folder:
            ids += store.recording_ids(folder=a.folder)
        if a.all:
            ids += store.recording_ids()
        if not ids:
            print("nothing selected: pass --recording ID, --god NAME, --folder NAME, or --all", file=sys.stderr)
            return 2
        n = store.set_visibility(sorted(set(ids)), vis)
        s = store.stats()
    print(f"{n} recording(s) now {vis}; archive: {s['by_visibility']}")
    return 0


def cmd_stats(a) -> int:
    with Store(a.db) as store:
        s = store.stats()
        print(f"recordings: {s['recordings']} done ({s['hours']} h), status {s['by_status']},"
              f" visibility {s.get('by_visibility')}")
        print(f"segments:   {s['segments']} ({s['words']} words)")
        print(f"events:     {s['events']}")
        print(f"range:      {s['oldest']} .. {s['newest']}")
        for g in s["gods"]:
            print(f"  {g['god']:<16} {g['recordings']:>3} rec  {g['hours']:>5} h")
        if a.errors:
            for r in store.list_recordings("error"):
                print(f"  ERROR {r['path']}: {r['error']}")
    return 0


def main(argv: Optional[List[str]] = None, defaults: Optional[dict] = None) -> int:
    a = build_parser(defaults).parse_args(argv)
    handler = {"index": cmd_index, "search": cmd_search, "browse": cmd_browse,
               "clip": cmd_clip, "stats": cmd_stats, "events": cmd_events,
               "publish": cmd_visibility, "unpublish": cmd_visibility, "embed": cmd_embed}[a.cmd]
    try:
        return handler(a)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
