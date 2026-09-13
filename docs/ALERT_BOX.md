# Hatmaster Alert Box, Sources page, and Bingo on stream

Spec agreed 2026-09-12. Status: **not built yet**. This page is the reference
while it is; each phase below gets a status line as it lands.

## Goals

1. **One place that lists every OBS browser source** the bot serves, with its
   URL, size, whether OBS is connected to it, and a Test button.
2. **One "Hatmaster Alert Box" browser source** that covers the whole 1920x1080
   scene and plays the bot's transient overlays (gamble, TTS, voicelines,
   dividend, match end, leaderboard, trade feed, portfolio, spin, co-caster
   line, bingo events) as *alert kinds*. James chooses per kind whether it
   shows, where on the scene, how big, for how long, and in which queue.
3. **Bingo on stream**: viewers opt in to have their card shown; one browser
   source rotates through opted-in cards; a viewer must press **Bingo!** on
   the site to win.

Streamlabs stays for subs, raids, follows, bits. This is not a replacement for
it; it is the bot's own alerts, folded into one source.

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Source** | One browser-source URL you add in OBS. |
| **Alert box** | The source at `/overlay/alerts?box=<name>`. `main` is the default. Several boxes can exist, each with its own enabled kinds. |
| **Alert kind** | One thing the box knows how to render: `gamble`, `tts`, `voiceline`, `dividend`, `match_end`, `leaderboard`, `tradefeed`, `portfolio`, `spin`, `cocaster`, `bingo_open`, `bingo_call`, `bingo_claim`, `bingo_closed`. Each kind is driven by one bot event and has its own template (the current overlay's HTML/CSS, moved). |
| **Lane** | A queue inside a box. Kinds in the same lane play one at a time, first in first out. Different lanes play at the same time. Defaults: lane `audio` (gamble, tts, voiceline), lane `economy` (dividend, match_end, leaderboard, tradefeed, portfolio, spin), lane `bingo` (bingo_*), lane `caption` (cocaster). Lanes are just names in the config; James can move a kind to any lane, or make a new one by typing a name. |
| **Placement** | Per kind, per box: `x, y, w, h` on the 1920x1080 canvas, plus an anchor (which corner the content grows from). |

## Config: `data/alerts.json`

Written by the dashboard, hand-editable, created with defaults on first run.

```json
{
  "boxes": {
    "main": {
      "kinds": {
        "gamble":      {"enabled": false, "lane": "audio",   "duration": 8,  "sound": true,
                        "x": 1420, "y": 40, "w": 460, "h": 200, "anchor": "top-right"},
        "tts":         {"enabled": false, "lane": "audio",   "duration": 30, "sound": true,
                        "x": 40, "y": 800, "w": 900, "h": 240, "anchor": "bottom-left"},
        "bingo_claim": {"enabled": true,  "lane": "bingo",   "duration": 12, "sound": true,
                        "x": 1380, "y": 300, "w": 500, "h": 300, "anchor": "top-right"}
      }
    }
  },
  "lanes": {"audio": {"max_queue": 20}, "economy": {"max_queue": 10}, "bingo": {"max_queue": 10}, "caption": {"max_queue": 5}}
}
```

Every kind starts **disabled** except the bingo ones, so folding changes nothing
on stream until James flips a kind on and removes the old source in OBS.
`duration` is a cap in seconds; kinds with their own natural end (TTS audio,
a voiceline clip) end early when the media ends. `max_queue` drops the oldest
queued alert in that lane when it is full, so a burst can never back up
forever.

## The Sources page: dashboard `/sources`

One row per source, grouped **Alert boxes / Persistent overlays / Legacy
(still served, TODO remove)**. Each row: name, what it shows, URL (copy
button), recommended size, connected OBS clients (from
`OverlayManager.client_count`), Test button (fires that source's sample
event through the real emit path), and a link to its settings page if it has
one (the alert box layout editor, the bingo control page).

| Group | Source | URL |
| --- | --- | --- |
| Alert box | Hatmaster Alert Box | `/overlay/alerts?box=main` (1920x1080) |
| Persistent | Now playing, God overlay, Death counter, Economy ticker, Live match, YouTube player, Bingo status, **Bingo cards** | existing URLs; `/overlay/bingo_cards` new |
| Legacy | sound_alerts, tts, voicelines, economy_dividend, economy_match_end, economy_leaderboard, economy_tradefeed, economy_portfolio, god_pool_spin, cocaster | existing URLs, unchanged behaviour, `TODO(alertbox)` comment on each route; remove once James has switched |

The registry that drives the page is one Python list (`core/sources.py`),
which is also what the README's overlay list will be generated from, so the
two cannot drift.

## The alert box: `/overlay/alerts?box=main`

- Transparent full-canvas page, `overlay_client.js` connection named
  `alerts:<box>`; the server-side rules for the box are computed from
  `alerts.json` rather than `overlay_rules.json` (which keeps ruling the
  persistent overlays).
- **Server side** (`core/alert_box.py`, `AlertBox`): listens to every emit,
  maps event -> kind, builds an alert `{id, kind, lane, data, duration,
  placement, sound}` for each box that has the kind enabled, and pushes
  `alert_queued`. The client owns the queues (one per lane) so timing stays
  smooth even if the socket stalls; the server also keeps the last 50 alerts
  per box for reconnect replay and for a Recent list on the layout page.
- **Client side** (`overlays/alerts.html` + `overlays/alerts/<kind>.js`
  render modules): each kind's template is the current overlay's markup and
  CSS moved into a module with `render(el, data)` and optional `onEnd()`.
  Placement is applied as absolute position on the 1920x1080 canvas; OBS
  scales the source to the scene.
- Audio plays through the browser source as today (OBS "control audio via
  OBS" works unchanged).

## Layout editor: dashboard `/alerts/layout`

- The 1920x1080 canvas drawn to scale with a faint grid and a screenshot
  placeholder (optional: upload a still of the scene for reference).
- One draggable, resizable box per enabled kind, showing a sample render of
  that kind so the size is judged on real content. Snap to 10 px; arrow keys
  nudge; numeric x/y/w/h fields beside the canvas for exactness.
- Per kind: enabled, lane, duration, sound, anchor. Per box: add/rename box.
- Save writes `alerts.json`; the running alert boxes pick the change up on
  the next alert (a `alerts_config` push re-reads placement without a page
  reload). Test button per kind, and a Recent list of the last alerts with a
  Replay button.
- Plain vanilla JS like the rest of the dashboard; no framework.

## Bingo changes

### Show my card on stream
- Checkbox on `hatmaster.tv/bingo`, per viewer, **off by default**,
  remembered across rounds (`bingo_prefs` table keyed by login, or a column
  on the viewer's cards; prefs table so it survives the round).
- `POST /api/bingo/prefs {on_stream: bool}`; `/api/bingo/me` returns it.

### Bingo cards carousel: `/overlay/bingo_cards`
- One card at a time: the viewer's display name over the 5x5 grid with
  marked squares filled, no fill-count text. About 8 s per card, crossfade,
  closest-to-bingo first then round robin; a card with a complete line gets a
  highlight ring so chat can see who is sitting on one.
- Only cards whose owner opted in, only while a round is open; hides itself
  when nobody has opted in or the round is closed. Live: a call re-renders the
  card on screen if it changed. Persistent source, ruled by
  `overlay_rules.json` like the bingo status panel.

### Manual claim
- The store keeps `bingo_at` (the moment a card completed a line) but a line
  no longer wins. `fire()` never pays out or closes the round.
- `POST /api/bingo/claim {card_id}` (Twitch session, origin + rate checks,
  same as buying a card): the server recomputes the marks from the round's
  calls and accepts only if the card holds a complete row, column, or
  diagonal (free square counts). First accepted claim wins the pot, closes the
  round, and emits `bingo_claim` (alert + chat + status panel). A second claim
  gets "round already won". A claim on a card without a line gets "not a
  bingo yet"; the button is only rendered when `/api/bingo/me` says the card
  has a line, so that path is a race, not a normal press.
- Site: when a card completes a line the page flashes the card, plays a short
  chime, and shows a big **Bingo!** button on that card. No chat command.
- Control page: shows "has bingo, not claimed" per card; the Undo rule is
  unchanged (undo can take a line away, and with it the button).

### Bingo alerts
Kinds `bingo_open` (round opened, pot), `bingo_call` (the square label and
how many cards it marked; James enables it per box, it is off by default so a
busy match does not spam), `bingo_claim` (winner, prize), `bingo_closed`
(manual close). Every bingo event has a kind, so any of them can be switched
on as an on-screen alert.

## API summary (new)

| Route | Where | Purpose |
| --- | --- | --- |
| `GET /sources`, `GET /api/sources` | dashboard | sources page + registry JSON with live client counts |
| `POST /api/sources/test?name=` | dashboard | fire the sample event for a source |
| `GET /overlay/alerts?box=` | dashboard | the alert box source |
| `GET /alerts/layout`, `GET/POST /api/alerts/config`, `POST /api/alerts/test?kind=&box=`, `GET /api/alerts/recent?box=`, `POST /api/alerts/replay?id=` | dashboard | layout editor and its data |
| `GET /overlay/bingo_cards`, `GET /api/bingo/cards_on_stream` | dashboard | carousel source + its data |
| `POST /api/bingo/prefs`, `POST /api/bingo/claim` | public site | opt-in, claim |

## Phases

1. **Sources registry + page** (`core/sources.py`, `overlays/sources.html`,
   README list generated from it). Small, useful on its own.
2. **Alert box core**: `core/alert_box.py`, `alerts.json` defaults,
   `overlays/alerts.html`, lanes/queues, the `bingo_*` kinds first (new code,
   nothing to migrate), layout editor with drag/resize/save/test.
3. **Migrate kinds** one at a time from the legacy overlays (gamble, tts,
   voiceline, cocaster, then the economy set, then spin), each as a render
   module; legacy routes kept with `TODO(alertbox)`.
4. **Bingo on stream**: prefs + checkbox, carousel source, manual claim
   (store/plugin/API/site/control page), tests.
5. Docs + Stream Deck notes; James switches sources in OBS; remove legacy
   routes in a later commit.

Tests per phase in the house style (`tests/test_sources.py`,
`tests/test_alert_box.py`, additions to `tests/test_bingo*.py`), all through
`tools/run_tests.py`.

## Open items

- A "Later" idea James raised: per-lane special rules (priority, preemption).
  The lane model leaves room; not in this pass.
- Scene screenshot upload for the layout editor is a nice-to-have, not
  required for phase 2.
