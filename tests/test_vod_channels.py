"""
Tests for "Ask the VOD for any Twitch channel": the channel registry,
the app-token Helix client, the yt-dlp option builder, the vods queue
table, and the detector-profile plumbing that other-channel VODs need.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp dirs and temp SQLite only; no network (the Helix client
gets a fake transport), no yt-dlp download, no ffmpeg, no GPU.
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import twitch_app as tw  # noqa: E402
from vodsearch import download as dl  # noqa: E402
from vodsearch.channels import Channel, Registry, normalize_login, parse_tracks  # noqa: E402
from vodsearch.store import OWNER_CHANNEL, Store  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="vod_channels_test_"))


# ── registry ──────────────────────────────────────────────────────────

def test_normalize_login():
    assert normalize_login(" @Foo_Bar ") == "foo_bar"
    assert normalize_login("https://www.twitch.tv/SomeOne/videos?filter=archives") == "someone"
    assert normalize_login("twitch.tv/abc#x") == "abc"
    for bad in ("", "bad name!", "a" * 26, "héllo"):
        try:
            normalize_login(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_tracks():
    assert parse_tracks("1:hatmaster,2:friends,3") == {1: "hatmaster", 2: "friends", 3: "track3"}
    assert parse_tracks("") == {}
    assert parse_tracks(" 0:foo ") == {0: "foo"}


def test_registry_roundtrip():
    d = _tmp()
    reg = Registry(d / "channels.json")
    assert len(reg) == 0 and reg.get("foo") is None
    ch = reg.add(Channel(login="Foo", user_id="1", root=str(d / "foo")))
    assert ch.login == "foo" and ch.added_at and ch.track_map() == {0: "foo"}
    try:
        reg.add(Channel(login="foo"))
        raise AssertionError("duplicate add must raise")
    except ValueError:
        pass
    reg.add(Channel(login="zed", enabled=False))
    reg.save()
    assert (d / "channels.json").exists() and not (d / "channels.json.tmp").exists()
    reg2 = Registry(d / "channels.json")
    assert reg2.get("FOO").user_id == "1"
    assert [c.login for c in reg2.enabled()] == ["foo"]
    assert "zed" in reg2 and "nope" not in reg2 and "bad name" not in reg2
    assert reg2.remove("foo") and not reg2.remove("foo")
    reg2.save()
    assert list(Registry(d / "channels.json").channels) == ["zed"]
    (d / "channels.json").write_text("{not json", encoding="utf-8")
    try:
        Registry(d / "channels.json")
        raise AssertionError("corrupt registry must raise")
    except ValueError as e:
        assert "channels.json" in str(e)


def test_channel_track_map_override():
    assert Channel(login="foo", tracks="0:me,1:guest").track_map() == {0: "me", 1: "guest"}


# ── twitch_app ────────────────────────────────────────────────────────

def test_parse_duration():
    assert tw.parse_duration("3h20m5s") == 12005.0
    assert tw.parse_duration("45m") == 2700.0
    assert tw.parse_duration("7s") == 7.0
    assert tw.parse_duration("1h") == 3600.0
    assert tw.parse_duration("") == 0.0 and tw.parse_duration(None) == 0.0
    try:
        tw.parse_duration("3 hours")
        raise AssertionError("garbage must raise")
    except ValueError:
        pass


def test_created_to_local_iso():
    out = tw.twitch_created_to_local_iso("2026-09-01T18:03:12Z")
    back = datetime.fromisoformat(out)
    assert back.tzinfo is None and back.microsecond == 0
    expected = datetime(2026, 9, 1, 18, 3, 12, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert back == expected, (back, expected)
    assert tw.twitch_created_to_local_iso("2026-09-01T18:03:12.123456+00:00") == out
    assert tw.parse_utc("2026-08-15") == datetime(2026, 8, 15, tzinfo=timezone.utc)


class _FakeHelix:
    """Fake transport: mints tokens, serves two pages of /videos, one
    /users row, and 401s the first token once on /users to exercise
    the retry path."""

    def __init__(self, expire_first_token: bool = False):
        self.calls = []
        self.tokens = 0
        self.expire_first_token = expire_first_token

    async def __call__(self, method, url, *, params=None, data=None, headers=None):
        self.calls.append((method, url, dict(params or {}), dict(data or {}), dict(headers or {})))
        if url.endswith("oauth2/token"):
            assert method == "POST" and data["grant_type"] == "client_credentials"
            self.tokens += 1
            return 200, {"access_token": f"tok{self.tokens}", "expires_in": 3600}
        auth = (headers or {}).get("Authorization", "")
        if self.expire_first_token and auth == "Bearer tok1":
            return 401, {"status": 401, "message": "Invalid OAuth token"}
        if url.endswith("/videos"):
            if not params.get("after"):
                return 200, {"data": [{"id": "10", "created_at": "2026-09-01T00:00:00Z"},
                                      {"id": "9", "created_at": "2026-08-20T00:00:00Z"}],
                             "pagination": {"cursor": "c1"}}
            return 200, {"data": [{"id": "8", "created_at": "2026-08-01T00:00:00Z"}],
                         "pagination": {}}
        if url.endswith("/users"):
            return 200, {"data": [{"id": "u1", "login": params.get("login")}]}
        if url.endswith("/games"):
            return 200, {"data": [{"id": "g1", "name": params.get("name")}]}
        if url.endswith("/streams"):
            return 200, {"data": [{"user_login": "a", "viewer_count": 5}]}
        return 404, {}


def test_helix_paginate_headers_and_since():
    fake = _FakeHelix()
    app = tw.TwitchApp("cid", "sec", cache_path=None, request=fake)
    vids = asyncio.run(app.archives("u1"))
    assert [v["id"] for v in vids] == ["10", "9", "8"], vids
    video_calls = [c for c in fake.calls if c[1].endswith("/videos")]
    assert len(video_calls) == 2
    p, h = video_calls[0][2], video_calls[0][4]
    assert p["user_id"] == "u1" and p["type"] == "archive" and p["first"] == "100" and "after" not in p
    assert h["Client-Id"] == "cid" and h["Authorization"] == "Bearer tok1"
    assert video_calls[1][2]["after"] == "c1"
    # since: stops at the first page containing an older VOD, drops the old ones
    fake.calls.clear()
    vids = asyncio.run(app.archives("u1", since="2026-08-25"))
    assert [v["id"] for v in vids] == ["10"], vids
    assert len([c for c in fake.calls if c[1].endswith("/videos")]) == 1
    # max_items truncates
    assert [v["id"] for v in asyncio.run(app.archives("u1", max_items=2))] == ["10", "9"]
    assert asyncio.run(app.user_by_login("foo"))["id"] == "u1"
    assert asyncio.run(app.game_id("SMITE 2")) == "g1"
    assert asyncio.run(app.live_streams("g1", first=500))[0]["user_login"] == "a"
    assert [c for c in fake.calls if c[1].endswith("/streams")][0][2]["first"] == "100"
    assert fake.tokens == 1, "token minted once and reused"


def test_app_token_cache_and_401_retry():
    d = _tmp()
    cache = d / "twitch_app_token.json"
    fake = _FakeHelix(expire_first_token=True)
    app = tw.TwitchApp("cid", "sec", cache_path=cache, request=fake)
    u = asyncio.run(app.user_by_login("foo"))
    assert u["login"] == "foo"
    assert fake.tokens == 2, "401 must re-mint exactly once and retry"
    user_calls = [c for c in fake.calls if c[1].endswith("/users")]
    assert [c[4]["Authorization"] for c in user_calls] == ["Bearer tok1", "Bearer tok2"]
    cached = json.loads(cache.read_text(encoding="utf-8"))
    assert cached["access_token"] == "tok2" and cached["expires_at"] > 0
    # a fresh client reuses the cache instead of minting
    app2 = tw.TwitchApp("cid", "sec", cache_path=cache, request=fake)
    assert asyncio.run(app2.token()) == "tok2" and fake.tokens == 2
    # an expired cache is ignored
    cache.write_text(json.dumps({"access_token": "old", "expires_at": 1.0}), encoding="utf-8")
    app3 = tw.TwitchApp("cid", "sec", cache_path=cache, request=fake)
    assert asyncio.run(app3.token()) == "tok3" and fake.tokens == 3


def test_helix_error_raises():
    async def req(method, url, *, params=None, data=None, headers=None):
        if url.endswith("oauth2/token"):
            return 200, {"access_token": "t", "expires_in": 60}
        return 429, {"message": "slow down"}
    app = tw.TwitchApp("cid", "sec", request=req)
    try:
        asyncio.run(app.get("users", login="x"))
        raise AssertionError("429 must raise")
    except tw.TwitchError as e:
        assert e.status == 429 and "slow down" in str(e)


# ── download helpers ──────────────────────────────────────────────────

def test_build_ydl_opts_and_classify():
    o = dl.build_ydl_opts(Path(r"D:\x\_inbox"), "123", "best[height<=1080]/best", 4)
    assert o["outtmpl"].endswith("v123.%(ext)s") and "_inbox" in o["outtmpl"]
    assert o["format"] == "best[height<=1080]/best"
    assert o["concurrent_fragment_downloads"] == 4 and o["continuedl"] is True
    assert o["merge_output_format"] == "mp4" and o["progress_hooks"] == []
    assert dl.build_ydl_opts(Path("x"), "1", "best", 0)["concurrent_fragment_downloads"] == 1
    assert dl.classify_error("ERROR: This video is only available to subscribers") == "sub_only"
    assert dl.classify_error("ERROR: Video 123 does not exist") == "unavailable"
    assert dl.classify_error("HTTP Error 503: Service Unavailable") == "network"
    assert dl.classify_error("something odd") == "other"
    e = dl.DownloadError("sub_only", "msg")
    assert e.kind == "sub_only" and str(e) == "msg"
    assert abs(dl.estimate_gb(3600) - dl.GB_PER_HOUR) < 1e-9


def test_sidecar_and_free_space():
    d = _tmp()
    mp4 = d / "v123.mp4"
    mp4.write_bytes(b"0")
    p = dl.write_twitch_sidecar(mp4, {"vod_id": "123", "title": "Ymir vs Loki"})
    assert p == dl.twitch_sidecar_path(mp4) and p.name == "v123.twitch.json"
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "Ymir vs Loki"
    assert not list(d.glob("*.tmp"))
    assert dl.free_gb(d / "does" / "not" / "exist") > 0


# ── vods queue table ──────────────────────────────────────────────────

def test_vods_table():
    s = Store(_tmp() / "t.db")
    assert s.upsert_vod("123", "foo", title="T", created_at="2026-09-01T00:00:00Z",
                        duration_s=10.0, url="https://www.twitch.tv/videos/123") is True
    assert s.upsert_vod("123", "foo", title="T2") is False
    row = s.get_vod("123")
    assert row["title"] == "T2" and row["status"] == "queued" and row["seen_at"]
    s.upsert_vod("122", "foo", created_at="2026-08-01T00:00:00Z")
    s.upsert_vod("5", "bar", created_at="2026-08-15T00:00:00Z")
    s.set_vod_status("123", "downloaded", path=r"D:\x\v123.mp4", size_bytes=5)
    assert s.get_vod("123")["path"].endswith("v123.mp4") and s.get_vod("123")["size_bytes"] == 5
    assert s.upsert_vod("123", "foo", title="T3") is False
    assert s.get_vod("123")["status"] == "downloaded", "upsert must not reset status"
    assert [v["vod_id"] for v in s.vods(channel="foo")] == ["123", "122"]
    assert [v["vod_id"] for v in s.vods(status="queued")] == ["5", "122"]
    assert s.vod_counts("foo") == {"downloaded": 1, "queued": 1}
    assert s.vod_counts() == {"downloaded": 1, "queued": 2}
    assert s.vod_meta_for_stem("v123")["title"] == "T3"
    assert s.vod_meta_for_stem("Ymir-1") is None and s.vod_meta_for_stem("") is None
    s.set_vod_status("122", "error", error="sub_only: nope")
    assert s.get_vod("122")["error"].startswith("sub_only")
    assert s.delete_vods("foo") == 2 and s.vod_counts() == {"queued": 1}
    assert OWNER_CHANNEL == "hatmaster"


def test_vods_table_survives_old_index():
    """An index created before the vods table existed gains it on open."""
    import sqlite3
    d = _tmp()
    db = d / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE recordings (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, folder TEXT,
            stem TEXT, god TEXT, gods_seen TEXT, duration_s REAL, size_bytes INTEGER, mtime REAL,
            recorded_at TEXT, indexed_at TEXT, model TEXT, status TEXT, error TEXT);
        INSERT INTO recordings (path, status) VALUES ('C:\\rec\\Ymir\\Ymir-1.mp4', 'done');
    """)
    conn.commit()
    conn.close()
    s = Store(db)
    assert s.upsert_vod("1", "foo") is True and s.vod_counts() == {"queued": 1}
    assert s.get_recording(1)["visibility"] == "private"


# ── harness ───────────────────────────────────────────────────────────

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if not TESTS:
        print("FAIL  no tests were collected")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
