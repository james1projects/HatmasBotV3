# hatmas-events (Factorio mod)

Twitch-driven gameplay events for HatmasBot. v0.1.0 ships two features:
viewer pets and boss biters. Everything is testable solo via console
commands; the `hatmas` remote interface is the surface the future
HatmasBot RCON plugin will call.

Targets Factorio 2.x (base; Space Age compatible). Pets handle planet
hops by respawning on the new surface (units cannot teleport
cross-surface).

## Install

Copy (or junction) this folder into the Factorio mods directory:

```
%APPDATA%\Factorio\mods\hatmas-events
```

Junction (keeps the repo as the single source, edits apply on next
save load):

```
mklink /J "%APPDATA%\Factorio\mods\hatmas-events" "C:\Users\james\HatmasBot\factorio_mod\hatmas-events"
```

Enable "Hatmas Events" in the Mods menu. Works on existing saves.

**Achievement warning:** the first console command (and later, RCON)
permanently disables achievements for that save. Use a dedicated
streaming save.

## Console test commands

| Command | What it does |
|---------|--------------|
| `/hatmas-pet <owner> [pet name ...] [small\|medium\|big\|behemoth]` | Spawn a pet for a viewer (replaces their old one) |
| `/hatmas-pet-grow <owner>` | Grow the pet one size |
| `/hatmas-pet-say <owner> <message>` | Speech text above the pet for 5s |
| `/hatmas-pet-remove <owner>` | Despawn the pet (no death announcement) |
| `/hatmas-boss <viewer> [north\|south\|east\|west] [distance]` | Spawn a named boss that attacks your position |

Examples:

```
/hatmas-pet hatmas_fan Gerald
/hatmas-pet-grow hatmas_fan
/hatmas-pet-say hatmas_fan hello chat
/hatmas-boss big_spender north 120
```

## Features

### Viewer pets (scripts/pets.lua)

- One pet per owner; name tag `PetName [owner]` floats above it.
- Follows the streamer (first connected player with a character):
  walks when >6 tiles away, catch-up teleport when >60, respawns on
  the new surface after a planet hop. Never fights
  (`distraction.none`).
- Friendly-fire immune: damage from the pet's own force is healed
  back via `on_entity_damaged`. Pets also have large HP pools
  (750/1500/3000/6000) so bursts don't one-shot them.
- Dies to enemies. Death is announced in game chat and written to the
  event outbox with owner, lifetime, and killer.
- Sizes: small / medium / big / behemoth (`hatmas-pet-*` prototypes).

### Boss biter (scripts/boss.lua)

- `hatmas-boss-biter`: behemoth clone, 40k HP, 1.6x sprite scale, red
  tint, 2x attack damage, 0.8x speed, drops 5-10 raw fish.
- Spawns `distance` tiles (default 150) N/S/E/W of the streamer
  (random direction if omitted), generates chunks if needed, then
  attack-moves to the streamer's position.
- Floating name tag ("viewer's Boss") + scripted HP bar that shrinks
  with damage and turns red below 25%.
- Enrages at 25% HP: 1.5x speed, "ENRAGED" flash, outbox event.
- Death announced with time-alive and final blow; outbox event.

## Remote interface (for the bot, via RCON)

```
/sc remote.call("hatmas", "ping")
/sc remote.call("hatmas", "spawn_pet", "owner", "PetName", "small")
/sc remote.call("hatmas", "upgrade_pet", "owner")
/sc remote.call("hatmas", "remove_pet", "owner")
/sc remote.call("hatmas", "pet_say", "owner", "message")
/sc remote.call("hatmas", "list_pets")
/sc remote.call("hatmas", "spawn_boss", "viewer", "north", 150)
```

All calls return "ok" or a plain-text error string.

## Event outbox (game -> bot)

The mod appends one JSON object per line to:

```
%APPDATA%\Factorio\script-output\hatmas\events.jsonl
```

Events: `pet_spawned`, `pet_upgraded`, `pet_removed`, `pet_died`,
`boss_spawned`, `boss_enraged`, `boss_died`. Every payload includes
`event` and `tick`. The future HatmasBot factorio plugin tails this
file and relays to Twitch chat.

## Things to verify in-game (first playtest)

1. Boss sprite scale/tint looks right (tweak `BOSS_SCALE` /
   `BOSS_TINT` in data.lua; mask layers may need tint tuning).
2. HP bar offsets sit above the boss (BAR_* constants in
   scripts/boss.lua).
3. Friendly-fire heal-back vs a true one-shot (point-blank nuke): the
   API docs don't specify whether restoring health inside
   `on_entity_damaged` prevents a killing blow. If the pet dies, that
   stays as a (funny) accepted edge or we add a guard.
4. Pet follow feel: speed 0.32, re-command every 120 ticks - tune
   PET_* constants in scripts/pets.lua.
5. Multiple pets pathing into each other when 3+ are active.

## Next steps (bot side, not in this mod)

- `plugins/factorio/` package in HatmasBot: asyncio RCON client,
  event catalog, outbox tailer -> Twitch chat announcements.
- Launch Factorio with `--rcon-port 27015 --rcon-password <pw>` and
  host the save as multiplayer so RCON is live.
- Then: channel point redemptions (pet spawn/grow/say), sub/raid/bits
  -> boss spawns and wave events.
