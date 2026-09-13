"""
God Request Plugin
===================
Viewers spend "God Tokens" (core/wallet.py, asset god_token) to request
which god Hatmaster plays next. Mods can manage the queue directly.

Queue logic:
  - !godrequest <god>   — Viewers spend a token to add a god to the queue
  - !godreq <god>       — Mods add a god to the queue (free, no token cost)
  - !godqueue           — Show the current god request queue
  - !godskip            — Mods skip/remove the next god in queue
  - !clear              — Mods clear the entire queue
  - !godtokens          — Check your God Token balance

OBS integration:
  - Updates an image source with the next god's portrait
  - Updates a text source with the god name or "!godrequest" when empty

Smite integration:
  - When the Smite plugin detects you're playing the next requested god,
    it auto-completes that request and advances the queue.

Wallet integration:
  - Checks/deducts God Tokens in the local wallet (core/wallet.py)
  - Awards tokens on subs and donations (via Twitch EventSub)
"""

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from core import db as _shared_db
from core import users as _users
from core import wallet as _wallet
from core.config import (
    BASE_DIR,
    GODREQ_QUEUE_FILE, GODREQ_HISTORY_FILE,
    GODREQ_MAX_QUEUE, GODREQ_TOKEN_COST,
    GODREQ_SUB_TOKENS, GODREQ_DONATION_THRESHOLD,
    SMITE2_GOD_IMAGES_DIR,
    OBS_SOURCE_GODREQ_IMAGE, OBS_SOURCE_GODREQ_TEXT,
    OBS_GODREQ_SCENE, OBS_GODREQ_GROUP,
    DATA_DIR
)
from core.god_resolver import resolve, resolve_sync, split_aspect
from core import aspect_roster, god_roster
from core.aspect_roster import display_god


# === KNOWN GODS ===
# The roster lives in core/god_roster.py now (bundled snapshot +
# data/god_roster.json cache + daily live refresh from the wiki) —
# the old hardcoded list here froze at 82 gods in April 2026 and made
# !godreq call Bastet and Chronos unknown. This module-level name is
# kept for importers (tools/eval_god_resolver.py); the plugin itself
# calls god_roster.names() per lookup so a mid-session roster refresh
# takes effect immediately.
SMITE2_GODS = god_roster.names()


