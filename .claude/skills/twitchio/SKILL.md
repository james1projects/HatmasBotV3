---
name: twitchio
description: |
  TwitchIO v3.2.1 reference for building Twitch bots with Python. Use this skill whenever working on Twitch bot code that uses TwitchIO v3, including: EventSub WebSocket subscriptions, channel point redemptions, chat message handling, whisper handling, OAuth token management, custom commands, raid/sub/gift events, or any TwitchIO API usage. Also use when debugging TwitchIO errors like EventSub 403s, token issues, or subscription failures. Trigger on mentions of TwitchIO, Twitch bot, EventSub, channel points, or Twitch API in a Python context.
---

# TwitchIO v3.2.1 Reference

This skill captures hard-won knowledge from building a production Twitch bot with TwitchIO v3.2.1. It covers the patterns that work, the pitfalls that don't, and the debugging strategies that save hours.

## Core Architecture

TwitchIO v3 uses a `commands.Bot` class (extends `Client`) with EventSub WebSocket for real-time events. The bot manages multiple OAuth tokens internally and routes events to handler methods.

```python
from twitchio.ext import commands
from twitchio import eventsub

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            client_id="YOUR_CLIENT_ID",
            client_secret="YOUR_CLIENT_SECRET",
            bot_id="BOT_USER_ID",       # The bot account's Twitch user ID
            owner_id="OWNER_USER_ID",    # The broadcaster's Twitch user ID
            prefix="!",
        )
```

### Token Management

TwitchIO v3 manages tokens internally via `add_token()`. Each token is validated and associated with a Twitch user ID. Call `add_token()` **before** `bot.start()`:

```python
# Add both tokens before starting
await bot.add_token(BOT_ACCESS_TOKEN, BOT_REFRESH_TOKEN)
await bot.add_token(BROADCASTER_ACCESS_TOKEN, BROADCASTER_REFRESH_TOKEN)

# Then start the bot (this triggers setup_hook)
await bot.start()
```

---

## EventSub Subscriptions

### The Table Reference

This maps Twitch event types to TwitchIO subscription classes, event handler names, and payload models:

| Event | Subscription Class | Event Handler | Payload Model |
|-------|-------------------|---------------|---------------|
| Chat Message | `ChatMessageSubscription` | `event_message` | `ChatMessage` |
| Whisper Received | `WhisperReceivedSubscription` | `event_message_whisper` | Whisper payload |
| Channel Subscribe | `ChannelSubscribeSubscription` | `event_subscribe` | Subscribe payload |
| Channel Resub Message | `ChannelSubscribeMessageSubscription` | `event_subscription_message` | Resub payload |
| Channel Gift Sub | `ChannelSubscriptionGiftSubscription` | `event_subscription_gift` | Gift payload |
| Channel Raid | `ChannelRaidSubscription` | `event_channel_raid` | Raid payload |
| Channel Points Redeem | `ChannelPointsRedeemAddSubscription` | `event_custom_redemption_add` | `ChannelPointsRedemptionAdd` |
| Channel Points Reward Add | `ChannelPointsRewardAddSubscription` | `event_custom_reward_add` | `ChannelPointsRewardAdd` |
| Channel Points Reward Remove | `ChannelPointsRewardRemoveSubscription` | `event_custom_reward_remove` | `ChannelPointsRewardRemove` |
| Channel Points Redeem Update | `ChannelPointsRedeemUpdateSubscription` | `event_custom_redemption_update` | `ChannelPointsRedemptionUpdate` |

All subscription classes live in `twitchio.eventsub`.

### Subscribing in setup_hook vs on_ready

`setup_hook()` is called during `bot.start()` and is the standard place for EventSub subscriptions. The `commands.Bot` class has special token handling during `setup_hook` for subscriptions using `bot_id`/`owner_id`.

Plugin `on_ready()` methods called from within `setup_hook` run in a different context where automatic token selection may not work correctly.

### CRITICAL: The `token_for` Parameter

This is the single most important gotcha in TwitchIO v3.

