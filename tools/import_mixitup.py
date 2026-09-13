"""
tools/import_mixitup.py — copy Hats + God Tokens out of MixItUp into the wallet
==============================================================================

One-shot, replayable. Walks MixItUp's Developer API (localhost:8911,
pages of 100 users), reads each Twitch user's Hats currency amount and
God Token inventory count, resolves the login to a user_uuid
(core/users.py) and credits the wallet with reason 'migration' and
ref 'miu:<mixitup user id>:<asset>' — so running it twice never
double-credits (a re-run imports anyone not imported yet). Watch time
(MixItUp's OnlineViewingMinutes) is applied as a floor on
users.watch_minutes on every run, so it is safe to re-run just for it.

    python tools/import_mixitup.py --dry-run     # report only
    python tools/import_mixitup.py               # import

Every run writes a wallet_imports row and one wallet_import_rows row
per MixItUp user (imported / skipped_zero / skipped_bot /
skipped_platform / already_imported / error), which is the side-by-side
you compare against MixItUp while it stays installed as the rollback.
Run it with the bot STOPPED (the wallet tables are in economy.db).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tls_trust  # noqa: F401,E402  (before aiohttp)
import aiohttp  # noqa: E402
import aiosqlite  # noqa: E402

from core import users as _users  # noqa: E402
from core import wallet as _wallet  # noqa: E402
from core.config import (  # noqa: E402
    ECONOMY_DB_PATH, ECONOMY_CURRENCY_NAME, ECONOMY_EXCLUDED_USERNAMES,
    TWITCH_BOT_USERNAME,
)

API = "http://localhost:8911/api/v2"
CURRENCY_NAME = ECONOMY_CURRENCY_NAME or "Hats"
INVENTORY_NAME = "God Tokens"
ITEM_NAME = "God Token"


class MixItUp:
    def __init__(self, session: aiohttp.ClientSession, base: str = API):
        self.s, self.base = session, base

    async def get(self, path: str):
        async with self.s.get(self.base + path) as r:
            if r.status == 404:
                return None
            r.raise_for_status()
            return await r.json()

    async def ids(self):
        currency_id = inventory_id = item_id = None
        for c in await self.get("/currency") or []:
            if (c.get("Name") or "").lower() == CURRENCY_NAME.lower():
                currency_id = c["ID"]
        for inv in await self.get("/inventory") or []:
            if (inv.get("Name") or "").lower() == INVENTORY_NAME.lower():
                inventory_id = inv["ID"]
                for it in inv.get("Items", []):
                    if (it.get("Name") or "").lower() == ITEM_NAME.lower():
                        item_id = it["ID"]
        return currency_id, inventory_id, item_id

    async def users(self):
        skip = 0
        while True:
            page = await self.get(f"/users?skip={skip}&pageSize=100")
            items = (page or {}).get("Users") or (page if isinstance(page, list) else [])
            if not items:
                return
            for u in items:
                yield u
            if len(items) < 100:
                return
            skip += len(items)

    async def amount(self, path: str) -> int:
        data = await self.get(path)
        return int((data or {}).get("Amount") or 0)


def twitch_login(user: dict):
    pdata = user.get("PlatformData") or {}
    for key, p in pdata.items():
        if (p.get("Platform") or key or "").lower() == "twitch":
            return (p.get("Username") or "").lower(), str(p.get("ID") or ""), p.get("DisplayName")
    return None, None, None


async def run(dry_run: bool, base: str) -> int:
    excluded = {u.lower() for u in (ECONOMY_EXCLUDED_USERNAMES or [])}
    if TWITCH_BOT_USERNAME and TWITCH_BOT_USERNAME != "YOUR_BOT_USERNAME":
        excluded.add(TWITCH_BOT_USERNAME.lower())

    db = await aiosqlite.connect(str(ECONOMY_DB_PATH))
    try:
        await _users.ensure_schema(db)
        await _wallet.ensure_schema(db)
        cur = await db.execute(
            "INSERT INTO wallet_imports (source, dry_run) VALUES ('mixitup', ?)",
            (1 if dry_run else 0,))
        import_id = int(cur.lastrowid)
        await db.commit()

        seen = imported = hats_total = tokens_total = minutes_total = 0
        notes = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            miu = MixItUp(s, base)
            try:
                currency_id, inventory_id, item_id = await miu.ids()
            except Exception as e:
                print(f"MixItUp API not reachable at {base}: {e}")
                print("Open MixItUp, enable Settings -> Developer API on 8911, retry.")
                return 1
            if not currency_id:
                print(f"Currency {CURRENCY_NAME!r} not found in MixItUp.")
                return 1
            if not (inventory_id and item_id):
                notes.append("God Token inventory not found; tokens skipped")
                print("WARNING: God Token inventory/item not found; importing hats only")

            async for u in miu.users():
                seen += 1
                miu_id = str(u.get("ID") or "")
                login, tid, display = twitch_login(u)
                status, detail, uid = "imported", None, None
                hats = tokens = 0
                minutes = int(u.get("OnlineViewingMinutes") or 0)
                try:
                    if not login:
                        status, detail = "skipped_platform", "no Twitch identity"
                        login = next(iter((u.get("PlatformData") or {}).values()), {}).get("Username") or "?"
                    elif login in excluded:
                        status = "skipped_bot"
                    else:
                        hats = await miu.amount(f"/currency/{currency_id}/{miu_id}")
                        if inventory_id and item_id:
                            tokens = await miu.amount(
                                f"/inventory/{inventory_id}/{item_id}/{miu_id}")
                        if not dry_run and minutes > 0:
                            # watch time is independent of the balances
                            if tid:
                                uid = await _users.get_or_create_twitch(
                                    db, tid, login, display, commit=False)
                            else:
                                uid = await _users.get_or_create_twitch_login(
                                    db, login, display, commit=False)
                            await _users.floor_watch_minutes(db, uid, minutes)
                            minutes_total += minutes
                        if hats <= 0 and tokens <= 0:
                            status = "skipped_zero"
                        elif dry_run:
                            # look, never create: a dry run leaves users alone
                            uid = (await _users.find_twitch_id(db, tid) if tid else None) \
                                or await _users.find_twitch_login(db, login)
                            if uid is None:
                                detail = "would create user"
                            done_h = await _wallet._ref_exists(db, "migration", f"miu:{miu_id}:hats")
                            done_t = await _wallet._ref_exists(db, "migration", f"miu:{miu_id}:god_token")
                            if done_h or done_t:
                                status = "already_imported"
                            else:
                                imported += 1
                                hats_total += hats
                                tokens_total += tokens
                        else:
                            if tid:
                                uid = await _users.get_or_create_twitch(
                                    db, tid, login, display, commit=False)
                            else:
                                uid = await _users.get_or_create_twitch_login(
                                    db, login, display, commit=False)
                            got = []
                            if hats > 0:
                                r = await _wallet.credit(
                                    db, uid, "hats", hats, "migration",
                                    ref=f"miu:{miu_id}:hats", actor="migration",
                                    channel="system", commit=False)
                                got.append(r is not None)
                            if tokens > 0:
                                r = await _wallet.credit(
                                    db, uid, "god_token", tokens, "migration",
                                    ref=f"miu:{miu_id}:god_token", actor="migration",
                                    channel="system", commit=False)
                                got.append(r is not None)
                            if got and not any(got):
                                status = "already_imported"
                        if status == "imported":
                            imported += 1
                            hats_total += hats
                            tokens_total += tokens
                except Exception as e:
                    status, detail = "error", str(e)[:200]
                await db.execute(
                    "INSERT OR REPLACE INTO wallet_import_rows (import_id, miu_user_id, "
                    "platform, username, user_uuid, miu_hats, miu_tokens, miu_minutes, "
                    "status, detail) VALUES (?, ?, 'Twitch', ?, ?, ?, ?, ?, ?, ?)",
                    (import_id, miu_id or f"row{seen}", login or "?", uid, hats, tokens,
                     minutes, status, detail))
                if seen % 100 == 0:
                    print(f"  ... {seen} users seen, {imported} to import")
                    await db.commit()

        await db.execute(
            "UPDATE wallet_imports SET finished_at = datetime('now'), users_seen = ?, "
            "users_imported = ?, hats_total = ?, tokens_total = ?, notes = ? WHERE id = ?",
            (seen, imported, hats_total, tokens_total, "; ".join(notes) or None, import_id))
        await db.commit()

        async with db.execute(
                "SELECT status, COUNT(*) FROM wallet_import_rows WHERE import_id = ? "
                "GROUP BY status ORDER BY 2 DESC", (import_id,)) as c:
            breakdown = await c.fetchall()
        print()
        print(f"{'DRY RUN' if dry_run else 'IMPORT'} #{import_id}: {seen} MixItUp users, "
              f"{imported} {'would be ' if dry_run else ''}imported, "
              f"{hats_total:,} hats, {tokens_total:,} tokens, "
              f"{minutes_total:,} watch minutes applied")
        for st, n in breakdown:
            print(f"  {st:18s} {n}")
        if dry_run:
            print("Nothing written to balances. Re-run without --dry-run to import.")
        return 0
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true", help="report only, write no balances")
    ap.add_argument("--api", default=API, help=f"MixItUp API base (default {API})")
    args = ap.parse_args()
    return asyncio.run(run(args.dry_run, args.api))


if __name__ == "__main__":
    sys.exit(main())