class GodRequestPlugin:
    def __init__(self):
        self.bot = None
        self._db = None      # shared economy.db connection (core/db.py)
        self.queue = []      # [{god, requester, requested_at, token_spent}]

        # Daily roster-refresh task (started in on_ready)
        self._roster_task = None

        # History listeners (append-only list, same pattern as the
        # kill detector's add_*_listener hooks). Fired with the final
        # history entry whenever a queue entry is resolved (status:
        # played | skipped | removed). PriorityRequestPlugin uses this
        # to stamp played_at on paid requests.
        self._history_listeners = []

        self._load_data()

    # === DATA PERSISTENCE ===

    def _load_data(self):
        if GODREQ_QUEUE_FILE.exists():
            try:
                with open(GODREQ_QUEUE_FILE) as f:
                    self.queue = json.load(f)
            except Exception:
                self.queue = []

    def _save_data(self):
        from core.atomic_io import atomic_write_json
        atomic_write_json(GODREQ_QUEUE_FILE, self.queue)

    def _save_history(self, entry):
        history = []
        if GODREQ_HISTORY_FILE.exists():
            try:
                with open(GODREQ_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                pass
        history.append(entry)
        from core.atomic_io import atomic_write_json
        atomic_write_json(GODREQ_HISTORY_FILE, history)
        for fn in self._history_listeners:
            try:
                fn(entry)
            except Exception as e:
                print(f"[GodRequest] history listener error: {e}")

    def add_history_listener(self, fn):
        """Subscribe to resolved queue entries. `fn` is called
        synchronously with the history dict each time an entry leaves
        the queue (includes status: played | skipped | removed, plus
        stripe_session_id for paid_priority entries)."""
        if fn not in self._history_listeners:
            self._history_listeners.append(fn)

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("godrequest", self.cmd_godrequest,
                             description="Spend a God Token to request a god", identity=True, plugin="godrequest")
        bot.register_command("godreq", self.cmd_godreq,
                             mod_only=True, description="Add a god to the queue for free", plugin="godrequest")
        bot.register_command("godqueue", self.cmd_godqueue,
                             description="Next 5 gods in the request queue", platforms=("twitch", "discord"), plugin="godrequest")
        bot.register_command("godskip", self.cmd_godskip,
                             mod_only=True, description="Remove the next god from the queue", plugin="godrequest")
        bot.register_command("clear", self.cmd_godclear,
                             mod_only=True, description="Clear the entire god queue", plugin="godrequest")
        bot.register_command("godtokens", self.cmd_godtokens,
                             description="Your God Token balance", identity=True, plugin="godrequest")
        bot.register_command("remove", self.cmd_remove,
                             mod_only=True, description="Remove the god at a queue position", plugin="godrequest")
        bot.register_command("godlist", self.cmd_godlist,
                             description="The entire god request queue", platforms=("twitch", "discord"), plugin="godrequest")

        # Register for Smite god detection events
        if "smite" in bot.plugins:
            bot.plugins["smite"].on_god_detected(self._on_god_detected)

    async def on_ready(self):
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[GodReq] DB unavailable — God Tokens disabled")

        # Push initial OBS state
        await self._update_obs_display()

        # Keep the god roster current without manual steps
        self._roster_task = asyncio.create_task(self._roster_refresh_loop())

    @property
    def _miu_connected(self) -> bool:
        """Kept under its old name for the dashboard state: True when
        the wallet (shared DB) is usable."""
        return self._db is not None

    # === ROSTER REFRESH ===

    async def _roster_refresh_loop(self):
        """Once at startup and then periodically: pull the live god
        list from the wiki (god_roster.refresh() no-ops while its
        cache is under 24h old, so the 6h loop is just a retry
        cadence). Newly released gods become requestable immediately;
        their icon + card art is fetched in the background so the OBS
        portrait works the same day."""
        while True:
            try:
                added = await asyncio.to_thread(god_roster.refresh)
                if added:
                    global SMITE2_GODS
                    SMITE2_GODS = god_roster.names()  # legacy importers
                    names = ", ".join(g["name"] for g in added)
                    print(f"[GodReq] New god(s) on the roster: {names}")

                # Aspect roster rides the same cadence (and the same
                # 24h cache logic inside refresh), so "which gods have
                # an Aspect" self-updates on launch and stays current
                # without manual steps. Failure is non-fatal — the
                # cached/bundled list keeps validating.
                try:
                    await asyncio.to_thread(aspect_roster.refresh)
                except Exception as e:
                    print(f"[GodReq] aspect roster refresh error: {e}")

                # Fetch art for any recent release still missing it —
                # covers both gods added just now and earlier download
                # failures (wiki hiccup at release time), which retry
                # each pass instead of waiting for manual --add.
                missing_art = [
                    g for g in god_roster.new_gods()
                    if not (DATA_DIR / "god_icons" / f"{g['slug']}.png").exists()
                    or not (DATA_DIR / "god_cards" / f"{g['slug']}.png").exists()
                ]
                if missing_art:
                    await self._download_new_god_assets(missing_art)
            except Exception as e:
                print(f"[GodReq] roster refresh error: {e}")
            await asyncio.sleep(6 * 3600)

    async def _download_new_god_assets(self, added):
        """Fetch icon + card art for newly released gods via the
        existing download tools (each validates against the wiki).
        Assets that already exist are skipped — --add force-downloads,
        and the retry pass would otherwise re-fetch a god's icon every
        cycle just because its card is still missing. Failures are
        logged and non-fatal — the request queue works without art."""
        import subprocess
        targets = [("download_god_icons.py", "god_icons"),
                   (str(Path("tools") / "download_god_cards.py"), "god_cards")]
        for g in added:
            for script, asset_dir in targets:
                if (DATA_DIR / asset_dir / f"{g['slug']}.png").exists():
                    continue
                cmd = [sys.executable, str(BASE_DIR / script),
                       "--add", g["name"]]
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run, cmd, capture_output=True,
                        timeout=180, cwd=str(BASE_DIR))
                    status = "ok" if proc.returncode == 0 else "FAILED"
                    print(f"[GodReq] asset fetch {status}: "
                          f"{Path(script).name} --add {g['name']}")
                except Exception as e:
                    print(f"[GodReq] asset fetch error for {g['name']}: {e}")

        # Let the spin pool recognize the new gods without a restart
        pool = self.bot.plugins.get("god_pool")
        if pool is not None and hasattr(pool, "reload_known_gods"):
            try:
                pool.reload_known_gods()
            except Exception as e:
                print(f"[GodReq] god_pool reload failed: {e}")

    # === GOD TOKENS (core/wallet.py) ===

    async def _uuid_for(self, who, display=None):
        """Accepts a user_uuid, a Twitch login, or a chatter object."""
        if who is None or self._db is None:
            return None
        if not isinstance(who, str):
            return await self.bot.user_uuid_for(who)
        if len(who) == 36 and who.count("-") == 4:
            return who
        try:
            return await _users.get_or_create_twitch_login(self._db, who, display)
        except Exception as e:
            print(f"[GodReq] cannot resolve {who}: {e}")
            return None

    async def _get_token_balance(self, who):
        """Get a user's God Token count (who = uuid, login or chatter)."""
        uid = await self._uuid_for(who)
        if not uid:
            return None
        return await _wallet.get(self._db, uid, "god_token")

    async def _spend_token(self, who, amount=1, note=None):
        """Deduct God Tokens from a user. Returns True if successful."""
        uid = await self._uuid_for(who)
        if not uid:
            return False
        res = await _wallet.debit(self._db, uid, "god_token", amount,
                                  "godreq_spend", note=note)
        return res is not None

    async def _award_token(self, who, amount=1, reason="sub_award", ref=None,
                           note=None):
        """Give God Tokens to a user. Returns True if successful (False
        when the wallet is down or `ref` was already paid)."""
        uid = await self._uuid_for(who)
        if not uid or amount <= 0:
            return False
        res = await _wallet.credit(self._db, uid, "god_token", amount, reason,
                                   ref=ref, note=note, channel="system")
        if res is not None:
            print(f"[GodReq] Awarded {amount} token(s) to {who} (balance: {res})")
            return True
        return False

    # === GOD NAME MATCHING ===

    @staticmethod
    def _normalize_god_name(name):
        """Normalize a god name for matching."""
        return name.lower().strip().replace("'", "").replace("-", " ")

    @staticmethod
    def _match_god(input_name):
        """Match a god name via the shared tiered resolver
        (exact/alias/prefix/contains/fuzzy — see core/god_resolver.py)
        against the live roster. Returns the canonical name or None."""
        hit = resolve_sync(input_name, god_roster.names())
        return hit[0] if hit else None

    @staticmethod
    async def _match_god_full(input_name):
        """_match_god plus the last-resort local-Ollama tier (strict
        timeout; silently a no-op when the model is cold or the GPU is
        busy with the game). Only ever returns names validated against
        the roster, so a hallucinated answer cannot get through."""
        god = GodRequestPlugin._match_god(input_name)
        if god:
            return god
        hit = await resolve(input_name, god_roster.names())
        return hit[0] if hit else None

    @staticmethod
    def _unknown_god_reply(raw_input):
        """A reply that actually helps: SMITE 1 gods that haven't been
        ported yet get named as such (the old reply blamed the viewer's
        spelling when Bastet simply wasn't on the list), and plain
        typos get a did-you-mean."""
        s1_hit = resolve_sync(raw_input, god_roster.smite1_only_names())
        if s1_hit:
            return (f"{s1_hit[0]} isn't in SMITE 2 yet — "
                    f"hopefully soon! Try another god.")

        import difflib
        names = god_roster.names()
        by_lower = {n.lower(): n for n in names}
        close = difflib.get_close_matches(
            raw_input.lower().strip(), list(by_lower), n=2, cutoff=0.5)
        if close:
            suggest = " or ".join(by_lower[c] for c in close)
            return f"Unknown god: {raw_input}. Did you mean {suggest}?"
        return f"Unknown god: {raw_input}. Check your spelling!"

    def _is_god_in_queue(self, god_name, use_aspect=False):
        """Check if a (god, aspect) request is already in the queue.
        Base god and aspect are distinct requests — Khepri queued
        doesn't block Khepri (Aspect)."""
        normalized = self._normalize_god_name(god_name)
        for entry in self.queue:
            if (self._normalize_god_name(entry["god"]) == normalized
                    and bool(entry.get("use_aspect")) == bool(use_aspect)):
                return True
        return False

    # === OBS DISPLAY ===

    async def _update_obs_display(self):
        """Update the OBS sources showing the next god in queue."""
        if "obs" not in self.bot.plugins:
            return

        obs = self.bot.plugins["obs"]
        scene = OBS_GODREQ_SCENE or None
        group = OBS_GODREQ_GROUP or None

        if self.queue:
            head = self.queue[0]
            next_god = head["god"]

            # Prefix the OBS text source with a PRIORITY tag when the
            # head-of-queue entry was paid for ($5 via Stripe through
            # the website). The image source is unchanged — the badge
            # is text-only because the existing OBS layout already has
            # a "GodReqText" element that's easy to repurpose without
            # asking James to add a new source. If you'd rather use a
            # dedicated badge image source later, drop this prefix and
            # add an OBS_SOURCE_GODREQ_PRIORITY_BADGE source instead.
            display_text = display_god(next_god, head.get("use_aspect"))
            if head.get("source") == "paid_priority":
                display_text = f"PRIORITY - {display_text}"

            # Set the text to the (possibly priority-prefixed) god name
            try:
                await obs.update_text_source(
                    OBS_SOURCE_GODREQ_TEXT, display_text)
            except Exception as e:
                print(f"[GodReq] OBS text error: {e}")

            # Set the image to the god's portrait
            image_path = self._find_god_image(next_god)
            if image_path:
                try:
                    await obs.set_image_source(OBS_SOURCE_GODREQ_IMAGE, str(image_path))
                    await obs.set_source_visible(OBS_SOURCE_GODREQ_IMAGE, True,
                                                  scene=scene, group=group)
                except Exception as e:
                    print(f"[GodReq] OBS image error: {e}")
            else:
                try:
                    await obs.set_source_visible(OBS_SOURCE_GODREQ_IMAGE, False,
                                                  scene=scene, group=group)
                except Exception:
                    pass
        else:
            # No queue — show "!godrequest" prompt
            try:
                await obs.update_text_source(OBS_SOURCE_GODREQ_TEXT, "!godrequest")
            except Exception:
                pass
            try:
                await obs.set_source_visible(OBS_SOURCE_GODREQ_IMAGE, False,
                                              scene=scene, group=group)
            except Exception:
                pass

    def _find_god_image(self, god_name):
        """Find a god portrait for the OBS next-up display.

        Priority: Custom God Icons (James's curated set) → downloaded
        SMITE 2 icon → SMITE 1 fallback art. The fallbacks mean a god
        released this morning still gets a portrait on stream tonight
        even before anyone curates a custom icon for it."""
        slug = god_roster.slug_for(god_name)

        if SMITE2_GOD_IMAGES_DIR:
            god_dir = Path(SMITE2_GOD_IMAGES_DIR)
            if god_dir.exists():
                names = [god_name, god_name.lower(), slug]
                for ext in [".gif", ".png"]:
                    for name in names:
                        path = god_dir / f"{name}{ext}"
                        if path.exists():
                            return path.resolve()

        for folder in (DATA_DIR / "god_icons", DATA_DIR / "god_icons_s1"):
            path = folder / f"{slug}.png"
            if path.exists():
                return path.resolve()
        return None

    # === SMITE INTEGRATION ===

    async def _on_god_detected(self, god_info):
        """Called when the Smite plugin detects which god you're playing."""
        if not self.bot.is_feature_enabled("god_requests"):
            return

        if not self.queue:
            return

        detected_god = god_info.get("name", "")
        next_god = self.queue[0]["god"]
        # Screen detection can't tell an Aspect game from a base-kit
        # game (same portrait), so a detected god completes the head
        # entry regardless of its use_aspect flag. The aspect suffix
        # still shows in the fulfillment shoutout below.
        next_aspect = bool(self.queue[0].get("use_aspect"))

        if self._normalize_god_name(detected_god) == self._normalize_god_name(next_god):
            completed = self.queue.pop(0)
            self._save_data()

            # Log to history
            self._save_history({
                **completed,
                "completed_at": datetime.now().isoformat(),
                "status": "played",
            })

            # If this entry came from !spin, the spin pool still has
            # the god in it (we don't remove on roll, only on play).
            # Drop it from god_pool now that the play is confirmed.
            # Paid / manual entries leave god_pool alone — the spin
            # pool is a separate viewer-voting mechanic.
            if completed.get("source") == "spin":
                try:
                    from core import db as _shared_db
                    if _shared_db.is_available():
                        conn = await _shared_db.get_db()
                        if conn is not None:
                            await conn.execute(
                                "DELETE FROM god_pool "
                                "WHERE god_name = ? AND use_aspect = ?",
                                (next_god, 1 if next_aspect else 0))
                            await conn.commit()
                            print(f"[GodRequest] Removed "
                                  f"{display_god(next_god, next_aspect)} "
                                  f"from spin pool (played after !spin).")
                except Exception as e:
                    print(f"[GodRequest] Failed to remove {next_god} "
                          f"from spin pool: {e}")

            played = display_god(next_god, next_aspect)
            # Spin entries carry a blank requester (by design — the
            # queue renderers skip the suffix), so don't announce
            # "requested by ." for them.
            if completed.get("requester"):
                lead = (f"God request fulfilled! Playing {played} as "
                        f"requested by {completed['requester']}.")
            elif completed.get("source") == "spin":
                lead = f"Spin fulfilled! Now playing {played}."
            else:
                lead = f"God request fulfilled! Now playing {played}."
            await self.bot.send_chat(
                lead + " "
                + (f"Next up: "
                   f"{display_god(self.queue[0]['god'], self.queue[0].get('use_aspect'))}"
                   if self.queue
                   else "Queue is now empty!")
            )

            await self._update_obs_display()

            # Update webserver state
            self._update_web_state()

    # === WEB STATE ===

    def _update_web_state(self):
        """Push god request state to webserver for overlays/control panel."""
        if not self.bot.web_server:
            return
        state = self.bot.web_server._state
        state["god_requests"] = {
            "queue": self.queue,
            "next_god": self.queue[0]["god"] if self.queue else None,
            "queue_length": len(self.queue),
            "wallet_ready": self._miu_connected,
        }

    # === COMMANDS ===

    async def cmd_godrequest(self, message, args, whisper=False):
        """!godrequest <god> — Viewers spend a token to request a god."""
        if not self.bot.is_feature_enabled("god_requests"):
            await self.bot.send_reply(message, "God requests are currently closed.", whisper)
            return

        if not args:
            # Show usage with token balance if available
            balance = None
            if self._miu_connected:
                balance = await self._get_token_balance(message.chatter)
            if balance is not None:
                await self.bot.send_reply(
                    message,
                    f"Usage: !godrequest <god name> | You have {balance} God Token(s). "
                    f"Earn tokens by subscribing or donating $5!",
                    whisper
                )
            else:
                await self.bot.send_reply(
                    message,
                    "Usage: !godrequest <god name> | Earn God Tokens by subscribing or donating $5!",
                    whisper
                )
            return

        # The word "aspect" anywhere in the request flags the god's
        # Aspect (alternate kit) — "!godrequest Khepri aspect" is a
        # distinct request from "!godrequest Khepri".
        cleaned, use_aspect = split_aspect(args)

        # Match the god name (all tiers including local LLM)
        god_name = await self._match_god_full(cleaned)
        if not god_name:
            await self.bot.send_reply(
                message, self._unknown_god_reply(cleaned.strip()), whisper
            )
            return

        if use_aspect and not aspect_roster.has_aspect(god_name):
            await self.bot.send_reply(
                message,
                f"{god_name} doesn't have an Aspect in SMITE 2 (yet). "
                f"Request without the word 'aspect'.",
                whisper
            )
            return

        # Check queue size
        if len(self.queue) >= GODREQ_MAX_QUEUE:
            await self.bot.send_reply(
                message, f"The god request queue is full ({GODREQ_MAX_QUEUE} max).", whisper
            )
            return

        # Check and spend a token from the wallet
        if not self._miu_connected:
            await self.bot.send_reply(
                message, "God tokens aren't available right now (wallet not ready).",
                whisper
            )
            return

        user_uuid = await self.bot.user_uuid_for(message.chatter)
        balance = await self._get_token_balance(user_uuid)
        if balance is None or balance < GODREQ_TOKEN_COST:
            await self.bot.send_reply(
                message,
                f"You need {GODREQ_TOKEN_COST} God Token(s) to request a god. "
                f"You have {balance or 0}. Earn tokens by subscribing or donating!",
                whisper
            )
            return

        success = await self._spend_token(
            user_uuid, GODREQ_TOKEN_COST, note=display_god(god_name, use_aspect))
        if not success:
            await self.bot.send_reply(
                message, "Failed to spend token. Try again!", whisper
            )
            return

        # Add to queue
        entry = {
            "god": god_name,
            "requester": message.chatter.name,
            "requested_at": datetime.now().isoformat(),
            "token_spent": True,
            "use_aspect": use_aspect,
        }
        self.queue.append(entry)
        self._save_data()

        position = len(self.queue)
        remaining = (balance - GODREQ_TOKEN_COST)
        await self.bot.send_chat(
            f"{message.chatter.name} requested "
            f"{display_god(god_name, use_aspect)}! "
            f"Position: #{position} in queue | "
            f"Tokens remaining: {remaining}"
        )

        await self._update_obs_display()
        self._update_web_state()

    # -----------------------------------------------------------------
    # Public queue API — callable from other plugins
    # -----------------------------------------------------------------
    #
    # Lets the spin pool plugin (or anything else) append a request
    # without going through the chat-command path. The `source` field
    # is preserved on the queue entry and consulted by
    # `_on_god_detected` to decide whether playing the god should
    # also reach into other tables (e.g. removing the god from the
    # spin pool when source == "spin").

    def queue_add(self, god_name: str, requester: str,
                  source: str = "paid",
                  token_spent: bool = False,
                  position: str = "end",
                  use_aspect: bool = False) -> dict:
        """Append (or prepend) a god to the request queue.

        Args:
          god_name:    proper-cased canonical god name. Caller should
                       have already resolved this — we don't re-validate.
          requester:   chat username or identifier (e.g. "!spin").
          source:      "paid"   — viewer spent a god token (default)
                       "manual" — mod added via !godreq
                       "spin"   — picked by !spin, will also remove
                                  from god_pool when played
          token_spent: True if a viewer token was deducted. !spin and
                       !godreq pass False; only !godrequest is True.
          position:    "head" pushes to position 0 (next-up), "end"
                       appends (default). !spin uses "head" so the
                       lobby plays the rolled god next, with paid
                       requests queued behind.
          use_aspect:  True when the request is for the god's Aspect
                       (alternate kit). Caller validates against
                       core.aspect_roster — we don't re-check.

        Returns the inserted entry dict. Caller is free to log it.
        """
        entry = {
            "god":          god_name,
            "requester":    requester,
            "requested_at": datetime.now().isoformat(),
            "token_spent":  bool(token_spent),
            "source":       source,
            "use_aspect":   bool(use_aspect),
        }
        if position == "head":
            self.queue.insert(0, entry)
        else:
            self.queue.append(entry)
        self._save_data()
        # Fire-and-forget OBS + web-state refresh. Not awaited because
        # this method is sync to keep the call sites simple.
        try:
            asyncio.create_task(self._update_obs_display())
        except RuntimeError:
            pass  # no running loop (tests / boot-time)
        self._update_web_state()
        return entry

    def queue_contains(self, god_name: str,
                       use_aspect: bool = False) -> bool:
        """True if this (god, aspect) request is already pending in
        the queue. Case-insensitive. Used by !spin to skip
        already-queued entries."""
        return self._is_god_in_queue(god_name, use_aspect)

    async def cmd_godreq(self, message, args, whisper=False):
        """!godreq <god> — Mods add a god to the queue (free)."""
        if not args:
            await self.bot.send_reply(
                message, "Usage: !godreq <god name> [aspect]", whisper
            )
            return

        cleaned, use_aspect = split_aspect(args)

        god_name = await self._match_god_full(cleaned)
        if not god_name:
            await self.bot.send_reply(
                message, self._unknown_god_reply(cleaned.strip()), whisper
            )
            return

        if use_aspect and not aspect_roster.has_aspect(god_name):
            await self.bot.send_reply(
                message,
                f"{god_name} doesn't have an Aspect in SMITE 2 (yet). "
                f"Request without the word 'aspect'.",
                whisper
            )
            return

        entry = {
            "god": god_name,
            "requester": message.chatter.name,
            "requested_at": datetime.now().isoformat(),
            "token_spent": False,
            "use_aspect": use_aspect,
        }
        self.queue.append(entry)
        self._save_data()

        position = len(self.queue)
        await self.bot.send_chat(
            f"{display_god(god_name, use_aspect)} added to the god "
            f"request queue by "
            f"{message.chatter.name}! Position: #{position}"
        )

        await self._update_obs_display()
        self._update_web_state()

    async def cmd_godqueue(self, message, args, whisper=False):
        """!godqueue — Show the current god request queue."""
        if not self.queue:
            await self.bot.send_reply(
                message, "The god request queue is empty! Use !godrequest <god> to add one.",
                whisper
            )
            return

        items = []
        for i, entry in enumerate(self.queue[:5]):
            # Spin entries store an empty requester so they display
            # as just the god name. Paid / manual entries keep the
            # "(requester)" suffix.
            req = entry.get("requester") or ""
            suffix = f" ({req})" if req else ""
            god = display_god(entry["god"], entry.get("use_aspect"))
            items.append(f"{i+1}. {god}{suffix}")

        text = " | ".join(items)
        remaining = len(self.queue) - 5
        if remaining > 0:
            text += f" | +{remaining} more"

        await self.bot.send_reply(message, f"God queue: {text}", whisper)

    async def cmd_godskip(self, message, args, whisper=False):
        """!godskip — Mods remove the next god from the queue."""
        if not self.queue:
            await self.bot.send_reply(message, "The queue is already empty.", whisper)
            return

        removed = self.queue.pop(0)
        self._save_data()

        self._save_history({
            **removed,
            "completed_at": datetime.now().isoformat(),
            "status": "skipped",
        })

        next_text = (f"Next up: "
                     f"{display_god(self.queue[0]['god'], self.queue[0].get('use_aspect'))}"
                     if self.queue else "Queue is now empty")
        await self.bot.send_chat(
            f"Skipped {display_god(removed['god'], removed.get('use_aspect'))} "
            f"(requested by {removed['requester']}). {next_text}"
        )

        await self._update_obs_display()
        self._update_web_state()

    async def cmd_godclear(self, message, args, whisper=False):
        """!clear — Mods clear the entire queue."""
        count = len(self.queue)
        if count == 0:
            await self.bot.send_reply(message, "The queue is already empty.", whisper)
            return

        self.queue.clear()
        self._save_data()

        await self.bot.send_chat(f"God request queue cleared ({count} requests removed).")

        await self._update_obs_display()
        self._update_web_state()

    async def cmd_godtokens(self, message, args, whisper=False):
        """!godtokens — Check your God Token balance."""
        if not self._miu_connected:
            await self.bot.send_reply(
                message, "God tokens aren't available right now.", whisper
            )
            return

        balance = await self._get_token_balance(message.chatter)

        if balance is not None:
            await self.bot.send_reply(
                message,
                f"You have {balance} God Token(s). "
                f"Use !godrequest <god> to request a god!",
                whisper
            )
        else:
            await self.bot.send_reply(
                message, "Couldn't check your balance. Try again later!", whisper
            )

    async def cmd_remove(self, message, args, whisper=False):
        """!remove <position> — Mods remove a god at a specific position in the queue."""
        if not args:
            await self.bot.send_reply(
                message, "Usage: !remove <position> (e.g., !remove 1)", whisper
            )
            return

        try:
            pos = int(args.strip())
        except ValueError:
            await self.bot.send_reply(
                message, "Position must be a number (e.g., !remove 1)", whisper
            )
            return

        if len(self.queue) == 0:
            await self.bot.send_reply(message, "Queue is empty.", whisper)
            return

        if pos < 1 or pos > len(self.queue):
            await self.bot.send_reply(
                message,
                f"Invalid position. Use a number between 1 and {len(self.queue)}.",
                whisper
            )
            return

        removed = self.queue.pop(pos - 1)
        self._save_data()

        self._save_history({
            **removed,
            "completed_at": datetime.now().isoformat(),
            "status": "removed",
        })

        await self.bot.send_chat(
            f"Removed #{pos} "
            f"{display_god(removed['god'], removed.get('use_aspect'))} "
            f"(requested by {removed['requester']}) "
            f"from the queue. {len(self.queue)} remaining."
        )

        await self._update_obs_display()
        self._update_web_state()

    async def cmd_godlist(self, message, args, whisper=False):
        """!godlist — Show all gods in the queue."""
        if not self.queue:
            await self.bot.send_reply(
                message, "The god request queue is empty!", whisper
            )
            return

        items = []
        for i, entry in enumerate(self.queue):
            # Spin entries have an empty requester and display as
            # just the god name. Paid / manual entries keep the
            # "(requester)" suffix.
            req = entry.get("requester") or ""
            suffix = f" ({req})" if req else ""
            god = display_god(entry["god"], entry.get("use_aspect"))
            items.append(f"{i+1}. {god}{suffix}")

        await self.bot.send_reply(message, "God queue: " + " | ".join(items), whisper)

    # === TOKEN AUTO-AWARD (called from bot event handlers) ===

    async def award_sub_tokens(self, username, ref=None):
        """Award tokens when someone subscribes. `ref` (the EventSub
        message id) makes a redelivered event a no-op."""
        success = await self._award_token(username, GODREQ_SUB_TOKENS,
                                          reason="sub_award", ref=ref)
        if success:
            await self.bot.send_chat(
                f"{username} earned {GODREQ_SUB_TOKENS} God Token(s) for subscribing! "
                f"Use !godrequest <god> to request a god."
            )

    async def award_donation_tokens(self, username, amount_dollars):
        """Award tokens based on donation amount ($5 per token)."""
        tokens = int(amount_dollars // GODREQ_DONATION_THRESHOLD)
        if tokens > 0:
            success = await self._award_token(
                username, tokens, reason="donation_award",
                note=f"${amount_dollars:.2f}")
            if success:
                await self.bot.send_chat(
                    f"{username} earned {tokens} God Token(s) for their "
                    f"${amount_dollars:.2f} donation! Use !godrequest <god>."
                )

    # === CLEANUP ===

    async def cleanup(self):
        if self._roster_task:
            self._roster_task.cancel()
        self._db = None
        self._save_data()