**`subscribe_websocket()` defaults to using the App Token (client credentials) when no `token_for` is specified.** The app token has zero user-specific scopes. For any subscription requiring broadcaster scopes, you MUST pass `token_for`:

```python
# WRONG - will 403 with "subscription missing proper authorization"
payload = eventsub.ChannelPointsRedeemAddSubscription(
    broadcaster_user_id=OWNER_ID,
    reward_id=some_reward_id,
)
await bot.subscribe_websocket(payload=payload)

# CORRECT - explicitly use the broadcaster's token
await bot.subscribe_websocket(
    payload=payload,
    token_for=OWNER_ID,  # User ID whose token has the required scopes
)
```

**When to use `token_for`:**
- Channel point redemptions (`channel:read:redemptions` / `channel:manage:redemptions`)
- Any subscription requiring broadcaster-only scopes
- Any subscription made outside of `setup_hook()` (e.g., in plugin `on_ready()`)
- When you have multiple tokens and need to control which one is used

**The `token_for` value** is a user ID string (or `PartialUser`). TwitchIO looks up the token whose validated user_id matches this value.

### Subscription Examples

```python
async def setup_hook(self):
    # Chat - needs bot token (handled automatically in setup_hook)
    await self.subscribe_websocket(payload=eventsub.ChatMessageSubscription(
        broadcaster_user_id=self._owner_id,
        user_id=self._bot_id,
    ))

    # Whispers - needs bot token
    await self.subscribe_websocket(payload=eventsub.WhisperReceivedSubscription(
        user_id=self._bot_id,
    ))

    # Raids - no specific scope needed
    await self.subscribe_websocket(payload=eventsub.ChannelRaidSubscription(
        to_broadcaster_user_id=self._owner_id,
    ))

    # Subscriptions - needs channel:read:subscriptions (broadcaster scope)
    await self.subscribe_websocket(payload=eventsub.ChannelSubscribeSubscription(
        broadcaster_user_id=self._owner_id,
    ))

# In a plugin's on_ready() - MUST use token_for
async def on_ready(self):
    payload = eventsub.ChannelPointsRedeemAddSubscription(
        broadcaster_user_id=OWNER_ID,
        reward_id=reward_id,        # optional filter
    )
    await self.bot.subscribe_websocket(
        payload=payload,
        token_for=OWNER_ID,         # REQUIRED outside setup_hook
    )
```

---

## Event Handlers

Event handlers are methods on the Bot class named after the event. The payload type varies by event.

```python
async def event_message(self, payload: twitchio.ChatMessage):
    """Chat messages. payload.chatter has user info, payload.text has the message."""
    chatter = payload.chatter
    content = payload.text.strip()
    # chatter.name, chatter.display_name, chatter.moderator, chatter.broadcaster

async def event_custom_redemption_add(self, payload):
    """Channel point redemptions."""
    reward_id = payload.reward.id
    user_name = payload.user_name

async def event_channel_raid(self, payload):
    """Incoming raids."""
    raider = payload.from_broadcaster  # PartialUser
    viewer_count = payload.viewer_count

async def event_subscribe(self, payload):
    """New subscriptions."""
    user = payload.user
    username = user.name

async def event_subscription_message(self, payload):
    """Resub messages."""
    user = payload.user

async def event_subscription_gift(self, payload):
    """Gift subs."""
    user = payload.user  # the gifter
    total = getattr(payload, "total", 1)

async def event_message_whisper(self, payload):
    """Incoming whispers. Different shape than ChatMessage."""
    text = payload.text
    sender = payload.sender      # PartialUser
    recipient = payload.recipient  # PartialUser (the bot)
```

---

## Sending Messages

```python
# Send a chat message (requires bot_id and owner_id)
broadcaster = bot.create_partialuser(int(OWNER_ID))
sender = bot.create_partialuser(int(BOT_ID))
await broadcaster.send_message(sender=sender, message="Hello chat!")

# Send a whisper
bot_user = bot.create_partialuser(int(BOT_ID))
target_user = bot.create_partialuser(int(TARGET_USER_ID))
await bot_user.send_whisper(to_user=target_user, message="Hello!")

# Reply to a chat message
await message.respond(f"@{message.chatter.name} reply text here")
```

