# HatmasBot — Cleanup & Hardening Plan

A focused audit of the v2.5 codebase before adding more features.
Scope: code quality & structure, bugs & risky behavior, docs vs reality drift.
Depth: thorough — every finding below has a file:line reference.

This document is a *report*, not a patchset. Nothing here has been changed.

---

## How to read this

Findings are grouped by priority. Within each group, items are roughly
ordered by impact, then by how cheap the fix is.

- **P0 — fix before next stream / push.** Security or correctness bugs
  with real-world consequences. Two items here.
- **P1 — fix this week.** Real bugs and structural problems that will
  cause confusion or breakage as features grow.
- **P2 — clean up before next big feature.** Drift, dead code, redundant
  files. Not urgent but the codebase will fight you until it's done.
- **P3 — nice to have.** Comments and small wins.

I've also added a closing section listing things that *look* like
problems on a quick scan but are actually fine, so you don't waste
cycles re-investigating them.

---

# P0 — fix before next stream / push

## P0-1. Real Twitch broadcaster tokens are checked into git

**File:** `core/config.py:36-37`

```python
TWITCH_BROADCASTER_TOKEN        = os.environ.get("TWITCH_BROADCASTER_TOKEN",        "5tdz6gvhatfa7f80txqz572imxe6m8")
TWITCH_BROADCASTER_REFRESH_TOKEN = os.environ.get("TWITCH_BROADCASTER_REFRESH_TOKEN", "iik6w9c3k0j22lmgffmy725bwbfhgzg579ti0y8isr4stbfc5k")
```

These are NOT placeholder strings — they are real OAuth credentials
sitting in committed source. Every other secret in the file uses
`"YOUR_..."` as the fallback (lines 21-24, 51-52, 109, 136). These two
slipped through.

