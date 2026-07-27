import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiosqlite
from core import account_linking as al

async def make_db():
    db = await aiosqlite.connect(':memory:')
    await db.execute('''CREATE TABLE portfolios (
        username  TEXT NOT NULL,
        god_name  TEXT NOT NULL,
        shares    REAL NOT NULL DEFAULT 0,
        avg_cost  REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (username, god_name)
    )''')
    await db.execute('''CREATE TABLE transactions (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        username  TEXT NOT NULL,
        god_name  TEXT NOT NULL,
        type      TEXT NOT NULL,
        shares    REAL NOT NULL,
        price     REAL NOT NULL,
        total     REAL NOT NULL,
        fee       REAL NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL DEFAULT (datetime('now'))
    )''')
    await db.execute('''CREATE TABLE youtube_holdings (
        yt_channel_id  TEXT NOT NULL,
        god_name       TEXT NOT NULL,
        shares         REAL NOT NULL DEFAULT 0,
        avg_cost       REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (yt_channel_id, god_name)
    )''')
    await db.execute('''CREATE TABLE youtube_transactions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        yt_channel_id  TEXT NOT NULL,
        god_name       TEXT NOT NULL,
        type           TEXT NOT NULL,
        shares         REAL NOT NULL,
        price          REAL NOT NULL,
        yt_video_id    TEXT,
        timestamp      TEXT NOT NULL DEFAULT (datetime('now'))
    )''')
    await al.ensure_schema(db)
    return db

async def test_link_and_migrate_fresh_no_holdings():
    db = await make_db()
    try:
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result['ok'] == True
        assert result['already'] == False
        assert result['migrated'] == []
        
        # Check account_links table
        async with db.execute("SELECT * FROM account_links") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 'UC123'
            assert rows[0][1] == 'viewer1'
    finally:
        await db.close()

