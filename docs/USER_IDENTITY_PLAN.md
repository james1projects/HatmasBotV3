# User identity plan: one UUID per viewer across Twitch and YouTube

Status: IMPLEMENTED 2026-09-12 (`core/users.py`, migration in
`plugins/economy/db.py:_migrate_to_user_uuid`, tests/test_users.py; see
"v2.16 Update" in HATMASBOT.md). Comes BEFORE `WALLET_PLAN.md`; the wallet
tables key on `user_uuid` from day one.

Deviations from the plan as written:
- The migration runs inside the normal schema callbacks at boot, not a
  separate tool. Logins are NOT resolved through Helix in a batch;
  they become placeholder identities (`login:<login>`) that the first
  id-bearing sighting upgrades. No `user_id` anywhere: `user_uuid`.
- Login-era tables are parked as `_legacy_*` (not dropped) for one
  release; `transactions.username` stays as a nullable legacy column.
- Preview-then-confirm (`/link/confirm`) only appears when the link
  would actually merge two users; a fresh channel links instantly.
- Rule 7's unlink UI is not shipped (deferred, case by case).

## Today

| Key | Tables / files |
|---|---|
| Twitch login (lowercase) | portfolios, transactions, god_pool, god_pool_votes, priority_payments, bingo cards / prefs / rounds.winner_login, chat_log.messages, godreq history, song_likes |
| YouTube channel id (`UC...`) | youtube_portfolios, youtube_holdings, youtube_transactions, pending_yt_nominations, youtube_processed_comments |
| Twitch numeric user id | session cookie only, never stored |
| account_links | one-way YouTube channel -> Twitch login, 1 row |

A YouTube session carries an empty login on purpose, so every
login-keyed guard rejects it: no hats, no trading, no bingo, no
nominations for YouTube-only viewers. Linking (`core/account_linking.py`)
folds YouTube holdings into the Twitch portfolio with a weighted-average
cost basis and empties the YouTube side.

Known defect of login keys: a viewer who renames on Twitch loses their
portfolio. The numeric Twitch id is stable; the login is not.

## Target

### users

| column | type | notes |
|---|---|---|
| uuid | TEXT PRIMARY KEY | UUID4; referenced everywhere as `user_uuid` so a reader never mistakes it for a username |
| display_name | TEXT NOT NULL | shown in chat replies, leaderboards, site |
| avatar_url | TEXT | from the most recent login |
| created_at | TEXT NOT NULL DEFAULT (datetime('now')) | |
| last_seen_at | TEXT | any chat message, command, or site visit |
| merged_into | TEXT | NULL unless this row was absorbed by a merge; lookups follow the pointer |
| leaderboard_opt_out | INTEGER NOT NULL DEFAULT 0 | moves here from portfolios / youtube_portfolios |

### user_identities

| column | type | notes |
|---|---|---|
| provider | TEXT NOT NULL | `twitch` / `youtube` (CHECK) |
| provider_id | TEXT NOT NULL | Twitch numeric id or `UC...` channel id; both stable |
| user_uuid | TEXT NOT NULL | users.uuid |
| login | TEXT | Twitch login (lowercase), updated on every sighting; NULL for YouTube |
| display_name | TEXT | provider-side name at last sighting |
| linked_at | TEXT NOT NULL DEFAULT (datetime('now')) | |
| last_seen_at | TEXT | |

Primary key `(provider, provider_id)`. Index on `user_uuid`. Index on
`(provider, login)` for the chat path before the numeric id is known.

Rule: an identity belongs to exactly one user; a user holds one or more
identities. A user with zero identities is invalid (unlink refuses).

### user_merges

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | |
| absorbed_uuid | TEXT NOT NULL | the user row that got `merged_into` set |
| survivor_uuid | TEXT NOT NULL | |
| initiated_by | TEXT NOT NULL | `user` (link page) / `migration` / `admin` |
| merged_at | TEXT NOT NULL DEFAULT (datetime('now')) | |
| summary | TEXT NOT NULL | JSON: rows moved per table, balances added, positions averaged |

### Every feature table gains `user_uuid TEXT NOT NULL`

portfolios, transactions, god_pool.added_by, god_pool_votes,
priority_payments, bingo cards / prefs / rounds.winner, chat_log
messages, pending_yt_nominations, and all wallet tables. The login /
channel-id columns stay for one release as denormalised display text,
then are dropped.

The `youtube_portfolios`, `youtube_holdings`, `youtube_transactions`
cluster collapses into `portfolios` / `transactions` keyed by user_uuid.
`youtube_processed_comments` and `youtube_video_gods` stay: they are
about comments and videos, not people. `account_links` is replaced by
`user_identities`.

## Resolving a viewer

- Chat / EventSub: Twitch numeric id -> `user_identities` -> user (cached
  in memory). Unknown id creates a user + identity on the spot.
- Site session cookie stores `user_uuid`, not a provider id. Reading it
  follows `merged_into` until it lands on a live row.
- Any lookup helper (`users.resolve(user_uuid)`) applies the same chain.

## Merge rules (linking a second provider)

The case: viewer uses Twitch (user A), later uses YouTube without
linking (user B), then links both. Two rows with data. Linking is a
merge with fixed rules:

1. **Survivor**: the row holding a Twitch identity. If neither or both,
   the older `created_at`.
2. **Summable rows** (wallet ledger, transactions, bingo cards, chat log,
   priority payments): re-point `user_uuid`. Wallet balances add.
3. **Positions** (portfolios shares + avg_cost): weighted-average merge,
   the same math `link_and_migrate` uses today.
4. **Singletons** (leaderboard_opt_out, bingo prefs, a same-day pool
   vote): keep the survivor's value, drop the absorbed one.
5. **Absorbed row is never deleted**: set `merged_into`; write a
   `user_merges` row with the per-table summary.
6. **Preview then confirm**: the link page shows both sides before
   committing ("combine 1,200 hats + 3 gods with 300 hats + 1 god").
7. **Unlink removes an identity, never splits data**; a user must keep
   at least one identity.
   Splitting a mistaken merge is DEFERRED (decided 2026-09-12): it will
   be handled case by case from the `user_merges` log in a dedicated
   session if it ever comes up. No unlink UI ships in this pass.
8. **Identity owned by another user**: refused (today's
   `linked_elsewhere`). An admin merge path exists for support cases.

## Migration from today's data (one-shot tool, dry-run first)

1. Distinct Twitch logins across every login-keyed table -> one user +
   twitch identity each. Resolve logins to numeric ids via Helix
   (100 per call). Unresolvable logins keep `provider_id = 'login:<x>'`
   until that person logs in and the row is upgraded.
2. One user + youtube identity per `youtube_portfolios` row.
3. Apply the existing `account_links` row as a merge (rule set above).
4. Backfill `user_uuid` on every table from the identities.
5. Switch code paths to `user_uuid`; keep the old columns one release.

Expected runtime: seconds (largest table is 20k transactions).

## Order of work

1. This document: identity tables + migration + code switch.
2. `WALLET_PLAN.md`: wallet keyed on `user_uuid`.
3. MixItUp import and cutover.
