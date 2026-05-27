"""
BackupManagerPlugin
===================
Periodic gzipped backups of economy.db using SQLite's native backup API.

Why the SQLite backup API and not shutil.copy:
  * SQLite has a special online-backup interface that handles
    concurrent writes correctly. A naive file copy on a database
    that's actively being written to can capture a partially-written
    page, producing a backup that won't open. The backup API copies
    page by page with proper locking — guaranteed-consistent output
    even mid-transaction.

Files land in data/backups/ as economy_YYYYMMDD_HHMMSS.db.gz (or
.db if compression is disabled). The plugin auto-deletes anything
older than BACKUP_RETENTION_DAYS, so storage stays bounded — by
default you have rolling 7 days of recovery available.

The bot has to be up for backups to fire. If your streaming schedule
is sporadic and you want guaranteed daily backups regardless, set up
a Windows Task Scheduler job that copies economy.db (or runs a small
script) on a schedule.
"""

import asyncio
import gzip
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.config import (
    BACKUP_DIR,
    BACKUP_INTERVAL_HOURS,
    BACKUP_RETENTION_DAYS,
    BACKUP_INITIAL_DELAY,
    BACKUP_COMPRESS,
    ECONOMY_DB_PATH,
)


class BackupManagerPlugin:
    def __init__(self):
        self.bot = None
        self._task: Optional[asyncio.Task] = None

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        # Make sure the backup folder exists.
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._backup_loop())
        print(f"[Backup] Daily backups -> {BACKUP_DIR} "
              f"(retain {BACKUP_RETENTION_DAYS} days, "
              f"{'gzipped' if BACKUP_COMPRESS else 'uncompressed'})")

    async def cleanup(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ──────────────────────────────────────────────────────────────────

    async def _backup_loop(self):
        # Initial wait so other plugins finish initializing first.
        try:
            await asyncio.sleep(BACKUP_INITIAL_DELAY)
        except asyncio.CancelledError:
            raise

        while True:
            try:
                await self._take_backup()
                await self._prune_old_backups()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad backup doesn't break the loop. Log and try
                # again on the next cycle.
                print(f"[Backup] cycle error: {e}")

            try:
                await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
            except asyncio.CancelledError:
                raise

    async def _take_backup(self):
        src = Path(ECONOMY_DB_PATH)
        if not src.exists():
            print(f"[Backup] source DB missing: {src}")
            return

        # SQLite backup runs synchronously; offload to a thread so we
        # don't block the asyncio loop on a multi-MB file.
        loop = asyncio.get_running_loop()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = ".db.gz" if BACKUP_COMPRESS else ".db"
        dst = Path(BACKUP_DIR) / f"economy_{ts}{ext}"

        def do_backup():
            # Always write to an uncompressed temp first so we can use
            # the SQLite backup API directly. Compress as a second pass.
            tmp_path = dst.with_suffix(".tmp")
            try:
                src_conn = sqlite3.connect(str(src))
                tmp_conn = sqlite3.connect(str(tmp_path))
                try:
                    with tmp_conn:
                        src_conn.backup(tmp_conn)
                finally:
                    tmp_conn.close()
                    src_conn.close()

                if BACKUP_COMPRESS:
                    # Gzip the temp into the final .db.gz, then delete
                    # the temp. mtime preserved as a courtesy.
                    with open(tmp_path, "rb") as f_in, \
                            gzip.open(dst, "wb", compresslevel=6) as f_out:
                        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
                    tmp_path.unlink()
                else:
                    tmp_path.replace(dst)
            except Exception:
                # Clean up partial output if anything went sideways.
                for p in (tmp_path, dst):
                    if p.exists():
                        try:
                            p.unlink()
                        except Exception:
                            pass
                raise

        await loop.run_in_executor(None, do_backup)

        size_mb = dst.stat().st_size / (1024 * 1024)
        print(f"[Backup] wrote {dst.name} ({size_mb:.2f} MB)")

    async def _prune_old_backups(self):
        cutoff = time.time() - (BACKUP_RETENTION_DAYS * 86400)
        loop = asyncio.get_running_loop()

        def do_prune():
            removed = 0
            for f in Path(BACKUP_DIR).glob("economy_*.db*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except Exception:
                    pass
            return removed

        removed = await loop.run_in_executor(None, do_prune)
        if removed:
            print(f"[Backup] pruned {removed} expired backup(s) "
                  f"(older than {BACKUP_RETENTION_DAYS}d)")
