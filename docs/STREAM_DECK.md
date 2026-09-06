# Stream Deck Setup

One-time wiring guide for the HatmasBot page on the **Stream Deck XL**
(32 keys, 8x4). Every deck-facing launcher lives in one folder:

- **`streamdeck\`** — all the `.bat` files buttons point at
- **`assets\streamdeck_icons\`** — the button icons (regenerate with
  `py tools\make_streamdeck_icons.py`)

The only launcher NOT in `streamdeck\` is `run_bot.bat` at the repo
root — the Task Scheduler logon task and `tools\bot_ctl.ps1` reference
it there, and it is not meant to be a button (BOT RESTART covers it).

The Stream Deck + keeps its existing "Streaming" profile (OBS +
MixItUp launcher, Wave Link dial). The XL gets the bot page.

---

## The layout (8x4 XL grid)

Rows are stream phases, colored by the icon's top bar:

- **Row 1 — Pre-stream** (Cool Steel bar)
- **Row 2 — Going live** (Scarlet bar)
- **Row 3 — End of stream** (Bronze bar)
- **Row 4 — Utility** (muted bar)

| Key | Button | Action type | Path / setting | Icon |
|-----|--------|-------------|----------------|------|
| 1,1 | CHECK READY | System: Open | `C:\Projects\HatmasBot\streamdeck\readiness_ui.bat` | `check_ready.png` |
| 1,2 | BOT RESTART | System: Open | `C:\Projects\HatmasBot\streamdeck\restart_bot.bat` | `bot_restart.png` |
| 1,3 | DASHBOARD | System: Website | `http://localhost:8069/` | `dashboard.png` |
| 1,4 | OBS + MIXITUP | Multi Action | copy from the Plus "Streaming" profile (Advanced Launcher x2) | `launch_stack.png` |
| 1,5 | START FACTORIO | System: Open | `C:\Projects\HatmasBot\streamdeck\start_factorio.bat` | `factorio.png` |
| 2,1 | GO LIVE | System: Open | `C:\Projects\HatmasBot\streamdeck\go_live.bat` | `go_live.png` |
| 3,1 | GO OFFLINE | System: Open | `C:\Projects\HatmasBot\streamdeck\go_offline.bat` | `go_offline.png` |
| 3,2 | PROCESS RECS | System: Open | `C:\Projects\HatmasBot\streamdeck\process_recordings.bat` | `process_recordings.png` |
| 3,3 | SORT UNKNOWNS | System: Open | `C:\Projects\HatmasBot\streamdeck\sort_unknowns.bat` | `sort_unknowns.png` |
| 3,4 | THUMB STUDIO | System: Open | `C:\Projects\HatmasBot\streamdeck\thumbnail_studio.bat` | `thumbnail_studio.png` |
| 4,2 | EARPIECE | System: Open | `C:\Projects\HatmasBot\streamdeck\earpiece_test.bat` | `earpiece.png` |
| 4,3 | VOD REVIEW | System: Open | `C:\Projects\HatmasBot\streamdeck\vod_review.bat` | `vod_review.png` |
| 4,4 | VOD SEARCH | System: Open | `C:\Projects\HatmasBot\streamdeck\vod_search.bat` | `vod_search.png` |
| 4,5 | BINGO START | System: Open | `C:\Projects\HatmasBot\streamdeck\bingo_start.bat` | `bingo_start.png` |
| 4,6 | BINGO END | System: Open | `C:\Projects\HatmasBot\streamdeck\bingo_end.bat` | `bingo_end.png` |
| 4,7 | BINGO: NO MANA | System: Open | `C:\Projects\HatmasBot\streamdeck\bingo_call.bat no_mana` (add the argument in the Open action) | `bingo_call.png` |
| 3,5 | RESOLVE IMPORT | System: Open | `C:\Projects\HatmasBot\streamdeck\resolve_import.bat` | `resolve_import.png` |
| 3,6 | RESOLVE TIKTOK | System: Open | `C:\Projects\HatmasBot\streamdeck\resolve_tiktok.bat` | `resolve_tiktok.png` |
| 4,1 | DISCORD TEST | System: Open | `C:\Projects\HatmasBot\streamdeck\discord_test.bat` | `discord_test.png` |
| 4,8 | BOT STOP | System: Open | `C:\Projects\HatmasBot\streamdeck\stop_bot.bat` | `bot_stop.png` |