---

## OAuth Scopes Reference

### Bot Scopes (bot account)
```
chat:read, chat:edit, whispers:read, whispers:edit,
moderator:manage:banned_users,
user:read:chat, user:write:chat, user:bot,
user:read:whispers, user:manage:whispers
```

### Broadcaster Scopes (channel owner account)
```
channel:manage:broadcast          # Update stream title/game
channel:manage:predictions        # Create/resolve predictions
channel:read:subscriptions        # Detect subs/resubs/gifts
moderator:manage:shoutouts        # Official shoutout API
channel:manage:redemptions        # Create/manage channel point rewards
channel:read:redemptions          # Detect channel point redemptions
```

### OAuth URL Construction
Always include `force_verify=true` when re-authing to ensure Twitch shows the consent screen with all scopes (otherwise it may auto-approve with stale scopes):

```python
params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": " ".join(SCOPES),
    "force_verify": "true",  # Forces re-consent for new scopes
}
auth_url = f"https://id.twitch.tv/oauth2/authorize?{urlencode(params)}"
```

---

## Twitch Helix API Patterns

### Creating Channel Point Rewards
```python
async with session.post(
    f"https://api.twitch.tv/helix/channel_points/custom_rewards"
    f"?broadcaster_id={OWNER_ID}",
    headers={
        "Client-Id": CLIENT_ID,
        "Authorization": f"Bearer {BROADCASTER_TOKEN}",
        "Content-Type": "application/json",
    },
    json={
        "title": "My Reward",
        "cost": 500,
        "prompt": "Description shown to viewers",
        "is_enabled": True,
        "background_color": "#FFD700",
        "should_redemptions_skip_request_queue": True,
    },
) as resp:
    data = await resp.json()
    reward_id = data["data"][0]["id"]
```

### Fetching Existing Rewards
```python
async with session.get(
    f"https://api.twitch.tv/helix/channel_points/custom_rewards"
    f"?broadcaster_id={OWNER_ID}&only_manageable_rewards=true",
    headers=headers,
) as resp:
    data = await resp.json()
    for reward in data.get("data", []):
        print(reward["title"], reward["id"])
```

### Official Shoutout
```python
# POST https://api.twitch.tv/helix/chat/shoutouts
# ?from_broadcaster_id={OWNER_ID}&to_broadcaster_id={RAIDER_ID}&moderator_id={OWNER_ID}
# Returns 204 on success. Rate limited to 1 per 2 minutes.
```

### Token Refresh
```python
async with session.post(
    "https://id.twitch.tv/oauth2/token",
    data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": current_refresh_token,
    }
) as resp:
    data = await resp.json()
    new_access = data["access_token"]
    new_refresh = data["refresh_token"]
```

### Token Validation
```python
async with session.get(
    "https://id.twitch.tv/oauth2/validate",
    headers={"Authorization": f"OAuth {access_token}"}
) as resp:
    valid = resp.status == 200
```

---

## Common Pitfalls and Debugging

### 1. EventSub 403 "subscription missing proper authorization"
**Cause:** TwitchIO v3.2.1 picks the wrong token for certain subscription types, notably `ChannelPointsRedeemAddSubscription`. Even when the broadcaster token has the correct scopes and matching user_id, `subscribe_websocket()` uses the bot token. Confirmed by: validating the broadcaster token scopes via Twitch API right before the call (correct), then making the same subscription manually via Helix API with the broadcaster token (succeeds).

**Fix:** `token_for=BROADCASTER_USER_ID` may work in newer versions, but in v3.2.1 the workaround is to make the subscription directly via the Helix API:

```python
async def _manual_channel_points_subscribe(self):
    """Bypass TwitchIO's broken token selection for channel point subscriptions."""
    # Get session_id from TwitchIO internals:
    # self._websockets is defaultdict[user_id_str] → dict[session_id_str] → Websocket
    ws_session_id = None
    ws_obj = getattr(self, "_websockets", None)
    if isinstance(ws_obj, dict):
        for key in (str(self._owner_id), str(self._bot_id)):
            inner = ws_obj.get(key, {})
            if isinstance(inner, dict):
                for ws_node in inner.values():
                    if hasattr(ws_node, "session_id"):
                        ws_session_id = ws_node.session_id
                        break
            if ws_session_id:
                break
    if not ws_session_id:
        raise RuntimeError("Cannot find WebSocket session_id")

    headers = {
        "Client-Id": CLIENT_ID,
        "Authorization": f"Bearer {BROADCASTER_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "type": "channel.channel_points_custom_reward_redemption.add",
        "version": "1",
        "condition": {"broadcaster_user_id": str(OWNER_ID)},
        "transport": {"method": "websocket", "session_id": ws_session_id},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.twitch.tv/helix/eventsub/subscriptions",
            headers=headers, json=body,
        ) as resp:
            if resp.status not in (200, 202):
                raise RuntimeError(f"Subscription failed: {resp.status} {await resp.text()}")
```

The `event_custom_redemption_add` handler still fires normally — TwitchIO routes incoming WebSocket events by type regardless of how the subscription was created.

### 2. OAuth re-auth doesn't show new scopes
**Cause:** Twitch auto-approves returning users without showing the consent screen.
**Fix:** Add `force_verify=true` to the OAuth URL parameters.

### 3. Python module scoping with `global`
**Cause:** `global SOME_VAR` only affects the current module's namespace, not the module where the variable was originally defined. If `config.py` defines `TITLE = "x"` and `plugin.py` does `from config import TITLE; global TITLE; TITLE = "y"`, only `plugin.py`'s local reference changes — `config.TITLE` is still `"x"`.
**Fix:** Use `import core.config as _cfg; _cfg.TITLE = "y"` to modify the source module directly.

### 4. Whisper payload differs from ChatMessage
**Cause:** Whisper events have `.sender`, `.recipient`, `.text` — not `.chatter`. Code expecting `payload.chatter.name` will crash on whispers.
**Fix:** Create an adapter class that wraps the whisper payload with a `.chatter`-like interface, or handle whispers separately.

### 5. OBS source names are case-sensitive
When using OBS WebSocket's `GetSourceScreenshot`, the source name must match exactly (e.g., "Smite 2" not "smite 2"). A wrong case returns a valid but empty/black image with no error.

### 6. Token refresh race conditions
Multiple parts of the bot may hit 401s simultaneously and all try to refresh the same token. Use `asyncio.Lock()` per token and throttle refreshes (e.g., minimum 30s between attempts).

---

## Plugin Architecture Pattern

A clean plugin pattern for TwitchIO v3 bots:

```python
class MyPlugin:
    def __init__(self, token_manager=None):
        self.bot = None
        self._token_manager = token_manager

    def setup(self, bot):
        """Called during bot.register_plugin(). Store bot reference, register commands."""
        self.bot = bot
        bot.register_command("mycommand", self.handle_mycommand)

    async def on_ready(self):
        """Called after bot connects. Subscribe to EventSub, create API resources."""
        # EventSub subscriptions that need broadcaster scopes:
        payload = eventsub.SomeSubscription(broadcaster_user_id=OWNER_ID)
        await self.bot.subscribe_websocket(payload=payload, token_for=OWNER_ID)

    async def cleanup(self):
        """Called on bot shutdown. Close HTTP sessions, save state."""
        pass

    async def handle_mycommand(self, message, args):
        await self.bot.send_chat(f"Hello {message.chatter.name}!")
```

Register in main.py:
```python
plugin = MyPlugin(token_manager=token_mgr)
bot.register_plugin("myplugin", plugin)
```

The bot calls `plugin.setup(bot)` during registration, `plugin.on_ready()` during `setup_hook()`, and `plugin.cleanup()` during shutdown.

---

## Useful Links

- TwitchIO 3.2.1 docs: https://twitchio.dev/en/stable/
- EventSub subscription types: https://twitchio.dev/en/stable/references/eventsub_subscriptions.html
- Twitch EventSub reference: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/
- Twitch API reference: https://dev.twitch.tv/docs/api/reference/
