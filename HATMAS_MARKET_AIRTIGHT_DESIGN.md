# Hatmas Market — Airtight Economy Design

**Status:** Approved, ready to implement
**Author:** Claude + Hatmaster
**Date:** May 23, 2026
**Companion to:** `HatmasBot.md` (sections "God Economy / Hatmas Market" and "Match Settlement & Periodic Backfill")

---

## 0. Decisions from Hatmaster review

- **Dividend stays at match-start**, gated on tracker.gg confirmation (not portrait). The +5% "match is real, here's a blip" feel is preserved.
- **Backfill can also pay the dividend** if it picks up a match whose start-dividend was never paid AND the broadcaster is currently live. Bookkeeping via a new `dividends.match_id` column so we don't double-pay.
- **Transaction fees are removed entirely.** `ECONOMY_TRANSACTION_FEE` is deleted from config and from `execute_buy` / `execute_sell`. This brings the code in line with `HatmasBot.md`'s claim of "No transaction fees — removed to keep it fun" (which the code currently contradicts).
- **Cosmetic price snap-back on public-website refresh is acceptable.** Live viewers see flashes during practice; refreshed pages show the persisted price. No special handling required.
- **Free shares on backfill-settled live matches go to current chatters**, not a snapshot of chatters from match-start. Can be revisited later.
- **Voicelines stay disabled** (per existing `ECONOMY_VOICELINES_ENABLED=False`). No change.

---

## 1. Problem

The Hatmas Market economy currently conflates two different signals as if they were the same:

1. **The overlay's view of "what god is on screen right now"** — produced by the OBS-driven portrait matcher plus the local KDA template reader. Designed to be fast, optimistic, and work everywhere (jungle practice, custom games, casual modes tracker.gg doesn't fully cover).
2. **The authoritative record of "what real match the broadcaster just played"** — produced by tracker.gg, with a real `match_id`, a verified win/loss, and a canonical final KDA.

The overlay system is correctly cosmetic in tone (it's allowed to lag, jitter, even occasionally misidentify). The economy is *not* cosmetic — it moves real Hat balances, real share counts, and feeds the public website. Today the two share the same callback list, which means anything that triggers an overlay also triggers an economic event.

### Concrete leaks

| # | Leak | Consequence |
|---|------|-------------|
| 1 | `set_god_from_portrait()` in `plugins/smite.py` fires `on_god_detected` callbacks with no `is_in_match` guard. `economy.on_god_detected` calls `_pay_dividend()` unconditionally. | A jungle-practice session, a custom game, or even a lobby false-positive that holds for 3 frames pays a real 5% Hat dividend to every holder of that god. Permanent debit. |
| 2 | The same callback sets `economy._match_active = True`. Kill-detector ticks (`on_kill`/`on_death`/`on_assist`) then mutate `god_prices.price` and append rows to `price_history`. | Practice-session ticks pollute the canonical price and the sparkline. Viewers can `!buy`/`!sell` against the polluted price; their `avg_cost` gets poisoned. The next real-match settlement overwrites the price (via `calculate_fair_value`), but only after viewers may already have realized PnL on the leak. |
| 3 | `settle_match()` accepts `match_id=None` from the live path with only a `print` warning (`economy.py:1313`). | Now that backfill exists, the same match will likely be re-settled by backfill later. The live row didn't make it into `processed_matches`, so dedup can't catch it. Risk of double-counting one match's W/L and KDA in `god_prices`. |
| 4 | Backfill (`source="backfill"`) skips *all* live-only side effects, including dividends. | If you're streaming live and the broadcaster never resolves the prediction (or doesn't have time to), backfill settles the price but no holder gets a dividend for that match, and no current chatter gets a free share. The "social" half of a real, live match is silently dropped. |

There are also some smaller related items:
- The simulator (`simulate_game()`) feeds `on_god_detected` directly. After the fix it needs to bypass the new gating or feed through a synthetic-authoritative entry point so test runs still exercise the full pipeline.
- The "force_end_match" path (kill detector sees non-gameplay) clears `_match_active` correctly, but the dividend and any leaked ticks before that point are already persisted.

---

## 2. Design principles

