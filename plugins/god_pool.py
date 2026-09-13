"""
GodPoolPlugin
=============
Viewer-driven god voting. Each Twitch viewer can nominate one god per
day via `!nominate <god>`. The god joins a pool if it's not already
there; repeat votes for the same god increment its popularity counter
so the broadcaster can see which picks are most-wanted.

Commands:
    !nominate <god>   Everyone — add god to pool (1/day per user)
    !pool             Everyone — top 5 most-voted gods readout
    !spin             Mod-only — picks random god from pool, removes it
    !poolclear        Mod-only — wipe the pool

The daily window resets automatically because the votes table is
keyed on (voter_username, vote_date). Old vote rows are pruned on
each startup so the table doesn't grow unbounded.

Data lives in economy.db (we open our own aiosqlite connection so
this plugin works even if the economy plugin is disabled).

The website's /community page reads the pool live via /api/community
and renders it in a card next to (or instead of) the godreq queue.
"""

import asyncio
import random
from datetime import date
from pathlib import Path
from typing import List, Optional

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

from core import aspect_roster
from core.aspect_roster import display_god
from core import db as _shared_db
from core.config import BASE_DIR, ECONOMY_DB_PATH, TWITCH_CHANNEL
from core.god_resolver import resolve_sync, split_aspect
from core.youtube_parser import load_known_gods


VOTE_RETENTION_DAYS = 30  # prune ancient vote records on startup


