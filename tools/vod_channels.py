"""
vod_channels.py — "Ask the VOD" for any Twitch channel
========================================================

Add a Twitch channel by login, download its archive VODs to local disk,
run the offline detector/sorter on them with that channel's detector
profile, and transcribe/index them into the same vod_index.db James's
own recordings live in (recordings.channel = <login>).

Layout per channel (VOD_CHANNELS_ROOT/<login>/, D: by default):
    _inbox/            in-flight yt-dlp output (.part resumes next run)
    v<id>.mp4          downloaded, not yet scanned (root level = unprocessed)
    v<id>.twitch.json  Helix metadata (title, created_at, duration ...)
    <God>/v<id>.mp4    after the sorter filed it (+ .events.json + .twitch.json)

State: the registry is data/vod/channels.json (hand-editable); per-VOD
progress is the `vods` table in vod_index.db (queued -> downloading ->
downloaded -> scanned -> indexed | error | evicted).

Usage:
    python tools\\vod_channels.py discover [--game "SMITE 2"] [--first 50] [--min-viewers 20]
    python tools\\vod_channels.py add <login> [--root DIR] [--profile JSON] [--keep N] [--quality F]
    python tools\\vod_channels.py list
    python tools\\vod_channels.py sync <login> | --all [--max N] [--since 2026-08-01]
                                   [--stage download|scan|index] [--dry-run] [--retry-errors]
    python tools\\vod_channels.py status [<login>]
    python tools\\vod_channels.py remove <login> [--delete-files --yes] [--purge-index]

Calibration: the scan stage needs the channel's detector profile
(data/vod/channels/<login>/profile.json). Until it exists `sync` stops
after downloading and prints the tools\\vod_calibrate.py steps.

Twitch access is an app-access token (core/twitch_app.py); the bot's
user tokens are never touched, so this is safe to run while the bot
is up. Downloading another creator's archives is for private, local
analysis only. Recordings of other channels can never be published:
the Store refuses, and visitors never see them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import config  # noqa: E402
from core.twitch_app import TwitchApp, TwitchError, parse_duration  # noqa: E402
from vodsearch import download as dl  # noqa: E402
from vodsearch.channels import Channel, Registry, normalize_login  # noqa: E402
from vodsearch.store import Store  # noqa: E402

STAGES = ("download", "scan", "index")
LOCK_STALE_S = 12 * 3600
ON_DISK = ("downloaded", "scanned", "indexed")


# ── helpers ───────────────────────────────────────────────────────────

def _app() -> TwitchApp:
    cid, sec = config.TWITCH_CLIENT_ID, config.TWITCH_CLIENT_SECRET
    if not cid or not sec or cid.startswith("YOUR_") or sec.startswith("YOUR_"):
        raise SystemExit("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set (core/config_local.py).")
    return TwitchApp(cid, sec, cache_path=Path(config.VOD_APP_TOKEN_FILE))


def _registry() -> Registry:
    return Registry(config.VOD_CHANNELS_FILE)


def _store() -> Store:
    return Store(config.VOD_DB_PATH)


def channel_root(ch: Channel) -> Path:
    return Path(ch.root) if ch.root else Path(config.VOD_CHANNELS_ROOT) / ch.login


def profile_path(ch: Channel) -> Path:
    return Path(ch.profile) if ch.profile else Path(config.DATA_DIR) / "vod" / "channels" / ch.login / "profile.json"


def _run(cmd: List[str]) -> int:
    print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


def _keep(ch: Channel) -> int:
    return ch.keep if ch.keep >= 0 else int(config.VOD_CHANNELS_KEEP)


def _sidecar_paths(mp4: Path) -> List[Path]:
    return [mp4.with_name(mp4.stem + ".events.json"), dl.twitch_sidecar_path(mp4)]


def _find_vod_file(root: Path, vod_id: str) -> Optional[Path]:
    """Where v<id>.mp4 currently lives: root level or any god folder."""
    top = root / f"v{vod_id}.mp4"
    if top.exists():
        return top
    for sub in sorted(root.iterdir()) if root.exists() else []:
        if sub.is_dir() and not sub.name.startswith(("_", ".")):
            p = sub / f"v{vod_id}.mp4"
            if p.exists():
                return p
    return None


class SyncLock:
    def __init__(self, path: Path, force: bool = False):
        self.path, self.force = path, force

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if self.force or age > LOCK_STALE_S:
                self.path.unlink()
            else:
                raise SystemExit(f"another sync is running ({self.path}, {age / 60:.0f} min old); "
                                 f"use --force-unlock if it crashed")
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ── commands ──────────────────────────────────────────────────────────

def cmd_discover(a) -> int:
    reg = _registry()

    async def go():
        app = _app()
        try:
            gid = await app.game_id(a.game)
            if not gid:
                print(f"no Twitch game named {a.game!r}")
                return 2
            return await app.live_streams(gid, first=a.first)
        finally:
            await app.close()

    streams = asyncio.run(go())
    if isinstance(streams, int):
        return streams
    print(f"{'login':<26}{'viewers':>8}  {'res':<10}{'title'}")
    for s in streams:
        if int(s.get("viewer_count") or 0) < a.min_viewers:
            continue
        mark = "*" if s.get("user_login", "") in reg else " "
        print(f"{mark}{s.get('user_login', ''):<25}{int(s.get('viewer_count') or 0):>8}  "
              f"{'':<10}{(s.get('title') or '')[:70]}")
    print("\n* = already in the registry. Resolution is not in Helix; the first VOD tells you.")
    return 0


def cmd_add(a) -> int:
    login = normalize_login(a.login)
    reg = _registry()
    if login in reg:
        print(f"{login} is already registered ({channel_root(reg.get(login))})")
        return 1
    user_id, display = a.user_id or "", a.display or ""
    if not user_id:
        async def go():
            app = _app()
            try:
                return await app.user_by_login(login)
            finally:
                await app.close()
        u = asyncio.run(go())
        if not u:
            print(f"no Twitch user named {login!r}")
            return 2
        user_id, display = str(u["id"]), display or str(u.get("display_name") or login)
    ch = Channel(login=login, user_id=user_id, display_name=display,
                 root=str(Path(a.root).resolve()) if a.root else "",
                 profile=str(Path(a.profile).resolve()) if a.profile else "",
                 tracks=a.tracks or "", quality=a.quality or "", keep=a.keep if a.keep is not None else -1)
    reg.add(ch)
    reg.save()
    root = channel_root(ch)
    (root / "_inbox").mkdir(parents=True, exist_ok=True)
    print(f"added {login} (user id {user_id}) -> {root}")
    print(f"profile: {profile_path(ch)}  (missing: sync stops after download until you calibrate)")
    return 0


def cmd_list(a) -> int:
    reg = _registry()
    if not reg.channels:
        print(f"no channels yet ({config.VOD_CHANNELS_FILE}); try `discover` then `add <login>`")
        return 0
    with _store() as store:
        print(f"{'login':<22}{'id':<12}{'on':<4}{'prof':<6}{'vods (status)':<40}{'root'}")
        for ch in sorted(reg.channels.values(), key=lambda c: c.login):
            counts = store.vod_counts(ch.login)
            cs = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"
            print(f"{ch.login:<22}{ch.user_id:<12}{'y' if ch.enabled else 'n':<4}"
                  f"{'y' if profile_path(ch).exists() else 'n':<6}{cs:<40}{channel_root(ch)}")
    return 0


def cmd_status(a) -> int:
    reg = _registry()
    chans = [reg.get(a.login)] if a.login else sorted(reg.channels.values(), key=lambda c: c.login)
    if a.login and chans[0] is None:
        print(f"unknown channel {a.login!r}")
        return 2
    with _store() as store:
        for ch in chans:
            root = channel_root(ch)
            print(f"== {ch.login}  root={root}  free={dl.free_gb(root):.0f} GB  "
                  f"profile={'yes' if profile_path(ch).exists() else 'MISSING'}")
            for v in store.vods(channel=ch.login):
                err = f"  [{v['error']}]" if v.get("error") else ""
                print(f"  v{v['vod_id']:<12}{v['status']:<12}{(v.get('created_at') or '')[:10]}  "
                      f"{parse_duration_safe(v.get('duration_s')):>6}  {(v.get('title') or '')[:50]}{err}")
    return 0


def parse_duration_safe(d) -> str:
    try:
        s = int(float(d or 0))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def cmd_remove(a) -> int:
    reg = _registry()
    ch = reg.get(a.login)
    if ch is None:
        print(f"unknown channel {a.login!r}")
        return 2
    root = channel_root(ch)
    if a.delete_files:
        if not a.yes:
            print(f"refusing to delete {root} without --yes")
            return 1
        if root.exists():
            shutil.rmtree(root)
            print(f"deleted {root}")
    with _store() as store:
        if a.purge_index or a.delete_files:
            n = 0
            for rec in store.list_recordings():
                if rec.get("channel") == ch.login or (str(rec["path"]).lower().startswith(str(root).lower())):
                    store.delete_recording(int(rec["id"]))
                    n += 1
            m = store.delete_vods(ch.login)
            print(f"index: removed {n} recordings, {m} vod rows")
    reg.remove(ch.login)
    reg.save()
    print(f"removed {ch.login} from the registry")
    return 0


# ── sync ──────────────────────────────────────────────────────────────

def _refresh_archives(app: TwitchApp, ch: Channel, store: Store, want: int, since: Optional[str]) -> int:
    vids = asyncio.run(app.archives(ch.user_id, max_items=max(200, want), since=since))
    new = live = 0
    for v in vids:
        # An archive of a stream still in progress has no thumbnail yet
        # (Helix leaves it blank / "404_processing"); leave it out until
        # the broadcast ends so we never download a half-written VOD.
        thumb = str(v.get("thumbnail_url") or "")
        if not thumb or "404_processing" in thumb:
            live += 1
            continue
        try:
            dur = parse_duration(v.get("duration"))
        except ValueError:
            dur = 0.0
        if store.upsert_vod(str(v["id"]), ch.login, user_id=str(v.get("user_id") or ch.user_id),
                            title=v.get("title"), created_at=v.get("created_at"), duration_s=dur,
                            url=v.get("url"), view_count=int(v.get("view_count") or 0)):
            new += 1
    print(f"  helix: {len(vids)} archives listed, {new} new" + (f", {live} still live (skipped)" if live else ""))
    return new


def _download_stage(ch: Channel, store: Store, a) -> int:
    root = channel_root(ch)
    quality = ch.quality or config.VOD_CHANNELS_QUALITY
    n_max = a.max if a.max is not None else int(config.VOD_CHANNELS_MAX_PER_SYNC)
    cands = store.vods(channel=ch.login, status="queued")
    if a.retry_errors:
        cands += [v for v in store.vods(channel=ch.login, status="error")
                  if not str(v.get("error") or "").startswith(("sub_only", "unavailable"))]
    cands.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    cands = cands[:n_max]
    if not cands:
        print("  nothing queued")
        return 0
    done = failed = 0
    for v in cands:
        vid = v["vod_id"]
        need = dl.estimate_gb(float(v.get("duration_s") or 0)) + float(config.VOD_CHANNELS_MIN_FREE_GB)
        free = dl.free_gb(root)
        print(f"  v{vid}  {(v.get('created_at') or '')[:10]}  {parse_duration_safe(v.get('duration_s'))}  "
              f"{(v.get('title') or '')[:60]}")
        if a.dry_run:
            continue
        if free < need:
            print(f"  stop: {free:.0f} GB free on {root.drive or root}, need ~{need:.0f} GB")
            break
        store.set_vod_status(vid, "downloading")
        try:
            final = dl.download_vod(vid, root / "_inbox", root, quality, int(config.VOD_CHANNELS_FRAGMENTS))
        except KeyboardInterrupt:
            store.set_vod_status(vid, "error", error="interrupted")
            raise
        except dl.DownloadError as e:
            store.set_vod_status(vid, "error", error=f"{e.kind}: {e}")
            print(f"  v{vid} failed ({e.kind}): {str(e)[:160]}")
            failed += 1
            continue
        dl.write_twitch_sidecar(final, {
            "vod_id": vid, "channel": ch.login, "user_id": ch.user_id, "display_name": ch.display_name,
            "title": v.get("title"), "created_at": v.get("created_at"), "duration_s": v.get("duration_s"),
            "url": v.get("url"), "view_count": v.get("view_count"),
            "downloaded_at": datetime.now().replace(microsecond=0).isoformat(), "quality": quality})
        store.set_vod_status(vid, "downloaded", path=final, size_bytes=final.stat().st_size)
        print(f"  v{vid} -> {final}  ({final.stat().st_size / 1e9:.2f} GB)")
        done += 1
    return 1 if (cands and failed and not done and not a.dry_run) else 0


def _reconcile(ch: Channel, store: Store) -> None:
    """Follow files the sorter moved: downloaded -> scanned once the mp4
    sits in a god folder next to its events.json."""
    root = channel_root(ch)
    for v in store.vods(channel=ch.login, status="downloaded") + store.vods(channel=ch.login, status="scanned"):
        p = _find_vod_file(root, v["vod_id"])
        if p is None:
            continue
        # A --reprocess-all rescan can re-file a VOD (Thor/ -> mixed/ once a
        # later match showed a second god): follow it wherever it went.
        if p.parent != root and p.with_name(p.stem + ".events.json").exists():
            if v["status"] != "scanned" or str(p) != str(v.get("path") or ""):
                store.set_vod_status(v["vod_id"], "scanned", path=p)


def _scan_stage(ch: Channel, store: Store, a) -> Optional[int]:
    root = channel_root(ch)
    pending = sorted(root.glob("v*.mp4")) if root.exists() else []
    if not pending:
        _reconcile(ch, store)
        print("  scan: nothing at root level")
        return 0
    prof = profile_path(ch)
    if not prof.exists():
        print(f"  scan: {len(pending)} VOD(s) waiting but no detector profile at\n    {prof}")
        print("  calibrate first:\n"
              f"    python tools\\vod_calibrate.py frames \"{pending[0]}\"\n"
              f"    python tools\\vod_calibrate.py write \"{prof}\" --channel {ch.login} --kda x1,y1,x2,y2 ...\n"
              f"    python tools\\vod_calibrate.py preview \"{pending[0]}\" --profile \"{prof}\"\n"
              f"  then: python tools\\vod_channels.py sync {ch.login} --stage scan")
        return None
    if a.dry_run:
        print(f"  scan: would run process_recordings on {len(pending)} VOD(s) with {prof}")
        return 0
    rc = _run([sys.executable, str(REPO_ROOT / "tools" / "process_recordings.py"),
               "--source", str(root), "--profile", str(prof), "--keep-stem",
               "--keep-scanning", "--no-dashboard", "--no-open-browser"])
    _reconcile(ch, store)
    if rc != 0:
        print(f"  scan: process_recordings exited {rc}")
    return rc


def _index_stage(ch: Channel, store: Store, a) -> int:
    root = channel_root(ch)
    if a.dry_run:
        print("  index: would run vod_index.py")
        return 0
    tracks = ch.tracks or f"0:{ch.login}"
    rc = _run([sys.executable, str(REPO_ROOT / "tools" / "vod_index.py"), "index",
               "--recordings", str(root), "--channel", ch.login, "--tracks", tracks])
    store.conn.execute("SELECT 1")  # keep the connection alive across the subprocess
    n = 0
    for v in store.vods(channel=ch.login, status="scanned"):
        rec = store.find_recording(v["path"]) if v.get("path") else None
        if rec and rec.get("status") == "done":
            store.set_vod_status(v["vod_id"], "indexed", path=v["path"])
            n += 1
    print(f"  index: exit {rc}, {n} VOD(s) now indexed")
    return rc


def _evict(ch: Channel, store: Store, a) -> None:
    keep = _keep(ch)
    if keep <= 0:
        return
    rows = [v for v in store.vods(channel=ch.login) if v["status"] in ON_DISK]
    rows.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    for v in rows[keep:]:
        p = Path(v["path"]) if v.get("path") else None
        if a.dry_run:
            print(f"  evict: would remove v{v['vod_id']} ({p})")
            continue
        if p and p.exists():
            rec = store.find_recording(p)
            if rec:
                store.delete_recording(int(rec["id"]))
            for f in [p] + _sidecar_paths(p):
                try:
                    f.unlink()
                except OSError:
                    pass
        store.set_vod_status(v["vod_id"], "evicted", path=v.get("path"))
        print(f"  evict: v{v['vod_id']} removed (keeping newest {keep})")


def cmd_sync(a) -> int:
    reg = _registry()
    if a.all:
        chans = reg.enabled()
    else:
        if not a.login:
            print("sync needs a <login> or --all")
            return 2
        ch = reg.get(a.login)
        if ch is None:
            print(f"unknown channel {a.login!r}; `add` it first")
            return 2
        chans = [ch]
    if not chans:
        print("no enabled channels")
        return 0
    stage_n = STAGES.index(a.stage)
    lock = Path(config.DATA_DIR) / "vod" / "vod_sync.lock"
    worst = 0
    with SyncLock(lock, force=a.force_unlock), _store() as store:
        app = _app()
        try:
            for ch in chans:
                print(f"== {ch.login}  ({channel_root(ch)})")
                try:
                    _refresh_archives(app, ch, store, a.max or int(config.VOD_CHANNELS_MAX_PER_SYNC), a.since)
                except TwitchError as e:
                    print(f"  helix failed: {e}")
                    worst = max(worst, 1)
                    continue
                rc = _download_stage(ch, store, a)
                worst = max(worst, rc)
                if stage_n >= 1:
                    rc = _scan_stage(ch, store, a)
                    if rc is None:
                        continue      # no profile yet: stop this channel here, exit 0
                    worst = max(worst, rc)
                if stage_n >= 2:
                    worst = max(worst, _index_stage(ch, store, a))
                if not a.no_evict:
                    _evict(ch, store, a)
        except KeyboardInterrupt:
            print("\ninterrupted; partial downloads resume on the next sync")
            return 130
        finally:
            asyncio.run(app.close())
    return worst


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ask the VOD for any Twitch channel: register, download, scan, index.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list live channels playing a game (to pick samples)")
    d.add_argument("--game", default="SMITE 2")
    d.add_argument("--first", type=int, default=50)
    d.add_argument("--min-viewers", type=int, default=0)
    d.set_defaults(fn=cmd_discover)

    ad = sub.add_parser("add", help="register a channel by login")
    ad.add_argument("login")
    ad.add_argument("--root", help=f"download dir (default {config.VOD_CHANNELS_ROOT}\\<login>)")
    ad.add_argument("--profile", help="detector profile JSON (default data/vod/channels/<login>/profile.json)")
    ad.add_argument("--quality", help=f"yt-dlp format (default {config.VOD_CHANNELS_QUALITY})")
    ad.add_argument("--keep", type=int, help=f"VODs kept on disk (default {config.VOD_CHANNELS_KEEP}, 0 = all)")
    ad.add_argument("--tracks", help="audio tracks as index:label (default 0:<login>)")
    ad.add_argument("--display", help="display name (default from Helix)")
    ad.add_argument("--user-id", help="skip the Helix lookup")
    ad.set_defaults(fn=cmd_add)

    ls = sub.add_parser("list", help="registered channels and their VOD counts")
    ls.set_defaults(fn=cmd_list)

    s = sub.add_parser("sync", help="list new archives, download, scan, index")
    s.add_argument("login", nargs="?")
    s.add_argument("--all", action="store_true", help="every enabled channel")
    s.add_argument("--max", type=int, help=f"downloads per channel (default {config.VOD_CHANNELS_MAX_PER_SYNC})")
    s.add_argument("--since", help="ignore archives older than this date (YYYY-MM-DD)")
    s.add_argument("--stage", choices=STAGES, default="index", help="stop after this stage (default index = everything)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--retry-errors", action="store_true", help="retry VODs that failed for network/other reasons")
    s.add_argument("--no-evict", action="store_true")
    s.add_argument("--force-unlock", action="store_true", help="ignore a stale vod_sync.lock")
    s.set_defaults(fn=cmd_sync)

    st = sub.add_parser("status", help="per-VOD state")
    st.add_argument("login", nargs="?")
    st.set_defaults(fn=cmd_status)

    rm = sub.add_parser("remove", help="unregister a channel")
    rm.add_argument("login")
    rm.add_argument("--delete-files", action="store_true", help="also delete the channel root (needs --yes)")
    rm.add_argument("--yes", action="store_true")
    rm.add_argument("--purge-index", action="store_true", help="also drop its rows from vod_index.db")
    rm.set_defaults(fn=cmd_remove)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return int(a.fn(a) or 0)
    except ValueError as e:
        print(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