BOT STOP sits alone in the far corner on purpose — it should never be
pressed by muscle memory.

CHECK READY opens the readiness panel (tools/readiness_ui.py) — a
standalone app window that runs every probe and puts a fix button next
to each non-green row (Start bot, Launch OBS / MixItUp / SMITE 2,
Restart cloudflared, Re-auth tokens, Open recordings). It runs its own
server so it works when the bot is down. `streamdeck\check_stream.bat`
is the same set of probes as a plain console report if you ever want it.

`streamdeck\build_thumbnail.bat` (the older prompt-driven thumbnail
builder) is still there if you prefer it over the studio; swap the
THUMB STUDIO path if so.

---

## Wiring steps (once, ~10 minutes)

1. Open the Stream Deck app and select the **XL** at the top.
2. Create a profile named **HatmasBot** (Preferences → Profiles → +),
   or reuse the empty WinToolsXL profile.
3. For each row in the table: drag **System → Open** onto the key,
   click **Choose...** and pick the `.bat` path from the table.
4. Right-click the key → **Set custom image** → pick the matching PNG
   from `C:\Projects\HatmasBot\assets\streamdeck_icons\`. Delete the
   Title text — labels are baked into the icons.
5. DASHBOARD key: drag **System → Website** instead, URL
   `http://localhost:8069/`.
6. OBS + MIXITUP key: right-click the existing multi action on the
   Plus "Streaming" profile → Copy, then Paste onto the XL key (or
   rebuild it: Multi Action containing two Advanced Launcher steps,
   `obs64.exe` and `MixItUp.exe` — the plugin is already installed).

---

## The flow the buttons give you

**Before stream:** OBS + MIXITUP → BOT RESTART (starts the bot if it
is down) → CHECK READY ~30s before going live. Green report = clear.

**Going live:** GO LIVE (LIVE badges on the last 8 YouTube thumbnails).

**During stream:** DASHBOARD opens the control panel (song queue,
overlays, kill-detector debug, economy sim).

**After stream:** GO OFFLINE → PROCESS RECS (sorts everything in
`recordings\` into per-god folders, ~minutes per big VOD) →
SORT UNKNOWNS if anything landed in `recordings\unknown\` →
THUMB STUDIO for the YouTube thumbnail → RESOLVE IMPORT /
RESOLVE TIKTOK to cut the VOD in DaVinci.

Optional refinement once the basics feel good: an END STREAM Multi
Action that fires `go_offline.bat` then `process_recordings.bat` in
one press.

BINGO START / BINGO END open and close a Stream Bingo round. Manual squares are
called with `bingo_call.bat <square_id>`: make one key per square you call
often (no_mana, blames_jungle, water, ...) by putting the id in the Open
action's arguments; the full list with buttons lives on the dashboard at
`http://localhost:8069/bingo`, handy on the second monitor.

VOD REVIEW / VOD SEARCH open the Ask the VOD pages in the default browser:
from the bot's public server (localhost:8070) when the bot is up, otherwise
from the standalone dev host, which the button starts on localhost:8078 the
first time and leaves running. Both pages are local-only.

EARPIECE speaks a canned line through the co-caster's private headphone
channel (works with the toggle off) so the routing can be checked before
going live; `earpiece_test.bat now` runs a real chat summary instead.

PROCESS RECS also runs the "Ask the VOD" indexer (`tools\vod_index.py`)
after the sorter, so tonight's recordings are searchable on
hatmaster.tv/vod a few minutes later. Its output lands in the same
`data\process_recordings.log`; a failure there never masks the sorter's
exit code.

---

## Maintenance

- **Adding a button:** put the new `.bat` in `streamdeck\` (start it
  with `pushd "%~dp0.."` so it runs from the repo root), add an entry
  to the `ICONS` dict in `tools\make_streamdeck_icons.py`, re-run it,
  and add a row to the table above.
- **Working directory contract:** every `.bat` in `streamdeck\`
  assumes the repo root is one level up (`%~dp0..`). If the folder
  ever moves deeper, fix those pushd lines.
- `restart_bot.bat` / `stop_bot.bat` wrap `tools\bot_ctl.ps1`:
  restart kills the bot child and lets the supervisor relaunch it
  (~5s), or opens a new `run_bot.bat` console if nothing is running;
  stop kills the supervisor first so it cannot relaunch. Hard kill is
  safe (atomic state writes + SQLite WAL); typing `quit` in the bot
  console stays the graceful path.
