# Overnight Report — night of July 1 → 2, 2026

Good morning! Everything below happened on branch **`overnight-2026-07-01`**
in an isolated worktree at `C:\Projects\HatmasBot-overnight`. Your main
checkout was never touched, nothing was pushed, and the bot was never
started. Review this file, then merge whatever you like:

```
cd C:\Projects\HatmasBot
git merge overnight-2026-07-01        # take everything
# or cherry-pick individual commits from the table below
```

**First: re-enable PC sleep** (I disabled it for the run):
`powercfg /change standby-timeout-ac 30` and
`powercfg /change hibernate-timeout-ac 180` (or your preferred timeouts).

---

## TL;DR

**22 fix commits**: 1 security hole, 7 money-path integrity fixes, 3
"whole-bot freezes" fixes, crash-safe state persistence across every plugin,
1 truncated-file recovery, 3 test-suite repairs, and a new regression suite.

**Everything verified**: all five runnable test suites exit 0 on the branch
tip (economy harness, trading hardening 4/4, web session 11/11, web trade
20/20, priority request 18/18), all 73 core+plugin modules import cleanly,
and every edited file passed the repo's py_compile/encoding checks. (The
KDA fixture test isn't runnable in the worktree — its digit templates live
in the gitignored `data/`.)

## The five you should read first

1. **SECURITY — whisper bypass of mod-only commands (`e7b5388`).** The
   whisper command path never checked `mod_only`. Any viewer could whisper
   the bot `!spin`, `!godclear`, `!poolclear`, `!scene`, etc. and they would
   execute. Chat has always enforced this; whispers now do too.

2. **`plugins/economy/plugin.py` was truncated on disk (`cc92d8b`).** The
   file ended mid-comment-word — the exact Edit/Write desync HATMASBOT.md
   warns about, shipped since at least the v2.5 catch-up commit. It still
   compiled (a docstring is a valid function body), so `cleanup()` has
   silently been a **no-op on every shutdown**: backfill task never
   cancelled, MixItUp session never closed. Body reconstructed. I swept
   every .py file for the same signature; songrequest.py was only missing
   its trailing newline (logic intact, restored in `c6dc5db`).

3. **Two "whole bot freezes" bugs.** `@HatmasBot` mentions used the *sync*
   Anthropic client (`dfddff4`) and highlighted-message TTS ran gTTS's
   blocking HTTPS call inline (`bb2d860`) — each froze the entire event
   loop (chat, kill detection, overlays, both web servers) for the full
   network round-trip. Both now run off-loop.

4. **Trades could strand or duplicate money on partial failure**
   (`6577e80` + `f83e297`). If the portfolio write failed after MixItUp
   deducted hats, the viewer paid and got nothing (the old docstring
   admitted this). Buys now fully unwind (shares + refund), sells restore
   shares if the credit fails, a per-user lock closes the concurrent
   double-spend window across chat + website, and sell payouts round
   exactly. Covered by 4 new regression tests (`b54d3dc`).

5. **Match settlement had a double-count crash window (`11bd4aa`).** A
   mid-settlement commit could persist a match's W/L/KDA stat bump without
   the dedup claim; the next backfill would settle the same match again
   and permanently skew that god's price inputs. Settlement now lands in
   one atomic commit.

## All commits (oldest first)