1. **Overlays can lie. The economy cannot.** Anything that touches Hat balances, share counts, `god_prices.price`, `price_history`, `portfolios`, or `processed_matches` must be triggered exclusively by tracker.gg-verified events with a real `match_id`.
2. **Tracker.gg is the only source of truth for K/D/A and outcome.** Local KDA reads from the HUD are good enough for live celebration overlays. They are not good enough to settle a price.
3. **Live latency matters for the celebration, not for correctness.** It's acceptable for a dividend or free-share distribution to fire up to `SMITE2_BACKFILL_INTERVAL` (5 min default) after a match ends, as long as the broadcaster is still live when it lands.
4. **Both paths fire the same math.** Live settlement and backfill settlement run identical code (`settle_match()`). They differ only in *which side effects* fire, and that decision is data-driven (am I live right now?), not source-driven.
5. **No dedup-bypass paths.** Every settlement that touches the DB must record into `processed_matches`. No more `match_id=None` shortcut.

---

## 3. The new event contract

### 3.1 In `plugins/smite.py`

Split the existing `on_god_detected` callback list into two:

```
self._on_god_detected_callbacks         # existing — "visual" signal
self._on_match_confirmed_callbacks      # NEW — "authoritative" signal
```

- `set_god_from_portrait()` (the portrait-matcher entry point) continues to fire `_on_god_detected_callbacks` only.
- The tracker.gg poll loop, on the SEARCHING → FOUND transition where it has both `is_in_match=True` AND a real `match_id`, fires *both* lists: first the visual list (for parity with portrait-only flow — voicelines, godrequest, OBS title, etc., subscribers don't care which path produced the god), then `_on_match_confirmed_callbacks` with a payload containing `match_id`, `god_name`, and the team color.

Public API helpers:

```python
def on_match_confirmed(self, callback):
    """Subscribe to authoritative match-start events (tracker.gg verified)."""
    self._on_match_confirmed_callbacks.append(callback)
```

`force_end_match()` keeps firing `_on_match_end_callbacks`. No change there.

### 3.2 In `plugins/economy.py`

Drop the existing `on_god_detected` subscription. Replace with `on_match_confirmed`. Rename internally for clarity:

```python
async def on_match_confirmed(self, data: Dict):
    """Tracker.gg has confirmed a real match start. THIS is when the
    dividend fires and the match-start price is captured."""
    match_id = data["match_id"]
    god_name = data["god"]
    # ... existing _pay_dividend + _ensure_god_exists + state setup
```

New internal flag split:

```python
self._match_god_visual: Optional[str] = None      # set by portrait, OK to be wrong
self._match_authoritative: bool = False           # only True after tracker.gg confirms
self._match_id: Optional[str] = None              # tracker.gg match_id, never None when _match_authoritative is True
self._match_start_price: float = 0.0
self._match_kda_cosmetic = [0, 0, 0]              # from kill detector; for OBS overlays only
```

### 3.3 KDA tick path (cosmetic only)

`on_kill` / `on_death` / `on_assist` keep firing on every detected event but:

- They never write to `god_prices.price`.
- They never insert into `price_history`.
- They never update `_session_changes`.
- They compute a *cosmetic* price for the overlay locally:
  ```python
  cosmetic_price = self._match_start_price * (1 + ECONOMY_KILL_TICK) ** self._match_kda_cosmetic[0] \
                                          * (1 + ECONOMY_DEATH_TICK) ** self._match_kda_cosmetic[1] \
                                          * (1 + ECONOMY_ASSIST_TICK) ** self._match_kda_cosmetic[2]
  ```
- They emit `god_stock_update_kd` with the cosmetic price, exactly as today, so the live match overlay and ticker behave identically. The public website's `/ws/god/{name}` socket gets the cosmetic price too — viewers see flashes during practice, but the persisted price doesn't move.
- They fire in *both* authoritative and visual modes (cosmetic overlay works in jungle practice as well as real matches).
- They emit the `economy_big_spike` / `economy_big_crash` overlay events as today.

### 3.4 Dividend at match-start, with backfill catch-up

The dividend stays at match-start, but is now gated on tracker.gg confirmation plus a "broadcaster is live" check. If the live path missed it (bot offline at match-start), backfill can catch up at settlement time — but only if the broadcaster is live then.

**At match-start (live path):**
```python
async def on_match_confirmed(self, data: Dict):
    match_id = data["match_id"]
    god_name = data["god"]
    await self._ensure_god_exists(god_name)
    self._match_authoritative = True
    self._match_id = match_id
    self._match_god_visual = god_name
    self._match_start_price = self._prices[god_name]
    self._match_kda_cosmetic = [0, 0, 0]
    if self._is_broadcaster_live():
        await self._pay_dividend(god_name, match_id=match_id)
    self._emit_overlay_event("economy_god_detected", { ... })
```

**At settlement (live or backfill), inside `settle_match`:**
```python
# Side-effects fire whenever broadcaster is currently live.
if self._is_broadcaster_live():
    # Backfill catch-up: if no dividend was recorded for this match
    # (e.g., bot was offline at match-start), pay it now.
    if not await self._dividend_already_paid(match_id):
        await self._pay_dividend(god_name, match_id=match_id)
    await self._distribute_free_shares(god_name)
    self._emit_overlay_event("match_end_economy", { ... })
    await self._emit_leaderboard()
    # voicelines remain disabled per ECONOMY_VOICELINES_ENABLED
```

**Bookkeeping:** add a `match_id TEXT DEFAULT NULL` column to the existing `dividends` table. `_pay_dividend` writes the match_id with the dividend row; `_dividend_already_paid(match_id)` is a one-line SELECT. Old historical dividend rows have NULL match_id — they predate this change and were already paid, so they're harmless.

**Other `settle_match` changes:**

1. **Hard reject `match_id is None`.** The `print("[Economy] WARNING: live settle_match has no match_id …")` path becomes:
   ```python
   if not match_id:
       print(f"[Economy] settle_match({source}): refusing to settle without match_id")
       return False
   ```
2. **Replace the `source == "live"` side-effect gate with `_is_broadcaster_live()`.** Settlement math runs identically either way; only the celebration side-effects are gated by live status. Helper:
   ```python
   def _is_broadcaster_live(self) -> bool:
       ss = self.bot.plugins.get("stream_status")
       if not ss:
           return False
       return bool(ss.get_status().get("is_live"))
   ```
3. The settlement record stores `was_live_at_settle INTEGER` on `processed_matches` so logs and the dashboard can show which matches actually triggered the celebration suite.

### 3.5 Backfill path

`backfill_recent_matches()` is unchanged structurally, but:

- `settle_match(source="backfill", ...)` now consults `_is_broadcaster_live()` for side-effect gating instead of hardcoding "skip everything on backfill."
- Result: if you're live when backfill picks up a match, the celebration overlay fires, free shares distribute to *current* chatters, and the dividend pays **if it wasn't already paid at match-start by the live path** (checked via `_dividend_already_paid(match_id)`). The normal case is: live path was active at match-start and already paid the dividend, so backfill skips it. The catch-up case is: bot was offline at match-start, dividend was never paid, broadcaster is live now → backfill pays it.
- The voiceline trigger stays disabled (per `ECONOMY_VOICELINES_ENABLED=False` today) — re-enable it later via the existing config flag, no design change needed.
- If you're offline (post-stream backfill of matches you played in the morning, or matches you played after closing the bot), backfill silently corrects `god_prices.price` and `processed_matches` only. No dividends to holders for matches you weren't streaming. This matches the principle: dividends are a thank-you to people watching you play, not a passive yield.

### 3.6 Race policy: implicit, not explicit

Because both paths now run identical math AND identical side-effect gating, the dedup PK on `processed_matches.match_id` resolves the race correctly without further policy.

- If you resolve the prediction within ~5 min of match end, the live path settles first. Dedup blocks backfill from re-running.
- If you miss the window, backfill settles first. Dedup blocks the live path's later `on_match_result` from re-running. Stream viewers still get the celebration because `_is_broadcaster_live()` will return True.
- If the bot is offline during a match, only backfill runs at next launch. No celebration because you aren't live.

No new config knobs, no timing windows to tune. The dedup table does its job.

---

## 4. File-by-file change list

### `plugins/smite.py`

- Add `self._on_match_confirmed_callbacks = []` to `__init__`.
- Add `def on_match_confirmed(self, callback)` public method.
- In the SEARCHING → FOUND transition (line 1041), after firing the existing `_on_god_detected_callbacks`, also fire `_on_match_confirmed_callbacks` with `{"match_id": match_id, "god": god_info["name"], "team": god_info.get("team")}`.
- No change to `set_god_from_portrait()`.
- No change to `force_end_match()` (it already fires `_on_match_end_callbacks`, which the economy uses to clear live state).

### `plugins/economy.py`

- Rename `on_god_detected` → `on_match_confirmed`. Update internals to use `match_id` from the payload instead of starting a "match" off a name-only god dict. Pay dividend here (gated on `_is_broadcaster_live()`).
- Update `_pay_dividend(god_name, *, match_id: Optional[str] = None)` to accept a match_id and record it on the `dividends` row.
- Add `_dividend_already_paid(match_id: str) -> bool` helper — one-line SELECT against `dividends.match_id`.
- Rewrite `on_kill`, `on_death`, `on_assist` to never mutate the DB. They update `_match_kda_cosmetic` and emit overlay events with a locally-computed `cosmetic_price`. They run whenever a god is visually identified, authoritative or not.
- Add `_is_broadcaster_live()` helper.
- In `settle_match()`:
  - Hard-reject `match_id is None`.
  - Replace `if source == "live":` with `if self._is_broadcaster_live():`.
  - Inside that branch, call `_pay_dividend(god_name, match_id=match_id)` **only if `_dividend_already_paid(match_id)` is False** — backfill catch-up logic.
  - Then call `_distribute_free_shares` and emit `match_end_economy` / leaderboard as today.
  - Add `was_live_at_settle INTEGER NOT NULL DEFAULT 0` column to `processed_matches` (migration: idempotent ADD COLUMN).
- **Remove transaction fees entirely.** In `execute_buy`: drop the `fee = int(math.ceil(hat_amount * ECONOMY_TRANSACTION_FEE))` line and the `net_amount` adjustment. `shares = hat_amount / price`. `fee=0` in the inserted transactions row. Same for `execute_sell`: `net_received = int(gross_value)`, `fee=0`.
- In `cleanup` / `simulate_game`: route simulator through a new `_simulate_authoritative_match()` helper that supplies a fake `match_id` (e.g., `"sim-<uuid>"`) so the full pipeline including settlement + side effects runs end-to-end.
- Delete the dead `source == "live"` branches once the gate is rewritten.

### `main.py`

- Replace `smite_plugin.on_god_detected(economy.on_god_detected)` with `smite_plugin.on_match_confirmed(economy.on_match_confirmed)`.
- Keep `smite_plugin.on_match_end(economy.on_match_end)` and `smite_plugin.on_match_result(economy.on_match_result)` as-is.
- Keep the `kd.add_kill_listener` / `add_death_listener` / `add_assist_listener` lines as-is (the listeners themselves now no-op the DB and just animate overlays).
- Optionally subscribe `economy.on_god_detected_visual` (a new lightweight method that just tracks `_match_god_visual` so the cosmetic price math knows which god to animate during practice).

### `core/config.py`

- **Remove `ECONOMY_TRANSACTION_FEE`** (and its import in `plugins/economy.py`). Fees are gone. Trading is fee-free, matching what `HatmasBot.md` already claims.
- All other constants remain: `ECONOMY_DIVIDEND_RATE`, `ECONOMY_KILL_TICK`, `ECONOMY_DEATH_TICK`, `ECONOMY_ASSIST_TICK`, `ECONOMY_FREE_SHARE_COUNT`, `SMITE2_BACKFILL_INTERVAL`.

### `HatmasBot.md`

- Update the "God Economy / Hatmas Market" section to reflect the new event contract (visual vs authoritative) and the move of dividend to settlement time.
- Update the "Match Settlement & Periodic Backfill" section to remove the "live-only side effects" framing and replace with "side effects fire whenever broadcaster is live."

---

## 5. Migration

Two idempotent schema changes, both via the existing `_migrate_*` pattern in `economy.py`:

```sql
ALTER TABLE processed_matches ADD COLUMN was_live_at_settle INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dividends         ADD COLUMN match_id           TEXT    DEFAULT NULL;
```

Existing rows backfill correctly: `was_live_at_settle=0` means "we don't know, treat as historical"; `match_id IS NULL` means "this is a pre-feature dividend with no match attribution, so backfill catch-up won't try to re-pay it" (the catch-up SELECT keys on a specific match_id, so NULL rows are invisible to it).

Deploy timing notes:
- **Dividend behavior is unchanged at deploy time** because we kept it at match-start. Any in-flight live match will pay its dividend exactly once, as today.
- **Transaction fees stop applying immediately.** Buys/sells in flight at deploy time complete with the new (zero) fee. No half-state risk because each command is a single async function.
- **Live ticks stop mutating the DB immediately.** A match in progress at deploy time keeps animating overlays but stops moving the persisted price. The final settlement still runs as today and writes the correct fair-value price.

No backfill of historic price data is needed — the polluted-by-leak prices have already been overwritten by subsequent real-match settlements (or, if the affected god hasn't had a real match since the leak, will be on the next one).

Optional cleanup: a one-shot tool `tools/sanitize_price_history.py` to scrub `price_history` rows whose `event` was a kill/death/assist tick from a non-tracker.gg match. Hard to detect retroactively without a `match_id` column on `price_history` — probably not worth the effort. Skip.

---

## 6. Edge cases

- **Lobby false-positive on portrait.** The 3-consecutive-frame requirement plus the new "no economic effect until tracker.gg confirms" gate eliminates the only path where a lobby misread costs real Hats.
- **Tracker.gg confirms a different god than portrait.** Today, smite.py handles this by re-firing `on_god_detected` with the corrected name. After the change: the portrait fire happens first (cosmetic only). The corrected fire happens with `match_id` (authoritative). Economy state pivots cleanly to the correct god the moment tracker.gg confirms.
- **Tracker.gg never confirms (jungle practice, custom game).** No dividend, no free shares, no DB writes. Overlay animates with cosmetic ticks. `force_end_match` clears visual state when you exit.
- **Match finishes off-stream.** Backfill settles the price next launch. `_is_broadcaster_live()` returns False, no dividend, no free shares. The price record is correct; the social events don't fire because there's nobody watching.
- **Broadcaster goes offline mid-match.** Stream end → `is_live` flips to False. If the match-end backfill arrives a few minutes after stream end, no dividend / free shares (correct: nobody watching). The price still settles correctly.
- **Backfill picks up a match while live, then live path's `on_match_result` arrives.** Settlement dedup (`processed_matches`) blocks the second settle. Dividend dedup (`_dividend_already_paid(match_id)`) blocks any second dividend. Free shares already distributed. Live path is a silent no-op. ✓
- **Bot was offline at match-start, comes back online, backfill picks up the match while broadcaster is live.** No dividend was paid at start (bot was off). `_dividend_already_paid(match_id)` returns False. Backfill pays the dividend now. ✓
- **Live path paid dividend at match-start, then bot crashed mid-match, restarted, backfill picks it up.** Dividend already in DB with match_id; `_dividend_already_paid` returns True. Backfill skips the dividend, just settles. ✓
- **Simulator.** Runs through the new authoritative entry point with a synthetic `match_id="sim-<uuid>"`. Behaves identically to a real match for testing purposes. Cleanup tool: delete `sim-*` rows from `processed_matches`.

---

## 7. Out of scope (for this pass)

- Re-enabling voicelines. The gating change makes voiceline triggers a single-line uncomment when ready; the file-naming inconsistency that prompted the disable is unrelated.
- Position-limit enforcement (`ECONOMY_POSITION_LIMIT`). The constant exists in config but isn't enforced in the `execute_buy` path. Separate task.
- A `match_id` column on `price_history` for retroactive auditability. Worth it long-term, not blocking.
- Bot-account scrubbing in legacy data (`tools/purge_excluded.py` already exists).
- Snapshotting chatters at match-start (so free shares on a backfill-settled match go to the chatters who were watching when the match was played, not the chatters in chat now). Approved as a future improvement; current chatters is fine for now.
- Public-website real-time price updates over the `god_stock_update_kd` channel. The website keeps receiving cosmetic ticks during practice — viewers see flashes, refreshed pages show the persisted price. Snap-back on refresh is approved as acceptable.

---

*End of design doc. Approved for implementation.*
