# Cross-Platform Commands + Mod Page (Discord Phase 4)

Captured and built 2026-06-12. Decisions: native Discord slash
commands (Discord intercepts plain "/" text), read-only commands
first (identity commands wait for Phase 5 linking), mod page scoped
to a command matrix.

## Goal

One command registry, three surfaces:

1. Twitch chat: `!command`, unchanged.
2. Discord: native slash commands (`/stats`, `/god args:zeus`),
   auto-generated from the registry.
3. hatmaster.tv/mod: hidden, Twitch-OAuth-gated page with a
   per-command, per-platform toggle matrix that applies live.

## Design

### A. Registry metadata (core/bot.py)

`register_command` gains optional metadata; old call sites work
unchanged:

```python
bot.register_command("stats", self.cmd_stats,
    description="Ranked Conquest K/D/A, win rate, and KDA",
    platforms=("twitch", "discord"),  # default ("twitch",)
    identity=False,   # True = needs to know who asks (Phase 5)
    plugin="smite")   # grouping label for /mod
```

Commands also carry a per-user `cooldown` in seconds, default 0
(off) for everything. Enforced on both platforms from one registry
value; mods are exempt on Twitch; Twitch blocks silently, Discord
replies ephemerally with the wait time. gamble and suggest keep their
own built-in cooldowns independent of this layer.

Mod-page overrides persist to `data/command_platforms.json` and beat
code defaults. APIs: `get_command_catalog()`,
`set_command_platform(name, platform, enabled)`,
`set_command_cooldown(name, seconds)`, `check_cooldown(name,
user_key)`, `is_command_enabled(name, platform)`. Setters persist and
fire `add_catalog_listener` callbacks. Both Twitch dispatch paths
(chat, whisper) check the twitch toggle, so mods can kill a command
live without a restart.

### B. Slash bridge (plugins/discord_bridge.py)

- `CommandTree` on the existing client. On ready, and debounced 5s
  after any catalog change, rebuild and `tree.sync(guild=...)`.
  Guild-scoped sync is instant; a signature hash skips no-op syncs
  (Discord limits registration updates).
- Each command: sanitized name, description (<=100 chars), one
  optional string param `args`. Maps 1:1 to handler signatures.
- `_DiscordInteractionAdapter` mimics ChatMessage (.chatter,
  .respond), same trick as the whisper adapter. Handlers and
  send_reply run unmodified.
- Discord voids interactions after 3s, so the bridge always defers
  and replies via followup.
- Cooldowns come from the registry (see A). Handler exceptions get a
  plain error reply.
- mod_only and identity commands are never exposed on Discord in v1.

### C. Mod page (core/public_webserver.py + public/mod.html)

Auth, fail closed: Twitch OAuth session (existing flow), then the
login must be in the mod set = MODERATORS config + broadcaster +
Helix Get Moderators (5-min cache; needs moderation:read on the
broadcaster token, degrades to config-only with a console warning).
Logged out and non-mod both get a plain 404; the page is never
revealed. No links to /mod anywhere public.

Routes: `GET /mod` (page), `GET /api/mod/commands` (catalog),
`POST /api/mod/commands` body `{name, platform, enabled}` for
availability or `{name, cooldown}` (0-3600 seconds) for cooldowns. POST
guards in order: session+mod, Origin header (CSRF, same pattern as
/api/trade), per-IP rate limit, body validation. Every toggle goes to
console + `data/mod_audit.log`.

UI: single file, site theme. Table grouped by plugin with
description, mod/identity badges, a cooldown seconds input, and
Twitch and Discord toggles (disabled with tooltip where ineligible).
Text filter, optimistic updates that revert on error.

## Launch command set

Discord-enabled (23, read-only): god, stats, rank, match, winrate,
kda, damage, team, lastmatch, record, price, market, stocks,
dividend, jackpot, godqueue, godlist, song, songlist, topsongs,
toprequester, socials, uptime.

Phase 5 (identity): portfolio, buy, sell, gamble, godtokens,
godrequest, sr, wrongsong, mysongs, like, voteskip, songstatus,
suggest.

## Status

All built and covered by automated checks: registry
defaults/overrides/persistence, slash invocation through the real
send_reply path, cooldown, toggled-off rejection, exception
containment, mod auth matrix (logged out / non-mod / mod /
broadcaster), CSRF, invalid bodies.

Manual checklist (next stream):
1. Restart bot: console shows "Synced N slash commands to guild".
2. Discord: /stats and /god args:zeus match Twitch replies.
3. /mod logged out: 404. Logged in as mod: matrix loads.
4. Toggle /stats off for Discord: gone within ~10s.
5. Toggle !gamble off for Twitch: bot ignores it; flip back.
6. Set a 30s cooldown on !stats from /mod: second use within 30s is
   ignored on Twitch and gets an ephemeral wait message on Discord.
7. data/mod_audit.log records each change.

## Custom commands (added 2026-06-12)

Mods create text commands from /mod, no code, no restart.
`plugins/custom_commands.py` owns `data/custom_commands.json` and
registers each command with plugin="custom". Registered LAST in
main.py so built-in names always win collisions (skipped with a
console warning). Placeholders: {user} and {args}. Names a-z 0-9 _ -
(also valid slash names), responses max 450 chars, cap 100 commands.
Customs get the full matrix treatment: platform toggles, cooldowns,
Discord slash sync, audit log.

API: `POST /api/mod/custom-commands` body {name, response} upserts;
`DELETE /api/mod/custom-commands/{name}` removes. Same guards as the
other mod endpoints. GET /api/mod/commands now also returns a status
block (uptime, discord connection, commands run) shown on the page.

UI: add box at the top (name + response + button), custom rows show
an inline editable response field and a delete button.

## Out of scope

Account linking (Phase 5), mod-only commands on Discord, broader mod
dashboard modules. The /mod auth layer is the foundation for these.