| Commit | What | Risk |
|--------|------|------|
| `0c8d3d5` | test_economy.py hung forever at exit (aiosqlite thread never released after the shared-DB refactor) | none |
| `fc5bee5` | token_manager: atomic token writes (corrupted file = manual re-auth, since Twitch rotates refresh tokens) | low |
| `6577e80` | Trading: per-user lock, refund/restore on partial failure, exact sell rounding | low-med, tested |
| `08def9b` | Dividends: only ledger credits MixItUp accepted; all-failed dividends stay unclaimed so catch-up retries | low |
| `e7b5388` | **Security: enforce mod_only on whispered commands** | low |
| `e4b63f0` | auth.py: atomic token writes | none |
| `7aa355d` | New `core/atomic_io.py`; every plugin JSON state write is atomic (godreq queue incl. paid entries, song queue, KDA state, jackpot, …) | low, mechanical |
| `b54d3dc` | tests/test_trading_hardening.py (4 regression tests) | none |
| `acdb230` | overlay_manager: WS broadcast crashed if a client connected/dropped mid-send; token_manager.close() awaits its task | low |
| `bb2d860` | TTS: gTTS off the event loop | low |
| `dfddff4` | claude_chat: AsyncAnthropic | low |
| `6e4f47e` | test_priority_request.py hung at exit; now exits 0 (18/18) | none |
| `e7ce6bf` | Sell-"all" (chat + web) rounds — no dust shares left behind | low |
| `ba7d8ef` | Spin fulfillment no longer announces "requested by ." | none |
| `4538021` | Voiceline redemptions refund when the god has no voice-line files (parity with the no-god refund) | low |
| `11bd4aa` | Atomic match settlement (double-count crash window) | low-med, tested |
| `3059510` | Boot banner v2.5 → v2.8 | none |
| `cc92d8b` | **Reconstruct truncated EconomyPlugin.cleanup()** | low |
| `c6dc5db` | songrequest.py trailing newline | none |
| `f83e297` | Buy unwind also removes granted shares, not just the refund | low, tested |
| `b08848e` | Gamble: claim cooldown before the awaited balance fetch (two rapid `!gamble all` could double-bet one balance) | low |
| `c886ae7` | tracker.gg calls serialized onto one executor thread (shared curl session isn't thread-safe) | low |

## Verified-but-NOT-fixed — your call

1. **`ECONOMY_POSITION_LIMIT` is still not enforced** in `execute_buy`
   (known TODO, HATMAS_MARKET_AIRTIGHT_DESIGN.md:279). Say the word.
2. **Shared-DB implicit transactions.** All plugins share one aiosqlite
   connection with `execute(); commit()` patterns; interleaved coroutines
   can commit each other's half-done multi-statement sequences (nomination
   approval, and a trade landing mid-settlement). Tonight's atomic-
   settlement fix shrank the worst window ~1000x; the thorough fix is a
   small `db_transaction()` async-lock helper. Medium refactor, happy to
   do it on request.
3. **Partial dividend failure**: if only *some* MixItUp credits fail, the
   failed holders aren't retried (the all-failed case now retries).
4. **nsfw_check fails open** (album art shows if the vision API errors).
   Flip to fail-closed if you'd rather block art during outages.
5. **Gamble win/loss `_adjust_balance` results aren't checked** — a failed
   MixItUp write still announces the result in chat (pool stays virtual,
   no ledger, so impact is cosmetic-ish).
6. **claude_chat history grows unbounded** per user (only the last 10
   messages go to the API, but the JSON keeps everything forever).
7. **obs.py uses the sync obsws-python client inline** — each OBS call
   blocks the loop for its round-trip (localhost, so ~ms; it's been fine
   live, but an executor wrap would be strictly better).
8. **Fair-value quirk** (by design, looks odd on stream): a god priced far
   below fair value can *rise* sharply on a LOSS because settlement snaps
   the price to the formula (seen in the test harness: Loki 30 → 78 on a
   1/10/1 loss).
9. **Fire-and-forget `asyncio.create_task`** without keeping references —
   codebase-wide pattern, low practical risk.

## Process notes

- Your local fleet (qwen3.5:35b on the 5090) did a first-pass review of all
  106 Python files (harness + full per-file findings preserved in the
  session scratchpad). I verified every finding against source before
  acting: roughly 80% were false alarms or context-blind — consistent with
  TODO.md's "verify-before-trusting" warning — but the areas it flagged led
  me to the whisper hole, both event-loop freezes, the WS broadcast crash,
  and the gamble race. One review (factorio/catalog.py) degenerated into a
  repetition loop; treat that model as needing a repetition-penalty tweak
  for long reviews.
- Test suites were also repaired as a side effect: **three of your existing
  suites could never finish unattended** (hung at exit on leaked aiosqlite
  threads) — all now exit cleanly, so they're Stream-Deck/CI-able.
- Tone-rule spot check (TODO nice-to-have): no emojis anywhere in chat
  strings or overlays. Boot banner version drift fixed.
- StreamingSpaceGame untouched, per instructions.
- Worktree cleanup after merging: `git worktree remove C:\Projects\HatmasBot-overnight`.
