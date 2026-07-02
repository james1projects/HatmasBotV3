"""
God Request Plugin
===================
Viewers spend "God Tokens" (MixItUp inventory items) to request
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

MixItUp integration:
  - Checks/deducts God Tokens via MixItUp's Developer API
  - Awards tokens on subs and donations (via Twitch EventSub)
"""

import asyncio
import json
import time
import aiohttp
from datetime import datetime
from pathlib import Path

from core.config import (
    MIXITUP_API_BASE, MIXITUP_INVENTORY_NAME, MIXITUP_ITEM_NAME,
    GODREQ_QUEUE_FILE, GODREQ_HISTORY_FILE,
    GODREQ_MAX_QUEUE, GODREQ_TOKEN_COST,
    GODREQ_SUB_TOKENS, GODREQ_DONATION_THRESHOLD,
    SMITE2_GOD_IMAGES_DIR,
    OBS_SOURCE_GODREQ_IMAGE, OBS_SOURCE_GODREQ_TEXT,
    OBS_GODREQ_SCENE, OBS_GODREQ_GROUP,
    DATA_DIR
)


# === KNOWN GODS ===
# Canonical list for fuzzy matching (lowercase).
# Source of truth: Gods - SMITE 2 Wiki.html (82 gods as of April 2026).
# Update by re-saving https://wiki.smite2.com/w/Gods and running
# download_god_icons.py to refresh both this list and the icon library.
SMITE2_GODS = [
    "achilles", "agni", "aladdin", "amaterasu", "anhur", "anubis",
    "aphrodite", "apollo", "ares", "artemis", "artio", "athena", "atlas",
    "awilix", "bacchus", "baron samedi", "bellona", "cabrakan", "cerberus",
    "cernunnos", "chaac", "charon", "chiron", "cupid", "da ji", "danzaburou",
    "discordia", "eset", "fenrir", "ganesha", "geb", "gilgamesh", "guan yu",
    "hades", "hecate", "hercules", "hou yi", "hua mulan", "hun batz",
    "ishtar", "izanami", "janus", "jing wei", "jormungandr", "kali",
    "khepri", "kukulkan", "loki", "medusa", "mercury", "merlin", "mordred",
    "morgan le fay", "ne zha", "neith", "nemesis", "nu wa", "nut", "odin",
    "osiris", "pele", "poseidon", "princess bari", "ra", "rama", "ratatoskr",
    "scylla", "sobek", "sol", "sun wukong", "susano", "sylvanus", "thanatos",
    "the morrigan", "thor", "tsukuyomi", "ullr", "vulcan", "xbalanque",
    "yemoja", "ymir", "zeus",
]


