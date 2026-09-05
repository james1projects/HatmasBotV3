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
    VOD_FFMPEG, VOD_STREAM_MAX_CONCURRENT, VOD_STREAM_MAX_S,
)
from vodsearch import clips as clips_mod
from vodsearch.store import Store

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
_KEY_RE = re.compile(r"^([se])(\d{1,12})$")
_EVENTS = {"any", "kill", "multikill", "death", "assist", "double", "triple", "quadra", "penta"}
SEGMENT_PRE_S = 5.0     # a spoken line: a little lead-in, a bit more after
SEGMENT_POST_S = 8.0
SEGMENT_MAX_S = 40.0    # a moment is a sentence, not a monologue
EVENT_EXTRA_POST_S = 3.0  # detector windows are cut tight for editing; breathe
RENDER_TIMEOUT_S = 240.0


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

    # ── wiring ────────────────────────────────────────────────────────

    def register(self) -> None:
        r = self.server.app.router
        r.add_get("/vod", self.handle_page)
        r.add_get("/api/vod/stats", self.handle_stats)
        r.add_get("/api/vod/search", self.handle_search)
        r.add_get("/api/vod/clip/{key}.mp4", self.handle_clip)
        r.add_get("/api/vod/thumb/{key}.jpg", self.handle_thumb)
        r.add_get("/api/vod/stream/{key}.mp4", self.handle_stream)

    def _enabled(self) -> bool:
        try:
            on = bool(self.server._feature_on("web_vod"))
        except Exception:
            on = True
        return on

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
            if d.get("segment_id"):
                key = f"s{int(d['segment_id'])}"
            elif d.get("event_id"):
                key = f"e{int(d['event_id'])}"
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
        if not self._enabled():
            raise web.HTTPNotFound()
        return web.FileResponse(PUBLIC_DIR / "vod.html",
                                headers={"Cache-Control": "no-cache"})

    async def handle_stats(self, request: web.Request):
        if not self._enabled():
            raise web.HTTPNotFound()
        if not self._index_ready():
            return web.json_response({"ready": False, "recordings": 0, "hours": 0,
                                      "segments": 0, "events": {}, "gods": []})
        stats = await self._with_store(lambda s: s.stats())
        stats["ready"] = stats.get("recordings", 0) > 0
        return web.json_response(stats, headers={"Cache-Control": "public, max-age=60"})

    async def handle_search(self, request: web.Request):
        if not self._enabled():
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
            moment = await self._with_store(lambda s: self._moment_for_key(s, key))
            if moment is None:
                return web.json_response({"mode": "key", "total": 0, "moments": [], "q": ""})
            return web.json_response({"mode": "key", "total": 1, "q": "",
                                      "moments": self._decorate([moment], local)},
                                     headers={"Cache-Control": "public, max-age=300"})

        def _query(store: Store) -> dict:
            if q:
                return store.search(q, god=god, event=event, limit=limit, offset=offset)
            return store.browse(god=god, event=event or "any", limit=limit, offset=offset)

        res = await self._with_store(_query)
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

    @staticmethod
    def _moment_for_key(store: Store, key: str) -> Optional[dict]:
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
                "path": seg.get("path"),
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
        near = store.segments_near(int(ev["recording_id"]), ts, window_s=12.0)
        text = " ".join(x["text"] for x in near)
        return {
            "segment_id": None, "event_id": int(ev["id"]),
            "recording_id": int(ev["recording_id"]), "path": ev.get("path"),
            "god": ev.get("god") or ev.get("rec_god"), "recorded_at": ev.get("recorded_at"),
            "start_s": max(0.0, ts - float(ev.get("pre_s") or 0)),
            "end_s": ts + float(ev.get("post_s") or 0), "ts_s": ts,
            "speaker": near[0]["speaker"] if near else None,
            "text": text, "snippet": text,
            "events": [store._event_dict(ev)],
            "duration_s": float(ev.get("duration_s") or 0),
        }

    # ── clips ─────────────────────────────────────────────────────────

    async def _resolve_key(self, key: str) -> Optional[Tuple[int, str, float, float, float]]:
        """-> (recording_id, source_path, start_s, end_s, mid_s) or None."""
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
                                                   dur, SEGMENT_PRE_S, SEGMENT_POST_S,
                                                   max_len=SEGMENT_MAX_S)
                mid = (float(seg["start_s"]) + float(seg["end_s"])) / 2.0
                return int(seg["recording_id"]), seg["path"], start, end, mid
            ev = store.get_event(ident)
            if not ev:
                return None
            dur = float(ev.get("duration_s") or 0)
            pre = float(ev.get("pre_s") or 0) or clips_mod.DEFAULT_PRE_S
            post = (float(ev.get("post_s") or 0) or clips_mod.DEFAULT_POST_S) + EVENT_EXTRA_POST_S
            ts = float(ev["ts_s"])
            start, end = clips_mod.clip_window(ts, None, dur, pre, post)
            return int(ev["recording_id"]), ev["path"], start, end, ts

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
        if not self._enabled() or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key)
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
        if not self._enabled() or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key)
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

    async def handle_thumb(self, request: web.Request):
        if not self._enabled() or not self._index_ready():
            raise web.HTTPNotFound()
        key = request.match_info.get("key", "")
        resolved = await self._resolve_key(key)
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
