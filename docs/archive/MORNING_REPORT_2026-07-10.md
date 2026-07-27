# Morning Report — overnight run 2026-07-09 → 07-10 (KDA reliability night)

Branch **`overnight-2026-07-09`** (worktree `C:\Projects\HatmasBot-overnight-0709`),
3 commits, never pushed, main untouched. All 6 test suites green (the 5 usual +
a new killdetector hardening suite).

## Merge — and ONE morning action that matters

```
cd C:\Projects\HatmasBot
git merge overnight-2026-07-09
git worktree remove C:\Projects\HatmasBot-overnight-0709
```

⚠️ **Then restart the bot** (it's been running since yesterday morning and keeps
the OLD detector in memory until restarted). Kill the python task and let the
logon task relaunch it, or just reboot.

## Why your live overlay misbehaved — both mechanisms found and fixed

Your symptoms were precise and both traced to root causes:

**"It messes up around 1st/2nd kill" / "says I have an assist when I don't"** —
the live detector accepted ANY single frame that read as a plausible increase.
Kill moments are exactly when the HUD strip is noisiest (kill banner, gold
popups), so a one-frame misread like `0/0/0 → 0/0/1` instantly fired chat,
overlay, an economy tick — and even **enrolled its garbage digits as new
templates**, slowly polluting the matcher. Fix: `DELTA_CONFIRM_READS` — a
change must repeat identically on 2 consecutive frames (0.8s apart) before
anything fires. Misreads almost never produce the same wrong digits twice;
real kills persist. Cost: kill popups arrive ~1s later. (`5ef1e36`)

**"...and then it stays messed up"** — once a phantom committed, every real
read afterwards looked like a *decrease* and was rejected, forever, until your
real KDA caught up to the phantom. This was the poisoned-baseline recovery the
7/4 run added to the VOD scanner but never mirrored live (it was an open
thread in my notes). Now 3 consecutive agreeing "decreased" reads correct the
baseline and the on-screen counters. (`5ef1e36`)

Also promoted `group_mode="fields"` (yesterday's positional-window grouping) to
the **default** for both live and VOD paths after the promotion gate passed —
see A/B below. Instant revert if a SMITE patch moves the HUD:
`KdaReader(group_mode="gaps")`.

## The A/B (promotion gate) — double confirmation

Rescanned 20 recordings (~5.5h footage) with fields mode and diffed against
your batch's gaps-mode output:
- **Primary detections: identical on all 20.** Zero regression. Gate passed.
- The only diffs were the merged-key fix (yesterday's `deb0f88`) doing its job:
  10 recordings gained back trade deaths/assists the old merge silently
  swallowed — including both cases I autopsied yesterday, at exactly the
  predicted timestamps (Horus death@226.8s, Atlas-101 death@1062.5s).

## Your offstream ranked sessions are now diagnosis sessions (`adfb15d`)

The detector got a **flight recorder**: every decision (event, reject,
suppressed phantom, rebaseline, reset) is journaled to
`data/kda_sessions/<timestamp>/` with the KDA-strip crop saved for each
anomaly. Zero configuration — it runs whenever detection runs, live or not.

After you play with your girlfriend, just run:

```
py tools\kda_session_report.py
```

You get a timeline of everything the detector decided, plus a verdict like
*"3 phantom reads caught by the confirm gate before firing — these would have
been live misfires before 7/10"*. The suppressed-phantom frames are saved
PNGs — every misread the gate catches becomes labeled evidence, so if
anything ever still misbehaves we'll have the exact frame that did it.

## Commits

| commit | what | verification |
|---|---|---|
| `5ef1e36` | delta confirm + rebaseline recovery + fields default + `--group-mode` A/B flag | new test suite drives the REAL detection loop with scripted reads: confirmed kill, phantom suppression, flicker, poison recovery, multikill-through-gate — all green; 20-recording A/B |
| `adfb15d` | flight recorder + session report tool | report timeline of the test-suite session matches the script exactly |
| *(uses yesterday's `deb0f88`)* | merged-key + fields groundwork | committed on main yesterday |

## Brainstorm — ranked

1. **Overlay resync on rebaseline** — when a correction fires, push the fixed
   KDA to the on-stream overlay immediately instead of waiting for the next
   event. Small; the listener plumbing already exists.
2. **Digit-template audit** — templates enrolled before tonight's confirm gate
   may include garbage from phantom frames. One-off tool: re-match every
   template against the 122-frame corpus, quarantine the ones that never win
   cleanly. Small-medium; directly improves read quality.
3. **Rescan favorite recordings** for the `merged` key (your 7/9 batch predates
   it): `py tools\extract_events.py recordings\<God> --overwrite` per folder
   you care about. Batch-sized but optional — only affects markers/chapters
   completeness on trades.
4. **Kill-feed cross-confirmation** (bigger): read SMITE's kill-feed banner as
   a second independent signal; fire instantly when both agree, hold 1 read
   when they disagree. Would recover the 0.8s latency the confirm gate added.
5. **Detector health in /health** — surface per-session counts (phantoms
   caught, rebaselines) on the dashboard badge so drift is visible weekly.
6. **One-button edit-ready import** (carried from last night, still the best
   video-pipeline win): fold markers + video kit into resolve_import.py.
7. **OBS replay-buffer auto-clip on kill** — now that kill events are
   confirmed-reliable, trigger OBS's replay buffer save on multikills for
   instant clips. Fun, medium.
8. **A≥10 watchlist** (carried): first double-digit-assist recording tests the
   strip-edge clip risk.

## Notes

- The running bot was never touched; economy.db opened read-only throughout.
- Worktree junctions (god art, digit templates) + A/B hardlinks are scratch —
  `git worktree remove` after merge cleans the tree; A/B files live in my
  session scratchpad and vanish with it.
- Keepawake sentinel deleted at end of run — normal sleep policy restored.
