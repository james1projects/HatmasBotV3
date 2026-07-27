# Streaming Space Game — Design & Implementation Plan (v2)

> Status: **Proposal / planning** — June 2026. A `hatmaster.tv/StreamingSpaceGame`
> extension to HatmasBot. A Galaga-style shooter where a streamer plays live and
> their Twitch viewers spend a shared game currency to spawn enemy ships at them —
> including named boss ships. Built in **Phaser 3**, architected as multi-channel
> **rooms** (launching with Hatmaster's room first), with a single global game
> currency and a viewer "warp-in" deploy mechanic as the headline feature.
>
> Companion reading: `WEBSITE_TRADING_DESIGN.md` (the login + "one action path" +
> rate-limit pattern this reuses), `HatmasBot.md` (architecture), `README.md`
> (overlay + plugin conventions).

### What changed since v1 (decisions locked)

| Decision | v1 | **v2 (locked)** |
|---|---|---|
| Engine | Vanilla Canvas (rec) | **Phaser 3** |
| Tenancy | Single channel (Hatmaster) | **Multi-channel rooms — Hatmaster's room first, built room-ready** |
| Currency | Reuse Hats | **Scrapped. One global game currency ("Warp Fuel") across all channels** |
| Spawn UX | Buttons + chat | **Simple buttons + chat first (MVP); ghost-placement "warp deploy" is a later marquee phase** |
| Access | (open question) | **Host-configurable per room — everyone / followers / subs, default open** |

---

## 1. The pitch

A streamer plays a vertical space shooter live. Viewers, logged in with Twitch on
`hatmaster.tv/StreamingSpaceGame`, spend **Warp Fuel** (a game currency they earn
by watching) to spawn enemy ships against the player — from the site or from chat.
Cheap ships drift down; pricier ones shoot, chase, or kamikaze. The marquee
purchase is a **boss ship stamped with the buyer's Twitch username and a health
bar**. The player loses if too many ships slip past the bottom. The audience
becomes the level designer.

Each Twitch channel is its own **room**. We launch with Hatmaster's room, but the
whole thing is built so any streamer can later log in, claim their own room (their
authenticated Twitch channel proves it's them), and play with their own chat.

---

## 2. Architecture

### 2.1 Rooms, and who simulates them (authority)

Each room is **host-authoritative**: the streamer's browser runs the Phaser
simulation for their room and is the single source of truth. The server is a
**matchmaker + relay**, not a physics engine. This is the only model that scales
on modest infra (each host does their own compute) and it sidesteps server-side
netcode and cheating entirely.

- A **host** opens the play page and authenticates with Twitch. Their
  authenticated channel *is* their room — they cannot host a channel they don't
  own. That's the "streamer confirms it's them."
- A room is **listed in the lobby only when** the channel is live on Twitch *and*
  the host page is actively connected (a heartbeat ping). We already have
  stream-status plumbing to check live status.
- **Viewers** browse the lobby, enter a room, watch the real game via that
  channel's Twitch embed, and deploy ships.

We build the room abstraction now (a `RoomManager` keyed by channel, room-scoped
WebSocket topics) but **launch with exactly one room — Hatmaster's**. Adding more
channels later is configuration and a lobby, not a rewrite.

```
   Viewer in room "hatmaster"          Streamer (host of "hatmaster")
   ───────────────────────────         ─────────────────────────────
   hatmaster.tv/StreamingSpaceGame      hatmaster.tv/StreamingSpaceGame
        /hatmaster                            /hatmaster/host (Twitch-gated)
        │   watches Twitch embed              │   PHASER sim (authoritative)
        │   spends Warp Fuel                  │   captured by OBS → stream
        ▼                                     ▲
   ┌─────────────────────────────────────────┴───────────────────────┐
   │  HatmasBot process — Public server :8070                         │
   │   • Twitch OAuth + signed sessions (reused as-is)                │
   │   • RoomManager (registry, heartbeat, live-filter)              │
   │   • spacegame plugin: deploy_ship() — ONE action path           │
   │   • Warp Fuel ledger (global currency)                          │
   │   • room-scoped WS: /ws/spacegame/{channel}                     │
   └─────────────────────────────────────────────────────────────────┘
        ▲ chat !red/!green/!boss (Twitch IRC, via the bot)
```

End-to-end, a deploy:

1. Viewer clicks **Green** (or types `!green`) in the `hatmaster` room.
2. Request hits `deploy_ship()` — access policy checked, **Warp Fuel** debited,
   per-user cooldown + per-IP rate-limit enforced. (Same discipline as a trade
   today: web action and chat action call **one** path.)
3. The plugin routes a `spacegame_deploy` event to **that room's** host over
   `/ws/spacegame/hatmaster`.
4. The host's Phaser game warps the green ship in at the top, carrying the
   viewer's username.
5. OBS captures the host's game; viewers see it on the Twitch embed a few seconds
   later (latency is handled by the warp fiction — §6).

### 2.2 Pages & routes (public server, :8070)

| Route | Who | Purpose |
|---|---|---|
| `GET /StreamingSpaceGame` | anyone | **Lobby** — live rooms (just Hatmaster's at launch) |
| `GET /StreamingSpaceGame/{channel}` | anyone | **Viewer page** — Twitch embed + Fuel + spawn UI + feed |
| `GET /StreamingSpaceGame/{channel}/host` | the broadcaster | **Host canvas** — authoritative Phaser game; OBS source |
| `POST /api/spacegame/deploy` | logged-in viewer | The deploy endpoint (near-clone of `/api/trade`) |
| `GET /api/me/fuel` | logged-in viewer | Warp Fuel balance for the page |
| `GET /ws/spacegame/{channel}` | host + viewers | Room-scoped event channel (deploys, run state, feed) |
| `POST /api/spacegame/{channel}/host/*` | the broadcaster | Host control: start/pause/reset/heartbeat/access policy |

The host gate reuses the same idea as the existing `/mod` page: the session's
Twitch `uid`/`login` must equal the channel being hosted. Auth, session cookies,
cooldowns, per-IP caps, and a fail-closed master switch (`SPACEGAME_ENABLED`) are
all the existing web-trading machinery, re-pointed.

### 2.3 Why viewers don't render the game

Viewers watch the real game through the channel's **Twitch embed** — we do not
replicate the Phaser sim into every viewer's browser (that would mean state sync,
authority, and anti-cheat for no benefit). Their page is: the embed + their Fuel +
the spawn UI + a live "who deployed what" feed. The warp-deploy staging area
(§6) is the one place a viewer's browser draws ships, and that's purely a
client-side composition surface — no authority needed.

---

## 3. Engine: Phaser 3

Phaser 3 is a free 2D browser game framework: sprite loading + animation, an
"arcade physics" engine (velocity, overlap/collision), input, sound, tweens,
particles, and scene management out of the box. The game host page is a Phaser
app; the rest of the system (rooms, currency, deploys) is engine-agnostic
plumbing.

Notes for this build:
- One **Boot/Preload scene** (load the sprite atlas + audio) → **Game scene**
  (the run) → lightweight **GameOver** overlay state.
- Enemies are Phaser sprites in physics groups; collisions are arcade-physics
  overlaps (player-vs-bullet, bullet-vs-enemy, player-vs-enemy). The four AIs are
  small per-type `update()` behaviors.
- Tweens/particles give the warp-in flash, explosions, and screen shake cheaply.
- The host page loads Phaser from the same static-asset setup the site already
  uses; keep it a single bundled file in the overlay/public convention.

---

## 4. Currency: **Warp Fuel** (global)

A single game currency, the same everywhere, replacing the Hats idea entirely.

- **Earned by watching.** Logged-in viewers accrue Fuel while present in a *live*
  room (e.g. +N/minute), tracked server-side via the room's WS presence/heartbeat
  so it can't be farmed offline. Optional extras: a daily login grant, a small
  bonus for the room you're actively in.
- **Spent to deploy.** Each ship type has a Fuel price; a deploy is rejected
  before it touches the game if the viewer can't afford it (just like an
  unaffordable trade). Bosses cost a lot.
- **Global ledger.** A new currency store keyed by **Twitch user id**, shared
  across all rooms — your Fuel is your Fuel everywhere. New sqlite DB in the
  `data/` convention (like the economy DB), owned by the spacegame plugin.
- **Shown on the page.** New `GET /api/me/fuel`; the viewer page renders the
  balance and per-button prices, with live updates over the room WS.

Anti-farming guardrails: accrue only while authenticated and the room is genuinely
live; one accrual stream per user even across multiple open tabs/rooms; per-period
accrual cap. (Same fail-closed philosophy as the trading kill-switch.)

> Boss ships are expensive enough you may still want a premium top-up rail later
> (Twitch Bits or Streamloots → Fuel). Flagged in §11, not required for launch.

---

## 5. The ships

Naming is a suggestion; stats are first-pass starting points to tune live.

### 5.1 Enemy archetypes

**Red — "Drifter"** *(cheapest).* Drifts straight down at a slow constant speed,
no weapons. Pure pressure — dangerous only because **reaching the bottom counts
against the player** (§7). HP 1. The chaff you spam to overwhelm.

**Green — "Gunner".** Descends to a holding band and strafes, firing **lead-
predicted** shots (aims slightly ahead of the player's movement), so standing
still is punished but changing direction beats it. HP ~2.

**Yellow — "Seeker".** Fires **slow homing missiles that accelerate over time but
turn lazily** — easy to out-angle if you keep moving, deadly if you don't.
Missiles can optionally be shot down. Ship is fragile (HP ~2); the threat is the
missiles it leaves on the field.

**Orange — "Kamikaze".** **Accelerates toward the player and collides for damage**,
then **detonates after a fuse** (small blast even on a miss). Fast and scary;
countered by shooting it early or baiting its line. HP ~2.

| Ship | Move | Attack | HP | Threat | Fuel cost |
|---|---|---|---|---|---|
| Red Drifter | Straight down, slow | None | 1 | Leaks past bottom | ★ |
| Green Gunner | Descend + strafe | Lead-predicted shots | 2 | Ranged chip | ★★ |
| Yellow Seeker | Hovers/repositions | Accelerating homing missiles | 2 | Dodge pressure | ★★★ |
| Orange Kamikaze | Accelerates at player | Collision + timed blast | 2 | Burst / collision | ★★★★ |

### 5.2 Boss ships

An upgraded archetype carrying the **buyer's Twitch username + a health bar**.
Big HP, visually distinct, ideally one at a time (queue extras).

- **Red Boss** — a hulking drifter / formation wall that must be cleared before it
  leaks.
- **Green Boss** — multi-barrel spreads / aimed bursts.
- **Yellow Boss** — missile volleys instead of singles.
- **Orange Boss** — large blast radius and/or spawns kamikaze minions as it dies.

Design touches (§11): does killing a boss reward the *summoner* (Fuel payout /
leaderboard credit / "you took X% of the run") or the player? Optional sub-50%-HP
phase changes. One concurrent boss vs. a queue.

### 5.3 The player

Sits near the bottom, moves horizontally (optionally a vertical band), fires
upward. Recommended for a streamer splitting attention: **auto-fire + keyboard
left/right (+ a dodge/bomb)**. Lives or an HP bar; collisions and hits chip it.

---

## 6. The headline feature: ghost placement + warp deploy *(later phase)*

Per your call we ship **simple buttons + chat first**, then add this. It's the
mechanic that makes the game feel special, so it's specced now.

How it works from the viewer's side:

1. Above the play area (the Twitch embed) is a **staging strip** — "space" the
   viewer can click to drop **ghost ships** (semi-transparent previews). They
   arrange a **formation**, pick types, and a Fuel cost tallies up.
2. They hit **Build / Deploy**. The ghosts fly down, **warp out** of the staging
   strip (a jump-flash, they vanish)…
3. …and **warp in** at the top of the host's actual game at the matching
   positions, then begin their normal behavior. A brief warp-in telegraph on the
   host side keeps it fair and readable.

Why it's worth it:

- **It hides latency by design.** The staging strip is pure client-side
  composition — instant and satisfying, no authority. The warp-out/warp-in is
  *diegetic cover* for network + Twitch-embed delay: the viewer never expects to
  watch continuous motion across the gap, so the lag becomes fiction, not a flaw.
- **Spatial agency.** Viewers choose lanes and formations, not just "a green ship."
- **Clean economy unit.** One Build = one priced, rate-limited transaction.

Design care:
- The staging strip maps to **normalized coordinates** (x as a fraction of field
  width) so a placement lines up with the host's field at any resolution.
- Because the host dodges on a delayed feed, the mechanic rewards **lanes/
  formations, not sniping the player's exact position** — the fixed warp-in band
  enforces this naturally.
- Caps on ghosts per build and a cooldown between builds; the host's spawn cap and
  queue absorb overflow from many simultaneous viewers.

---

## 7. Lose condition & scoring

- **Primary ("the leak"):** every enemy that crosses the bottom edge increments a
  counter; at a threshold (e.g. 10) → **Game Over**. This is the original "too
  many ships pass" rule and makes cheap Red Drifters genuinely threatening in bulk.
- **Secondary (optional):** a player HP/lives bar drained by hits and orange
  collisions; zero also ends the run. Run leak-only, HP-only, or both.
- **Score / stats:** time survived, ships destroyed, bosses downed. On Game Over,
  an overlay crowns the run and (optionally) the viewer who dealt the final leak or
  downed the last boss — a tie-in to existing leaderboard/overlay conventions.

All thresholds, costs, and rates live in config for live balancing (mirrors how
the economy/gamble constants work today).

---

## 8. Integration with HatmasBot

### 8.1 New plugin: `plugins/spacegame/`

Mirrors the `economy`/`factorio` layout:

```
plugins/spacegame/
  plugin.py      # setup(bot); registers commands; owns RoomManager + deploy path
  commands.py    # !red !green !yellow !orange !boss (+ aliases), !spacegame
  rooms.py       # RoomManager: registry, heartbeat, live-filter, room-scoped routing
  fuel.py        # Warp Fuel ledger: accrual, balance, debit (global, by Twitch uid)
  state.py       # per-room run state mirror, leak count, active boss, cooldowns
  db.py          # sqlite for Fuel ledger + persistent stats
  overlays.py    # emit room-scoped spacegame_* events
```

The heart is one function — the game's `execute_buy`:

```text
deploy_ship(room, user, ship_type, source, placement=None)   # source ∈ {web,chat}
  1. room exists + live + game running + not over spawn cap
  2. access policy allows user (everyone / followers / subs)
  3. check + debit Warp Fuel                              ← ONE action path
  4. per-user cooldown + per-IP rate cap
  5. route "spacegame_deploy" to room's host via /ws/spacegame/{channel}
  6. return ok / too_poor / cooldown / capped / not_allowed / disabled
```

Both the chat command and `POST /api/spacegame/deploy` call only this — the same
"one path" discipline that keeps web and chat trading consistent today.

### 8.2 Reused vs. new

**Reused as-is:** Twitch OAuth + signed sessions (`core/web_session.py`), the
`/api/trade`-style authed-mutation pattern, per-user cooldowns + per-IP caps +
master kill-switch, the WebSocket-from-bot-to-page mechanism, stream-status checks
for live filtering, the plugin + overlay conventions, static-asset serving.

**New:** the `RoomManager` (multi-tenant routing + heartbeat), the global Warp
Fuel ledger + accrual loop, room-scoped WS topics, the Phaser host canvas, the
viewer page (embed + buttons + feed, later the staging strip), and the deploy
endpoint.

### 8.3 Assets / your GameMaker art

Still in your old GameMaker project, not in this repo. We need it exported to web
files: **sprites as a PNG atlas (+ a frame manifest Phaser can load)** and **SFX**
(OGG/MP3). They'd live under `public/spacegame/assets/`. Point me at the
GameMaker project folder and I can help script the atlas export + manifest.

### 8.4 Latency note

The Twitch embed runs seconds behind real time, so a deployed ship shows on the
video a few seconds after the click. The button/build must confirm **immediately**
("Deployed!") the instant the bot accepts it — independent of when it appears on
the video. The warp fiction makes the gap feel intentional.

---

## 9. Anti-abuse, fairness & access

- **Host-configurable access per room:** everyone / followers-only / subs-only,
  default **everyone**. Stored per room, changeable live from the host page.
- **Per-user cooldown** between deploys; **per-IP rate cap** on the deploy
  endpoint (clones of `WEB_TRADE_COOLDOWN` / `WEB_TRADE_MAX_PER_MIN`).
- **Global spawn cap / max concurrent enemies** per room — protects the host's
  framerate and readability; over the cap, reject or queue with a clear message.
  Debit only on accept (or refund on reject).
- **Fuel guardrails:** accrue only while authenticated + room live; one accrual
  stream per user; per-period cap.
- **Boss limits:** one active (or a short queue); high Fuel cost / premium rail.
- **Host + mod controls:** pause, clear field, disable a ship type, panic off —
  honored instantly over the room WS.
- **Master kill-switch:** `SPACEGAME_ENABLED=False` disables deploys end-to-end,
  fail-closed (same philosophy as web trading).

---

## 10. Phased build plan

Each phase is independently demoable.

**Phase 0 — Setup.** Phaser project skeleton + Boot/Preload scene; export
GameMaker art → atlas + manifest (§8.3); Warp Fuel DB schema.

**Phase 1 — Playable core (no Twitch).** Phaser game: player + auto-fire, all four
enemy AIs, bullets/missiles, collisions, leak counter + Game Over. Dev buttons /
keyboard to tune feel. Runs at the host canvas route. *Deliverable: a fun
single-screen shooter you can play locally.*

**Phase 2 — Rooms + simple spawns (your room).** `RoomManager` (single room:
Hatmaster), the `spacegame` plugin, chat commands (`!red…!boss`) + **simple web
spawn buttons**, room-scoped WS → ships warp in bearing the spawner's username.
Cooldowns, spawn cap, host gate. *Deliverable: chat + a basic site can attack
the player live.* **(This is the MVP per your "buttons + chat first" call.)**

**Phase 3 — Warp Fuel + access.** Global currency: watch-to-earn accrual, balance
display (`/api/me/fuel`), deploys debit Fuel, anti-farm guardrails, kill-switch;
host-configurable access policy. *Deliverable: the real economy + gating.*

**Phase 4 — Bosses.** Username + health-bar bosses, the four upgraded patterns,
boss cost, one-at-a-time queue, defeat rewards. *Deliverable: named boss fights.*

**Phase 5 — Warp deploy UI (headline).** The staging strip, ghost placement +
formation building, normalized coordinates, warp-out/warp-in + telegraph (§6).
*Deliverable: the signature mechanic.*

**Phase 6 — Multi-channel rollout.** Lobby of live rooms, any streamer claims
their room via Twitch login, heartbeat + live filtering, scale pass — and, if it
grows, extract the room/relay into a small standalone service. *Deliverable: open
to other streamers.*

**MVP = Phases 1–3** (real game, real currency, your room). Bosses (4) and the
warp UI (5) are the headline follow-ups; multi-channel (6) opens the doors.

---

## 11. Open decisions (smaller now)

1. **Currency name + accrual rate** — is "Warp Fuel" the name? Fuel-per-minute,
   any daily grant, any premium top-up (Bits/Streamloots) for bosses? (§4)
2. **Boss reward** — does downing a boss pay the summoner (Fuel/leaderboard) or
   the player? One boss at a time or a queue? (§5.2)
3. **Player controls** — auto-fire + keyboard (rec) vs. manual/mouse-aim; lives vs.
   HP bar; a dodge/bomb? (§5.3)
4. **Lose condition** — leak-only (your original), HP-only, or both, and the
   thresholds. (§7)
5. **The art** — where's the GameMaker project so we can export the atlas + SFX? (§8.3)

---

## 12. Bottom line

Confirmed and de-risked. The new currency is a self-contained global ledger (no
dependence on your channel-specific Hats), rooms are host-authoritative so they
scale without server-side netcode, and the scary web plumbing — auth, an authed
action path shared by web + chat, cooldowns, rate-limits, kill-switches, live
push — is the proven web-trading machinery re-pointed. The genuinely new work is a
Phaser renderer, four small enemy AIs, a room relay, the Fuel loop, and (later)
the warp-deploy UI. Decide §11.1–§11.4, get me the GameMaker art, and Phase 1 can
start.
