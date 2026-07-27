# Social Media Tabs for hatmaster.tv — Implementation Plan

A tab strip on the landing page that surfaces your latest content from Twitch (live-only, existing), YouTube, TikTok, and Bluesky. To pick this back up tomorrow, just say "let's do the social tabs."

---

## Handles to wire in

| Platform | Handle / URL | Notes |
|---|---|---|
| Twitch | (already configured via `TWITCH_OWNER_USERNAME`) | Live status drives tab visibility. |
| YouTube | `@Hatmaster` → `https://www.youtube.com/@Hatmaster` | Need the underlying `UC…` channel ID. Either resolve via `youtube/v3/channels?forHandle=@Hatmaster` or check `core/config_local.py:YOUTUBE_CHANNEL_ID`. |
| TikTok | `@awfulmasterhat` → `https://www.tiktok.com/@awfulmasterhat` | Auto-fetch is unreliable; we'll go with manual `TIKTOK_LATEST_VIDEO_URL` config + profile link. |
| Bluesky | `hatmasteryt.bsky.social` → `https://bsky.app/profile/hatmasteryt.bsky.social` | AT Protocol public API, no auth needed. |

---

## Backend (in `core/public_webserver.py`)

Three new read-only endpoints. All cache aggressively (15 min) so we don't hit any rate limits.

### `GET /api/social/youtube`

```python
async def api_social_youtube(request):
    # Cached 15 min in self._cache["youtube_videos"]
    # Calls https://www.googleapis.com/youtube/v3/search
    #   ?key=YOUTUBE_API_KEY
    #   &channelId=YOUTUBE_CHANNEL_ID
    #   &part=snippet
    #   &order=date
    #   &maxResults=12
    #   &type=video
    # Returns: list of {video_id, title, thumbnail_url, published_at, view_count?}
```

User chose **all uploads, latest first** — no title filtering.

### `GET /api/social/bluesky`

```python
async def api_social_bluesky(request):
    # Cached 15 min in self._cache["bluesky_feed"]
    # Calls https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed
    #   ?actor=hatmasteryt.bsky.social
    #   &limit=10
    # No auth required.
    # Returns: list of {cid, text, created_at, embed_image_url?, like_count, reply_count}
```

Skip reposts (`reason.$type == 'app.bsky.feed.defs#reasonRepost'`) and replies for the public feed.

### `GET /api/social/tiktok`

```python
async def api_social_tiktok(request):
    # No live API call — just returns the manually-configured latest video URL
    # plus the profile link. Add to config:
    #   TIKTOK_USERNAME = "awfulmasterhat"
    #   TIKTOK_LATEST_VIDEO_URL = "https://www.tiktok.com/@awfulmasterhat/video/<id>"
    return {
        "profile_url": f"https://www.tiktok.com/@{TIKTOK_USERNAME}",
        "latest_video_url": TIKTOK_LATEST_VIDEO_URL,
    }
```

If we ever want auto-discovery, options are: TikTok Display API (requires app registration), rsshub.app feed, or a small scraper (fragile). Manual works for now.

---

## Frontend (in `public/landing.html`)

Replace the current Twitch-only embed section with a tab strip:

