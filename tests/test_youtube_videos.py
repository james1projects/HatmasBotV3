import sys
import os
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncio
import aiosqlite
import tempfile
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer
import core.youtube_videos as yt_videos
import core.youtube_admin_web as yaw
from core.youtube_schema import ensure_youtube_schema
import core.public_webserver as pw
import core.web_session as ws
from types import SimpleNamespace

SECRET = 'a' * 40
TEST_GODS = ['Ymir','Hou Yi','Loki','Ah Muzen Cab']

# Login must be enabled on the server (secret + real-looking Twitch app
# creds) and the broadcaster login must be known, or every route 404s.
pw.WEB_SESSION_SECRET = SECRET
pw.WEB_OAUTH_REDIRECT_URI = 'https://hatmaster.tv/auth/twitch/callback'
pw.TWITCH_CLIENT_ID = 'test_client_id'
pw.TWITCH_CLIENT_SECRET = 'test_client_secret'
_ORIG_CHANNEL = pw._config.TWITCH_CHANNEL
pw._config.TWITCH_CHANNEL = 'hatmaster'
# Never write the audit log into data/ from a test run.
yaw.AUDIT_LOG = Path(tempfile.mkdtemp()) / 'audit.log'

class FakePlugin:
    def __init__(self):
        self.ready = True
        self.grant = 2
        self.refresh_videos_calls = []
        self.scan_video_calls = []

    def is_ready(self):
        return self.ready

    def known_gods(self):
        return TEST_GODS

    async def refresh_videos(self, limit=None):
        self.refresh_videos_calls.append({'limit': limit})
        return {'seen': 3, 'added': 1, 'auto_tagged': 1}

    async def scan_video(self, video_id):
        self.scan_video_calls.append(video_id)
        return self.grant

async def run_test(test_func):
    try:
        await test_func()
        print(f'{test_func.__name__} PASS')
        return True
    except Exception as e:
        print(f'{test_func.__name__} FAIL: {e}')
        return False

async def test_valid_video_id():
    assert yt_videos.valid_video_id('a' * 11) == True
    assert yt_videos.valid_video_id('A-Z0_9') == True
    assert yt_videos.valid_video_id('a' * 21) == False
    assert yt_videos.valid_video_id('a/b') == False

async def test_upsert_video():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    new = await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    assert new == True
    new = await yt_videos.upsert_video(db, 'abc123', 'Updated Title', '2023-01-01T00:00:00Z')
    assert new == False
    await db.close()

async def test_is_skipped():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    skipped = await yt_videos.is_skipped(db, 'abc123')
    assert skipped == False
    await yt_videos.set_skipped(db, 'abc123', True)
    skipped = await yt_videos.is_skipped(db, 'abc123')
    assert skipped == True
    await db.close()

async def test_set_god():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    await yt_videos.set_god(db, 'abc123', 'Ymir', 'web:test')
    god = await yt_videos.get_god(db, 'abc123')
    assert god == 'Ymir'
    await db.close()

async def test_list_videos():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    await yt_videos.set_god(db, 'abc123', 'Ymir', 'web:test')
    videos = await yt_videos.list_videos(db, TEST_GODS)
    assert len(videos) == 1
    assert videos[0]['status'] == 'categorized'
    await db.close()

async def test_suggest_god():
    title = 'Hou Yi vs Loki - Epic Battle'
    god = yt_videos.suggest_god(title, TEST_GODS)
    assert god == 'Hou Yi'

async def test_resolve_god():
    god = yt_videos.resolve_god('hou yi', TEST_GODS)
    assert god == 'Hou Yi'

async def test_admin_web_no_auth():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get('/api/admin/videos')
        assert resp.status == 404
    finally:
        await client.close()
    await db.close()

