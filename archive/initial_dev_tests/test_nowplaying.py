"""
Test Now Playing overlay — heart animations, auto show/hide, playlist display.

Starts the webserver and walks through each scenario with timed steps.
Open http://localhost:8069/overlay/nowplaying in your browser (or OBS preview)
before running, then watch the overlay as each test plays out.

Usage:
  python test_nowplaying.py              — Run all tests sequentially
  python test_nowplaying.py hearts       — Only test heart animations
  python test_nowplaying.py playlist     — Only test playlist song display
  python test_nowplaying.py showhide     — Only test auto show/hide timing
  python test_nowplaying.py likeshow     — Only test like re-show behavior
  python test_nowplaying.py transition   — Only test requested→playlist transition
  python test_nowplaying.py playlisthide — Only test playlist auto-hide (15s)
  python test_nowplaying.py playlistlike — Only test liking playlist songs
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")
from core.webserver import WebServer


def make_requested_song(title="Bohemian Rhapsody", artist="Queen",
                        requester="SomeViewer", likes=0):
    return {
        "title": title,
        "artist": artist,
        "requester": requester,
        "album_art": None,
        "duration_ms": 240000,
        "likes": likes,
        "started_at": time.time(),
        "source": "spotify",
        "progress_ms": 0,
        "is_playing": True,
        "last_updated": time.time(),
    }


def make_playlist_song(title="Comfortably Numb", artist="Pink Floyd", likes=0):
    return {
        "title": title,
        "artist": artist,
        "requester": None,
        "album_art": None,
        "duration_ms": 380000,
        "likes": likes,
        "started_at": time.time(),
        "source": "spotify",
        "is_playlist": True,
        "progress_ms": 0,
        "is_playing": True,
        "last_updated": time.time(),
    }


async def wait(seconds, label=""):
    if label:
        print(f"    ⏳ {label} ({seconds}s)")
    await asyncio.sleep(seconds)


# =============================================================
# TEST: Heart animations on like
# =============================================================

async def test_hearts(web):
    print(f"\n{'='*60}")
    print(f"TEST: Heart Animations")
    print(f"{'='*60}")
    print(f"  Expect: Overlay slides in, then hearts float up on each like\n")

    # Show a requested song
    song = make_requested_song()
    web.update_now_playing(song)
    await wait(2, "Song appears")

    # Single like
    print("  >> Like #1")
    song["likes"] = 1
    web.update_now_playing(song)
    web.trigger_like_event()
    await wait(3, "Hearts float up")

    # Rapid likes (3 in quick succession)
    print("  >> Rapid likes (3x)")
    for i in range(2, 5):
        song["likes"] = i
        web.update_now_playing(song)
        web.trigger_like_event()
        await asyncio.sleep(0.8)
    await wait(3, "Multiple heart bursts")

    # One more like after a pause
    print("  >> Like after pause")
    song["likes"] = 5
    web.update_now_playing(song)
    web.trigger_like_event()
    await wait(2, "Final hearts")

    web.update_now_playing(None)
    await wait(6, "Overlay hides")

    print("  HEARTS TEST COMPLETE")


# =============================================================
# TEST: Playlist song display
# =============================================================

async def test_playlist(web):
    print(f"\n{'='*60}")
    print(f"TEST: Playlist Song Display")
    print(f"{'='*60}")
    print(f"  Expect: Overlay shows 'playing from playlist', no likes counter,")
    print(f"  no requester, then transitions to a requested song\n")

    # Show a playlist song
    print("  >> Playlist song")
    web.update_now_playing(make_playlist_song())
    await wait(4, "Shows 'playing from playlist'")

    # Transition to a different playlist song
    print("  >> Next playlist song")
    web.update_now_playing(make_playlist_song(
        title="Time", artist="Pink Floyd"
    ))
    await wait(4, "New playlist song appears")

    # Transition to a requested song
    print("  >> Requested song comes on")
    web.update_now_playing(make_requested_song(
        title="Don't Stop Me Now", artist="Queen",
        requester="HatFan42", likes=0
    ))
    await wait(4, "Shows 'now playing' with requester and likes")

    # Back to playlist
    print("  >> Back to playlist")
    web.update_now_playing(make_playlist_song(
        title="Wish You Were Here", artist="Pink Floyd"
    ))
    await wait(4, "Back to 'playing from playlist'")

    web.update_now_playing(None)
    await wait(6, "Overlay hides")

    print("  PLAYLIST TEST COMPLETE")


# =============================================================
# TEST: Auto show/hide timing
# =============================================================

async def test_showhide(web):
    print(f"\n{'='*60}")
    print(f"TEST: Auto Show/Hide Timing")
    print(f"{'='*60}")
    print(f"  Expect: Overlay slides in for each song, hides 5s after song stops\n")

    # Song starts — overlay should appear
    print("  >> Song starts")
    web.update_now_playing(make_requested_song())
    await wait(3, "Overlay slides in")

    # Song ends — overlay should hide after 5s
    print("  >> Song stops (now_playing = None)")
    web.update_now_playing(None)
    print("    ⏳ Overlay should still be visible...")
    await asyncio.sleep(3)
    print("    ⏳ ...still visible (3s elapsed)...")
    await asyncio.sleep(3)
    print("    ✓ Should be hidden now (6s elapsed)")

    await wait(2, "Pause before next test")

    # New song — overlay should re-appear
    print("  >> New song starts")
    web.update_now_playing(make_requested_song(
        title="Under Pressure", artist="Queen & David Bowie",
        requester="ChatUser99"
    ))
    await wait(3, "Overlay slides back in")

    # Song stops mid-play
    print("  >> Song stops again")
    web.update_now_playing(None)
    await wait(7, "Overlay hides after 5s")

    print("  SHOW/HIDE TEST COMPLETE")


# =============================================================
# TEST: Like re-show behavior
# =============================================================

async def test_likeshow(web):
    print(f"\n{'='*60}")
    print(f"TEST: Like Re-Show Behavior")
    print(f"{'='*60}")
    print(f"  Expect: After overlay hides, a like brings it back,")
    print(f"  then it hides 5s after last like. Multiple likes reset the timer.\n")

    # Show a song, then let it "end" so overlay hides
    print("  >> Song plays, then stops")
    song = make_requested_song(title="Somebody to Love", artist="Queen",
                               requester="MercuryFan", likes=2)
    web.update_now_playing(song)
    await wait(3, "Overlay visible")

    web.update_now_playing(None)
    await wait(7, "Overlay hides after 5s")

    # Bring back the song data (simulate: song is still technically "current"
    # in the bot even though playback monitor cleared it)
    print("  >> Late like while overlay is hidden")
    web.update_now_playing(song)
    song["likes"] = 3
    web.update_now_playing(song)
    web.trigger_like_event()
    await wait(2, "Overlay re-shows with hearts!")

    # Another like resets the 5s timer
    print("  >> Another like at 2s (should reset 5s timer)")
    song["likes"] = 4
    web.update_now_playing(song)
    web.trigger_like_event()
    print("    ⏳ Timer resets — overlay stays visible...")
    await asyncio.sleep(3)
    print("    ⏳ ...still visible (3s since last like)...")
    await asyncio.sleep(3)
    print("    ✓ Should be hidden now (6s since last like)")

    await wait(2, "Pause")

    # Verify: likes during active playback do NOT cause hiding
    print("  >> Likes during normal playback (should NOT trigger hide)")
    song2 = make_requested_song(title="Radio Ga Ga", artist="Queen",
                                requester="FreddieFan", likes=0)
    web.update_now_playing(song2)
    await wait(2, "Song playing normally")

    for i in range(1, 4):
        song2["likes"] = i
        web.update_now_playing(song2)
        web.trigger_like_event()
        print(f"    >> Like #{i}")
        await asyncio.sleep(1.5)

    await wait(6, "Overlay should STILL be visible (no hide timer)")

    # Now clear it
    web.update_now_playing(None)
    await wait(7, "Song stops, overlay hides normally")

    print("  LIKE RE-SHOW TEST COMPLETE")


# =============================================================
# TEST: Requested → playlist transition
# =============================================================

async def test_transition(web):
    print(f"\n{'='*60}")
    print(f"TEST: Requested → Playlist Transition")
    print(f"{'='*60}")
    print(f"  Expect: Requested song plays, then overlay hides for ~5s,")
    print(f"  then playlist song slides in. Mimics what the bot does when")
    print(f"  a queued song ends and Spotify resumes the playlist.\n")

    # Requested song playing
    print("  >> Requested song playing")
    web.update_now_playing(make_requested_song(
        title="We Will Rock You", artist="Queen",
        requester="RockFan88", likes=3
    ))
    await wait(4, "Overlay shows requested song")

    # Requested song ends — bot sets now_playing to None
    print("  >> Requested song ends (now_playing = None)")
    web.update_now_playing(None)
    print("    ⏳ Overlay should start hiding (5s timer)...")
    await asyncio.sleep(3)
    print("    ⏳ ...still visible (3s)...")
    await asyncio.sleep(3)
    print("    ✓ Should be hidden now (6s)")

    await wait(2, "Brief pause (simulating the 8s delay in the bot)")

    # Bot shows playlist song after delay
    print("  >> Playlist song appears after delay")
    web.update_now_playing(make_playlist_song(
        title="Stairway to Heaven", artist="Led Zeppelin"
    ))
    await wait(4, "Playlist song slides in")

    # Playlist song should auto-hide after 15s
    print("  >> Waiting for playlist auto-hide (15s)...")
    await asyncio.sleep(8)
    print("    ⏳ ...still visible (8s)...")
    await asyncio.sleep(5)
    print("    ⏳ ...still visible (13s)...")
    await asyncio.sleep(4)
    print("    ✓ Should be hidden now (17s)")

    await wait(2, "Pause")

    # New requested song comes in — should show immediately
    print("  >> New requested song interrupts")
    web.update_now_playing(make_requested_song(
        title="Killer Queen", artist="Queen",
        requester="FreddieLover", likes=0
    ))
    await wait(4, "Requested song slides in (stays visible)")

    web.update_now_playing(None)
    await wait(7, "Clean up")

    print("  TRANSITION TEST COMPLETE")


# =============================================================
# TEST: Playlist auto-hide (15s)
# =============================================================

async def test_playlisthide(web):
    print(f"\n{'='*60}")
    print(f"TEST: Playlist Auto-Hide (15 seconds)")
    print(f"{'='*60}")
    print(f"  Expect: Playlist song shows for 15s then auto-hides.")
    print(f"  Next playlist song re-shows it for another 15s.\n")

    # First playlist song
    print("  >> Playlist song #1")
    web.update_now_playing(make_playlist_song(
        title="Money", artist="Pink Floyd"
    ))
    print("    ⏳ Overlay shows, counting 15s...")
    await asyncio.sleep(8)
    print("    ⏳ ...still visible (8s)...")
    await asyncio.sleep(5)
    print("    ⏳ ...still visible (13s)...")
    await asyncio.sleep(4)
    print("    ✓ Should be hidden now (17s)")

    await wait(3, "Pause between songs")

    # Second playlist song — overlay should re-appear
    print("  >> Playlist song #2")
    web.update_now_playing(make_playlist_song(
        title="Brain Damage", artist="Pink Floyd"
    ))
    await wait(5, "Overlay re-appears with new song")

    # Like during the 15s window — resets to 5s hide (from like logic)
    print("  >> Like during playlist display")
    np = web._state.get("now_playing", {})
    if np:
        np["likes"] = 1
        web.update_now_playing(np)
    web.trigger_like_event()
    await wait(2, "Hearts appear, timer resets to 5s")
    print("    ⏳ Waiting 6s for like-based hide...")
    await asyncio.sleep(6)
    print("    ✓ Should be hidden now (5s after like)")

    await wait(2, "Pause")

    # Verify requested songs do NOT auto-hide
    print("  >> Requested song (should NOT auto-hide)")
    web.update_now_playing(make_requested_song(
        title="Thunderstruck", artist="AC/DC",
        requester="RockChatter"
    ))
    await wait(18, "Overlay stays visible the entire time (no 15s hide)")

    web.update_now_playing(None)
    await wait(7, "Clean up")

    print("  PLAYLIST HIDE TEST COMPLETE")


# =============================================================
# TEST: Liking playlist songs
# =============================================================

async def test_playlistlike(web):
    print(f"\n{'='*60}")
    print(f"TEST: Liking Playlist Songs")
    print(f"{'='*60}")
    print(f"  Expect: Playlist songs with existing likes show the counter.")
    print(f"  Songs with 0 likes hide the counter. Likes trigger hearts.\n")

    # Playlist song with no likes
    print("  >> Playlist song with 0 likes (counter hidden)")
    web.update_now_playing(make_playlist_song(
        title="Eclipse", artist="Pink Floyd", likes=0
    ))
    await wait(4, "No likes counter visible")

    # Playlist song with existing likes (was requested before)
    print("  >> Playlist song with 3 existing likes (counter visible)")
    web.update_now_playing(make_playlist_song(
        title="Bohemian Rhapsody", artist="Queen", likes=3
    ))
    await wait(4, "Shows ♡ 3")

    # Someone likes the playlist song
    print("  >> Like on playlist song")
    np = web._state.get("now_playing", {})
    if np:
        np["likes"] = 4
        web.update_now_playing(np)
    web.trigger_like_event()
    await wait(3, "Hearts + counter updates to ♡ 4")

    # Another like
    print("  >> Another like")
    np = web._state.get("now_playing", {})
    if np:
        np["likes"] = 5
        web.update_now_playing(np)
    web.trigger_like_event()
    await wait(3, "Hearts + counter updates to ♡ 5")

    web.update_now_playing(None)
    await wait(6, "Overlay hides")

    print("  PLAYLIST LIKE TEST COMPLETE")


# =============================================================
# MAIN
# =============================================================

TESTS = {
    "hearts": test_hearts,
    "playlist": test_playlist,
    "showhide": test_showhide,
    "likeshow": test_likeshow,
    "transition": test_transition,
    "playlisthide": test_playlisthide,
    "playlistlike": test_playlistlike,
}


async def main():
    web = WebServer()
    await web.start()

    print(f"\n{'='*60}")
    print(f"  Now Playing Overlay Test")
    print(f"  Open http://localhost:8069/overlay/nowplaying in your browser")
    print(f"{'='*60}")

    # Give user time to open the overlay
    await wait(3, "Starting tests...")

    # Determine which tests to run
    if len(sys.argv) > 1:
        test_name = sys.argv[1].lower()
        if test_name in TESTS:
            await TESTS[test_name](web)
        else:
            print(f"  Unknown test: {test_name}")
            print(f"  Available: {', '.join(TESTS.keys())}")
            await web.stop()
            return
    else:
        for name, test_fn in TESTS.items():
            await test_fn(web)
            await wait(2, "Pause between tests")

    print(f"\n{'='*60}")
    print(f"ALL TESTS COMPLETE")
    print(f"{'='*60}")

    await web.stop()


if __name__ == "__main__":
    asyncio.run(main())