```
┌──────────────────────────────────────────────────────────────┐
│ [ TWITCH 🔴LIVE ]  [ YOUTUBE ]  [ TIKTOK ]  [ BLUESKY ]      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│        (content for the currently-selected tab)              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**Default-active tab logic:**
- If `is_live` from `/api/stream-status` → Twitch tab.
- Else if YouTube has any videos → YouTube tab.
- Else → Bluesky tab.

**Tab content:**

| Tab | When live (Twitch) | When offline |
|---|---|---|
| Twitch | `<iframe>` player + chat (existing code) | Tab grayed/hidden |
| YouTube | List of 12 latest videos as cards (thumbnail + title + relative date). Clicking embeds the video inline above the grid. | Same — always available. |
| TikTok | Embed of `TIKTOK_LATEST_VIDEO_URL` via TikTok's `<blockquote class="tiktok-embed">` script, plus a "Visit my TikTok" button to the profile URL. | Same. |
| Bluesky | Twitter-style feed: card per post with text, optional image, like/reply counts, timestamp. Click a card → opens the post on bsky.app in a new tab. | Same. |

Persist last-active tab in `localStorage('hatmas:active-social-tab')` so revisitors land on whatever they had open last.

**Styling:** match the existing brand band — B612 Mono caps for tab labels, sharp 4px corners, amber accent color on active tab, terminal-style underline on the active tab.

---

## WebSocket integration (optional polish)

When `stream_live` fires from `OverlayManager` (already wired), broadcast a small `social_status_update` event to all `/ws/social` clients so the landing page can flip the Twitch tab to LIVE without a full reload. Mirrors what the existing `/ws/yt/<channel>` and `/ws/twitch/<user>` patterns already do for portfolio pages.

---

## New config values

```python
# core/config.py
TIKTOK_USERNAME = "awfulmasterhat"
TIKTOK_LATEST_VIDEO_URL = ""  # paste the URL of your latest TikTok here when you post one
BLUESKY_HANDLE = "hatmasteryt.bsky.social"

# Cache TTLs
SOCIAL_FEED_CACHE_TTL = 900  # 15 min — tune down for testing
```

`YOUTUBE_API_KEY` and `YOUTUBE_CHANNEL_ID` already exist in `config_local.py` (verify before we start).

---

## Build order

1. **Verify `YOUTUBE_CHANNEL_ID`.** Quick `youtube/v3/channels?forHandle=@Hatmaster&key=…` curl to confirm the ID matches what's in config. Update if needed.
2. **Add the three new config values** (`TIKTOK_USERNAME`, `TIKTOK_LATEST_VIDEO_URL`, `BLUESKY_HANDLE`). Default the TikTok URL to a known recent video so the tab isn't empty on first load.
3. **Implement the three `/api/social/...` endpoints** in `public_webserver.py` with 15-min caches.
4. **Add `_fetch_youtube_videos()`** helper that parses the YouTube API response into the shape the frontend expects.
5. **Add `_fetch_bluesky_feed()`** helper using `aiohttp.ClientSession`. Public Bluesky API; no auth needed.
6. **Build the tab UI in `landing.html`.** Five small components: tab strip, Twitch panel (lift existing code), YouTube grid, TikTok embed, Bluesky feed.
7. **Smoke test** with a live stream toggle (set `is_live=true` in test mode), confirm tabs auto-switch.
8. **Update `HatmasBot.md`** v2.5 section with the new endpoints and feature.

Estimated build: 2–3 hours total. Most time goes into the Bluesky feed renderer and getting the Twitch tab styling consistent with the new tab strip.

---

## Things to watch out for

- **TikTok embed script** is `<script async src="https://www.tiktok.com/embed.js"></script>` — load it once globally, not per-tab-switch, or it'll re-init weirdly.
- **Bluesky API rate limits** are generous (3,000 requests / 5 min for unauthenticated public reads), but cache anyway.
- **YouTube Data API quota** is the real constraint: 10,000 units/day, and `search.list` costs **100 units per call**. 15-min cache = 96 calls/day = 9,600 units. Cutting it close. Consider bumping cache TTL to 30 min or using `playlistItems.list` (1 unit) on the channel's "uploads" playlist instead — same data, 100x cheaper.
- **TikTok auto-discovery is intentionally hard.** If you ever want to remove the manual step, the cleanest path is registering a TikTok for Developers app + OAuth, which is a multi-day undertaking. Not worth it for v1.

---

## Future iteration ideas (parking lot)

- Make the YouTube grid filterable by title pattern (back to the "Full Gameplay only" idea if you change your mind).
- Replace the manual TikTok URL with a small button on your Stream Deck that prompts "paste new TikTok URL" and updates `TIKTOK_LATEST_VIDEO_URL` in `config_local.py`. Zero-friction posting.
- Bluesky post composer right on the page (would need OAuth — heavier).
- Add a 5th tab for the YouTube "Shorts" tab if you ever want to feature them separately.
- "Latest from each" view: a small fifth tab that shows ONE recent item from each platform side-by-side, for visitors who want a quick scan.
