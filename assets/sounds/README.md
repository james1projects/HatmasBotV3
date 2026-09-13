# assets/sounds

Sound samples shared by the whole project (the alert box today; anything
else that wants a real sound tomorrow). Served by the dashboard server at
`/assets/sounds/` (core/webserver.py, `SOUNDS_DIR` in core/config.py) and by
`tools/bingo_devserver.py`.

## kenney/

Four packs by Kenney Vleugels, https://kenney.nl, downloaded 2026-09-13.
License: **Creative Commons Zero (CC0)**, see each pack's `License.txt`.
No attribution is required; every file is kept so the packs stay
browsable for future features.

| Folder | Pack | Files | What is in it |
|---|---|---|---|
| `casino-audio/` | Casino Audio 1.1 | 54 | card fans/slides/shoves, chip lay/stack/collide/handle, dice grab/shake/throw |
| `impact-sounds/` | Impact Sounds | 130 | footsteps (5 surfaces), impacts: bell, generic, glass, metal, mining, plank, plate, punch, soft, tin, wood |
| `ui-audio/` | UI Audio | 51 | click1-5, mouseclick, mouserelease, rollover1-6, switch1-38 |
| `interface-sounds/` | Interface Sounds | 100 | back, bong, click, close, confirmation, drop, error, glass, glitch, maximize, minimize, open, pluck, question, scratch, scroll, select, switch, tick, toggle |

All files are `.ogg`.

## How the alert box uses them

`overlays/alerts/alerts_core.js` has one table, `SOUNDS`, mapping a
*role* (`spin_tick`, `gamble_jackpot`, `burn_boom`, ...) to one or more
sample names under `kenney/`; a kind calls `ctx.play(role, {gain, rate,
at})` and a random variant plays through that kind's volume slider. To
change a sound, edit the table. A sample that fails to load logs a
console warning and the kind falls back to its old synthesised sound, so
a typo is never a silent alert. `tests/test_alert_box.py` checks every
name in the table exists on disk.
