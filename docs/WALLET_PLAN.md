# Wallet plan: Hats and God Tokens without MixItUp

Status: IMPLEMENTED 2026-09-13 (`core/wallet.py`, `plugins/wallet.py`,
`plugins/economy/hats.py`, `tools/import_mixitup.py`, `tools/wallet_audit.py`,
tests/test_wallet.py; see "v2.17 Update" in HATMASBOT.md).

Deviations from the plan as written:
- Rates: James chose 5 hats/min (MixItUp paid 1/min) -> tick every 5 min,
  25 hats per tick. Sub multiplier and chat bonus only apply to viewers
  who chatted since the last tick (sub status rides on chat badges;
  Helix's chatters list does not carry it). Defaults: multiplier 1.0,
  bonuses 0.
- `wallet_balances` has no display_name column (names come from users).
- Event bonuses: sub/resub/gift/raid hooks wired; bits and first-message
  keys exist but have no hook yet.
- `mod_grant` / `mod_take` ship as !givehats / !takehats.
- The MixItUp import ref is `miu:<user id>:<asset>` (one row per asset).
Depends on `USER_IDENTITY_PLAN.md`: all viewer keys are `user_uuid`
(users.uuid); the identity work lands first.
Owner of the tables: a new `core/wallet.py` module that registers its
schema with `core/db.py` (same pattern as `core/account_linking.py`),
so every consumer shares the one `data/economy.db` connection.

## Principles

- **One key: the user UUID.** Viewers are identified by `users.uuid` from
  `USER_IDENTITY_PLAN.md`, so a YouTube-only viewer has a wallet exactly
  like a Twitch viewer, and linking accounts merges balances by the
  rules in that document. Every table below keys on
  `user_uuid`.
- **Whole units.** Hats and God Tokens are `INTEGER`. MixItUp's API only
  ever accepted integers, so every existing caller already rounds.
- **Balances are derived, the ledger is truth.** `wallet_balances` is a
  cache kept in the same transaction as each `wallet_ledger` insert.
  `SUM(delta)` over the ledger must always equal the balance; a tool
  checks that (`tools/wallet_audit.py`).
- **Every change has a reason and, when it comes from an event, a
  reference.** The `(reason, ref)` pair is unique, which is what makes
  sub awards, dividends, and the MixItUp import safe to replay.
- **No negative balances.** Enforced by a CHECK constraint, and every
  debit is a single `UPDATE ... WHERE amount >= ?` so two concurrent
  spends cannot both succeed.
- **Timestamps** are UTC `TEXT` from `datetime('now')`, matching the
  rest of `economy.db` (bingo's REAL epochs stay in `bingo.db`).

## New tables

### wallet_balances: current amount per viewer per asset

| column | type | notes |
|---|---|---|
| user_uuid | TEXT NOT NULL | users.uuid |
| asset | TEXT NOT NULL | `hats` or `god_token` (CHECK) |
| amount | INTEGER NOT NULL DEFAULT 0 | CHECK (amount >= 0) |
| created_at | TEXT NOT NULL DEFAULT (datetime('now')) | first row for this viewer/asset |
| updated_at | TEXT NOT NULL DEFAULT (datetime('now')) | last ledger write |
| last_earned_at | TEXT | last passive watch credit (hats only); the earn loop uses it so a restart never double-pays a tick |

Primary key `(user_uuid, asset)`. Display names come from `users`. Index on `(asset, amount DESC)` for the
leaderboard and the site's top-holders panel.

### wallet_ledger: append-only history of every change

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | |
| user_uuid | TEXT NOT NULL | users.uuid |
| asset | TEXT NOT NULL | `hats` / `god_token` |
| delta | INTEGER NOT NULL | signed; never 0 |
| balance_after | INTEGER NOT NULL | the cached balance after this row, for fast statements and audits |
| reason | TEXT NOT NULL | see reason list below |
| ref | TEXT | what caused it: `transactions.id`, tracker match id, bingo round id, EventSub event id, `miu:<userId>` for the import |
| actor | TEXT NOT NULL DEFAULT 'system' | `system`, a mod's user_uuid for grants/takes, `migration` |
| channel | TEXT NOT NULL DEFAULT 'chat' | `chat` / `web` / `system`, mirrors `transactions.channel` |
| note | TEXT | free text, e.g. the god name, the dice roll |
| created_at | TEXT NOT NULL DEFAULT (datetime('now')) | |

Indexes: `(user_uuid, created_at DESC)` for "my history"; `(reason,
created_at DESC)` for dashboards; **partial UNIQUE `(reason, ref)` WHERE
ref IS NOT NULL** for idempotency.

Reasons (a CHECK constraint, so a typo cannot create a new kind):

| reason | delta | ref | written by |
|---|---|---|---|
| `buy` | minus | transactions.id | economy trading |
| `sell` | plus | transactions.id | economy trading |
| `refund` | plus | transactions.id | trading rollback when the share grant fails |
| `dividend` | plus | match id + god | economy dividends |
| `gamble_win` / `gamble_loss` | either | null | gamble |
| `bingo_card` | minus | round id + card seq | bingo card purchase |
| `bingo_prize` | plus | round id | bingo payout |
| `godreq_spend` | minus (god_token) | queue entry id | god request |
| `sub_award` | plus (god_token) | EventSub message id | god request, on sub |
| `donation_award` | plus (god_token) | donation id or null | dashboard donation hook |
| `watch` | plus | tick id | passive earning loop |
| `chat_bonus` / `sub_bonus` / `raid_bonus` / `bits_bonus` / `first_msg_bonus` | plus | EventSub message id | passive earning events |
| `mod_grant` / `mod_take` | either | null | `!givehats` / `!takehats`, actor = the mod |
| `link_merge` | either | absorbed user_uuid | account merge (USER_IDENTITY_PLAN rules) |
| `migration` | plus | `miu:<userId>` | the MixItUp import |
| `adjust` | either | null | `tools/wallet_audit.py --fix`, actor = who ran it |

### wallet_earn_ticks: one row per passive-earning pass

Answers "why did I not get hats" without reading logs.

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | the `ref` used by `watch` ledger rows |
| ticked_at | TEXT NOT NULL DEFAULT (datetime('now')) | |
| was_live | INTEGER NOT NULL | 0/1 from the stream status plugin |
| chatters | INTEGER NOT NULL | size of the Helix chatters list |
| credited | INTEGER NOT NULL | viewers who received hats |
| skipped | INTEGER NOT NULL | excluded bots, too soon since last_earned_at |
| hats_total | INTEGER NOT NULL | sum paid this tick |
| error | TEXT | Helix failure text, else NULL |

### wallet_imports: one row per MixItUp import run

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | |
| source | TEXT NOT NULL | `mixitup` |
| dry_run | INTEGER NOT NULL | 1 = report only, nothing written to balances |
| started_at / finished_at | TEXT | |
| users_seen | INTEGER NOT NULL DEFAULT 0 | every user MixItUp listed |
| users_imported | INTEGER NOT NULL DEFAULT 0 | rows that produced a ledger entry |
| hats_total / tokens_total | INTEGER NOT NULL DEFAULT 0 | what was credited |
| notes | TEXT | skipped bots, pagination stops, errors |

### wallet_import_rows: the per-user reconciliation record

Kept so MixItUp can stay installed as the rollback and you can compare
the two side by side weeks later.

| column | type | notes |
|---|---|---|
| import_id | INTEGER NOT NULL | wallet_imports.id |
| miu_user_id | TEXT NOT NULL | MixItUp's internal id |
| platform | TEXT NOT NULL | `Twitch`, `YouTube`, ... only Twitch is imported |
| username | TEXT NOT NULL | as MixItUp reported it |
| user_uuid | TEXT | the wallet user matched via the Twitch identity (login, then Helix id); NULL when skipped |
| miu_hats | INTEGER NOT NULL DEFAULT 0 | balance read from MixItUp |
| miu_tokens | INTEGER NOT NULL DEFAULT 0 | inventory count read from MixItUp |
| status | TEXT NOT NULL | `imported` / `skipped_zero` / `skipped_bot` / `skipped_platform` / `already_imported` / `error` |
| detail | TEXT | error text or skip reason |

Primary key `(import_id, miu_user_id)`.

## Existing tables

No columns change. `transactions`, `dividends`, `portfolios`, and the
YouTube tables keep their meaning; `wallet_ledger.ref` points at them
instead of duplicating them. `data/gamble_jackpot.json` stays a file for
now (single number, no per-user data).

## Config that replaces the MixItUp block

Removed: `MIXITUP_API_BASE`, `MIXITUP_INVENTORY_NAME`, `MIXITUP_ITEM_NAME`,
`GAMBLE_CURRENCY_NAME`. `ECONOMY_CURRENCY_NAME` stays as the display
word ("Hats").

Added (numbers are placeholders until copied from your MixItUp settings):

| key | default | meaning |
|---|---|---|
| `WALLET_EARN_ENABLED` | True | passive earning loop on/off (also a feature toggle) |
| `WALLET_EARN_INTERVAL_MIN` | 10 | minutes between ticks |
| `WALLET_EARN_HATS_PER_TICK` | 10 | hats per present viewer per tick |
| `WALLET_EARN_SUB_MULTIPLIER` | 2.0 | subs earn this multiple |
| `WALLET_EARN_OFFLINE` | False | pay ticks while not live |
| `WALLET_EARN_CHAT_BONUS` | 0 | extra hats per tick for viewers who chatted since the last tick |
| `WALLET_BONUS_SUB` / `_RAID` / `_BITS_PER_100` / `_FIRST_MSG` | 0 | one-off hats on those events |
| `WALLET_EXCLUDED_LOGINS` | reuses `ECONOMY_EXCLUDED_USERNAMES` + the bot | Twitch logins that never earn and never appear on leaderboards (resolved to user_uuid at runtime) |

## Code that changes

- `core/wallet.py` (new): schema, `get(user_uuid, asset)`, `credit(...)`,
  `debit(...)` returning False on insufficient funds,
  `leaderboard(asset, n)`, `history(user_uuid, n)`.
- `plugins/economy/mixitup.py` is deleted; `_get_balance` /
  `_adjust_balance` become thin wallet calls so trading, dividends, api,
  bingo, and the site keep their call sites.
- `plugins/gamble.py` and `plugins/godrequest.py`: their private HTTP
  clients go; the "not connected" branches go.
- `plugins/wallet_earn.py` (new): the tick loop + event bonuses.
- Chat: `!hats`, `!tophats`, `!givehats <user> <n>` / `!takehats` (mods).
- `core/public_webserver.py`: the two MixItUp-up gates become "bot up".
- `tools/import_mixitup.py` (new, `--dry-run` first), `tools/wallet_audit.py`.
- Readiness probe, control-panel badge, `auth.js` copy, docs.

## Rollout

1. Ship the wallet with the import tool; run `--dry-run`, read the report.
2. Run the real import right before a stream, then restart the bot.
3. Leave MixItUp installed but closed for at least two weeks; the
   `wallet_import_rows` table is the comparison if anything looks off.
4. MixItUp chat commands and the timed "say this every N minutes"
   messages are out of scope here and will be rebuilt in the bot later.