class GodRequestPlugin:
    def __init__(self):
        self.bot = None
        self.session = None  # aiohttp session for MixItUp API
        self.queue = []      # [{god, requester, requested_at, token_spent}]

        # MixItUp IDs (resolved on startup)
        self._miu_inventory_id = None
        self._miu_item_id = None
        self._miu_connected = False

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
        self.session = aiohttp.ClientSession()
        await self._resolve_mixitup_ids()

        # Push initial OBS state
        await self._update_obs_display()

    # === MIXITUP API ===

    async def _miu_get(self, path):
        """GET request to MixItUp API."""
        try:
            async with self.session.get(f"{MIXITUP_API_BASE}{path}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"[GodReq] MixItUp GET {path}: {resp.status}")
                    return None
        except aiohttp.ClientConnectorError:
            return None
        except Exception as e:
            print(f"[GodReq] MixItUp error: {e}")
            return None

    async def _miu_patch(self, path, data):
        """PATCH request to MixItUp API."""
        try:
            async with self.session.patch(
                f"{MIXITUP_API_BASE}{path}",
                json=data,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    body = await resp.text()
                    print(f"[GodReq] MixItUp PATCH {path}: {resp.status} {body}")
                    return None
        except Exception as e:
            print(f"[GodReq] MixItUp error: {e}")
            return None

    async def _resolve_mixitup_ids(self):
        """Look up the inventory ID and item ID for God Tokens."""
        inventories = await self._miu_get("/inventory")
        if not inventories:
            print(f"[GodReq] Could not connect to MixItUp API at {MIXITUP_API_BASE}")
            print(f"[GodReq] Make sure MixItUp is running and Developer API is enabled")
            return

        for inv in inventories:
            if inv["Name"].lower() == MIXITUP_INVENTORY_NAME.lower():
                self._miu_inventory_id = inv["ID"]
                for item in inv.get("Items", []):
                    if item["Name"].lower() == MIXITUP_ITEM_NAME.lower():
                        self._miu_item_id = item["ID"]
                        break
                break

        if self._miu_inventory_id and self._miu_item_id:
            self._miu_connected = True
            print(f"[GodReq] MixItUp connected — inventory: {self._miu_inventory_id}, "
                  f"item: {self._miu_item_id}")
        else:
            print(f"[GodReq] MixItUp inventory '{MIXITUP_INVENTORY_NAME}' or "
                  f"item '{MIXITUP_ITEM_NAME}' not found!")
            print(f"[GodReq] Create them in MixItUp: Consumables → Inventory")
            if inventories:
                names = [inv["Name"] for inv in inventories]
                print(f"[GodReq] Available inventories: {names}")

    async def _get_miu_user_id(self, twitch_username):
        """Look up the MixItUp internal user ID from a Twitch username."""
        data = await self._miu_get(f"/users/Twitch/{twitch_username}")
        if data and "User" in data:
            return data["User"]["ID"]
        return None

    async def _get_token_balance(self, twitch_username):
        """Get a user's God Token count."""
        if not self._miu_connected:
            return None

        user_id = await self._get_miu_user_id(twitch_username)
        if not user_id:
            return None

        data = await self._miu_get(
            f"/inventory/{self._miu_inventory_id}/{self._miu_item_id}/{user_id}"
        )
        if data:
            return data.get("Amount", 0)
        return 0

    async def _spend_token(self, twitch_username, amount=1):
        """Deduct God Tokens from a user. Returns True if successful."""
        if not self._miu_connected:
            return False

        user_id = await self._get_miu_user_id(twitch_username)
        if not user_id:
            return False

        # Check balance first
        balance = await self._get_token_balance(twitch_username)
        if balance is None or balance < amount:
            return False

        result = await self._miu_patch(
            f"/inventory/{self._miu_inventory_id}/{self._miu_item_id}/{user_id}",
            {"Amount": -amount}
        )
        return result is not None

    async def _award_token(self, twitch_username, amount=1):
        """Give God Tokens to a user. Returns True if successful."""
        if not self._miu_connected:
            return False

        user_id = await self._get_miu_user_id(twitch_username)
        if not user_id:
            return False

        result = await self._miu_patch(
            f"/inventory/{self._miu_inventory_id}/{self._miu_item_id}/{user_id}",
            {"Amount": amount}
        )
        if result is not None:
            new_balance = result.get("Amount", "?")
            print(f"[GodReq] Awarded {amount} token(s) to {twitch_username} "
                  f"(balance: {new_balance})")
            return True
        return False

    # === GOD NAME MATCHING ===

    @staticmethod
    def _normalize_god_name(name):
        """Normalize a god name for matching."""
        return name.lower().strip().replace("'", "").replace("-", " ")

    @staticmethod
    def _match_god(input_name):
        """Fuzzy match a god name. Returns the canonical name or None."""
        normalized = GodRequestPlugin._normalize_god_name(input_name)

        # Exact match
        for god in SMITE2_GODS:
            if GodRequestPlugin._normalize_god_name(god) == normalized:
                return god.title()

        # Partial match (starts with)
        matches = []
        for god in SMITE2_GODS:
            if GodRequestPlugin._normalize_god_name(god).startswith(normalized):
                matches.append(god.title())

        if len(matches) == 1:
            return matches[0]

        # Partial match (contains)
        if not matches:
            for god in SMITE2_GODS:
                if normalized in GodRequestPlugin._normalize_god_name(god):
                    matches.append(god.title())
            if len(matches) == 1:
                return matches[0]

        return None

    def _is_god_in_queue(self, god_name):
        """Check if a god is already in the queue."""
        normalized = self._normalize_god_name(god_name)
        for entry in self.queue:
            if self._normalize_god_name(entry["god"]) == normalized:
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
            display_text = next_god
            if head.get("source") == "paid_priority":
                display_text = f"PRIORITY - {next_god}"

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
        """Find a god portrait image in the custom icons folder."""
        if not SMITE2_GOD_IMAGES_DIR:
            return None

        god_dir = Path(SMITE2_GOD_IMAGES_DIR)
        if not god_dir.exists():
            return None

        slug = god_name.lower().replace(" ", "-").replace("'", "")
        names = [god_name, god_name.lower(), slug]
        for ext in [".gif", ".png"]:
            for name in names:
                path = god_dir / f"{name}{ext}"
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
                                "DELETE FROM god_pool WHERE god_name = ?",
                                (next_god,))
                            await conn.commit()
                            print(f"[GodRequest] Removed {next_god} from "
                                  f"spin pool (played after !spin).")
                except Exception as e:
                    print(f"[GodRequest] Failed to remove {next_god} "
                          f"from spin pool: {e}")

            # Spin entries carry a blank requester (by design — the
            # queue renderers skip the suffix), so don't announce
            # "requested by ." for them.
            if completed.get("requester"):
                lead = (f"God request fulfilled! Playing {next_god} as "
                        f"requested by {completed['requester']}.")
            elif completed.get("source") == "spin":
                lead = f"Spin fulfilled! Now playing {next_god}."
            else:
                lead = f"God request fulfilled! Now playing {next_god}."
            await self.bot.send_chat(
                lead + " "
                + (f"Next up: {self.queue[0]['god']}" if self.queue
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
            "mixitup_connected": self._miu_connected,
        }

    # === COMMANDS ===

    async def cmd_godrequest(self, message, args, whisper=False):
        """!godrequest <god> — Viewers spend a token to request a god."""
        if not self.bot.is_feature_enabled("god_requests"):
            await self.bot.send_reply(message, "God requests are currently closed.", whisper)
            return

        if not args:
            # Show usage with token balance if available
            username = message.chatter.name.lower()
            balance = None
            if self._miu_connected:
                balance = await self._get_token_balance(username)
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

        # Match the god name
        god_name = self._match_god(args)
        if not god_name:
            await self.bot.send_reply(
                message, f"Unknown god: {args}. Check your spelling!", whisper
            )
            return

        # Check queue size
        if len(self.queue) >= GODREQ_MAX_QUEUE:
            await self.bot.send_reply(
                message, f"The god request queue is full ({GODREQ_MAX_QUEUE} max).", whisper
            )
            return

        username = message.chatter.name.lower()

        # Check and spend token via MixItUp
        if not self._miu_connected:
            await self.bot.send_reply(
                message, "God tokens aren't available right now (MixItUp not connected).",
                whisper
            )
            return

        balance = await self._get_token_balance(username)
        if balance is None or balance < GODREQ_TOKEN_COST:
            await self.bot.send_reply(
                message,
                f"You need {GODREQ_TOKEN_COST} God Token(s) to request a god. "
                f"You have {balance or 0}. Earn tokens by subscribing or donating!",
                whisper
            )
            return

        success = await self._spend_token(username, GODREQ_TOKEN_COST)
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
        }
        self.queue.append(entry)
        self._save_data()

        position = len(self.queue)
        remaining = (balance - GODREQ_TOKEN_COST)
        await self.bot.send_chat(
            f"{message.chatter.name} requested {god_name}! "
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
                  position: str = "end") -> dict:
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

        Returns the inserted entry dict. Caller is free to log it.
        """
        entry = {
            "god":          god_name,
            "requester":    requester,
            "requested_at": datetime.now().isoformat(),
            "token_spent":  bool(token_spent),
            "source":       source,
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

    def queue_contains(self, god_name: str) -> bool:
        """True if `god_name` is already pending in the queue.
        Case-insensitive. Used by !spin to skip already-queued gods."""
        target = self._normalize_god_name(god_name)
        return any(self._normalize_god_name(e["god"]) == target
                   for e in self.queue)

    async def cmd_godreq(self, message, args, whisper=False):
        """!godreq <god> — Mods add a god to the queue (free)."""
        if not args:
            await self.bot.send_reply(
                message, "Usage: !godreq <god name>", whisper
            )
            return

        god_name = self._match_god(args)
        if not god_name:
            await self.bot.send_reply(
                message, f"Unknown god: {args}. Check your spelling!", whisper
            )
            return

        entry = {
            "god": god_name,
            "requester": message.chatter.name,
            "requested_at": datetime.now().isoformat(),
            "token_spent": False,
        }
        self.queue.append(entry)
        self._save_data()

        position = len(self.queue)
        await self.bot.send_chat(
            f"{god_name} added to the god request queue by "
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
            items.append(f"{i+1}. {entry['god']}{suffix}")

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

        next_text = f"Next up: {self.queue[0]['god']}" if self.queue else "Queue is now empty"
        await self.bot.send_chat(
            f"Skipped {removed['god']} (requested by {removed['requester']}). {next_text}"
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

        username = message.chatter.name.lower()
        balance = await self._get_token_balance(username)

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
            f"Removed #{pos} {removed['god']} (requested by {removed['requester']}) "
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
            items.append(f"{i+1}. {entry['god']}{suffix}")

        await self.bot.send_reply(message, "God queue: " + " | ".join(items), whisper)

    # === TOKEN AUTO-AWARD (called from bot event handlers) ===

    async def award_sub_tokens(self, username):
        """Award tokens when someone subscribes."""
        success = await self._award_token(username, GODREQ_SUB_TOKENS)
        if success:
            await self.bot.send_chat(
                f"{username} earned {GODREQ_SUB_TOKENS} God Token(s) for subscribing! "
                f"Use !godrequest <god> to request a god."
            )

    async def award_donation_tokens(self, username, amount_dollars):
        """Award tokens based on donation amount ($5 per token)."""
        tokens = int(amount_dollars // GODREQ_DONATION_THRESHOLD)
        if tokens > 0:
            success = await self._award_token(username, tokens)
            if success:
                await self.bot.send_chat(
                    f"{username} earned {tokens} God Token(s) for their "
                    f"${amount_dollars:.2f} donation! Use !godrequest <god>."
                )

    # === CLEANUP ===

    async def cleanup(self):
        if self.session:
            await self.session.close()
        self._save_data()
