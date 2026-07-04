# God Roster Report — 2026-07-04 (afternoon run)

**Ask:** `!godreq bastet` says "unknown god"; `download_god_icons.py` claims
everything is downloaded even though Bastet and Chronos just released. Fix it,
make `!godreq` robust for future releases, pre-stock SMITE 1 art, prioritize
new gods.

**Branch:** `godreq-roster-2026-07-04` — built ON TOP of `overnight-2026-07-04`
(it extends last night's god resolver). Merging this branch brings both.
If you'd rather merge the overnight branch alone first, it's untouched.

---

## Root cause (two stale lists, same freeze date)

1. `plugins/godrequest.py` had a **hardcoded 82-god list** from April 2026.
2. `download_god_icons.py` got its god list by parsing the **saved
   "Gods - SMITE 2 Wiki.html"** file on disk — also April. So it checked its
   82 gods, found all 82, and honestly reported "all downloaded."

Six gods had released since: **Ah Puch, Bastet, Chronos, Cu Chulainn, Horus,
Xing Tian**. Bonus latent bug: the filename parser's hand-maintained
`concat_map` didn't know the new concatenated names, so Cu Chulainn would have
come out as "Cuchulainn" and Xing Tian as "Xingtian" even after a manual
HTML re-save.

## What changed (6 commits)

| Commit | What |
|---|---|
| `09b881b` | **core/god_roster.py** — single source of truth. Bundled 88-god snapshot + `data/god_roster.json` cache with per-god `first_seen` dates + live refresh from wiki.smite2.com (curl_cffi; Cloudflare 403s plain urllib). Also bundles the frozen 130-god SMITE 1 catalog. Generic camel-splitting replaces `concat_map`. |
| `9ff1977` | Both download tools use the live roster (saved HTML is now just an offline fallback). New `--s1` mode, `--offline` flag, new-gods-first ordering. `tools/update_bundled_roster.py` re-bakes the snapshot. |
| `4e0f9e7` | godrequest wired to the roster + UX + auto-refresh (details below). |
| `01c2e84` | Card tool: marker-less filename patterns (Cu Chulainn / Xing Tian are hosted S1-style on the S2 wiki). |
| `228a81f` | `cu`/`xt` aliases, 19 new eval cases, `tools/test_god_roster.py` (11 tests). |
| `b39214c` | Review hardening: copy-on-merge; failed asset downloads self-heal on the 6h cycle. |

## How `!godreq` behaves now

- **`!godreq bastet`** → works. Resolves against the live roster; same for
  `!godrequest`, `!nominate` (after icon download), and the paid website flow
  (it validates through `godreq._match_god`).
- **New god releases** → a background task refreshes the roster daily
  (6h retry cadence). New gods become requestable immediately, their icon +
  card auto-download, and god_pool's list reloads — **no bot restart, no
  manual steps**. If the wiki hiccups at release time, the next cycle retries.
- **`!godreq bakasura`** (SMITE 1, not ported) → "Bakasura isn't in SMITE 2
  yet — hopefully soon! Try another god." instead of blaming spelling.
- **`!godreq posiedonn`** → "Did you mean Poseidon?"
- **OBS portrait fallback:** Custom God Icons → `data/god_icons/` →
  `data/god_icons_s1/`. A god released this morning gets a portrait tonight.

## Assets downloaded (into `C:\Projects\HatmasBot\data\`, gitignored)

- `god_icons/` 83 → **88** (all 6 new gods; Chronos/Cu Chulainn/Xing Tian art
  came from the fandom fallback — when the S2 wiki uploads proper (S2) icons,
  a `--force` run or the wiki_filename tracking will pick them up)
- `god_cards/` 84 → **88** (Cu Chulainn + Xing Tian at 750x1000, same 3:4)
- `god_icons_s1/` **130/130** — full SMITE 1 catalog (per your idea: stocked
  in advance, so future ports have instant art)
- `god_cards_s1/` **129/130** — Ix Chel has no default card on the fandom
  wiki at all (only skin cards); harmless, S2 art will exist if she ports
- `god_roster.json` — live cache seeded today

Kept **strictly separate** on purpose: `data/god_icons/` feeds the portrait
matcher's fingerprints, so SMITE 1 art never goes in there (false-match risk).

## Verification

- `tools/test_god_roster.py` — **11/11** (merge semantics, first_seen
  preservation, corrupt/tiny-cache fallback, S1-only exclusion incl.
  Mulan→Hua Mulan)
- `tools/eval_god_resolver.py` — **127/127, 0 junk false-positives**
  (was 110; added bastet/chronos/cuchu/xt/… + bakasura/cthulhu as
  must-NOT-resolve)
- All five regression suites green (economy, trading_hardening, web_session,
  web_trade, priority_request)
- Live smoke: resolver hits for all 6 new gods and their shorthands;
  `_find_god_image` chain verified incl. S1 fallback
- Local reviewer pass: 10 findings, 8 false alarms, 2 fixed (copy-on-merge,
  download retry)

## Notes / possible follow-ups (not done)

- The bot could **announce** a newly detected god in chat/Discord ("Bastet
  just hit SMITE 2 — !godrequest her!"). Skipped — didn't want the bot
  posting unprompted; say the word and it's a 5-line change.
- `plugins/economy/god_names.py` still builds its index from god_prices +
  Custom God Icons (deliberate: money path, pre-buy semantics). Unchanged.
- The saved "Gods - SMITE 2 Wiki.html" no longer needs re-saving, ever.
