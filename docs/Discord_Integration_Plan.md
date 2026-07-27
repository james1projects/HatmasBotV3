# Discord Integration Plan

Captured 2026-06-12. Phases 1, 2, and 4 are built. Phase 3 skipped.

## Architecture

One plugin, same process, same asyncio loop.

- Library: `discord.py>=2.6` (latest release has Python 3.14 fixes).
- Plugin: `plugins/discord_bridge.py`. Never name a file `discord.py`;
  it shadows the library import.
- Lifecycle: `setup(bot)` spawns the gateway task; `cleanup()` closes
  the client in the normal shutdown order.
- Listener API: `add_message_listener(coro)`, killdetector convention.
- Disabled by default. No token or `DISCORD_ENABLED = False` means the
  plugin prints one line and goes inert.

## One-time Discord setup (5 minutes)

1. discord.com/developers/applications: New Application, name it
   HatmasBot.
2. Bot tab: copy the token into `config_local.py` as
   `DISCORD_BOT_TOKEN`.
3. Same tab: enable MESSAGE CONTENT INTENT. Without it all message
   text arrives empty. SERVER MEMBERS stays off until Phase 5.
4. Installation tab: scope `bot`; permissions View Channels, Send
   Messages, Read Message History, Embed Links. Invite via the URL.
5. With Discord Developer Mode on: right-click the server for
   `DISCORD_GUILD_ID`, right-click the target channel for
   `DISCORD_DEFAULT_CHANNEL_ID`.
6. Set `DISCORD_ENABLED = True`.

## Config keys (config_local.py)

| Key | Notes |
|---|---|
| DISCORD_ENABLED | master switch, default False |
| DISCORD_BOT_TOKEN | secret |
| DISCORD_GUILD_ID | int, server ID |
| DISCORD_DEFAULT_CHANNEL_ID | int, default send target |
| DISCORD_ANNOUNCE_ENABLED | go-live announcements, default False |
| DISCORD_ANNOUNCE_CHANNEL_ID | falls back to default channel |
| DISCORD_ANNOUNCE_ROLE_ID | optional role ping, 0 = none |

## Phase 1: foundation (built)

Gateway connection, message observation with listener dispatch,
`send_message(text, channel_id=None, embed=None)`, graceful cleanup.
Mod commands `!discordstatus` and `!discordtest` for verification.

Test: fill config, restart, check console connect line, type in
Discord (console logs it), run both mod commands, quit cleanly.

**Quick channel test (added 2026-06-14).** To confirm the bot can
post to a specific channel without starting the whole bot, run
`python tools/discord_test.py --list` to see every channel it can
reach (with ids), then `python tools/discord_test.py <channel_id>
"your message"` to send. `discord_test.bat` is the one-press
wrapper. `!discordtest` also now takes an optional leading channel
id or `<#mention>` so a mod can target a channel from Twitch chat
(e.g. `!discordtest 123456789012345678 hello`); with no id it uses
DISCORD_DEFAULT_CHANNEL_ID as before.

## Phase 2: go-live announcements (built)

On the stream_status live transition, posts an embed (title, game,
link, thumbnail, optional role ping) to the announce channel.

Dedupe rule: max ONE automatic announcement per calendar day, local
time, persisted to `data/discord_announce.json`. Bot restarts and
stream restarts never re-announce. The day is only marked used after
a successful send, so failures retry on the next transition. Second
announcements in a day are manual-only.

Accepted edge: a stream crossing midnight that drops and recovers can
announce again. New calendar day, still one per day.

Test: enable, go live (announces within ~60s), restart bot while live
(suppressed), re-stream same day (suppressed), next day (announces).
Delete the state file to force a re-announce while testing.

## Phase 4: cross-platform commands + mod page (built)

See `Crossplatform_Commands_Plan.md`.

## Future phases

- Phase 3, stream recaps: end-of-stream summary (W-L, market movers,
  top song) posted automatically. Skipped for now.
- Phase 5, account linking: map Discord user to Twitch user so
  identity commands (!portfolio, !gamble) work from Discord. Needs a
  linking flow and the SERVER MEMBERS intent.

## Gotchas

- MESSAGE CONTENT intent must be on in BOTH the portal and the code.
- Token leaks mean full bot takeover. config_local.py only.
- If discord.py breaks on Python 3.14, Pycord (3.10-3.14, same API)
  is a drop-in fallback touching only this plugin's imports.