`git log -S` confirms they were introduced in commit `d4406e6`
("Additional features made to song request, now will check if album art
is NSFW...") and have stayed in HEAD ever since.

The repo is **public on GitHub** (`github.com/james1projects/HatmasBotV3`),
so anyone scraping for leaked Twitch tokens already has these.

**What to do, in order:**
1. Revoke the broadcaster token now via Twitch dev console / CCM —
   anything stored on twitch.tv stops being usable.
2. Re-auth locally: `python -m core.auth --broadcaster`. The new token
   lands in `data/twitch_broadcaster_token.json` (gitignored, fine).
3. Edit `core/config.py:36-37` to use `"YOUR_BROADCASTER_TOKEN"` /
   `"YOUR_BROADCASTER_REFRESH_TOKEN"` placeholders, matching the bot
   token convention immediately above.
4. Decide whether to rewrite history. The leaked token is in commit
   `d4406e6` and many commits since. After step 1 the token is dead, so
   strictly you don't *have* to scrub history — but if you want the
   string out of public git, BFG Repo-Cleaner or `git filter-repo` is
   the standard tool. Force-push after.

While you're in `core/config.py`, audit the rest:
- `TWITCH_BOT_USERNAME = "YOUR_BOT_USERNAME"` and `TWITCH_CHANNEL = "YOUR_CHANNEL"` (lines 19-20) are placeholders
  in the committed file, but `core/config_local.py` overrides them. That's the right pattern.
- `TWITCH_BOT_ID = "YOUR_BOT_ID"` / `TWITCH_OWNER_ID = "YOUR_OWNER_ID"` (lines 25-26) — same, fine.
- Earlier git history (commit `26ec1113` from Apr 5) had real values
  `TWITCH_BOT_ID = "234224228"` and `TWITCH_OWNER_ID = "33955087"` baked
  into the initial commit. Those are public IDs (you can look them up
  for any Twitch user) so it's not a security issue, but worth knowing
  they're in history.

---

## P0-2. `YouTubeLiveBadgePlugin.setup()` is `async` and never awaited

**File:** `plugins/youtube_live_badge.py:58`

```python
async def setup(self, bot):       # <-- async, but...
    self.bot = bot
    try:
        if hasattr(bot, "features") and FEATURE_KEY not in bot.features:
            bot.features[FEATURE_KEY] = True
    except Exception:
        pass
```

`core/bot.py:170-174`:

```python
def register_plugin(self, name, plugin_instance):
    self.plugins[name] = plugin_instance
    if hasattr(plugin_instance, "setup"):
        plugin_instance.setup(self)        # <-- synchronous call
```

Every other plugin uses `def setup(self, bot)` (verified across all 17
plugins — youtube_live_badge is the only async one). Because `setup()`
is called synchronously and never awaited, this plugin's setup body
*never runs*. `self.bot` stays `None`. The feature toggle never gets
registered. Python will print a `RuntimeWarning: coroutine
'YouTubeLiveBadgePlugin.setup' was never awaited` at GC time, easy to
miss in stream logs.

This is why the plugin currently appears to "work" — `on_ready()` *is*
awaited (bot.py:159-164), and `on_ready()` is where the actual event
subscription happens. So the LIVE badge feature still functions. But:
- `self.bot` is never assigned, so anything in this plugin that reads
  `self.bot.feature_enabled(...)` would crash. Check
  `_is_feature_enabled` at line 192 — yes, it does `self.bot.features`.
  If you ever call into that path before something else has set
  `self.bot` (it never gets set otherwise), it AttributeErrors.
- The feature defaults to enabled because of `bot.features.get(FEATURE_KEY, True)`
  on line 192, so the missing toggle registration is silently
  papered over by the default. Toggling from the dashboard would
  work because the dashboard writes directly to `bot.features`.

**Fix:** drop the `async`. Two-line change:

```python
def setup(self, bot):
    self.bot = bot
    if hasattr(bot, "features") and FEATURE_KEY not in bot.features:
        bot.features[FEATURE_KEY] = True
```

The `try/except` was hiding the fact that none of this runs. Remove it
or replace with a real error log.

---

# P1 — fix this week

## P1-1. Plugin registration order is documented wrong

**File:** `HatmasBot.md` lines 38-54 vs `main.py:71-221`.

The doc lists 11 plugins in a specific order. The code registers 16, in
a different order. The most consequential drift:

| # | HatmasBot.md says | main.py actually registers | Notes |
|---|---|---|---|
| 8 | KillDeathDetector | deathcounter | reversed — see "comment in main.py at lines 80-83 is wrong about why" below |
| 9 | VoiceLinePlugin | killdetector | |
| 10 | DeathCounterPlugin | voicelines | |
| 11 | EconomyPlugin | economy | (matches) |
| 12 | (omitted) | youtube_rewards | new in v2.5, missing from this section |
| 13 | (omitted) | stream_status | new in v2.5, missing from this section |
| 14 | (omitted) | youtube_live_badge | undocumented anywhere in HatmasBot.md |
| 15 | (omitted) | backup_manager | undocumented |
| 16 | (omitted) | god_pool | undocumented |

`SnapPlugin` is correctly noted as "exists but commented out" on line 52.

The reordering of deathcounter↔killdetector is intentional — `main.py:80-83`
explains it ("registered before killdetector so the on_death callback can
reference it"). The doc just hasn't been updated.

**Fix:** rewrite that section to match the actual 16-plugin order, and
add one-line summaries for the four undocumented plugins
(`youtube_live_badge`, `backup_manager`, `god_pool`, plus calling out
that `youtube_rewards` and `stream_status` belong here even though they're
covered later in the v2.5 section).

While in `main.py:49-51`, also fix the version banner — it still says
`HatmasBot v2.0` / `April 2026`, but the doc and README both say v2.5 / May 2026.

---

## P1-2. Kill-detector callback chaining is fragile and has a near-bug

**File:** `main.py:88-182`.

The wiring goes:

```python
# Round 1 — webserver/death-counter handlers
kd.on_kill = on_kill           # main.py:98
kd.on_multikill = on_multikill # main.py:99
kd.on_death = on_death         # main.py:100

# Round 2 — economy hooks
_original_on_kill  = kd.on_kill   # main.py:164
_original_on_death = kd.on_death  # main.py:165

async def _economy_on_kill(...):
    if _original_on_kill:
        await _original_on_kill(kill_type, count)   # main.py:169
    await economy.on_kill(kill_type, count)

kd.on_kill = _economy_on_kill   # main.py:180
kd.on_death = _economy_on_death # main.py:181
kd.on_assist = _economy_on_assist # main.py:182  ← never previously set
```

Two issues:

1. **`on_multikill` is not chained.** It gets set in Round 1 (line 99) and
   never touched again. That happens to be fine today because
   `_classify_multikill` in `killdetector.py:309` is also called inside
   the regular kill path, so economy still ticks on every kill. But it's
   silent — if you ever decide economy needs to do something different
   on multikill (e.g., bigger spike on a penta), nothing in this wiring
   tells you "you also need to chain `on_multikill`."

2. **`on_assist` chaining is one-shot.** Round 2 captures the *current*
   value of `kd.on_kill` and `kd.on_death` into `_original_on_kill` /
   `_original_on_death` at module-load time. If anything *later*
   reassigns those callbacks (a future plugin doing the same trick),
   the new wiring will silently lose whoever was chained before. The
   pattern doesn't scale past two consumers.

**Fix:** replace single-callback fields with a real listener list on
`KillDeathDetector`:

```python
# in killdetector.py
self._kill_listeners = []
self._death_listeners = []
self._assist_listeners = []
self._multikill_listeners = []

def add_kill_listener(self, fn): self._kill_listeners.append(fn)
# ... etc

# in the detection loop, replace `await self.on_kill(...)` with:
for fn in self._kill_listeners:
    try:
        await fn(kill_type, count)
    except Exception as e:
        _log(f"[KillDetector] kill listener error: {e}")
```

Then `main.py` becomes:

```python
kd.add_kill_listener(on_kill_to_overlay)
kd.add_kill_listener(economy.on_kill)
kd.add_death_listener(on_death_to_overlay)
kd.add_death_listener(death_counter.increment)
kd.add_death_listener(economy.on_death)
kd.add_assist_listener(economy.on_assist)
kd.add_multikill_listener(on_multikill_to_overlay)
```

No more "did the wiring preserve the previous callback?" footgun. This
also makes it trivial to add a 3rd listener (e.g., for the YouTube
rewards plugin) without touching kill detector internals.

This is the single most valuable structural cleanup in the whole codebase
because every new feature adds another consumer of these events.

---

## P1-3. Multiple `aiosqlite` connections to the same DB in one process

**File:** `plugins/economy.py:396`, `plugins/god_pool.py:62`,
`plugins/youtube_rewards.py:115`, `core/public_webserver.py:148`.

Four separate connections to `data/economy.db` from the same process.
WAL mode (set at `economy.py:397`) makes concurrent *readers* cheap, and
SQLite serializes writes anyway, so this won't crash. But:

- Schema is owned by `EconomyPlugin._init_db` (`economy.py:394`). Every
  other consumer just hopes the tables exist. `youtube_rewards.py:115`
  connects without any `CREATE TABLE IF NOT EXISTS` itself — if it ever
  runs before economy's on_ready (registration order keeps that from
  happening today, but it's fragile), it would silently fail or worse.
- `public_webserver.py` and the plugins each maintain their own
  prepared-statement cache. A schema migration in one place doesn't
  propagate.
- Plugin registration order in `main.py:151-202` happens to be:
  economy → youtube_rewards → stream_status → youtube_live_badge →
  backup_manager → god_pool. So `god_pool.py` connects after economy
  has run on_ready, fine. `youtube_rewards.py` connects in *its*
  on_ready, also after. But nothing actually enforces this.

**Fix (small):** make `youtube_rewards.py` and `god_pool.py` re-call
their schema's `CREATE TABLE IF NOT EXISTS` on connect, even if economy
"owns" them. Cheap belt-and-braces.

**Fix (better):** centralize the DB connection in a small `core/db.py`
module that returns a singleton `aiosqlite.Connection`. All plugins use
that. One place to manage WAL mode, foreign keys, migrations, and the
"don't close while another consumer is using me" problem. ~50 lines.

This is also what would let you write integration tests against the DB
without cycling the entire bot.

---

## P1-4. `_trigger_voiceline()` has 50 lines of unreachable code

**File:** `plugins/economy.py:2088-2150`.

```python
def _trigger_voiceline(self, trigger_key, god_name=None):
    """..."""
    return  # Disabled — voiceline naming too inconsistent
    suffixes = VGS_TRIGGERS.get(trigger_key)   # ← unreachable from here on
    if not suffixes:
        return
    ...
```

There are 5 callers (`economy.py:1135, 1167, 1382, 1633` and the docstring
at 2092). The doc accurately describes this state ("Currently disabled
(early `return` in `_trigger_voiceline()`)"), but having 50 lines of
dead code in a 2635-line file is a smell — IDEs flag it, future Claude
might "fix" it by deleting the return, etc.

**Fix:** convert the body to a `# noqa` block or guard the whole thing
behind a config flag:

```python
ECONOMY_VOICELINES_ENABLED = False  # config.py

def _trigger_voiceline(self, trigger_key, god_name=None):
    if not ECONOMY_VOICELINES_ENABLED:
        return
    ...  # rest as written
```

Now flipping the flag in `core/config_local.py` is enough to test the
voiceline path without code changes, AND the dead-code warning goes
away.

---

## P1-5. Test-feature-enabled inconsistency across plugins

**File:** `core/config.py:289-299`, plus various plugin checks.

`DEFAULT_FEATURES` lists 16 toggles. Plugin-by-plugin, only some plugins
actually consult `bot.is_feature_enabled(...)`:

| Plugin | Toggle exists | Plugin checks it? |
|---|---|---|
| basic | (no toggle) | n/a |
| smite | `smite_tracking`, `predictions`, `auto_scene_switch`, `auto_title` | yes (5 sites) |
| songrequest | `song_requests` | yes |
| obs | `now_playing_overlay`, `auto_scene_switch` | partial |
| godrequest | `god_requests` | yes (2 sites) |
| claude_chat | `claude_chat` | yes |
| gamble | `gamble` | yes |
| killdetector | `kill_detection` | **no — never checks** |
| voicelines | `voicelines` | **no — never checks** |
| deathcounter | (no toggle) | n/a |
| economy | `economy` | yes (6 sites) |
| youtube_rewards | `youtube_rewards` | **no — never checks** |
| stream_status | (no toggle) | n/a (always on) |
| youtube_live_badge | `youtube_live_badge` | yes (line 192) |
| backup_manager | (no toggle) | n/a |
| god_pool | (no toggle) | n/a |
| snap | `snap` | n/a, plugin disabled |

Three plugins (`killdetector`, `voicelines`, `youtube_rewards`) have
toggles in `DEFAULT_FEATURES` but no actual check inside the plugin.
Toggling them off in the dashboard does nothing — the plugin keeps
running.

**Fix:** either add the checks at the top of each plugin's main
event-handler (one or two lines per plugin), or remove the toggles
from `DEFAULT_FEATURES` and the doc. Decision time.

---

## P1-6. `requirements.txt` is wildly out of date

**File:** `requirements.txt`.

Current contents (7 lines):

```
twitchio>=2.10.0
aiohttp>=3.9.0
aiosqlite>=0.20.0
obsws-python>=1.7.0
anthropic>=0.40.0
spotipy>=2.24.0
yt-dlp>=2024.0.0
```

Issues:
- **`twitchio>=2.10.0`** — the codebase requires v3.x (HatmasBot.md says
  "TwitchIO v3.2.1", and `bot.py` uses v3-only APIs like
  `eventsub.ChatMessageSubscription`). v2 is incompatible. Anyone running
  `pip install -r requirements.txt` on a fresh machine gets a broken
  install.
- **`spotipy`** is listed but not imported anywhere — `songrequest.py`
  uses raw `aiohttp` against the Spotify Web API.
- **Missing**, but actually used:
  - `Pillow` (PIL — `core/kda_reader.py`, `tools/build_thumbnail.py`, etc.)
  - `opencv-python` (cv2 — `core/god_matcher.py`, `core/digit_matcher.py`,
    `core/kda_reader.py`)
  - `numpy` (multiple files)
  - `gtts` (`core/webserver.py`)
  - `curl_cffi` (`tools/download_voicelines.py`,
    `tools/download_god_cards.py`)
  - `pytesseract` (optional — `core/kda_reader.py`)
  - `psd-tools` (optional — `tools/build_thumbnail.py`)
  - `google-auth-oauthlib`, `google-api-python-client` (optional —
    `tools/youtube_live_badge.py`)

**Fix:** rewrite `requirements.txt` to match reality and split optional
deps into a comment block:

```
# Core
twitchio>=3.2.1
aiohttp>=3.9.0
aiosqlite>=0.20.0
obsws-python>=1.7.0
anthropic>=0.40.0
yt-dlp>=2024.0.0
gtts>=2.5.0

# Image / video
Pillow>=10.0.0
opencv-python>=4.9.0
numpy>=1.26.0
curl_cffi>=0.6.0

# Optional — install if you want OCR fallback
# pytesseract>=0.3.10
# Optional — install if you want layered .psd output from build_thumbnail
# psd-tools>=1.10
# Optional — install if you want YouTube live-badge automation
# google-auth-oauthlib>=1.2
# google-api-python-client>=2.130
```

Drop `spotipy>=2.24.0`. It's not used.

---

## P1-7. `economy.py` and `smite.py` are too big to navigate

`plugins/economy.py` is **2635 lines / 119 KB**.
`plugins/smite.py` is **1988 lines / 82 KB**.
`plugins/songrequest.py` is **1293 lines / 53 KB**.

These files have grown to the point where:
- Reading them top-to-bottom is a 30-minute exercise.
- Search-replace refactors are scary because the same identifier means
  different things in different sections.
- `economy.py` mixes the fair-value formula, SQLite schema, MixItUp
  integration, all 7 chat commands, all 7 overlay event emitters, the
  YouTube schema, dividends, the periodic backfill, and a defunct
  voiceline trigger — all in one class.

**Suggested split for `economy.py`:**

```
plugins/economy/
  __init__.py           # re-export EconomyPlugin
  plugin.py             # EconomyPlugin class — lifecycle, command registration, hookups (~400 lines)
  fair_value.py         # calculate_fair_value() + constants (~80 lines)
  db.py                 # _init_db, _migrate_*, schema (~300 lines)
  trading.py            # cmd_buy, cmd_sell, position-limit, MixItUp calls (~400 lines)
  match.py              # settle_match, on_god_detected, on_match_end, on_match_result, backfill loop (~600 lines)
  ticking.py            # on_kill / on_death / on_assist live ticks, big_spike/big_crash (~200 lines)
  overlays.py           # _emit_overlay_event, _emit_trade_event, _emit_leaderboard, _trigger_voiceline (~300 lines)
  youtube.py            # _pay_youtube_dividend, _grant_youtube_share, query helpers (~200 lines)
  api.py                # register_api_routes + the /api/economy/* handlers (~150 lines)
```

**Suggested split for `smite.py`:** mostly along the same lines —
tracker.gg HTTP wrapper, match-state machine, title management,
prediction wrapping, and the new v2.5 `parse_listing_entry` /
`get_god_aggregates` helpers each want their own file.

These are big refactors. They're P1 because every new feature you add
without splitting makes the eventual split harder.

---

# P2 — clean up before next big feature

## P2-1. 8 stale `test_*.py` scripts at repo root

**Files at repo root** (all from Apr 6-7, untouched since):

```
test_fade.py            6.5K
test_god_image.py       8.7K
test_godrequest.py     13.2K
test_nowplaying.py     15.8K
test_obs_align.py       6.9K
test_title.py           3.4K
test_tracker.py         6.5K
test_tracker_teams.py   4.5K
```

These look like single-shot scripts that were used to validate features
during initial development (the timestamps line up with the early
commits). They're not pytest tests; the proper pytest test is
`tests/test_economy.py`, the only file in `tests/`.

**What to do:**
- Move what's still useful to `tests/` and convert to pytest, OR
- Move all 8 to `archive/initial_dev_tests/` to preserve the history
  without cluttering root, OR
- Delete (git keeps them).

I'd lean **archive folder** — they're tiny, and the manual frame-grabber
in `test_god_image.py` could plausibly be useful for debugging again.

While there, also consider archiving:
- `capture_frames.py` (root, 3.8K) — does what `tools/obs_screenshot.py`
  does, less well.
- `download_god_icons.py` (root, 12.4K) — companion to the
  `tools/download_god_cards.py` that lives in `tools/`. The doc lists
  `download_god_icons.py` as "tools/" by mistake (HatmasBot.md doesn't
  actually mention this script's location; just that it exists).
- `spotify_auth.py` (root, 3.7K) — listed in `Command_Line_Tools.md` but
  not actually called by anything in the live bot codebase.

These three would also fit naturally in `tools/`. Pick one:
*either* the bot actually uses them and they belong in `tools/`, *or*
they're vestigial and belong in `archive/`. They shouldn't sit at root.

## P2-2. `Command_Line_Tools.md` references things that don't exist anymore

**File:** `Command_Line_Tools.md` (37 KB, last touched May 3 — recent).

Spot checks:
- `### spotify_auth.py` (line 70) — file exists at root, but
  `core/auth.py` is now the OAuth flow (HatmasBot.md says "Spotify:
  auto-generated on first run via browser OAuth"). Two different paths.
- `### capture_frames.py` (line 540) — see P2-1.
- The "Quick Reference" table (line 837+) lists every tool, including
  the ones that have been superseded. Hard to tell what's current.

`HatmasBot.md` is now the primary reference (it's bigger and more
recent). `Command_Line_Tools.md` and `Commands.md` are partial
duplicates that drift independently.

**Fix:** decide which doc is authoritative. Either:
- Fold `Command_Line_Tools.md` and `Commands.md` into `HatmasBot.md`
  (which already has Commands Reference and Tools sections), then
  delete the standalone files; or
- Strip `HatmasBot.md`'s tools/commands sections down to one-line
  pointers and let `Command_Line_Tools.md` / `Commands.md` be the
  detailed reference.

I'd lean **fold into HatmasBot.md** — it's already the canonical doc
the project README points to.

## P2-3. 11 `prototype_*.html` files in `overlays/`

```
overlays/prototype_dividend.html
overlays/prototype_dividend_v2.html
overlays/prototype_leaderboard_v2.html
overlays/prototype_match_end.html
overlays/prototype_match_end_v2.html
overlays/prototype_match_live.html
overlays/prototype_match_live_v2.html
overlays/prototype_portfolio.html
overlays/prototype_ticker.html
overlays/prototype_ticker_v2.html
overlays/prototype_tradefeed_v2.html
```

Cross-referenced against `core/webserver.py`: none of these are wired
into routes. They're pre-design experiments that informed the actual
`economy_*` overlays. Each is 5-30 KB.

**Fix:** move to `overlays/_prototypes/` if you want to keep them as
visual references, or delete entirely. Either way, get them out of the
top-level overlays/ folder so it's clear what's actually a live OBS
browser source.

## P2-4. 6 stale scan logs at repo root

```
scan.log               12.5K
scan_cuda.log          12.5K
scan_enroll.log        12.5K
scan_no_refined.log    10.7K
scan_optimized.log     12.6K
scan_post_enroll.log   12.5K
```

All from Apr 18-19 — perf-tuning sessions for the VOD detector. Not in
.gitignore, so they're committed (`git status` would tell you for sure
but they appear in `ls -la` and aren't filtered). They're useful as a
historical record (HatmasBot.md cites the timing numbers from these
runs) but not as live working files.

**Fix:** add `scan*.log` to `.gitignore` and either delete or move to
`archive/perf_logs/`.

## P2-5. Test render artifacts at repo root

```
test.png         1.2 MB
test.psd         7.8 MB
test_preview.png 1.2 MB
twitch_logo.png  9.2 KB
test_layers/     11 files, 1.9 MB
```

Outputs from `tools/build_thumbnail.py` left at root from May 3
testing. The thumbnail tool's documented output dir is `thumbnails/`
(per HatmasBot.md line 781). These should land there too.

**Fix:** delete or move to `thumbnails/_test/`. Add `test*.png`,
`test*.psd`, `test_layers/` to `.gitignore`.

## P2-6. Tools directory has 4 undocumented scripts

`HatmasBot.md`'s Tools section documents 12 scripts. The actual
`tools/` directory has 19. Undocumented:

- `tools/capture_god_reference.py` — companion to `obs_screenshot.py`?
  Captures god portrait references for the matcher.
- `tools/diagnose_god_detection.py` — debug helper for the god matcher.
- `tools/process_vods.py` — **the Sony Vegas orchestrator**. The doc
  says this is "pending (Step 9)" in `SonyVegasTODO.md`, but the file
  exists and has 580+ lines of working code. Doc is out of date.
- `tools/sort_unknowns.py` — sorting helper for the recordings/unknown/ folder.

The `mark_youtube_video.py`, `replay_economy.py`, and `purge_excluded.py`
tools ARE documented (under "v2.5 Update" section), so it's just these 4.

**Fix:** add brief descriptions to the doc (or move these to
`tools/_dev/` if they aren't shipping tooling).

## P2-7. `SonyVegasTODO.md` and `CURRENT_TASK.md` status drift

**File:** `SonyVegasTODO.md` (Apr 24, 21 KB) — referenced by
`HatmasBot.md` line 562 as "in progress — see SonyVegasTODO.md". Steps
8-12 listed as "Next." But `tools/process_vods.py` exists (Step 11 done?)
and `vegas_scripts/ProcessVideo.cs`'s status isn't clear from the file
listing. Worth a 5-min review and update.

**File:** `CURRENT_TASK.md` (Apr 13, 26 KB) — gitignored per `.gitignore:13`.
Pre-v2.5 task list, presumably superseded by HatmasBot.md's v2.5 update
section. Probably safe to delete since it's not in git anyway.

`Social_Tabs_Plan.md` (May 2, 8 KB) — recent planning doc. Worth a
sentence in HatmasBot.md saying "what's coming next" so a fresh reader
knows it's an active design doc, not a vestige.

## P2-8. `requirements.txt` `spotipy>=2.24.0` is dead

Already covered in P1-6 as part of the requirements rewrite. Calling it
out separately because it's a clean signal: nothing imports `spotipy`,
and `songrequest.py` rolls its own Spotify HTTP client with `aiohttp`.
Safe to drop.

## P2-9. `tio.tokens.json` at root — unclear purpose

**File:** `.tio.tokens.json` (388 bytes, May 3 — currently fresh).

Likely TwitchIO's own token cache. Hidden file (leading dot), in
`.gitignore` (line 2). Nothing in the codebase reads it explicitly, so
it's TwitchIO internal. No action needed; just calling it out for
completeness.

## P2-10. Documentation calls out `tools/obs_screenshot.py` placement, but the actual placement is correct.

Minor: HatmasBot.md says `download_god_icons.py` is in `tools/` (line
... actually re-reading, the doc lists it ambiguously). The file is
actually at repo root. See P2-1.

---

# P3 — nice to have

## P3-1. Logging is per-module, but no global setup

`KillDetector` has its own `logging.getLogger("KillDetector")` setup
(`killdetector.py:53-66`). Most other plugins use `print()`. There's
no central `logging.basicConfig` or shared format. Migrating everything
to `logging` with a single root setup in `main.py` would give you log
levels (silence INFO chat at debug=False), file rotation, and correct
output redirection on Windows where stdout buffering is its own
adventure.

Low priority; works fine today.

## P3-2. `core/cache.py` is 581 bytes

It's a small TTL cache class. Nothing wrong with that; just worth
asking whether it's used. Quick grep suggests it's referenced from
`smite.py` and not much else. If usage is minimal you could inline it
and drop the file.

## P3-3. Empty directories from the Sony Vegas pipeline

`highlight/` and `rendered/` are empty. `inbox/` has 2 files, 6.1 GB
(real recordings). These should be either:
- Created on-demand by the orchestrator (not committed empty), or
- Documented with a `.gitkeep` and a one-line README.

## P3-4. Bare `except Exception` is widespread

70 occurrences across 25 files. Most are reasonable ("don't crash the
bot if a single chat handler errors"), but a few swallow real bugs
silently. Examples:

- `plugins/godrequest.py:102, 336, 342, 347` — four pass-on-error
  branches. At least one wraps a JSON write that should warn on
  failure (you'd silently drop a viewer's god request).
- `plugins/youtube_live_badge.py:64` — wraps the broken async setup
  body, hiding the real failure.

Not fixing these is fine. But if you ever want to systematically clean
them up, the `except Exception:\n pass` pattern (from the grep above)
catches them in 30 lines.

---

# Things that LOOK problematic but aren't

So you don't burn cycles re-investigating:

1. **Multiple aiosqlite connections to economy.db.** Mentioned in P1-3
   as a *structural* concern, but it's not a runtime bug — WAL mode
   plus single-process means writes serialize and reads don't block.
   The system works.
2. **`SnapPlugin` exists but is commented out.** Doc correctly notes
   this (HatmasBot.md:52). The webserver still routes `/overlay/snap`
   (`webserver.py:80`) and there's `snap_active` state — these are
   dead but not actively broken. Cheap to clean up later, but harmless
   today.
3. **TwitchIO v3.2.1 token-bug workaround in `bot.py`.** The
   `_manual_channel_points_subscribe()` workaround is clearly
   documented (HatmasBot.md:174-180) and works. Keep watching for a
   TwitchIO upstream fix and remove the workaround when one ships, but
   don't touch it now.
4. **The kill-detector reading OBS screenshots in a tight 0.8s loop.**
   Looks expensive; it's not. The doc explains the 4-7ms typical
   per-frame cost (HatmasBot.md:306).
5. **The big_spike / big_crash thresholds aren't documented.**
   `economy.py:1132-1167` references these events but the doc doesn't
   say what triggers them. Minor; the event names are mostly
   self-explanatory and the relevant constants live nearby.

---

# Suggested order of operations

If I were going to spend a stream's worth of cleanup time, I'd do it
in this order:

**Day 1 (an hour):**
1. P0-1 — revoke broadcaster token, replace with placeholder, re-auth.
2. P0-2 — drop `async` from youtube_live_badge.setup.
3. P1-6 — fix requirements.txt (anyone reproducing your env will thank you).

**Day 2 (a couple hours):**
4. P1-1 — update HatmasBot.md's Plugin Registration Order section
   (also fixes the v2.0/April banner in main.py).
5. P1-2 — listener-list refactor for KillDeathDetector callbacks
   (highest structural payoff).
6. P1-5 — decide on the three orphan feature toggles
   (kill_detection / voicelines / youtube_rewards).

**Day 3 (an afternoon):**
7. P2-1 through P2-5 — root-folder hygiene (move tests, delete logs,
   move thumbnail outputs, gitignore additions).
8. P2-2 — fold `Command_Line_Tools.md` and `Commands.md` into
   `HatmasBot.md`.

**When you next touch the economy plugin (or before adding another
feature to it):**
9. P1-7 — split economy.py and smite.py into packages.

The DB centralization (P1-3) and the rest of P1 / P2 / P3 can wait for
the next natural lull.

---

# Appendix A — File-Split Plan for the Three Big Plugins

This section turns P1-7 into a concrete refactor recipe. Goal: take
`economy.py` (2635 lines), `smite.py` (1988), and `songrequest.py` (1293)
from "monolithic plugin file" to "small package with focused modules,"
without changing any external behavior.

## Common pattern

For a plugin that's grown too big, the cleanest split is:

```
plugins/<name>/                 # was plugins/<name>.py
  __init__.py                   # re-exports the plugin class for back-compat
  plugin.py                     # the lifecycle class — setup, on_ready, cleanup, hookups
  <feature1>.py                 # focused module
  <feature2>.py
  ...
```

Two things to preserve:

1. **External imports must keep working.** `main.py` does
   `from plugins.economy import EconomyPlugin`. After the split, that
   line should still work — the package's `__init__.py` re-exports the
   class. Zero changes outside the package.

2. **The instance variables stay on the class.** Don't try to move
   `self._prices` into a separate "PriceCache" class on the first pass.
   Submodules expose **functions** that take the plugin instance as
   their first argument, or sit as **mixins** the main class
   inherits from. The mixin pattern keeps `self.X` syntax unchanged
   inside the bodies of moved methods, which is what makes the diff
   reviewable.

The mixin pattern:

```python
# plugins/economy/db.py
class _DBMixin:
    async def _init_db(self):
        ...
    async def _migrate_god_prices_kda_columns(self):
        ...
    # etc — all methods that were on EconomyPlugin before, unchanged

# plugins/economy/plugin.py
from .db import _DBMixin
from .trading import _TradingMixin
from .match import _MatchMixin
# ...

class EconomyPlugin(_DBMixin, _TradingMixin, _MatchMixin, ...):
    def __init__(self, token_manager=None):
        ...   # state stays here
    def setup(self, bot): ...
    async def on_ready(self): ...
    async def cleanup(self): ...

# plugins/economy/__init__.py
from .plugin import EconomyPlugin
__all__ = ["EconomyPlugin"]
```

`from plugins.economy import EconomyPlugin` keeps working unchanged.

Why mixins instead of "extract pure functions": the methods reference
~20 instance attributes (`self._db`, `self._prices`, `self.bot`, etc.)
and call ~30 sibling methods. Converting to free functions means
threading all of that through arguments, which is a bigger diff for no
real gain. Mixins keep the diff to a *file move* with import tweaks,
not a behavior change.

---

## A.1 — `plugins/economy.py` → `plugins/economy/`

Current sections (verified by `grep ^class|^    def|^    async def`):

| Lines | Section |
|---|---|
| 51-58 | `_build_excluded_set()` |
| 66-71 | `VOLATILITY_TIERS` |
| 84-115 | Fair-value constants |
| 118-213 | `calculate_fair_value()` |
| 215-253 | Match-end win/loss constants + VGS triggers |
| 256-389 | `class EconomyPlugin: __init__ / setup / on_ready / _run_periodic_backfill / cleanup` |
| 394-613 | DB: `_init_db`, `_migrate_god_prices_kda_columns`, `_load_prices`, `_get_recent_prices`, `_update_price`, `_ensure_god_exists` |
| 699-739 | God name resolution: `_build_god_name_index`, `_resolve_god_name` |
| 745-781 | Volatility + match-end change calc |
| 787-857 | MixItUp API helpers |
| 860-1068 | Trading: `execute_buy`, `execute_sell`, `_add_shares`, `_remove_shares`, `_get_holding`, `_get_position_value`, `_get_portfolio_value`, `_get_full_portfolio` |
| 1070-1103 | `on_god_detected` |
| 1105-1190 | Live ticking: `on_kill`, `on_death`, `on_assist` |
| 1192-1408 | Match lifecycle: `on_match_end`, `on_match_result`, `settle_match` |
| 1410-1541 | `backfill_recent_matches` |
| 1543-1689 | Dividends: `_pay_dividend`, `_pay_youtube_dividend` |
| 1691-1846 | Profile/chatter helpers, `_distribute_free_shares` |
| 1848-2078 | Test/sim emit methods (`simulate_game`, `emit_test_*`, `reload_prices`) |
| 2080-2191 | Overlay event emitters (`_emit_overlay_event`, `_trigger_voiceline`, `_emit_trade_event`, `_emit_leaderboard`) |
| 2193-2512 | Chat commands (`cmd_buy`, `cmd_sell`, `cmd_portfolio`, `cmd_price`, `cmd_market`, `cmd_dividend`) + `_check_cooldown` |
| 2514-2596 | `register_api_routes` |
| 2597-end | `seed_prices` (helper used by tools/seed_economy.py) |

Proposed split:

```
plugins/economy/
  __init__.py                  # 5 lines
  plugin.py                    # ~250 lines
    – class EconomyPlugin
    – __init__, setup, on_ready, cleanup, _run_periodic_backfill
    – the multiple-inheritance line that pulls in all mixins
  fair_value.py                # ~130 lines
    – FAIR_VALUE_* constants
    – calculate_fair_value()
    – VOLATILITY_TIERS
    – WIN_BASE_*, LOSS_BASE_* constants
    – _calculate_match_end_change()  (used by settle_match)
  db.py                        # ~280 lines
    – class _DBMixin: _init_db, _migrate_*, _load_prices,
                       _get_recent_prices, _update_price,
                       _ensure_god_exists
  god_names.py                 # ~50 lines
    – class _GodNamesMixin: _build_god_name_index, _resolve_god_name
  mixitup.py                   # ~100 lines
    – class _MixItUpMixin: _miu_get, _miu_patch,
                            _resolve_currency_id, _get_user_id,
                            _get_balance, _adjust_balance
  trading.py                   # ~250 lines
    – class _TradingMixin: execute_buy, execute_sell,
                            _add_shares, _remove_shares,
                            _get_holding, _get_position_value,
                            _get_portfolio_value, _get_full_portfolio
  match.py                     # ~450 lines
    – class _MatchMixin: on_god_detected, on_match_end,
                          on_match_result, settle_match,
                          backfill_recent_matches,
                          _distribute_free_shares
  ticking.py                   # ~120 lines
    – class _TickingMixin: on_kill, on_death, on_assist
  dividends.py                 # ~190 lines
    – class _DividendsMixin: _pay_dividend, _pay_youtube_dividend
  overlays.py                  # ~190 lines
    – VGS_TRIGGERS, VOICELINE_DIR
    – class _OverlaysMixin: _emit_overlay_event, _trigger_voiceline,
                             _emit_trade_event, _emit_leaderboard
  helpers.py                   # ~180 lines
    – _build_excluded_set(), EXCLUDED_USERS_LOWER (module-level)
    – class _HelpersMixin: _get_profile_image, _get_chatters,
                            is_excluded_user
  testing.py                   # ~250 lines
    – class _TestingMixin: simulate_game, emit_test_dividend,
                            emit_test_leaderboard, emit_test_portfolio,
                            emit_test_tradefeed, emit_test_match_end,
                            emit_test_ticker, reload_prices, seed_prices
  commands.py                  # ~340 lines
    – class _CommandsMixin: cmd_buy, cmd_sell, cmd_portfolio,
                             cmd_price, cmd_market, cmd_dividend,
                             _check_cooldown
  api.py                       # ~90 lines
    – class _APIMixin: register_api_routes + handlers
```

Mixin order in `plugin.py`:

```python
class EconomyPlugin(
    _DBMixin,
    _GodNamesMixin,
    _MixItUpMixin,
    _TradingMixin,
    _MatchMixin,
    _TickingMixin,
    _DividendsMixin,
    _OverlaysMixin,
    _HelpersMixin,
    _TestingMixin,
    _CommandsMixin,
    _APIMixin,
):
    ...
```

External imports that still need to work (verified via grep):

- `from plugins.economy import EconomyPlugin` — `main.py:39`
- `tools/seed_economy.py` calls `plugin.seed_prices(...)` — covered by
  `_TestingMixin` so the method stays on the instance.
- `tools/replay_economy.py` calls fair-value math — should import from
  `plugins.economy.fair_value` directly. Tiny edit.

**How to do it (one branch, one PR):**
1. Make a `plugins/economy/` directory.
2. Drop `plugin.py` skeleton with `__init__` + lifecycle methods only.
3. For each mixin: cut the relevant block out of the old `economy.py`,
   wrap in `class _XMixin:`, paste into its file. Add necessary imports
   at the top. Repeat for each block.
4. Add the mixin imports + the multiple-inheritance line to `plugin.py`.
5. Write `__init__.py` to re-export `EconomyPlugin`.
6. Delete the old `economy.py`.
7. Run `python -c "from plugins.economy import EconomyPlugin; e = EconomyPlugin()"`
   to catch any missed imports.
8. Run `python tests/test_economy.py` (the existing pytest in `tests/`).
9. Smoke test: launch bot, hit `!portfolio`, hit `!buy ymir 100`, run
   `sim_economy` from the dashboard. If those three work, you're done.

**Time estimate:** 2–3 hours focused. Most of it is the cut-and-paste
plus tracking down the imports each new file needs.

---

## A.2 — `plugins/smite.py` → `plugins/smite/`

Current sections:

| Lines | Section |
|---|---|
| 45-201 | `class SmitePlugin: __init__`, state load/save, record helpers |
| 202-263 | `setup`, `on_ready`, callback registration (`on_match_start`, `on_god_detected`, `on_match_end`, `on_match_result`), `_fire_event` |
| 265-313 | `force_end_match` |
| 315-367 | `set_god_from_portrait` |
| 368-466 | tracker.gg HTTP: `_cffi_get`, `_tracker_get`, `_fetch_live_match`, `_fetch_profile`, `_fetch_profile_for_mode`, `_fetch_summary`, `_fetch_match_detail` |
| 468-716 | History/aggregates: `get_match_history`, `_extract_god_segments`, `get_god_aggregates` |
| 718-855 | Listing parsing: `parse_listing_entry`, `parse_match_for_settlement` |
| 857-967 | Live-data extraction: `_find_my_segment`, `_extract_god_info`, `_extract_all_players`, stat helpers |
| 969-1192 | Poll loop: `_poll_loop`, `_check_live_match` (the big one) |
| 1194-1426 | OBS god image management: `_set_god_image`, `_try_set_god_image`, `_set_god_background`, `_startup_hide_god_image`, `_clear_god_image`, `_update_overlay_state` |
| 1455-1478 | Twitch headers: `_twitch_headers`, `_broadcaster_headers` |
| 1480-1648 | Title management: `_command_rotation_loop`, `_get_current_song`, `_update_stream_title`, `set_title_template`, `_save/_load_title_templates`, rotation command CRUD |
| 1650-1751 | Predictions: `_create_prediction`, `resolve_prediction` |
| 1753-1976 | Chat commands: `cmd_god`, `cmd_stats`, `cmd_rank`, `cmd_match`, `cmd_winrate`, `cmd_kda`, `cmd_damage`, `cmd_team`, `cmd_lastmatch`, `cmd_record` |
| 1978-end | `cleanup` |

Proposed split:

```
plugins/smite/
  __init__.py                  # re-export
  plugin.py                    # ~280 lines
    – SmitePlugin class
    – __init__, setup, on_ready, cleanup
    – callback registration (on_match_start/end/etc, _fire_event)
    – record/state helpers (record_result, get_record_string,
                             _load_state, _save_state, _check_day_reset)
  tracker_client.py            # ~200 lines
    – _cffi_get, _tracker_get
    – _fetch_live_match, _fetch_profile, _fetch_profile_for_mode,
      _fetch_summary, _fetch_match_detail
    – Bundled as _TrackerClientMixin
  history.py                   # ~270 lines
    – get_match_history, _extract_god_segments, get_god_aggregates
    – parse_listing_entry, parse_match_for_settlement
    – Bundled as _HistoryMixin
  match_state.py               # ~330 lines
    – _poll_loop, _check_live_match
    – force_end_match, set_god_from_portrait
    – live-data extraction (_find_my_segment, _extract_god_info,
                              _extract_all_players, stat helpers)
    – Bundled as _MatchStateMixin
  obs_portrait.py              # ~250 lines
    – _set_god_image, _try_set_god_image, _set_god_background,
      _startup_hide_god_image, _clear_god_image, _update_overlay_state
    – Bundled as _OBSPortraitMixin
  title.py                     # ~200 lines
    – _command_rotation_loop, _get_current_song, _update_stream_title,
      set_title_template, _save_title_templates, _load_title_templates,
      add_rotation_command, remove_rotation_command, get_rotation_commands
    – Bundled as _TitleMixin
  predictions.py               # ~110 lines
    – _create_prediction, resolve_prediction
    – Bundled as _PredictionsMixin
  twitch_api.py                # ~30 lines
    – _twitch_headers, _broadcaster_headers
    – Bundled as _TwitchAPIMixin
  commands.py                  # ~250 lines
    – cmd_god, cmd_stats, cmd_rank, cmd_match, cmd_winrate, cmd_kda,
      cmd_damage, cmd_team, cmd_lastmatch, cmd_record
    – Bundled as _CommandsMixin
```

External callers:

- `main.py:29` — `from plugins.smite import SmitePlugin`
- `plugins/economy.py:on_god_detected` — calls `smite_plugin.get_match_history`,
  `smite_plugin.parse_listing_entry`, `smite_plugin.get_god_aggregates`. All
  remain methods on the instance.
- `tools/replay_economy.py` — same calls.
- The kill detector hooks `smite_plugin.set_god_from_portrait` /
  `force_end_match`. Both stay on the instance.

**Special note on `_check_live_match`:** that method is ~200 lines and
the most state-machine-heavy code in the plugin. Don't try to split it
inside the mixin move. If you want to refactor it later, do it as a
follow-on after the mixin split lands and is verified.

**Time estimate:** 3–4 hours focused.

---

## A.3 — `plugins/songrequest.py` → `plugins/songrequest/`

Current sections:

| Lines | Section |
|---|---|
| 35-152 | `class SongRequestPlugin`: `__init__`, data load/save (likes, history, blacklist, state) |
| 153-225 | Spotify auth: `on_ready`, `_load_spotify_token`, `_refresh_spotify_token`, `_spotify_headers` |
| 233-371 | Search/resolve: `_search_spotify`, `_yt_dlp_extract`, `_search_youtube`, URL detection helpers, `_resolve_spotify_url`, `_resolve_youtube_url`, `_extract_youtube_id` |
| 372-465 | YouTube playback: `_start_youtube_playback`, `on_youtube_ended/started/progress` |
| 466-541 | Spotify playback control: queue add, current playback, skip, pause, resume, blacklist check, dup check, wait estimate |
| 542-1029 | Chat commands: `cmd_sr`, `cmd_skip`, `cmd_wrongsong`, `cmd_songlist`, `cmd_song`, `cmd_like`, `cmd_mysongs`, `cmd_toprequester`, `cmd_voteskip`, `cmd_blacklistsong`, `cmd_songstatus`, `cmd_topsongs` |
| 1031-1287 | Playback monitor + transitions: `_playback_monitor`, `_on_track_change`, `_delayed_playlist_show`, `_show_playlist_song`, `_transition_to_next`, `skip_current` |
| 1288-end | `cleanup` |

Proposed split:

```
plugins/songrequest/
  __init__.py
  plugin.py                    # ~230 lines
    – SongRequestPlugin class
    – __init__, setup, on_ready, cleanup
    – data load/save (_load_data, _save_data, _save_blacklist,
                       _save_state, _save_history, _song_key)
  spotify.py                   # ~170 lines
    – _load_spotify_token, _refresh_spotify_token, _spotify_headers
    – _search_spotify, _resolve_spotify_url, _is_spotify_url
    – _add_to_spotify_queue, _get_current_playback, _skip_track,
      _pause_spotify, _resume_spotify
    – Bundled as _SpotifyMixin
  youtube_audio.py             # ~140 lines
    – _yt_dlp_extract, _search_youtube, _is_youtube_url,
      _resolve_youtube_url, _extract_youtube_id
    – _start_youtube_playback, on_youtube_ended/started/progress
    – Bundled as _YouTubeMixin
  queue_logic.py               # ~80 lines
    – _is_blacklisted, _is_duplicate_in_queue, _estimate_wait_ms
    – _transition_to_next, skip_current
    – Bundled as _QueueMixin
  playback_monitor.py          # ~260 lines
    – _playback_monitor, _on_track_change,
      _delayed_playlist_show, _show_playlist_song
    – Bundled as _MonitorMixin
  commands.py                  # ~490 lines
    – cmd_sr, cmd_skip, cmd_wrongsong, cmd_songlist, cmd_song,
      cmd_like, cmd_mysongs, cmd_toprequester, cmd_voteskip,
      cmd_blacklistsong, cmd_songstatus, cmd_topsongs
    – Bundled as _CommandsMixin
```

**Time estimate:** 2 hours focused.

---

## How to land all three without breaking the bot

Don't do all three in one go. Recommended sequence:

1. **Land economy first.** Biggest payoff, cleanest boundaries, has an
   existing pytest. Do the split, run the test, smoke-test in a
   non-streaming session.
2. **Wait one or two stream sessions.** Confirms nothing weird turns up
   under real load. Roll back if so.
3. **Then smite.** Bigger blast radius (touches more of the bot's
   surface area), so it gets the longer warm-up window.
4. **Then songrequest.** Smallest of the three, lowest risk.

Each split is its own commit, ideally its own PR if you're using PRs.
Resist the temptation to "while I'm in here..." rename anything. The
goal is *only* to move code — preserve behavior, preserve diff
reviewability.

---

## A side effect worth knowing about

Mixins make `pytest` fixtures slightly noisier — `EconomyPlugin()` now
inherits from a dozen classes, so `dir(plugin)` is busy. If that
becomes annoying in tests, you can switch from mixins to "delegate
objects" (e.g., `self.db = _DBManager(self)` and
`await self.db.init_db()`), which is what FastAPI-style apps do. But
that's a bigger refactor and a bigger diff. Mixins first; delegates
later if needed.