class GodPoolPlugin:
    def __init__(self):
        self.bot = None
        self._db: Optional["aiosqlite.Connection"] = None
        self._known_gods: List[str] = []

    def setup(self, bot):
        self.bot = bot
        bot.register_command("nominate", self.cmd_nominate,
                             description="Nominate a god for the pool", identity=True, plugin="god_pool")
        bot.register_command("pool", self.cmd_pool,
                             description="Current god pool nominations", plugin="god_pool")
        bot.register_command("spin", self.cmd_spin,
                             mod_only=True, description="Spin the god pool wheel", plugin="god_pool")
        bot.register_command("poolclear", self.cmd_pool_clear,
                             mod_only=True, description="Clear the god pool", plugin="god_pool")

        # Load the known god list eagerly here — NOT in on_ready().
        # setup() runs synchronously during plugin registration in
        # main.py, well before bot.start() subscribes to chat events.
        # If we waited until on_ready(), there would be a startup race:
        # chat subscription happens in bot.setup_hook() BEFORE the
        # per-plugin on_ready() loop runs, so a !nominate command
        # arriving in that window would hit an empty _known_gods list
        # and every input — regardless of case — would be reported as
        # "Unknown god". load_known_gods is a synchronous filesystem
        # scan, perfectly safe to call here.
        self._known_gods = load_known_gods(BASE_DIR)

        # Register our schema with the shared DB. Runs once at
        # core.db.init_db() time, before any plugin's on_ready fires.
        if _shared_db.is_available():
            _shared_db.register_schema(self._init_schema)

    async def on_ready(self):
        if not _shared_db.is_available():
            print("[GodPool] aiosqlite missing — disabled")
            return

        # Schema callback already ran inside core.db.init_db(); it
        # cached the connection on self._db for us. Defensive get_db()
        # handles the unlikely case of init_db being skipped.
        if self._db is None:
            self._db = await _shared_db.get_db()
        if self._db is None:
            print("[GodPool] DB unavailable — init_db may not have run")
            return

        await self._prune_old_votes()
        # _known_gods was already loaded in setup() to avoid the
        # startup race described there. Refresh here in case new
        # god icons landed between setup and on_ready (rare, but
        # cheap and idempotent).
        self.reload_known_gods()

    def reload_known_gods(self):
        """Re-scan data/god_icons/ for the validation list. Also
        called by godrequest's roster-refresh loop after it downloads
        art for a newly released god, so !nominate knows the god
        without a bot restart."""
        self._known_gods = load_known_gods(BASE_DIR)
        print(f"[GodPool] {len(self._known_gods)} gods loaded for "
              f"validation")

    async def cleanup(self):
        # Shared connection is closed by main.py / core.db at shutdown.
        # Just clear our reference.
        self._db = None

    # ──────────────────────────────────────────────────────────────────
    #   SCHEMA
    # ──────────────────────────────────────────────────────────────────

    async def _init_schema(self, conn):
        """Schema callback registered with core.db. Stores the shared
        connection on self._db so existing methods keep working.

        use_aspect (0/1) is part of the pool identity: a base-god
        nomination and an aspect nomination for the same god are
        separate pool entries with separate vote counts, so the PK is
        (god_name, use_aspect). Pre-aspect DBs have a god_name-only PK
        and are rebuilt in place (SQLite can't ALTER a primary key);
        existing rows migrate as use_aspect = 0."""
        self._db = conn
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS god_pool (
                god_name     TEXT NOT NULL,
                use_aspect   INTEGER NOT NULL DEFAULT 0,
                added_by     TEXT NOT NULL,
                vote_count   INTEGER NOT NULL DEFAULT 1,
                added_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (god_name, use_aspect)
            );

            CREATE TABLE IF NOT EXISTS god_pool_votes (
                voter_username TEXT NOT NULL,
                vote_date      TEXT NOT NULL,  -- YYYY-MM-DD
                god_name       TEXT NOT NULL,
                voted_at       TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (voter_username, vote_date)
            );
        """)

        # ── migrations for pre-aspect DBs ──
        async with self._db.execute(
                "PRAGMA table_info(god_pool)") as cur:
            pool_cols = {row[1] for row in await cur.fetchall()}
        if "use_aspect" not in pool_cols:
            await self._db.executescript("""
                CREATE TABLE god_pool_new (
                    god_name     TEXT NOT NULL,
                    use_aspect   INTEGER NOT NULL DEFAULT 0,
                    added_by     TEXT NOT NULL,
                    vote_count   INTEGER NOT NULL DEFAULT 1,
                    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (god_name, use_aspect)
                );
                INSERT INTO god_pool_new
                       (god_name, use_aspect, added_by, vote_count,
                        added_at)
                SELECT god_name, 0, added_by, vote_count, added_at
                  FROM god_pool;
                DROP TABLE god_pool;
                ALTER TABLE god_pool_new RENAME TO god_pool;
            """)
            print("[GodPool] god_pool migrated to (god_name, "
                  "use_aspect) primary key")

        # god_pool_votes keeps its (voter, date) PK — use_aspect here
        # is display-only ("you already nominated Khepri (Aspect)").
        async with self._db.execute(
                "PRAGMA table_info(god_pool_votes)") as cur:
            vote_cols = {row[1] for row in await cur.fetchall()}
        if "use_aspect" not in vote_cols:
            await self._db.execute(
                "ALTER TABLE god_pool_votes "
                "ADD COLUMN use_aspect INTEGER NOT NULL DEFAULT 0")
            vote_cols.add("use_aspect")

        # ── user_uuid (core/users.py, docs/USER_IDENTITY_PLAN.md) ──
        # god_pool.added_by stays as display text; added_by_uuid is
        # the key. god_pool_votes is rebuilt with PRIMARY KEY
        # (user_uuid, vote_date) -- the daily cap is per person, on
        # every platform. Legacy "yt:<name>" rows (YouTube nominations
        # approved from /community) cannot be mapped and are dropped;
        # they only ever enforced a cap that has since expired.
        from core import users as _users
        if "added_by_uuid" not in pool_cols:
            await self._db.execute(
                "ALTER TABLE god_pool ADD COLUMN added_by_uuid TEXT")
        if "user_uuid" not in vote_cols:
            rows = []
            async with self._db.execute(
                    "SELECT voter_username, vote_date, god_name, voted_at, "
                    "use_aspect FROM god_pool_votes") as cur:
                rows = await cur.fetchall()
            await self._db.executescript("""
                CREATE TABLE god_pool_votes_v2 (
                    user_uuid      TEXT NOT NULL,
                    vote_date      TEXT NOT NULL,  -- YYYY-MM-DD
                    god_name       TEXT NOT NULL,
                    voted_at       TEXT NOT NULL DEFAULT (datetime('now')),
                    use_aspect     INTEGER NOT NULL DEFAULT 0,
                    voter_username TEXT,           -- display only
                    PRIMARY KEY (user_uuid, vote_date)
                );
            """)
            kept = 0
            for voter, vdate, god, voted_at, use_aspect in rows:
                voter = (voter or "").lower()
                if not voter or voter.startswith("yt:"):
                    continue
                uid = await _users.get_or_create_twitch_login(
                    self._db, voter, commit=False)
                await self._db.execute(
                    "INSERT OR IGNORE INTO god_pool_votes_v2 (user_uuid, "
                    "vote_date, god_name, voted_at, use_aspect, voter_username) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, vdate, god, voted_at, use_aspect or 0, voter))
                kept += 1
            await self._db.executescript("""
                DROP TABLE god_pool_votes;
                ALTER TABLE god_pool_votes_v2 RENAME TO god_pool_votes;
            """)
            print(f"[GodPool] god_pool_votes re-keyed on user_uuid "
                  f"({kept}/{len(rows)} rows kept)")

        await self._db.commit()

    async def _prune_old_votes(self):
        """Drop vote records older than VOTE_RETENTION_DAYS so the
        table doesn't grow without bound."""
        await self._db.execute("""
            DELETE FROM god_pool_votes
             WHERE vote_date < date('now', ?)
        """, (f"-{VOTE_RETENTION_DAYS} days",))
        await self._db.commit()

    # ──────────────────────────────────────────────────────────────────
    #   PERMISSIONS
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_broadcaster(chatter) -> bool:
        """True if the chatter is the channel broadcaster.

        Mirrors the broadcaster checks in core.bot.HatmasBot.is_mod
        without giving regular mods unlimited nominations — vote
        stacking would let a mod single-handedly skew the spin pool,
        so the bypass is intentionally narrower than is_mod()."""
        if chatter is None:
            return False
        if getattr(chatter, "broadcaster", False):
            return True
        badges = getattr(chatter, "badges", None) or []
        for badge in badges:
            badge_id = badge.id if hasattr(badge, "id") else str(badge)
            if badge_id == "broadcaster":
                return True
        name = getattr(chatter, "name", "")
        return bool(name) and name.lower() == TWITCH_CHANNEL.lower()

    # ──────────────────────────────────────────────────────────────────
    #   GOD NAME RESOLUTION
    # ──────────────────────────────────────────────────────────────────

    def _resolve_god(self, raw: str) -> Optional[str]:
        """Match user input to a known god via the shared tiered
        resolver (exact/alias/prefix/contains/fuzzy — see
        core/god_resolver.py). Returns proper-cased name."""
        if not raw or not raw.strip():
            return None
        hit = resolve_sync(raw, self._known_gods)
        return hit[0] if hit else None

    # ──────────────────────────────────────────────────────────────────
    #   COMMANDS
    # ──────────────────────────────────────────────────────────────────

    async def do_nominate(self, user_uuid: str, raw_god: str,
                          is_broadcaster: bool = False,
                          use_aspect: bool = False,
                          display_name: Optional[str] = None) -> dict:
        """Pure nomination logic. Callable from any context (chat
        command, webserver endpoint) — same pattern as ``do_spin``.

        The word "aspect" anywhere in ``raw_god`` requests the god's
        Aspect (alternate kit): "Khepri aspect please" nominates
        Khepri with use_aspect=1. Web callers pass the checkbox state
        via the ``use_aspect`` param instead; either signal wins.
        (god, aspect) is the pool identity — Khepri and Khepri
        (Aspect) are separate entries with separate vote counts.

        Chat and web nominations share the daily cap because both key
        god_pool_votes on the viewer's user_uuid (core/users.py), so a
        YouTube-login nomination on the site and a chat nomination by
        the same linked person count as one.

        Returns a dict for the caller to phrase feedback from:
            {"ok": False, "reason": "no_db"}
            {"ok": False, "reason": "unknown_god"}
            {"ok": False, "reason": "no_aspect", "god": ...}
            {"ok": False, "reason": "already_voted", "god": <today's
             pick>, "use_aspect": bool}
            {"ok": True, "god": ..., "use_aspect": bool, "votes": n,
             "pool_size": n}
        """
        if not self._db:
            return {"ok": False, "reason": "no_db"}
        user_uuid = (user_uuid or "").strip()
        if not user_uuid:
            return {"ok": False, "reason": "no_user"}
        added_by = display_name or user_uuid

        cleaned, aspect_word = split_aspect(raw_god or "")
        use_aspect = bool(use_aspect or aspect_word)

        god = self._resolve_god(cleaned.strip())
        if not god:
            return {"ok": False, "reason": "unknown_god"}

        # Aspect requests only make sense for gods that have one —
        # validated against the wiki-refreshed aspect roster.
        if use_aspect and not aspect_roster.has_aspect(god):
            return {"ok": False, "reason": "no_aspect", "god": god}

        aspect_i = 1 if use_aspect else 0
        today = date.today().isoformat()

        # Already voted today? Broadcaster bypasses this check so
        # Hatmaster can seed/curate the spin pool freely. The
        # corresponding god_pool_votes insert below is also skipped
        # for the broadcaster — see comment there.
        if not is_broadcaster:
            async with self._db.execute(
                    "SELECT god_name, use_aspect FROM god_pool_votes "
                    "WHERE user_uuid = ? AND vote_date = ?",
                    (user_uuid, today)) as cur:
                row = await cur.fetchone()
            if row:
                return {"ok": False, "reason": "already_voted",
                        "god": row[0], "use_aspect": bool(row[1])}

        # Add to pool (or increment vote count if already there).
        # The added_by column captures the FIRST nominator only.
        await self._db.execute("""
            INSERT INTO god_pool (god_name, use_aspect, added_by,
                                  added_by_uuid, vote_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(god_name, use_aspect) DO UPDATE SET
                vote_count = vote_count + 1
        """, (god, aspect_i, added_by, user_uuid))
        # Only viewers populate god_pool_votes — that table exists to
        # enforce the 1/day cap, which the broadcaster bypasses. Skipping
        # the insert avoids hitting the (voter_username, vote_date) PK
        # on Hatmaster's second nomination of the day.
        if not is_broadcaster:
            await self._db.execute("""
                INSERT INTO god_pool_votes (user_uuid, vote_date,
                                            god_name, use_aspect,
                                            voter_username)
                VALUES (?, ?, ?, ?, ?)
            """, (user_uuid, today, god, aspect_i, added_by))
        await self._db.commit()

        # Total pool size for the reply.
        async with self._db.execute(
                "SELECT COUNT(*) FROM god_pool") as cur:
            row = await cur.fetchone()
        n_in_pool = row[0] if row else 0

        # Look up vote count for this (god, aspect) entry.
        async with self._db.execute(
                "SELECT vote_count FROM god_pool "
                "WHERE god_name = ? AND use_aspect = ?",
                (god, aspect_i)) as cur:
            row = await cur.fetchone()
        votes = row[0] if row else 1

        return {"ok": True, "god": god, "use_aspect": use_aspect,
                "votes": int(votes), "pool_size": int(n_in_pool)}

    async def list_pool(self) -> list:
        """Every pool entry for the control panel, most-voted first.
        [{god, use_aspect, added_by, votes, added_at}]"""
        if not self._db:
            return []
        out = []
        async with self._db.execute("""
            SELECT god_name, use_aspect, added_by, vote_count, added_at
              FROM god_pool
             ORDER BY vote_count DESC, god_name ASC, use_aspect ASC
        """) as cur:
            async for r in cur:
                out.append({"god": r[0], "use_aspect": bool(r[1]),
                            "added_by": r[2], "votes": int(r[3] or 0),
                            "added_at": r[4]})
        return out

    async def set_votes(self, raw_god: str, use_aspect: bool,
                        votes: int) -> dict:
        """Control-panel override of a pool entry's vote count.

        Absolute set: votes >= 1 upserts the (god, aspect) entry to
        exactly that count (new entries get added_by='control_panel');
        votes <= 0 deletes the entry. The god name goes through the
        same resolver as chat, so "khepri" works. Like !poolclear,
        this never touches god_pool_votes — a viewer's daily
        nomination record stands even if Hatmaster prunes their vote.

        Returns:
            {"ok": False, "reason": "no_db" | "unknown_god" |
             "no_aspect"}
            {"ok": True, "god", "use_aspect", "votes", "removed"}
        """
        if not self._db:
            return {"ok": False, "reason": "no_db"}

        god = self._resolve_god((raw_god or "").strip())
        if not god:
            return {"ok": False, "reason": "unknown_god"}
        if use_aspect and not aspect_roster.has_aspect(god):
            return {"ok": False, "reason": "no_aspect", "god": god}

        aspect_i = 1 if use_aspect else 0
        votes = max(0, min(999, int(votes)))

        if votes == 0:
            cur = await self._db.execute(
                "DELETE FROM god_pool "
                "WHERE god_name = ? AND use_aspect = ?",
                (god, aspect_i))
            await self._db.commit()
            removed = cur.rowcount > 0
            print(f"[GodPool] control panel removed "
                  f"{display_god(god, use_aspect)} from pool "
                  f"(existed: {removed})")
            return {"ok": True, "god": god, "use_aspect": bool(use_aspect),
                    "votes": 0, "removed": removed}

        await self._db.execute("""
            INSERT INTO god_pool (god_name, use_aspect, added_by,
                                  vote_count)
            VALUES (?, ?, 'control_panel', ?)
            ON CONFLICT(god_name, use_aspect) DO UPDATE SET
                vote_count = excluded.vote_count
        """, (god, aspect_i, votes))
        await self._db.commit()
        print(f"[GodPool] control panel set "
              f"{display_god(god, use_aspect)} votes -> {votes}")
        return {"ok": True, "god": god, "use_aspect": bool(use_aspect),
                "votes": votes, "removed": False}

    async def cmd_nominate(self, message, args, whisper=False):
        """!nominate <god> — add a god to the pool (1 per viewer per day)."""
        if not self._db:
            return
        if not args or not args.strip():
            await self.bot.send_reply(
                message,
                "Use !nominate <god> to add to the spin pool. "
                "One nomination per day. Add the word 'aspect' to "
                "nominate a god's Aspect.",
                whisper)
            return

        user_uuid = await self.bot.user_uuid_for(message.chatter)
        if not user_uuid:
            await self.bot.send_reply(
                message, "The pool is still loading. Try again in a moment.",
                whisper)
            return
        display = (getattr(message.chatter, "display_name", None)
                   or message.chatter.name)

        result = await self.do_nominate(
            user_uuid, args, self._is_broadcaster(message.chatter),
            display_name=display)

        if not result["ok"]:
            reason = result.get("reason")
            if reason == "unknown_god":
                await self.bot.send_reply(
                    message,
                    f"Unknown god: '{args.strip()[:40]}'. Check spelling or "
                    f"try a partial name.",
                    whisper)
            elif reason == "no_aspect":
                await self.bot.send_reply(
                    message,
                    f"{result['god']} doesn't have an Aspect in SMITE 2 "
                    f"(yet). Nominate without the word 'aspect'.",
                    whisper)
            elif reason == "already_voted":
                await self.bot.send_reply(
                    message,
                    f"You already nominated "
                    f"{display_god(result['god'], result.get('use_aspect'))} "
                    f"today. Try again tomorrow.",
                    whisper)
            return

        god = display_god(result["god"], result["use_aspect"])
        votes = result["votes"]
        n_in_pool = result["pool_size"]
        if votes == 1:
            msg = (f"{god} added to the spin pool! "
                   f"({n_in_pool} entr{'ies' if n_in_pool != 1 else 'y'} in pool)")
        else:
            msg = (f"+1 vote for {god} (now {votes} total). "
                   f"{n_in_pool} entr{'ies' if n_in_pool != 1 else 'y'} in pool.")
        await self.bot.send_reply(message, msg, whisper)

    async def cmd_pool(self, message, args, whisper=False):
        """!pool — show the top 5 most-voted gods in the spin pool."""
        if not self._db:
            return
        async with self._db.execute("""
            SELECT god_name, use_aspect, vote_count FROM god_pool
             ORDER BY vote_count DESC, god_name ASC, use_aspect ASC
        """) as cur:
            rows = await cur.fetchall()

        if not rows:
            await self.bot.send_reply(
                message,
                "Spin pool is empty. Add a god with !nominate <god>",
                whisper)
            return

        top = rows[:5]
        line = " | ".join(
            f"{display_god(r[0], r[1])} ({r[2]})" if r[2] > 1
            else display_god(r[0], r[1])
            for r in top
        )
        suffix = (f" + {len(rows) - 5} more" if len(rows) > 5 else "")
        await self.bot.send_reply(
            message,
            f"Spin pool ({len(rows)} entries): {line}{suffix}",
            whisper)

    async def cmd_spin(self, message, args, whisper=False):
        """!spin chat command — thin wrapper around ``do_spin``.

        Chat-silent by design (May 2026): the OBS spin-reel overlay
        plus the delayed voice line carry all the visual feedback,
        so there's no point spamming chat. The mod who typed the
        command still sees the result via the overlay and the bot's
        console log.

        Empty-pool and queue-full edge cases also stay silent in
        chat for the same reason — visually, no overlay fires so the
        streamer notices instantly. Console logs the reason for
        post-stream debugging.
        """
        # Pull a mod identifier from the message if we can — useful
        # in the console log so the streamer can tell *who* spun.
        triggered_by = "!spin"
        try:
            chatter = getattr(message, "chatter", None)
            if chatter is not None:
                name = (getattr(chatter, "name", None)
                        or getattr(chatter, "display_name", None))
                if name:
                    triggered_by = f"!spin ({name})"
        except Exception:
            pass
        await self.do_spin(triggered_by=triggered_by)

    async def do_spin(self, triggered_by: str = "spin") -> dict:
        """Pure spin logic. Callable from any context (Twitch chat
        command, webserver endpoint, internal trigger).

        Reads the pool, picks weighted-random from gods that aren't
        already in the request queue, fires the OBS spin-reel
        overlay, pushes the pick to the godrequest queue head, and
        schedules the god-select voice line.

        ``triggered_by`` is recorded in the godrequest queue as the
        requester (so the dashboard can show "spun by Stream Deck"
        vs "spun by !spin (mod-name)").

        Returns a dict for callers that want feedback:
            {
              "ok":         bool,
              "reason":     str  (only when ok=False),
              "chosen_god": str  (only when ok=True),
              "votes":      int  (only when ok=True),
              "pool_size":  int  (only when ok=True),
            }
        Never sends chat. Console logs both success and failure paths.
        """
        if not self._db:
            print("[GodPool] do_spin: DB not initialized")
            return {"ok": False, "reason": "no_db"}

        # Read the candidate set. Rows are (god_name, use_aspect,
        # vote_count) — a god and its aspect are separate entries.
        async with self._db.execute("""
            SELECT god_name, use_aspect, vote_count FROM god_pool
        """) as cur:
            rows = await cur.fetchall()

        if not rows:
            print("[GodPool] do_spin: pool empty — nothing to spin")
            await self._emit_spin_toast(
                "Spin pool is empty — !nominate a god first")
            return {"ok": False, "reason": "pool_empty"}

        # Exclude entries already pending in the request queue.
        # Prevents re-rolling a god that was just spun and is waiting
        # in queue. Aspect-aware: base Khepri queued doesn't block an
        # aspect-Khepri spin, and vice versa.
        godreq = (self.bot.plugins.get("godrequest")
                  if self.bot and hasattr(self.bot, "plugins") else None)
        if godreq and hasattr(godreq, "queue_contains"):
            candidates = [r for r in rows
                          if not godreq.queue_contains(r[0], bool(r[1]))]
        else:
            candidates = list(rows)

        if not candidates:
            print("[GodPool] do_spin: every pool god is already queued — "
                  "play one before spinning again")
            await self._emit_spin_toast(
                "Every pool god is already queued — play one first")
            return {"ok": False, "reason": "all_queued"}

        # Weighted random — gods with more votes are more likely to win.
        weights = [r[2] for r in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]
        chosen_god = chosen[0]
        chosen_aspect = bool(chosen[1])
        chosen_votes = chosen[2]

        # OBS reel animation. Full candidate list so the visual scroll
        # matches what viewers expect to see (not the filtered set).
        await self._emit_spin_overlay(rows, chosen_god, chosen_aspect,
                                      chosen_votes)

        # Queue the pick at the head of the request queue. Source is
        # "spin" so godrequest clears the god_pool row when this play
        # is confirmed (god identified in-match).
        #
        # Requester is deliberately blank for spin entries. The queue
        # renderers (chat + community page) skip the "(requester)"
        # suffix when it's empty, so a spun god displays as just
        # "Atlas" instead of "Atlas (Stream Deck)" or
        # "Atlas (!spin (mod-name))". The ``triggered_by`` string
        # still carries through to the console log and voice line
        # attribution for debugging.
        if godreq and hasattr(godreq, "queue_add"):
            godreq.queue_add(chosen_god, "",
                             source="spin", position="head",
                             use_aspect=chosen_aspect)
        else:
            # Godrequest plugin missing — fall back to direct pool
            # deletion so the spin still has a visible effect.
            print("[GodPool] godrequest plugin unavailable — falling "
                  "back to direct pool deletion.")
            await self._db.execute(
                "DELETE FROM god_pool "
                "WHERE god_name = ? AND use_aspect = ?",
                (chosen_god, 1 if chosen_aspect else 0))
            await self._db.commit()

        # Play the god-select voice line — delayed so it lands AFTER
        # the spin reel animation completes and the win chime fades.
        # Timing math:
        #   - Reel transition: SPIN_DURATION_BASE_MS (2.2s) plus
        #     SPIN_DURATION_PER_GOD_MS (75ms) for every god in the
        #     pool beyond SPIN_DURATION_FREE_GODS (8). The overlay
        #     scales its spin so every god in the reveal pass reads
        #     past the marker, so big pools = longer spins.
        #   - Last tick + win chime fire at the end of the spin.
        #   - Win chime takes ~0.75s to ring out.
        #   - User-requested 1s gap after the last tick.
        #   → Voice line at t=spin_duration + 1.0s.
        # If the SPIN_DURATION_* constants in overlays/god_pool_spin.html
        # change, mirror them here.
        SPIN_DURATION_BASE_MS = 2200
        SPIN_DURATION_PER_GOD_MS = 75
        SPIN_DURATION_FREE_GODS = 8
        spin_duration_ms = (
            SPIN_DURATION_BASE_MS
            + max(0, len(rows) - SPIN_DURATION_FREE_GODS)
              * SPIN_DURATION_PER_GOD_MS
        )

        voicelines = (self.bot.plugins.get("voicelines")
                      if self.bot and hasattr(self.bot, "plugins") else None)
        if voicelines and hasattr(voicelines, "play_god_select"):
            SPIN_VOICELINE_DELAY_S = spin_duration_ms / 1000 + 1.0

            async def _delayed_voice_line(god=chosen_god, vl=voicelines):
                try:
                    await asyncio.sleep(SPIN_VOICELINE_DELAY_S)
                    vl.play_god_select(god, triggered_by=triggered_by)
                except Exception as e:
                    print(f"[GodPool] voice line trigger failed: {e}")

            try:
                asyncio.create_task(_delayed_voice_line())
            except RuntimeError:
                # No running loop (unusual — shouldn't happen in normal
                # bot flow). Fall back to immediate trigger.
                try:
                    voicelines.play_god_select(
                        chosen_god, triggered_by=triggered_by)
                except Exception as e:
                    print(f"[GodPool] voice line trigger failed: {e}")

        print(f"[GodPool] Spun: {display_god(chosen_god, chosen_aspect)} "
              f"({chosen_votes} votes) "
              f"via {triggered_by} — added to godrequest queue head")
        return {
            "ok": True,
            "chosen_god": chosen_god,
            "use_aspect": chosen_aspect,
            "votes": int(chosen_votes),
            "pool_size": len(rows),
        }

    async def _emit_spin_overlay(self, rows, chosen_god, chosen_aspect,
                                 chosen_votes):
        """Send the spin event to the overlay manager so the OBS
        browser source can run the slot-machine animation."""
        if not self.bot or not getattr(self.bot, "web_server", None):
            return
        overlay_mgr = getattr(self.bot.web_server, "overlay", None)
        if not overlay_mgr:
            return

        # All candidates ordered by vote count (most-voted first) so the
        # reel weighting visually matches the actual pick weighting.
        # `aspect` marks entries the overlay badges with the aspect icon.
        candidates = sorted(
            ({"god": r[0], "aspect": bool(r[1]), "votes": r[2]}
             for r in rows),
            key=lambda c: (-c["votes"], c["god"], c["aspect"]),
        )
        total_votes = sum(r[2] for r in rows)
        try:
            await overlay_mgr.emit("god_pool_spin", {
                "chosen": chosen_god,
                "chosen_aspect": chosen_aspect,
                "chosen_votes": chosen_votes,
                "candidates": candidates,
                "total_votes": total_votes,
            })
        except Exception as e:
            print(f"[GodPool] overlay emit failed: {e}")

    async def _emit_spin_toast(self, text: str):
        """Flash a short message on the OBS spin overlay when a spin
        can't proceed (empty pool, or every god already queued).

        Reuses the existing ``god_pool_spin`` browser source — the
        overlay renders this as a toast card instead of the reel — so
        no extra OBS setup is needed. The point is feedback: a Stream
        Deck press that no-ops otherwise looks broken, since there's
        no chat message and no browser window to read the result JSON.
        """
        # Mirrors the guard in _emit_spin_overlay above.
        if not self.bot or not getattr(self.bot, "web_server", None):
            return
        overlay_mgr = getattr(self.bot.web_server, "overlay", None)
        if not overlay_mgr:
            return
        try:
            await overlay_mgr.emit("god_pool_spin", {"toast": text})
        except Exception as e:
            print(f"[GodPool] toast emit failed: {e}")

    async def cmd_pool_clear(self, message, args, whisper=False):
        """!poolclear — mod-only. Wipe the pool (does not touch the
        per-user-per-day vote records, so people don't get a free
        re-vote from clearing)."""
        if not self._db:
            return
        async with self._db.execute(
                "SELECT COUNT(*) FROM god_pool") as cur:
            row = await cur.fetchone()
        n = row[0] if row else 0

        await self._db.execute("DELETE FROM god_pool")
        await self._db.commit()

        await self.bot.send_reply(
            message,
            f"Spin pool cleared ({n} entries removed). "
            f"Today's nominations still apply. Viewers can't re-nominate "
            f"until tomorrow.",
            whisper)
