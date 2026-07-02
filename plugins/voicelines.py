"""
VoiceLinePlugin
================
Channel point redemptions for god-specific voice lines (jokes, taunts, laughs).

Creates three channel point rewards on startup:
  - God Joke
  - God Taunt
  - God Laugh

When redeemed, plays a random voice line from the current god's folder
via the /overlay/voicelines OBS browser source.  If an MP4 animation
exists in data/smite_animations/<god>/<type>.mp4, it is played alongside
the audio.

Requires broadcaster scope: channel:manage:redemptions
Voice lines must be pre-downloaded via: python tools/download_voicelines.py
"""

import asyncio
import json
import random
import time
from pathlib import Path

import aiohttp
from twitchio import eventsub

from core.config import (
    TWITCH_CLIENT_ID, TWITCH_OWNER_ID,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VOICELINE_DIR = DATA_DIR / "smite_voicelines"
ANIMATION_DIR = DATA_DIR / "smite_animations"
STATE_FILE = DATA_DIR / "voiceline_rewards.json"

# ---------------------------------------------------------------------------
# Reward definitions
# ---------------------------------------------------------------------------

REWARD_DEFS = {
    "god_joke": {
        "title": "God Joke",
        "cost": 500,
        "prompt_template": "Play a joke for {god}",
        "folder": "jokes",
        "background_color": "#FFD700",
    },
    "god_taunt": {
        "title": "God Taunt",
        "cost": 500,
        "prompt_template": "Play a taunt for {god}",
        "folder": "taunts",
        "background_color": "#FF4444",
    },
    "god_laugh": {
        "title": "God Laugh",
        "cost": 200,
        "prompt_template": "{god} laughs",
        "folder": "laughs",
        "background_color": "#9B59B6",
    },
}


def _format_god_name(slug: str) -> str:
    """Convert a slug like 'ah_muzen_cab' into 'Ah Muzen Cab'."""
    return slug.replace("_", " ").title()

# Map from Twitch reward ID → our internal key (god_joke, god_taunt, god_laugh)
# Persisted to STATE_FILE so we don't recreate rewards every startup.


class VoiceLinePlugin:
    """Channel point voice line redemptions tied to the current Smite god."""

    def __init__(self, token_manager=None):
        self.bot = None
        self._token_manager = token_manager
        self._session: aiohttp.ClientSession | None = None
        self._web_server = None

        # reward_id → internal key mapping  (loaded from file or created)
        self._reward_map: dict[str, str] = {}
        # internal key → reward_id  (reverse)
        self._key_to_reward: dict[str, str] = {}

        # Current god slug for file lookup (e.g. "achilles", "ah_muzen_cab")
        self._current_god_slug: str | None = None
        self._last_god_slug: str | None = "sylvanus"  # default until first detection

    # -----------------------------------------------------------------
    # Plugin lifecycle
    # -----------------------------------------------------------------

    def setup(self, bot):
        self.bot = bot
        self._web_server = bot.web_server

    async def on_ready(self):
        """Create HTTP session, load/create rewards, subscribe to EventSub."""
        self._session = aiohttp.ClientSession()
        self._load_state()

        # Create any missing rewards via Twitch API
        await self._ensure_rewards()

        # NOTE: EventSub subscription for channel point redemptions is handled
        # in bot.py setup_hook() — not here — because setup_hook has correct
        # token context for broadcaster-scoped subscriptions. Subscribing from
        # plugin on_ready() resulted in 403 "subscription missing proper authorization".

    async def cleanup(self):
        if self._session:
            await self._session.close()

    # -----------------------------------------------------------------
    # God tracking — called from main.py hooks
    # -----------------------------------------------------------------

    def set_current_god(self, god_name: str | None):
        """Update the current god. Called when SmitePlugin detects a god."""
        if god_name:
            slug = god_name.lower().replace(" ", "_").replace("'", "")
            changed = slug != self._current_god_slug
            self._current_god_slug = slug
            self._last_god_slug = slug
            self._save_state()  # persist last god for restarts
            print(f"[VoiceLine] Current god: {god_name} ({slug})")
            if changed:
                # Fire-and-forget update of reward prompts on Twitch
                try:
                    asyncio.create_task(self._update_reward_prompts(slug))
                except RuntimeError:
                    # No running event loop (unlikely in normal bot flow)
                    pass
        else:
            self._current_god_slug = None

    def get_active_god_slug(self) -> str | None:
        """Return the current god slug, or the last known god as fallback."""
        return self._current_god_slug or self._last_god_slug

    # -----------------------------------------------------------------
    # Public playback API — callable from other plugins
    # -----------------------------------------------------------------
    #
    # The Twitch-redemption path through `handle_redemption()` is tied
    # to the EventSub data model (reward_id, user_name, etc). For
    # internal triggers (e.g. !spin announces the chosen god), we want
    # a thin wrapper that just plays the right file for a given god
    # without faking a redemption.

    def play_god_select(self, god_name: str,
                         triggered_by: str = "system") -> bool:
        """Push the god-selection voice line for `god_name` to the
        overlay. Returns True if a file was queued, False if no audio
        file exists for this god (or the web server isn't ready yet).

        Used by !spin to play the chosen god's "select" line when the
        spin lands. Falls into the same /api/voiceline_audio URL space
        the overlay already polls, so we don't need any new endpoint.
        """
        if not god_name:
            return False
        if not self._web_server:
            print("[VoiceLine] play_god_select: web server not ready")
            return False

        # Slug normalization matches the rest of the plugin:
        # "Ah Muzen Cab" -> "ah_muzen_cab", "Chang'e" -> "change".
        slug = god_name.lower().replace(" ", "_").replace("'", "")
        folder = "god_selection"
        vl_dir = VOICELINE_DIR / slug / folder
        if not vl_dir.exists():
            print(f"[VoiceLine] No {folder} folder for {slug}")
            return False

        ogg_files = list(vl_dir.glob("*.ogg"))
        if not ogg_files:
            print(f"[VoiceLine] No .ogg files in {vl_dir}")
            return False

        # Each god typically has exactly one *_Select.ogg, but if
        # multiple exist (e.g. alt skins), pick one at random.
        chosen = random.choice(ogg_files)
        audio_url = f"/api/voiceline_audio/{slug}/{folder}/{chosen.name}"

        # Optional animation: god_selection.mp4 in the god's anim dir.
        anim_path = ANIMATION_DIR / slug / "god_selection.mp4"
        video_url = None
        if anim_path.exists():
            video_url = f"/api/voiceline_video/{slug}/{anim_path.name}"

        god_display = god_name
        self._web_server.trigger_voiceline_event({
            "type":      "god_select",
            "god":       god_display,
            "user":      triggered_by,
            "audio_url": audio_url,
            "video_url": video_url,
            "timestamp": time.time(),
        })
        print(f"[VoiceLine] god_select for {god_display}: {chosen.name} "
              f"(via {triggered_by})")
        return True

    # -----------------------------------------------------------------
    # Helix: cancel/refund a channel-point redemption
    # -----------------------------------------------------------------

    async def _refund_redemption(self, reward_id: str,
                                 redemption_id: str | None) -> bool:
        """
        Cancel a channel-point redemption via the Helix API. This refunds
        the viewer's points and removes the redemption from the
        broadcaster's queue.

        Returns True on success, False on any failure (missing IDs,
        missing token manager, non-2xx response, exception).

        Endpoint:
            PATCH https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions
                ?broadcaster_id=<owner>&reward_id=<reward>&id=<redemption>
            Body: {"status": "CANCELED"}
        Requires the `channel:manage:redemptions` broadcaster scope.
        """
        if not redemption_id:
            print("[VoiceLine] Cannot refund: no redemption_id "
                  "(payload missing .id?).")
            return False
        if self._token_manager is None:
            print("[VoiceLine] Cannot refund: no token_manager wired up.")
            return False
        if not TWITCH_OWNER_ID or TWITCH_OWNER_ID == "YOUR_OWNER_ID":
            print("[VoiceLine] Cannot refund: TWITCH_OWNER_ID not configured.")
            return False

        url = (
            "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
            f"?broadcaster_id={TWITCH_OWNER_ID}"
            f"&reward_id={reward_id}"
            f"&id={redemption_id}"
        )
        body = {"status": "CANCELED"}

        async def _do_patch(headers):
            async with aiohttp.ClientSession() as session:
                async with session.patch(url, headers=headers, json=body) as resp:
                    return resp.status, await resp.text()

        try:
            headers = await self._token_manager.get_broadcaster_headers()
            status, text = await _do_patch(headers)

            # Token expired? Refresh and try once more.
            if status == 401:
                refreshed = await self._token_manager.handle_401("broadcaster")
                if refreshed:
                    headers = await self._token_manager.get_broadcaster_headers()
                    status, text = await _do_patch(headers)

            if 200 <= status < 300:
                print(f"[VoiceLine] Refunded redemption {redemption_id} "
                      f"(reward {reward_id}).")
                return True

            print(f"[VoiceLine] Refund failed: HTTP {status} {text}")
            return False
        except Exception as exc:
            print(f"[VoiceLine] Refund exception: {exc}")
            return False

    # -----------------------------------------------------------------
    # EventSub handler — called by bot when a redemption fires
    # -----------------------------------------------------------------

    async def handle_redemption(self, reward_id: str, user_name: str,
                                redemption_id: str | None = None):
        """
        Process a channel point redemption for one of our voice line rewards.
        Returns True if handled, False if not ours.

        `redemption_id` is the unique ID of THIS redemption event (distinct
        from `reward_id`, which is the same for every redemption of the
        same custom reward). It's used by the Helix refund API when we
        can't fulfill the redemption (e.g. no god selected).
        """
        key = self._reward_map.get(reward_id)
        if not key:
            return False

        # Honor the dashboard voicelines feature toggle. Returning True
        # tells the bot we recognized the reward (so it doesn't keep
        # dispatching it elsewhere) — we just don't play anything.
        if self.bot is not None and not self.bot.is_feature_enabled("voicelines"):
            return True

        god_slug = self.get_active_god_slug()
        if not god_slug:
            refunded = await self._refund_redemption(reward_id, redemption_id)
            if refunded:
                await self.bot.send_chat(
                    f"@{user_name} no god selected, points refunded."
                )
            else:
                await self.bot.send_chat(
                    f"@{user_name} no god selected; auto-refund failed, "
                    f"please ping a mod."
                )
            return True

        reward_def = REWARD_DEFS[key]
        folder = reward_def["folder"]

        # Find voice line files. Missing folder / no files is a
        # fulfillment failure just like "no god selected" — refund the
        # redemption instead of keeping the viewer's points.
        vl_dir = VOICELINE_DIR / god_slug / folder
        ogg_files = list(vl_dir.glob("*.ogg")) if vl_dir.exists() else []
        if not ogg_files:
            refunded = await self._refund_redemption(reward_id, redemption_id)
            suffix = (" Points refunded." if refunded
                      else " Auto-refund failed, please ping a mod.")
            await self.bot.send_chat(
                f"@{user_name} No {folder} voice lines found for "
                f"{god_slug.replace('_', ' ').title()}.{suffix}"
            )
            return True

        # Pick a random voice line
        chosen = random.choice(ogg_files)

        # Check for animation MP4
        animation_path = ANIMATION_DIR / god_slug / f"{folder.rstrip('s')}.mp4"
        # Try singular form: jokes→joke, taunts→taunt, laughs→laugh
        if not animation_path.exists():
            animation_path = ANIMATION_DIR / god_slug / f"{folder}.mp4"
        has_animation = animation_path.exists()

        # Build the relative URLs for the overlay
        # Audio served via /api/voiceline_audio/<god>/<folder>/<file>
        audio_url = f"/api/voiceline_audio/{god_slug}/{folder}/{chosen.name}"
        video_url = None
        if has_animation:
            video_url = (
                f"/api/voiceline_video/{god_slug}/"
                f"{animation_path.name}"
            )

        # Push event to webserver queue
        god_display = god_slug.replace("_", " ").title()
        event = {
            "type": key,
            "god": god_display,
            "user": user_name,
            "audio_url": audio_url,
            "video_url": video_url,
            "timestamp": time.time(),
        }

        if self._web_server:
            self._web_server.trigger_voiceline_event(event)

        print(
            f"[VoiceLine] {user_name} redeemed {reward_def['title']} "
            f"for {god_display}: {chosen.name}"
            f"{' (with animation)' if has_animation else ''}"
        )
        return True

    # -----------------------------------------------------------------
    # Twitch API — create/manage custom rewards
    # -----------------------------------------------------------------

    async def _broadcaster_headers(self):
        """Get auth headers using the broadcaster token."""
        from core import config as _cfg
        token = _cfg.TWITCH_BROADCASTER_TOKEN
        return {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _ensure_rewards(self):
        """Create any rewards that don't exist yet on Twitch."""
        headers = await self._broadcaster_headers()

        # First, fetch existing custom rewards to see if ours already exist
        existing_titles = {}
        try:
            async with self._session.get(
                f"https://api.twitch.tv/helix/channel_points/custom_rewards"
                f"?broadcaster_id={TWITCH_OWNER_ID}&only_manageable_rewards=true",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for r in data.get("data", []):
                        existing_titles[r["title"]] = r["id"]
                elif resp.status == 401 and self._token_manager:
                    print("[VoiceLine] Token expired, refreshing...")
                    if await self._token_manager.handle_401("broadcaster"):
                        headers = await self._broadcaster_headers()
                        async with self._session.get(
                            f"https://api.twitch.tv/helix/channel_points/custom_rewards"
                            f"?broadcaster_id={TWITCH_OWNER_ID}&only_manageable_rewards=true",
                            headers=headers,
                        ) as resp2:
                            if resp2.status == 200:
                                data = await resp2.json()
                                for r in data.get("data", []):
                                    existing_titles[r["title"]] = r["id"]
                else:
                    body = await resp.text()
                    print(f"[VoiceLine] Failed to fetch rewards: {resp.status} {body}")
        except Exception as e:
            print(f"[VoiceLine] Error fetching rewards: {e}")

        # Create or map each reward
        changed = False
        for key, defn in REWARD_DEFS.items():
            # Already have it mapped?
            if self._key_to_reward.get(key):
                continue

            # Exists on Twitch but not in our state file?
            if defn["title"] in existing_titles:
                rid = existing_titles[defn["title"]]
                self._reward_map[rid] = key
                self._key_to_reward[key] = rid
                print(f"[VoiceLine] Found existing reward: {defn['title']} ({rid})")
                changed = True
                continue

            # Create it
            active_slug = self.get_active_god_slug() or "sylvanus"
            initial_prompt = defn["prompt_template"].format(
                god=_format_god_name(active_slug)
            )
            body = {
                "title": defn["title"],
                "cost": defn["cost"],
                "prompt": initial_prompt,
                "is_enabled": True,
                "background_color": defn.get("background_color", "#9B59B6"),
                "should_redemptions_skip_request_queue": True,
            }
            try:
                async with self._session.post(
                    f"https://api.twitch.tv/helix/channel_points/custom_rewards"
                    f"?broadcaster_id={TWITCH_OWNER_ID}",
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        reward = data["data"][0]
                        rid = reward["id"]
                        self._reward_map[rid] = key
                        self._key_to_reward[key] = rid
                        print(f"[VoiceLine] Created reward: {defn['title']} ({rid})")
                        changed = True
                    else:
                        body_text = await resp.text()
                        print(
                            f"[VoiceLine] Failed to create {defn['title']}: "
                            f"{resp.status} {body_text}"
                        )
            except Exception as e:
                print(f"[VoiceLine] Error creating {defn['title']}: {e}")

        if changed:
            self._save_state()

        # Sync prompts to the current/last known god on startup so restored
        # state is reflected on Twitch even if no match has been detected yet.
        if self._key_to_reward:
            await self._update_reward_prompts()

    async def _update_reward_prompts(self, god_slug: str | None = None):
        """PATCH each reward on Twitch so its prompt references the given god."""
        slug = god_slug or self.get_active_god_slug()
        if not slug or not self._key_to_reward or not self._session:
            return

        god_display = _format_god_name(slug)
        headers = await self._broadcaster_headers()

        for key, reward_id in list(self._key_to_reward.items()):
            defn = REWARD_DEFS.get(key)
            if not defn:
                continue
            new_prompt = defn["prompt_template"].format(god=god_display)
            url = (
                f"https://api.twitch.tv/helix/channel_points/custom_rewards"
                f"?broadcaster_id={TWITCH_OWNER_ID}&id={reward_id}"
            )
            body = {"prompt": new_prompt}
            try:
                async with self._session.patch(
                    url, headers=headers, json=body
                ) as resp:
                    if resp.status == 200:
                        print(
                            f"[VoiceLine] Updated {defn['title']} prompt: "
                            f"{new_prompt}"
                        )
                    elif resp.status == 401 and self._token_manager:
                        print("[VoiceLine] Token expired on PATCH, refreshing...")
                        if await self._token_manager.handle_401("broadcaster"):
                            headers = await self._broadcaster_headers()
                            async with self._session.patch(
                                url, headers=headers, json=body
                            ) as resp2:
                                if resp2.status == 200:
                                    print(
                                        f"[VoiceLine] Updated {defn['title']} "
                                        f"prompt: {new_prompt}"
                                    )
                                else:
                                    text = await resp2.text()
                                    print(
                                        f"[VoiceLine] Failed to update "
                                        f"{defn['title']} after refresh: "
                                        f"{resp2.status} {text}"
                                    )
                    else:
                        text = await resp.text()
                        print(
                            f"[VoiceLine] Failed to update {defn['title']} "
                            f"prompt: {resp.status} {text}"
                        )
            except Exception as e:
                print(f"[VoiceLine] Error updating {defn['title']} prompt: {e}")

    # -----------------------------------------------------------------
    # State persistence
    # -----------------------------------------------------------------

    def _load_state(self):
        """Load reward ID mappings and last god from disk."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self._reward_map = data.get("reward_map", {})
                self._key_to_reward = {v: k for k, v in self._reward_map.items()}
                saved_god = data.get("last_god_slug")
                if saved_god:
                    self._last_god_slug = saved_god
                    print(f"[VoiceLine] Restored last god: {saved_god}")
                print(f"[VoiceLine] Loaded {len(self._reward_map)} reward mappings")
            except Exception as e:
                print(f"[VoiceLine] Failed to load state: {e}")

    def _save_state(self):
        """Persist reward ID mappings and last god to disk."""
        try:
            data = {
                "reward_map": self._reward_map,
                "last_god_slug": self._last_god_slug,
            }
            from core.atomic_io import atomic_write_json
            atomic_write_json(STATE_FILE, data)
        except Exception as e:
            print(f"[VoiceLine] Failed to save state: {e}")
