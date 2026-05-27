# HatmasBot TODO

Captured 2026-05-15 from the easy-wins planning session. Phases 1, 2,
and 3 are already committed (housekeeping + voicelines auto-refund +
HatmasBot.md polish). Everything below is what's left.

---

## Phase 4 — Unblock the Vegas highlight pipeline

Largely manual creative work in Vegas, then a couple of Python runs.
Big payoff: drains the inbox/recordings backlog into rendered
highlights for the first time.

1. Capture the horizontal full-gameplay Vegas preset.
   - Open Vegas, drag in any 1080p mp4 from `recordings/Atlas/`.
   - Set up the horizontal full-gameplay timeline the way every
     YouTube upload should look (audio levels, any pan/crop, FX).
   - With the project open, run `vegas_scripts/TuneFrame.cs` to
     dump the captured settings to
     `vegas_presets/horizontal_full.tune.json`.
   - SonyVegasTODO.md Step 8.

2. Dry-run `vegas_scripts/ProcessVideo.cs` against one of the
   backlog videos in `inbox/`. First real end-to-end test of that
   1360-line script per SonyVegasTODO.md Step 9. Watch for issues
   and fix as they come up.

3. Once the dry-run produces a good output, point
   `tools/process_vods.py` at the full backlog:
   - The 6 GB inbox video.
   - The two `.mp4`s in `recordings/Atlas/` with `.events.json`
     siblings.
   - Let it grind. This both clears the backlog and validates the
     pipeline at volume.

---

## Phase 5 — Social Media Tabs on the public landing page

Design doc already complete at `Social_Tabs_Plan.md`. Estimated
2–3 hours total.

4. Re-read `Social_Tabs_Plan.md` to refresh the design.

5. Verify `YOUTUBE_CHANNEL_ID` is in `core/config.py`. Add Bluesky
   handle if it's not there yet (Bluesky's public API needs no auth).

6. Add three endpoints to `core/public_webserver.py` — one each for
   YouTube, TikTok, and Bluesky — that fetch and cache the latest
   few posts/videos.

7. Add the tab UI to `public/landing.html`. Tabbed switcher;
   thumbnail + title + link per item.

8. Test on the public-facing landing page, then commit.

---

## Verify-before-trusting list (audit false alarms)

The original "easy wins" audit had multiple incorrect claims that
we caught while verifying. Treat the items below as **needs
investigation**, not confirmed issues — the auditor's track record
on this codebase is shaky.

- **"Three plugins (kill_detection, voicelines, youtube_rewards)
  have feature toggles but never check them."** Partially
  disproven: `plugins/voicelines.py:229` *does* check
  `self.bot.is_feature_enabled("voicelines")`. The same claim for
  `plugins/killdetector.py` and `plugins/youtube_rewards.py` was
  not independently verified. Spot-check before "fixing".

- **"Kill-detector callback chaining is fragile and doesn't scale;
  convert to listener lists."** Already done. `core/main.py` uses
  `kd.add_kill_listener` / `add_death_listener` /
  `add_assist_listener` / `add_god_identified_listener` /
  `add_gameplay_ended_listener` patterns. Multiple subscribers
  (overlay, deathcounter, economy) already coexist cleanly. Doc
  paragraph documenting the pattern was added 2026-05-15.

- **"Command_Line_Tools.md + Commands.md should fold into
  HatmasBot.md."** Maybe. Skip unless the drift between the three
  becomes actively painful — the merge is more work than it
  sounds and HatmasBot.md is already 120 KB.

- **"Four tools in tools/ are undocumented in HatmasBot.md."** Was
  not actually checked against the current HatmasBot.md state.
  Run a real check before acting.

---

## Future / nice-to-have (lower urgency)

- Tighten or split the long KillDeathDetector paragraph in
  HatmasBot.md (around line 67 in the Plugin Registration section)
  — works as-is, just dense.
- Spot-check the Tone/Style rule ("no emojis, no flair") against
  any recent overlay text — Phase 5 social tabs in particular
  should not slip emojis in.
- After Phase 4 produces its first horizontal-full render, drop
  the `vegas_presets/horizontal_full.tune.json` capture path into
  HatmasBot.md alongside the existing vertical preset section.
