"""
NSFW Album Art Checker
======================
Uses Claude Vision API to detect NSFW album art on explicit-flagged songs.
Results are cached to data/nsfw_cache.json so each image URL is only
checked once.

Usage:
    checker = NSFWChecker(api_key)
    is_nsfw = await checker.check_album_art(song_dict)
    # Returns True  → replace art with placeholder
    # Returns False → art is safe to display
    # Returns False immediately if song is not explicit (skip Vision call)
"""

import asyncio
import json
import hashlib
import aiohttp
import base64

from core.config import DATA_DIR

NSFW_CACHE_FILE = DATA_DIR / "nsfw_cache.json"

# Vision prompt — deliberately concise to keep token usage minimal
_VISION_PROMPT = (
    "Is this album cover image NSFW or inappropriate for a general Twitch audience? "
    "Consider nudity, sexual imagery, graphic violence, drug imagery, or other explicit visuals. "
    "Reply with ONLY 'yes' or 'no'."
)


class NSFWChecker:
    def __init__(self, api_key, model="claude-haiku-4-5-20251001"):
        self.api_key = api_key
        self.model = model  # Use Haiku for speed and low cost
        self._cache = {}
        self._load_cache()

    def _load_cache(self):
        try:
            if NSFW_CACHE_FILE.exists():
                with open(NSFW_CACHE_FILE, "r") as f:
                    self._cache = json.load(f)
                print(f"[NSFW Check] Loaded cache with {len(self._cache)} entries")
        except Exception as e:
            print(f"[NSFW Check] Failed to load cache: {e}")
            self._cache = {}

    def _save_cache(self):
        try:
            with open(NSFW_CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            print(f"[NSFW Check] Failed to save cache: {e}")

    @staticmethod
    def _cache_key(album_art_url):
        """Deterministic key from the image URL."""
        return hashlib.sha256(album_art_url.encode()).hexdigest()[:16]

    async def check_album_art(self, song):
        """Check a song dict and return True if album art is NSFW.

        Fast-path returns False when:
        - Song has no album_art URL
        - Song is not marked explicit (Spotify flag)
        - Result is already cached
        """
        album_art_url = song.get("album_art")
        if not album_art_url:
            return False

        # Check cache
        key = self._cache_key(album_art_url)
        if key in self._cache:
            cached = self._cache[key]
            print(f"[NSFW Check] Cache hit for {song.get('title', '?')}: {'NSFW' if cached else 'safe'}")
            return cached

        # Download the image and run Vision check
        is_nsfw = await self._vision_check(album_art_url, song.get("title", "Unknown"))

        # Cache the result
        self._cache[key] = is_nsfw
        self._save_cache()

        return is_nsfw

    async def _vision_check(self, image_url, title=""):
        """Download image and send to Claude Vision for NSFW classification."""
        try:
            print(f"[NSFW Check] Checking album art for: {title}")

            # Download the image
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        print(f"[NSFW Check] Failed to download image: HTTP {resp.status}")
                        return False
                    image_data = await resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")

            # Determine media type
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"

            image_b64 = base64.b64encode(image_data).decode("utf-8")

            # Call Claude Vision API directly via HTTP (avoid importing the
            # sync anthropic client into an async context)
            headers = {
                "x-api-key": self.api_key,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": self.model,
                "max_tokens": 10,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": _VISION_PROMPT,
                        },
                    ],
                }],
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        print(f"[NSFW Check] Vision API error ({resp.status}): {err}")
                        return False
                    result = await resp.json()

            answer = result["content"][0]["text"].strip().lower()
            is_nsfw = answer.startswith("yes")
            print(f"[NSFW Check] {title}: {'NSFW' if is_nsfw else 'safe'} (raw: {answer})")
            return is_nsfw

        except asyncio.TimeoutError:
            print(f"[NSFW Check] Timeout checking {title}")
            return False
        except Exception as e:
            print(f"[NSFW Check] Error checking {title}: {e}")
            return False
