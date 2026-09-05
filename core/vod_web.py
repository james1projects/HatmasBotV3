"""
core/vod_web.py — "Ask the VOD" routes for the public site.

Read-only glue between the `vodsearch` index (built offline by
tools/vod_index.py) and hatmaster.tv. Kept out of public_webserver.py
so that file stops growing; `VodWeb(server).register()` is the only
hook. Every DB call runs in a worker thread (sqlite3 is sync) and every
ffmpeg render is an asyncio subprocess, so the event loop that also
serves trading never blocks on the archive.

Routes
    GET /vod                        the search page
    GET /api/vod/stats              index size, gods, event counts
    GET /api/vod/search?q=&god=&event=&limit=&offset=
                                    q empty -> newest detector events (browse)
    GET /api/vod/clip/{key}.mp4     key = s<segment_id> | e<event_id>
    GET /api/vod/thumb/{key}.jpg    poster frame for the same key
    GET /api/vod/stream/{key}.mp4   the FULL recording from that moment on, live-
                                    transcoded as a fragmented MP4 (starts in ~1 s,
                                    limited seeking, capped at VOD_STREAM_MAX_S)

    GET /vod/review                 LOCAL ONLY: per-recording publish/unpublish page
    GET /api/vod/review             LOCAL ONLY: every recording + counts + visibility
    POST /api/vod/review            LOCAL ONLY: {"ids": [..], "visibility": "public"|"private"}
    POST /api/vod/hide              LOCAL ONLY: {"segment_id": N, "hidden": true|false}
                                    redact one transcript line from every visitor view

Access model (2026-09-05, James's privacy call):
  * A loopback browser (James at localhost) always gets the whole
    archive, toggle or not, plus file paths and the review page.
  * Everyone else gets nothing unless the "web_vod" toggle is on, and
    then only recordings whose visibility is 'public'. New recordings
    are private by default; only the review page / `vod_index.py
    publish` can flip them. Clips, thumbnails, and streams of private
    recordings 404 for non-local requests even with a valid key.

Clips are rendered on first request (H.264 720p, all voice tracks mixed
in) and cached under VOD_CLIPS_DIR; concurrent requests for the same
clip share one render, and total renders are capped by a semaphore so
a burst of viewers can't pin the GPU/CPU while a stream is live.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from aiohttp import web

from core.config import (
    VOD_CLIPS_DIR, VOD_CLIP_AUDIO_TRACKS, VOD_CLIP_CACHE_MAX_MB,
    VOD_CLIP_ENCODER, VOD_CLIP_HEIGHT, VOD_CLIP_MAX_CONCURRENT, VOD_DB_PATH,
    VOD_EMBED_HOST, VOD_EMBED_MODEL, VOD_FFMPEG, VOD_SEMANTIC_MIN_SCORE,
    VOD_STREAM_MAX_CONCURRENT, VOD_STREAM_MAX_S,
)
from vodsearch import clips as clips_mod
from vodsearch.embed import OllamaEmbedder, top_k
from vodsearch.store import Store

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
_KEY_RE = re.compile(r"^([se])(\d{1,12})$")
_EVENTS = {"any", "kill", "multikill", "death", "assist", "double", "triple", "quadra", "penta"}
# Clip windows. The detector's sidecar pre/post (7 s / 6 s) are editing
# cut points, far too tight to watch: James asked for longer previews
# (2026-09-05). Events get at least EVENT_MIN_PRE_S before and
# EVENT_MIN_POST_S after, plus EVENT_TIER_BONUS_S per extra kill in a
# streak; spoken lines get a lead-in and room after the sentence.
# ?len=short|normal|long scales the whole window (0.6x / 1x / 1.8x).
SEGMENT_PRE_S = 8.0
SEGMENT_POST_S = 15.0
SEGMENT_MAX_S = 60.0
EVENT_MIN_PRE_S = 12.0
EVENT_MIN_POST_S = 18.0
EVENT_TIER_BONUS_S = 4.0
LEN_SCALE = {"short": 0.6, "normal": 1.0, "long": 1.8}
RENDER_TIMEOUT_S = 240.0


def event_window(ev: dict, scale: float = 1.0) -> Tuple[float, float]:
    """(pre_s, post_s) for an event clip: never tighter than the minimums,
    wider when the detector's own window is wider, longer tails for
    bigger streaks, then scaled by ?len=."""
    pre = max(float(ev.get("pre_s") or 0), EVENT_MIN_PRE_S)
    post = max(float(ev.get("post_s") or 0), EVENT_MIN_POST_S)
    tier = int(ev.get("tier") or 0)
    if tier >= 2:
        post += EVENT_TIER_BONUS_S * (tier - 1)
    return pre * scale, post * scale


def _fmt_clock(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class VodWeb:
    def __init__(self, server, db_path: Path = VOD_DB_PATH,
                 clips_dir: Path = VOD_CLIPS_DIR):
        self.server = server
        self.db_path = Path(db_path)
        self.clips_dir = Path(clips_dir)
        self._sem = asyncio.Semaphore(max(1, int(VOD_CLIP_MAX_CONCURRENT)))
        self._stream_sem = asyncio.Semaphore(max(1, int(VOD_STREAM_MAX_CONCURRENT)))
        self._inflight: Dict[str, asyncio.Future] = {}
        self._last_evict = 0.0
        # Semantic search: the whole embedding matrix cached in memory,
        # reloaded when the (count, max id) version changes.
        self._embedder = OllamaEmbedder(VOD_EMBED_HOST, VOD_EMBED_MODEL)
        self._emb_version: Tuple[int, int] = (-1, -1)
        self._emb_ids = None
        self._emb_mat = None
        self._emb_lock = asyncio.Lock()

    # ── wiring ────────────────────────────────────────────────────────

    def register(self) -> None:
        r = self.server.app.router
        r.add_get("/vod", self.handle_page)
        r.add_get("/api/vod/stats", self.handle_stats)
        r.add_get("/api/vod/search", self.handle_search)
        r.add_get("/api/vod/clip/{key}.mp4", self.handle_clip)
        r.add_get("/api/vod/thumb/{key}.jpg", self.handle_thumb)
        r.add_get("/api/vod/stream/{key}.mp4", self.handle_stream)
        r.add_get("/vod/review", self.handle_review_page)
        r.add_get("/api/vod/review", self.handle_review_list)
        r.add_post("/api/vod/review", self.handle_review_set)
        r.add_post("/api/vod/hide", self.handle_hide)

    def _toggle_on(self) -> bool:
        try:
            return bool(self.server._feature_on("web_vod"))
        except Exception:
            return True

    def _enabled(self, request: Optional[web.Request] = None) -> bool:
        """Local browser: always. Tunneled visitor: only with the toggle."""
        if request is not None and self._is_local(request):
            return True
        return self._toggle_on()

    def _index_ready(self) -> bool:
        return self.db_path.exists()

    async def _with_store(self, fn: Callable[[Store], Any]) -> Any:
        def _run():
            store = Store(self.db_path)
            try:
                return fn(store)
            finally:
                store.close()
        return await asyncio.to_thread(_run)

    # ── decoration ────────────────────────────────────────────────────

    def _is_local(self, request: web.Request) -> bool:
        """Loopback browser, not a tunneled visitor. Reuses the public
        server's check when present (it knows the Cloudflare headers);
        the dev host has no such method and is local by construction."""
        fn = getattr(self.server, "_is_local_admin", None)
        if callable(fn):
            try:
                return bool(fn(request))
            except Exception:
                return False
        peer = request.remote or ""
        return peer in ("127.0.0.1", "::1")

    @staticmethod
    def _decorate(moments: List[dict], local: bool = False) -> List[dict]:
        out = []
        for m in moments:
            d = dict(m)
            if not local:
                d.pop("path", None)
                d.pop("visibility", None)
                d.pop("hidden", None)
            # Event moments (browse view) clip around the EVENT, even when a
            # spoken line sits nearby; only pure transcript hits use the
            # segment window. (Before 2026-09-05 a multikill card cut its
            # clip around the nearest sentence instead of the kill.)
            if d.get("event_id"):
                key = f"e{int(d['event_id'])}"
            elif d.get("segment_id"):
                key = f"s{int(d['segment_id'])}"
            else:
                key = None
            d["key"] = key
            d["clip_url"] = f"/api/vod/clip/{key}.mp4" if key else None
            d["thumb_url"] = f"/api/vod/thumb/{key}.jpg" if key else None
            d["stream_url"] = f"/api/vod/stream/{key}.mp4" if key else None
            d["clock"] = _fmt_clock(d.get("ts_s", d.get("start_s", 0)) or 0)
            d["date"] = (d.get("recorded_at") or "")[:10]
            out.append(d)
        return out

    # ── handlers ──────────────────────────────────────────────────────

    async def handle_page(self, request: web.Request):
        if not self._enabled(request):
            raise web.HTTPNotFound()
        return web.FileResponse(PUBLIC_DIR / "vod.html",
                                headers={"Cache-Control": "no-cache"})

    async def handle_stats(self, request: web.Request):
        if not self._enabled(request):
            raise web.HTTPNotFound()
        if not self._index_ready():
            return web.json_response({"ready": False, "recordings": 0, "hours": 0,
                                      "segments": 0, "events": {}, "gods": []})
        local = self._is_local(request)
        stats = await self._with_store(lambda s: s.stats(public_only=not local))
        stats["ready"] = stats.get("recordings", 0) > 0
        stats["local"] = local
        if not local:
            stats.pop("by_status", None)
            stats.pop("by_visibility", None)
        return web.json_response(stats, headers={"Cache-Control": "no-cache" if local else "public, max-age=60"})

    async def handle_search(self, request: web.Request):
        if not self._enabled(request):
            raise web.HTTPNotFound()
        q = (request.query.get("q") or "").strip()[:200]
        god = (request.query.get("god") or "").strip()[:40] or None
        event = (request.query.get("event") or "").strip().lower() or None
        if event and event not in _EVENTS:
            return web.json_response({"error": "bad event"}, status=400)
        try:
            limit = max(1, min(int(request.query.get("limit", 20)), 50))
            offset = max(0, min(int(request.query.get("offset", 0)), 5000))
        except ValueError:
            return web.json_response({"error": "bad paging"}, status=400)
        if not self._index_ready():
            return web.json_response({"mode": "empty", "total": 0, "moments": [], "q": q})
        local = self._is_local(request)
        key = (request.query.get("key") or "").strip()
        if key:
            # Shared link: exactly one moment by its clip key.
            if not _KEY_RE.match(key):
                return web.json_response({"error": "bad key"}, status=400)
            moment = await self._with_store(lambda s: self._moment_for_key(s, key, local))
            if moment is None or (not local and (moment.get("visibility") != "public"
                                                 or moment.get("hidden"))):
                return web.json_response({"mode": "key", "total": 0, "moments": [], "q": ""})
            return web.json_response({"mode": "key", "total": 1, "q": "",
                                      "moments": self._decorate([moment], local)},
                                     headers={"Cache-Control": "public, max-age=300"})

        mode = (request.query.get("mode") or "hybrid").lower()
        if mode not in ("hybrid", "keyword", "meaning"):
            mode = "hybrid"

        def _query(store: Store) -> dict:
            if q:
                return store.search(q, god=god, event=event, limit=limit, offset=offset,
                                    public_only=not local)
            return store.browse(god=god, event=event or "any", limit=limit, offset=offset,
                                public_only=not local)

        res = await self._with_store(_query)
        if q and mode != "keyword" and offset == 0:
            # Fill (or replace) with meaning matches: hybrid adds them when
            # the exact AND query came up short; "meaning" ranks by them only.
            need = limit if mode == "meaning" else max(0, limit - (
                len(res.get("moments") or []) if res.get("mode") == "and" else 0))
            if need > 0:
                extra = await self._semantic(q, god, event, not local, k=need + len(res.get("moments") or []))
                if extra:
                    seen = {m.get("segment_id") for m in res.get("moments") or []}
                    if mode == "meaning":
                        res["moments"] = extra[:limit]
                        res["total"] = len(res["moments"])
                        res["mode"] = "meaning"
                    else:
                        fresh = [m for m in extra if m.get("segment_id") not in seen][:need]
                        if res.get("mode") == "and":
                            res["moments"] = (res.get("moments") or []) + fresh
                        else:
                            res["moments"] = fresh + [m for m in (res.get("moments") or [])
                                                      if m.get("segment_id") not in {x["segment_id"] for x in fresh}]
                            res["moments"] = res["moments"][:limit]
                        res["total"] = max(int(res.get("total") or 0), len(res["moments"]))
                        res["mode"] = "hybrid" if fresh else res.get("mode")
        if local:
            # Editing workflow: the source file + offset, only for the
            # loopback browser. Never leaves the PC through the tunnel.
            def _attach_paths(store: Store, moments: List[dict]) -> None:
                cache: Dict[int, Optional[str]] = {}
                for m in moments:
                    rid = int(m["recording_id"])
                    if rid not in cache:
                        rec = store.get_recording(rid)
                        cache[rid] = rec["path"] if rec else None
                    m["path"] = cache[rid]
            await self._with_store(lambda st: _attach_paths(st, res.get("moments") or []))
        res["moments"] = self._decorate(res.get("moments") or [], local)
        res["local"] = local
        res["q"] = q
        res["god"] = god
        res["event"] = event
        res["limit"] = limit
        res["offset"] = offset
        return web.json_response(res, headers={"Cache-Control": "no-cache"})

    async def _semantic(self, q: str, god: Optional[str], event: Optional[str],
                        public_only: bool, k: int) -> List[dict]:
        """Meaning matches for q, filtered like search(). Empty when the
        embedder is unreachable or nothing is embedded; never raises."""
        try:
            async with self._emb_lock:
                version = await self._with_store(lambda s: s.embedding_version(self._embedder.model))
                if version[0] == 0:
                    return []
                if version != self._emb_version:
                    ids, mat = await self._with_store(lambda s: s.load_embeddings(self._embedder.model))
                    self._emb_ids, self._emb_mat, self._emb_version = ids, mat, version
            qvec = await asyncio.to_thread(self._embedder.embed_query, q)
            scored = top_k(qvec, self._emb_ids, self._emb_mat, k=max(k * 4, 40),
                           min_score=float(VOD_SEMANTIC_MIN_SCORE))
            if not scored:
                return []
            return await self._with_store(
                lambda s: s.moments_for_segments(scored, god=god, event=event,
                                                 public_only=public_only, limit=k))
        except Exception as e:
            print(f"[VodWeb] semantic search unavailable: {type(e).__name__}: {e}")
            return []

    @staticmethod
    def _moment_for_key(store: Store, key: str, local: bool = True) -> Optional[dict]:
        m = _KEY_RE.match(key)
        if not m:
            return None
        kind, ident = m.group(1), int(m.group(2))
        if kind == "s":
            seg = store.get_segment(ident)
            if not seg:
                return None
            mid = (float(seg["start_s"]) + float(seg["end_s"])) / 2.0
            return {
                "segment_id": int(seg["id"]), "recording_id": int(seg["recording_id"]),
                "path": seg.get("path"), "visibility": seg.get("visibility") or "private",
                "hidden": bool(seg.get("hidden") or 0),
                "god": seg.get("god"), "recorded_at": seg.get("recorded_at"),
                "start_s": float(seg["start_s"]), "end_s": float(seg["end_s"]),
                "speaker": seg.get("speaker"), "text": seg.get("text"),
                "snippet": seg.get("text"),
                "events": store.events_near(int(seg["recording_id"]), mid),
                "duration_s": float(seg.get("duration_s") or 0),
            }
        ev = store.get_event(ident)
        if not ev:
            return None
        ts = float(ev["ts_s"])
        near = store.segments_near(int(ev["recording_id"]), ts, window_s=12.0,
                                   public_only=not local)
        text = " ".join(x["text"] for x in near)
        return {
            "segment_id": None, "event_id": int(ev["id"]),
            "recording_id": int(ev["recording_id"]), "path": ev.get("path"),
            "visibility": ev.get("visibility") or "private",
            "god": ev.get("god") or ev.get("rec_god"), "recorded_at": ev.get("recorded_at"),
            "start_s": max(0.0, ts - float(ev.get("pre_s") or 0)),
            "end_s": ts + float(ev.get("post_s") or 0), "ts_s": ts,
            "speaker": near[0]["speaker"] if near else None,
            "text": text, "snippet": text,
            "events": [store._event_dict(ev)],
            "duration_s": float(ev.get("duration_s") or 0),
        }

    # ── clips ─────────────────────────────────────────────────────────

    async def _resolve_key(self, key: str, request: Optional[web.Request] = None
                           ) -> Optional[Tuple[int, str, float, float, float]]:
        """-> (recording_id, source_path, start_s, end_s, mid_s) or None.
        With `request`, a private recording resolves to None for anyone
        who is not the local browser, and ?len= scales the window."""
        scale = 1.0
        if request is not None:
            scale = LEN_SCALE.get((request.query.get("len") or "normal").lower(), 1.0)
        resolved = await self._resolve_key_raw(key, scale)
        if resolved is None:
            return None
        rec_id, src, start, end, mid, visibility = resolved
        if request is not None and not self._is_local(request) and visibility != "public":
            return None
        return rec_id, src, start, end, mid

    async def _resolve_key_raw(self, key: str, scale: float = 1.0):
        m = _KEY_RE.match(key or "")
        if not m:
            return None
        kind, ident = m.group(1), int(m.group(2))

        def _lookup(store: Store):
            if kind == "s":
                seg = store.get_segment(ident)
                if not seg:
                    return None
                dur = float(seg.get("duration_s") or 0)
                start, end = clips_mod.clip_window(float(seg["start_s"]), float(seg["end_s"]),
                                                   dur, SEGMENT_PRE_S * scale, SEGMENT_POST_S * scale,
                                                   max_len=min(clips_mod.MAX_CLIP_S, SEGMENT_MAX_S * scale))
                mid = (float(seg["start_s"]) + float(seg["end_s"])) / 2.0
                return (int(seg["recording_id"]), seg["path"], start, end, mid,
                        seg.get("visibility") or "private")
            ev = store.get_event(ident)
            if not ev:
                return None
            dur = float(ev.get("duration_s") or 0)
            pre, post = event_window(ev, scale)
            ts = float(ev["ts_s"])
            start, end = clips_mod.clip_window(ts, None, dur, pre, post)
            return (int(ev["recording_id"]), ev["path"], start, end, ts,
                    ev.get("visibility") or "private")

        return await self._with_store(_lookup)

    async def _run_ffmpeg(self, cmd: List[str], timeout_s: float) -> Tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "ffmpeg timed out"
        return int(proc.returncode or 0), (err or b"").decode("utf-8", "replace")[-400:]

    async def _render(self, out: Path, build: Callable[[Any, Path], List[str]],
                      attempts: List[Any]) -> None:
        """Render out via ffmpeg. build(attempt, tmp) returns the command;
        attempts are tried in order. Shared by clips and thumbs."""
        if out.exists():
            return
        key = str(out)
        fut = self._inflight.get(key)
        if fut is not None:
            await fut
            return
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            async with self._sem:
                if not out.exists():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    tmp = out.with_name(out.stem + ".part" + out.suffix)
                    last = ""
                    for attempt in attempts:
                        rc, err = await self._run_ffmpeg(build(attempt, tmp), RENDER_TIMEOUT_S)
                        if rc == 0 and tmp.exists() and tmp.stat().st_size > 0:
                            tmp.replace(out)
                            break
                        last = err
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    else:
                        raise RuntimeError(last or "render failed")
            fut.set_result(True)
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)
        await asyncio.to_thread(self._evict_cache)

    def _evict_cache(self) -> None:
        """Keep the clip cache under VOD_CLIP_CACHE_MAX_MB, oldest first.
        Throttled to once a minute; runs in a thread."""
        now = time.time()
        if now - self._last_evict < 60:
            return
        self._last_evict = now
        try:
            files = [p for p in self.clips_dir.iterdir() if p.is_file() and ".part" not in p.name]
        except OSError:
            return
        limit = int(VOD_CLIP_CACHE_MAX_MB) * 1024 * 1024
        total = sum(p.stat().st_size for p in files)
        if total <= limit:
            return
        for p in sorted(files, key=lambda f: f.stat().st_mtime):
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except OSError:
                continue
            if total <= limit:
                break

    async def handle_clip(self, request: web.Request):
        if not self._enabled(request) or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key, request)
        if not resolved:
            raise web.HTTPNotFound()
        rec_id, src, start, end, _mid = resolved
        src_path = Path(src)
        if not src_path.exists():
            raise web.HTTPNotFound()
        tracks = list(VOD_CLIP_AUDIO_TRACKS)
        out = self.clips_dir / clips_mod.clip_name(rec_id, start, end, VOD_CLIP_HEIGHT, tracks)
        try:
            await self._render(
                out,
                lambda attempt, tmp: clips_mod.build_clip_cmd(
                    VOD_FFMPEG, src_path, start, end, tmp, tracks, VOD_CLIP_HEIGHT,
                    attempt[1], hwaccel=attempt[0]),
                clips_mod.render_attempts(VOD_CLIP_ENCODER))
        except Exception as e:
            print(f"[VodWeb] clip render failed for {key}: {e}")
            raise web.HTTPServiceUnavailable(text="clip render failed")
        return web.FileResponse(out, headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Type": "video/mp4",
        })

    async def handle_stream(self, request: web.Request):
        """Live-transcode the recording from the moment's start until
        VOD_STREAM_MAX_S later (or the end of the file), piping ffmpeg's
        fragmented MP4 straight into the response. The browser starts
        playing after the first fragment. Client disconnect kills ffmpeg."""
        if not self._enabled(request) or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key, request)
        if not resolved:
            raise web.HTTPNotFound()
        rec_id, src, start, _end, _mid = resolved
        src_path = Path(src)
        if not src_path.exists():
            raise web.HTTPNotFound()
        rec = await self._with_store(lambda s: s.get_recording(rec_id))
        duration = float((rec or {}).get("duration_s") or 0)
        try:
            from_s = float(request.query.get("from", start))
        except ValueError:
            from_s = start
        from_s = max(0.0, from_s)
        length = float(VOD_STREAM_MAX_S)
        if duration > 0:
            length = max(1.0, min(length, duration - from_s))
        tracks = list(VOD_CLIP_AUDIO_TRACKS)

        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "video/mp4",
            "Cache-Control": "no-store",
            "X-Vod-From": f"{from_s:.3f}",
        })
        proc = None
        async with self._stream_sem:
            for hw, enc in clips_mod.render_attempts(VOD_CLIP_ENCODER):
                cmd = clips_mod.build_stream_cmd(VOD_FFMPEG, src_path, from_s, length, tracks,
                                                 VOD_CLIP_HEIGHT, enc, hwaccel=hw)
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                first = await proc.stdout.read(64 * 1024)
                if first:
                    break
                await proc.wait()
                err = (await proc.stderr.read()).decode("utf-8", "replace")[-300:]
                print(f"[VodWeb] stream attempt {hw}/{enc} failed for {key}: {err}")
                proc = None
            if proc is None:
                raise web.HTTPServiceUnavailable(text="stream failed")
            await resp.prepare(request)
            try:
                await resp.write(first)
                while True:
                    chunk = await proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    await resp.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                pass
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        try:
            await resp.write_eof()
        except Exception:
            pass
        return resp

    # ── review (local only) ───────────────────────────────────────────

    async def handle_review_page(self, request: web.Request):
        if not self._is_local(request):
            raise web.HTTPNotFound()
        return web.FileResponse(PUBLIC_DIR / "vod_review.html",
                                headers={"Cache-Control": "no-cache"})

    async def handle_review_list(self, request: web.Request):
        if not self._is_local(request):
            raise web.HTTPNotFound()
        if not self._index_ready():
            return web.json_response({"recordings": [], "toggle": self._toggle_on()})
        rows = await self._with_store(lambda s: s.review_list())
        for r in rows:
            key = f"e{r['top_event_id']}" if r.get("top_event_id") else (
                f"s{r['first_segment_id']}" if r.get("first_segment_id") else None)
            r["thumb_url"] = f"/api/vod/thumb/{key}.jpg" if key else None
            r["key"] = key
            r["date"] = (r.get("recorded_at") or "")[:10]
            r["clock"] = _fmt_clock(r.get("duration_s") or 0)
        return web.json_response({"recordings": rows, "toggle": self._toggle_on()},
                                 headers={"Cache-Control": "no-cache"})

    async def handle_review_set(self, request: web.Request):
        if not self._is_local(request):
            raise web.HTTPNotFound()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        vis = str(body.get("visibility") or "").lower()
        if vis not in ("public", "private"):
            return web.json_response({"error": "visibility must be public or private"}, status=400)
        try:
            ids = [int(i) for i in (body.get("ids") or [])][:5000]
        except (TypeError, ValueError):
            return web.json_response({"error": "bad ids"}, status=400)
        god = (body.get("god") or "").strip()[:40] or None
        folder = (body.get("folder") or "").strip()[:80] or None

        def _apply(store: Store) -> dict:
            target = list(ids)
            if god:
                target += store.recording_ids(god=god)
            if folder:
                target += store.recording_ids(folder=folder)
            if body.get("all"):
                target += store.recording_ids()
            n = store.set_visibility(sorted(set(target)), vis)
            return {"changed": n, "by_visibility": store.stats().get("by_visibility")}

        res = await self._with_store(_apply)
        res["ok"] = True
        res["visibility"] = vis
        return web.json_response(res)

    async def handle_hide(self, request: web.Request):
        if not self._is_local(request):
            raise web.HTTPNotFound()
        try:
            body = await request.json()
            seg_id = int(body.get("segment_id"))
            hidden = bool(body.get("hidden", True))
        except Exception:
            return web.json_response({"error": "need segment_id and hidden"}, status=400)
        n = await self._with_store(lambda s: s.set_hidden([seg_id], hidden))
        return web.json_response({"ok": n > 0, "segment_id": seg_id, "hidden": hidden})

    async def handle_thumb(self, request: web.Request):
        if not self._enabled(request) or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key, request)
        if not resolved:
            raise web.HTTPNotFound()
        rec_id, src, _start, _end, mid = resolved
        src_path = Path(src)
        if not src_path.exists():
            raise web.HTTPNotFound()
        out = self.clips_dir / f"r{rec_id}_t{int(round(mid * 10))}_360.jpg"

        def build(_attempt: Any, tmp: Path) -> List[str]:
            return [VOD_FFMPEG, "-v", "error", "-y", "-nostdin",
                    "-ss", f"{max(0.0, mid):.3f}", "-i", str(src_path),
                    "-frames:v", "1", "-vf", "scale=-2:360", "-q:v", "4", str(tmp)]

        try:
            await self._render(out, build, ["image"])
        except Exception as e:
            print(f"[VodWeb] thumb render failed for {key}: {e}")
            raise web.HTTPServiceUnavailable(text="thumb render failed")
        return web.FileResponse(out, headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Type": "image/jpeg",
        })