async def test_admin_web_broadcaster():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}'}
        resp = await client.get('/api/admin/videos', headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['ok'] == True
        assert data['user'] == 'hatmaster'
    finally:
        await client.close()
    await db.close()

async def test_admin_web_tag_video():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/abc123/god', json={'god': 'ymir'}, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['ok'] == True
        assert data['god'] == 'Ymir'
        assert data['action'] == 'tagged'
        assert bot.plugins['youtube_rewards'].scan_video_calls == ['abc123']
    finally:
        await client.close()
    await db.close()

async def test_admin_web_bad_god():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/abc123/god', json={'god': 'Nobody'}, headers=headers)
        assert resp.status == 400
    finally:
        await client.close()
    await db.close()

async def test_admin_web_refresh():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/refresh', headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['seen'] == 3
    finally:
        await client.close()
    await db.close()

async def test_admin_web_skip():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    await yt_videos.set_god(db, 'abc123', 'Ymir', 'web:test')
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        # Add grants to make it non-skippable
        await db.execute("INSERT INTO youtube_processed_comments (yt_video_id, yt_channel_id, comment_id, granted_at) VALUES ('abc123', 'channel1', 'comment1', '2023-01-01T00:00:00Z')")
        resp = await client.post('/api/admin/videos/abc123/skip', headers=headers)
        assert resp.status == 409
    finally:
        await client.close()
    await db.close()

async def test_admin_web_unskip():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    await yt_videos.set_skipped(db, 'abc123', True)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/abc123/unskip', headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'uncategorized'
    finally:
        await client.close()
    await db.close()

async def test_admin_web_rescan():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    await yt_videos.set_god(db, 'abc123', 'Ymir', 'web:test')
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/abc123/rescan', headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['granted'] == 2
    finally:
        await client.close()
    await db.close()

async def test_admin_web_bad_video_id():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/a/../b/god', json={'god': 'Ymir'}, headers=headers)
        assert resp.status in [400, 404]
    finally:
        await client.close()
    await db.close()

async def test_admin_web_bad_origin():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'abc123', 'Test Title', '2023-01-01T00:00:00Z')
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}'}
        resp = await client.post('/api/admin/videos/abc123/god', json={'god': 'Ymir'}, headers=headers)
        assert resp.status == 403
    finally:
        await client.close()
    await db.close()

async def test_admin_web_plugin_not_ready():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    bot.plugins['youtube_rewards'].ready = False
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/abc123/god', json={'god': 'ymir'}, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data['granted'] == 0
    finally:
        await client.close()
    await db.close()

async def test_admin_web_refresh_not_ready():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    bot = SimpleNamespace(plugins={'youtube_rewards': FakePlugin()})
    bot.plugins['youtube_rewards'].ready = False
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        token = ws.issue('1', 'hatmaster', 'hatmaster', secret=SECRET)
        headers = {'Cookie': f'{ws.SESSION_COOKIE}={token}', 'Origin': 'https://hatmaster.tv'}
        resp = await client.post('/api/admin/videos/refresh', headers=headers)
        assert resp.status == 503
    finally:
        await client.close()
    await db.close()

async def _server(db, plugin=None):
    bot = SimpleNamespace(plugins={'youtube_rewards': plugin or FakePlugin()})
    server = pw.PublicWebServer(economy=None, bot=bot)
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return client, bot

def _hdr(login='hatmaster', origin=True):
    token = ws.issue('1', login, login, secret=SECRET)
    h = {'Cookie': f'{ws.SESSION_COOKIE}={token}'}
    if origin:
        h['Origin'] = 'https://hatmaster.tv'
    return h

async def _paid(db, vid, god, n=3):
    await yt_videos.upsert_video(db, vid, 'Some Title', '2024-01-01T00:00:00Z')
    await yt_videos.set_god(db, vid, god, 'auto')
    for i in range(n):
        await db.execute(
            "INSERT INTO youtube_processed_comments "
            "(yt_video_id, yt_channel_id, comment_id) VALUES (?, ?, ?)",
            (vid, f'UC{i}', f'c{i}'))
    await db.commit()

async def test_admin_web_mod_is_not_broadcaster():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    client, _ = await _server(db)
    try:
        for path in ('/admin/videos', '/api/admin/videos', '/api/admin/videos/gods'):
            resp = await client.get(path, headers=_hdr('viewer1'))
            assert resp.status == 404, (path, resp.status)
        resp = await client.post('/api/admin/videos/abc123/god', json={'god': 'Ymir'},
                                 headers=_hdr('viewer1'))
        assert resp.status == 404
        assert await yt_videos.get_god(db, 'abc123') is None
    finally:
        await client.close()
    await db.close()

async def test_admin_web_change_blocked_after_payout():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await _paid(db, 'paidvid01', 'Loki', 3)
    client, bot = await _server(db)
    try:
        resp = await client.post('/api/admin/videos/paidvid01/god', json={'god': 'Ymir'},
                                 headers=_hdr())
        assert resp.status == 409
        data = await resp.json()
        assert data['ok'] is False and data['grants'] == 3 and data['current'] == 'Loki'
        assert await yt_videos.get_god(db, 'paidvid01') == 'Loki'
        assert bot.plugins['youtube_rewards'].scan_video_calls == []
        # skip is blocked too
        resp = await client.post('/api/admin/videos/paidvid01/skip', headers=_hdr())
        assert resp.status == 409
        assert await yt_videos.get_god(db, 'paidvid01') == 'Loki'
    finally:
        await client.close()
    await db.close()

