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
  - !godclear           — Mods clear the entire queue
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
# Canonical list for fuzzy matching (lowercase). Add new gods as they release.
SMITE2_GODS = [
    "agni", "ah muzen cab", "ah puch", "amaterasu", "anhur", "anubis",
    "ao kuang", "aphrodite", "apollo", "arachne", "ares", "artemis",
    "athena", "atlas", "awilix", "bacchus", "bakasura", "baron samedi",
    "bastet", "bellona", "cabrakan", "cerberus", "cernunnos",
    "chaac", "change", "chernobog", "chiron", "chronos", "cthulhu",
    "cu chulainn", "cupid", "da ji", "discordia", "erlang shen",
    "fafnir", "fenrir", "freya", "ganesha", "geb", "hades", "he bo",
    "hel", "hera", "hercules", "hou yi", "hun batz", "isis", "izanami",
    "janus", "jing wei", "kali", "khepri", "king arthur", "kukulkan",
    "kumbhakarna", "kuzenbo", "loki", "medusa", "mercury", "merlin",
    "mordred", "ne zha", "neith", "nemesis", "nike", "nox", "nu wa",
    "odin", "osiris", "pele", "persephone", "poseidon", "ra",
    "rama", "ravana", "scylla", "serqet", "skadi", "sobek",
    "sol", "sun wukong", "susano", "sylvanus", "terra", "thanatos",
    "the morrigan", "thor", "thoth", "tiamat", "tsukuyomi", "tyr",
    "ullr", "vulcan", "xbalanque", "xing tian", "yemoja", "ymir",
    "zeus", "zhong kui",
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
        with open(GODREQ_QUEUE_FILE, "w") as f:
            json.dump(self.queue, f, indent=2)

    def _save_history(self, entry):
        history = []
        if GODREQ_HISTORY_FILE.exists():
            try:
                with open(GODREQ_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                pass
        history.append(entry)
        with open(GODREQ_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("godrequest", self.cmd_godrequest)
        bot.register_command("godreq", self.cmd_godreq, mod_only=True)
        bot.register_command("godqueue", self.cmd_godqueue)
        bot.register_command("godskip", self.cmd_godskip, mod_only=True)
        bot.register_command("godclear", self.cmd_godclear, mod_only=True)
        bot.register_command("godtokens", self.cmd_godtokens)
        bot.register_command("remove", self.cmd_remove, mod_only=True)
        bot.register_command("godlist", self.cmd_godlist)

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
            next_god = self.queue[0]["god"]

            # Set the text to the god name
            try:
                await obs.update_text_source(OBS_SOURCE_GODREQ_TEXT, next_god)
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

            await self.bot.send_chat(
                f"God request fulfilled! Playing {next_god} as requested by "
                f"{completed['requester']}. "
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
            items.append(f"{i+1}. {entry['god']} ({entry['requester']})")

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
        """!godclear — Mods clear the entire queue."""
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

        if pos < 1 or pos > len(self.queue):
            await self.bot.send_reply(
                message,
                f"Invalid position. Queue has {len(self.queue)} entries (1-{len(self.queue)}).",
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
            items.append(f"{i+1}. {entry['god']} ({entry['requester']})")

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