async def test_link_and_migrate_fresh_with_holdings():
    db = await make_db()
    try:
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UC123', 'Ymir', 5.0, 40.0)")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result['ok'] == True
        assert result['already'] == False
        assert len(result['migrated']) == 1
        assert result['migrated'][0] == ('Ymir', 5.0)
        
        # Check portfolios table
        async with db.execute("SELECT shares, avg_cost FROM portfolios WHERE username='viewer1' AND god_name='Ymir'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert abs(row[0] - 5.0) < 1e-6
            assert abs(row[1] - 40.0) < 1e-6
            
        # Check youtube_holdings deleted
        async with db.execute("SELECT * FROM youtube_holdings WHERE yt_channel_id='UC123'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 0
    finally:
        await db.close()

async def test_link_and_migrate_already_linked():
    db = await make_db()
    try:
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC123', 'viewer1')")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result['ok'] == True
        assert result['already'] == True
        assert result['migrated'] == []
    finally:
        await db.close()

async def test_link_and_migrate_different_login():
    db = await make_db()
    try:
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC123', 'viewer2')")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result['ok'] == False
        assert result['reason'] == 'linked_elsewhere'
    finally:
        await db.close()

async def test_link_and_migrate_empty_args():
    db = await make_db()
    try:
        result = await al.link_and_migrate(db, '', 'viewer1')
        assert result['ok'] == False
        assert result['reason'] == 'bad_args'
        
        result = await al.link_and_migrate(db, 'UC123', '')
        assert result['ok'] == False
        assert result['reason'] == 'bad_args'
    finally:
        await db.close()

async def test_get_link():
    db = await make_db()
    try:
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC123', 'viewer1')")
        await db.commit()
        
        login = await al.get_link(db, 'UC123')
        assert login == 'viewer1'
        
        login = await al.get_link(db, 'UC456')
        assert login is None
    finally:
        await db.close()

async def test_get_links_for_twitch():
    db = await make_db()
    try:
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC123', 'viewer1')")
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC456', 'viewer1')")
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC789', 'viewer2')")
        await db.commit()
        
        links = await al.get_links_for_twitch(db, 'viewer1')
        assert set(links) == {'UC123', 'UC456'}
        
        links = await al.get_links_for_twitch(db, 'viewer2')
        assert links == ['UC789']
        
        links = await al.get_links_for_twitch(db, 'viewer3')
        assert links == []
    finally:
        await db.close()

async def test_grant_to_twitch_fresh():
    db = await make_db()
    try:
        await al.grant_to_twitch(db, 'viewer1', 'Loki', 2.0, 50.0)
        await db.commit()
        
        # Check portfolios
        async with db.execute("SELECT shares, avg_cost FROM portfolios WHERE username='viewer1' AND god_name='Loki'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert abs(row[0] - 2.0) < 1e-6
            assert abs(row[1] - 50.0) < 1e-6
            
        # Check transactions
        async with db.execute("SELECT shares, price, total FROM transactions WHERE username='viewer1' AND god_name='Loki'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert abs(row[0] - 2.0) < 1e-6
            assert abs(row[1] - 50.0) < 1e-6
            assert abs(row[2] - 100.0) < 1e-6
    finally:
        await db.close()

async def test_merge_math():
    db = await make_db()
    try:
        # Pre-seed portfolios
        await db.execute("INSERT INTO portfolios (username, god_name, shares, avg_cost) VALUES ('viewer1', 'Ymir', 10.0, 100.0)")
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UCabc', 'Ymir', 5.0, 40.0)")
        await db.commit()
        
        await al.link_and_migrate(db, 'UCabc', 'viewer1')
        
        # Check merged result
        async with db.execute("SELECT shares, avg_cost FROM portfolios WHERE username='viewer1' AND god_name='Ymir'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert abs(row[0] - 15.0) < 1e-6
            expected_avg = (10 * 100 + 5 * 40) / 15
            assert abs(row[1] - expected_avg) < 1e-6
    finally:
        await db.close()

async def test_zero_share_holdings_skipped():
    db = await make_db()
    try:
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UCabc', 'Ymir', 0.0, 40.0)")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UCabc', 'viewer1')
        assert result['ok'] == True
        assert result['already'] == False
        assert result['migrated'] == []
        
        # Check no portfolios row created
        async with db.execute("SELECT * FROM portfolios WHERE username='viewer1' AND god_name='Ymir'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 0
            
        # Check youtube_holdings deleted (or ignored)
        async with db.execute("SELECT * FROM youtube_holdings WHERE yt_channel_id='UCabc'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 0
    finally:
        await db.close()

async def test_link_and_migrate_case_insensitive():
    db = await make_db()
    try:
        result = await al.link_and_migrate(db, 'UC123', 'HatMaster')
        assert result['ok'] == True
        
        login = await al.get_link(db, 'UC123')
        assert login == 'hatmaster'
    finally:
        await db.close()

async def test_grant_target():
    db = await make_db()
    try:
        await db.execute("INSERT INTO account_links (yt_channel_id, twitch_login) VALUES ('UC123', 'viewer1')")
        await db.commit()
        
        target = await al.grant_target(db, 'UC123')
        assert target == 'viewer1'
        
        target = await al.grant_target(db, 'UC456')
        assert target is None
    finally:
        await db.close()

async def test_grant_to_twitch_existing():
    db = await make_db()
    try:
        # Pre-seed portfolio
        await db.execute("INSERT INTO portfolios (username, god_name, shares, avg_cost) VALUES ('viewer1', 'Loki', 2.0, 50.0)")
        await db.commit()
        
        await al.grant_to_twitch(db, 'viewer1', 'Loki', 3.0, 60.0)
        await db.commit()
        
        # Check updated portfolio
        async with db.execute("SELECT shares, avg_cost FROM portfolios WHERE username='viewer1' AND god_name='Loki'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            total_shares = 2.0 + 3.0
            expected_avg = (2.0 * 50.0 + 3.0 * 60.0) / total_shares
            assert abs(row[0] - total_shares) < 1e-6
            assert abs(row[1] - expected_avg) < 1e-6
    finally:
        await db.close()

async def test_get_links_for_twitch_case_insensitive():
    # Rows are stored lowercase (link_and_migrate lowercases on
    # insert); the QUERY side must lowercase its argument too.
    db = await make_db()
    try:
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result["ok"] is True

        links = await al.get_links_for_twitch(db, 'VIEWER1')
        assert links == ['UC123'], links
    finally:
        await db.close()

async def test_ensure_schema():
    db = await make_db()
    try:
        # Schema already created by make_db
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_links'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            
        # Test idempotent
        await al.ensure_schema(db)
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_links'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
    finally:
        await db.close()

async def test_link_and_migrate_multiple_gods():
    db = await make_db()
    try:
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UC123', 'Ymir', 5.0, 40.0)")
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UC123', 'Loki', 3.0, 60.0)")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result['ok'] == True
        assert len(result['migrated']) == 2
        
        # Check both gods in portfolios
        async with db.execute("SELECT god_name, shares, avg_cost FROM portfolios WHERE username='viewer1'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 2
            god_data = {row[0]: (row[1], row[2]) for row in rows}
            assert abs(god_data['Ymir'][0] - 5.0) < 1e-6
            assert abs(god_data['Loki'][0] - 3.0) < 1e-6
    finally:
        await db.close()

async def test_link_and_migrate_ledgers_both_sides():
    db = await make_db()
    try:
        await db.execute("INSERT INTO youtube_holdings (yt_channel_id, god_name, shares, avg_cost) VALUES ('UC123', 'Ymir', 5.0, 40.0)")
        await db.commit()
        
        result = await al.link_and_migrate(db, 'UC123', 'viewer1')
        assert result["ok"] is True

        # Migration is ledgered on BOTH sides: one 'yt_merge_in'
        # transactions row on the Twitch side...
        async with db.execute("SELECT type, shares FROM transactions WHERE username='viewer1'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1, rows
            assert rows[0][0] == 'yt_merge_in'
            assert abs(rows[0][1] - 5.0) < 1e-6

        # ...and one negative 'merged_to_twitch' row on the YT side.
        async with db.execute("SELECT type, shares FROM youtube_transactions WHERE yt_channel_id='UC123'") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 'merged_to_twitch'
            assert abs(rows[0][1] - (-5.0)) < 1e-6
    finally:
        await db.close()

async def test_grant_to_twitch_caller_commits():
    # grant_to_twitch leaves the commit to the caller (so a grant and
    # its processed-comment marker land atomically). Same-connection
    # reads see uncommitted writes, so "invisible before commit"
    # can't be asserted here — just prove commit-after-grant persists.
    db = await make_db()
    try:
        await al.grant_to_twitch(db, 'viewer1', 'Loki', 2.0, 50.0)
        await db.commit()

        async with db.execute("SELECT shares, avg_cost FROM portfolios WHERE username='viewer1' AND god_name='Loki'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert abs(row[0] - 2.0) < 1e-6
            assert abs(row[1] - 50.0) < 1e-6
    finally:
        await db.close()

async def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)
             and name_filter in n]
    failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    asyncio.run(main())