async def test_admin_web_change_and_unchanged():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    await yt_videos.upsert_video(db, 'freshvid01', 'Full Gameplay: Loki vs Ymir', '2024-01-01T00:00:00Z')
    await yt_videos.set_god(db, 'freshvid01', 'Loki', 'auto')
    client, bot = await _server(db)
    try:
        resp = await client.post('/api/admin/videos/freshvid01/god', json={'god': 'Loki'},
                                 headers=_hdr())
        assert resp.status == 200
        data = await resp.json()
        assert data['action'] == 'unchanged'
        async with db.execute("SELECT set_by FROM youtube_video_gods WHERE yt_video_id='freshvid01'") as c:
            assert (await c.fetchone())[0] == 'auto'  # untouched
        resp = await client.post('/api/admin/videos/freshvid01/god', json={'god': ' hou   yi '},
                                 headers=_hdr())
        assert resp.status == 200
        data = await resp.json()
        assert data['action'] == 'changed' and data['god'] == 'Hou Yi' and data['granted'] == 2
        async with db.execute("SELECT god_name, set_by FROM youtube_video_gods WHERE yt_video_id='freshvid01'") as c:
            assert tuple(await c.fetchone()) == ('Hou Yi', 'web:hatmaster')
        # list reflects it, with the icon slug
        resp = await client.get('/api/admin/videos', headers=_hdr(origin=False))
        data = await resp.json()
        v = data['videos'][0]
        assert v['status'] == 'categorized' and v['god_icon'] == '/god-icon/hou-yi'
        assert data['counts'] == {'uncategorized': 0, 'categorized': 1, 'skipped': 0}
    finally:
        await client.close()
    await db.close()

async def test_list_includes_cli_only_and_suggestions():
    db = await aiosqlite.connect(':memory:')
    await ensure_youtube_schema(db)
    # tagged by the old CLI before youtube_videos existed: no catalogue row
    await db.execute("INSERT INTO youtube_video_gods (yt_video_id, god_name, title, set_by) "
                     "VALUES ('clionly001', 'Ymir', 'old one', 'manual')")
    await yt_videos.upsert_video(db, 'newer00001', 'AH MUZEN CAB is busted', '2025-02-01T00:00:00Z')
    await yt_videos.upsert_video(db, 'older00001', 'tier list', '2025-01-01T00:00:00Z')
    await yt_videos.set_skipped(db, 'older00001', True)
    await db.commit()
    videos = await yt_videos.list_videos(db, TEST_GODS)
    by_id = {v['video_id']: v for v in videos}
    assert [v['video_id'] for v in videos][:2] == ['newer00001', 'older00001']  # newest first
    assert by_id['clionly001']['status'] == 'categorized' and by_id['clionly001']['set_by'] == 'manual'
    assert by_id['newer00001']['status'] == 'uncategorized'
    assert by_id['newer00001']['suggestion'] == 'Ah Muzen Cab'
    assert by_id['older00001']['status'] == 'skipped' and by_id['older00001']['suggestion'] is None
    # skipping a tagged video drops the god row; unskip leaves it untagged
    await yt_videos.set_god(db, 'newer00001', 'Ah Muzen Cab', 'web:x')
    await yt_videos.set_skipped(db, 'newer00001', True)
    assert await yt_videos.get_god(db, 'newer00001') is None
    await yt_videos.set_skipped(db, 'newer00001', False)
    assert not await yt_videos.is_skipped(db, 'newer00001')
    assert await yt_videos.get_god(db, 'newer00001') is None
    await db.close()


async def main():
    tests = [
        test_valid_video_id,
        test_upsert_video,
        test_is_skipped,
        test_set_god,
        test_list_videos,
        test_suggest_god,
        test_resolve_god,
        test_admin_web_no_auth,
        test_admin_web_broadcaster,
        test_admin_web_tag_video,
        test_admin_web_bad_god,
        test_admin_web_refresh,
        test_admin_web_skip,
        test_admin_web_unskip,
        test_admin_web_rescan,
        test_admin_web_bad_video_id,
        test_admin_web_bad_origin,
        test_admin_web_plugin_not_ready,
        test_admin_web_refresh_not_ready,
        test_admin_web_mod_is_not_broadcaster,
        test_admin_web_change_blocked_after_payout,
        test_admin_web_change_and_unchanged,
        test_list_includes_cli_only_and_suggestions,
    ]
    results = await asyncio.gather(*[run_test(t) for t in tests])
    passed = sum(results)
    failed = len(results) - passed
    print(f'{passed} passed, {failed} failed')
    if failed > 0:
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
