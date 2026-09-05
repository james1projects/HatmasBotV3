"""
Tests for plugins/cocaster (co-caster stage 1: chat log, prompts, voice
plumbing, plugin logic with fakes).

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp dirs only, no network, no audio playback, no PowerShell,
no Anthropic SDK calls (backends are faked).
"""

import asyncio
import json
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.cocaster.chatlog import ChatLog  # noqa: E402
from plugins.cocaster.summarizer import (RateLimiter, build_ear_prompt,  # noqa: E402
                                         build_line_prompt, clean_spoken,
                                         format_context, format_messages)
from plugins.cocaster.voice import Voice, find_output_device, wav_to_array  # noqa: E402
from plugins.cocaster import plugin as plugin_mod  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="cocaster_test_"))


# ── chat log ──────────────────────────────────────────────────────────

def test_chatlog_add_recent_count_stats():
    log = ChatLog(_tmp() / "chat.db")
    t0 = 1_000_000.0
    assert log.add("Dyna", "Dyna", "hello there", ts=t0) == 1
    assert log.add("bob", "Bob", "!sr some song", is_command=True, ts=t0 + 1) == 2
    assert log.add("bob", "Bob", "what build?", ts=t0 + 2, is_mod=True) == 3
    assert log.add("bob", "Bob", "   ", ts=t0 + 3) == 0          # blank dropped
    rec = log.recent(since_ts=t0)
    assert [m["text"] for m in rec] == ["what build?"]          # commands excluded, newer than t0
    assert [m["text"] for m in log.recent(since_ts=t0 - 1, include_commands=True)] == \
        ["hello there", "!sr some song", "what build?"]
    assert log.recent(limit=1)[0]["text"] == "what build?"
    assert log.count_since(t0) == 1 and log.count_since(t0, include_commands=True) == 2
    st = log.stats()
    assert st["messages"] == 3 and st["users"] == 2 and st["commands"] == 1
    assert st["first_ts"] == t0 and st["last_ts"] == t0 + 2
    assert rec[0]["user"] == "bob" and rec[0]["is_mod"] == 1
    log.close()


# ── prompts ───────────────────────────────────────────────────────────

def test_clean_spoken_strips_markdown_quotes_emoji_and_caps_words():
    assert clean_spoken('  "**Two people** want a song" ') == "Two people want a song"
    assert clean_spoken("!buy now") == "buy now"
    assert clean_spoken("Nice one \U0001F525\U0001F525") == "Nice one"
    long = " ".join(f"w{i}" for i in range(60))
    out = clean_spoken(long, max_words=10)
    assert out.endswith(".") and len(out.split()) == 10
    assert clean_spoken("") == ""
    # first line wins when the model rambles onto extra lines
    assert clean_spoken("Chat is asking about your build.\nAlso: more stuff") == "Chat is asking about your build."


def test_format_context_and_messages():
    ctx = {"is_live": True, "god": "Ymir", "kda": [3, 1, 4], "match_minutes": 12.6,
           "viewers": 41, "queue_len": 2, "song": "Favorite Liar"}
    text = format_context(ctx)
    assert "Stream: live" in text and "Playing Ymir, KDA 3/1/4, 12 min into the match" in text
    assert "Viewers: 41" in text and "God request queue: 2" in text and "Favorite Liar" in text
    assert "Not in a match" in format_context({})
    msgs = [{"display": "Dyna", "text": "hi"}, {"user": "bob", "text": "  "}, {"user": "cat", "text": "x" * 500}]
    fm = format_messages(msgs)
    assert fm.startswith("Dyna: hi\ncat: ") and len(fm.split("\n")) == 2 and len(fm) < 300


def test_build_ear_and_line_prompts():
    msgs = [{"display": "Dyna", "text": "what build are you going?"}]
    p = build_ear_prompt(msgs, {"god": "Loki"}, previous="Dyna asked about runes", max_words=30)
    assert "at most 30 words" in p["system"] and "Nothing new in chat" in p["system"]
    assert "Dyna: what build are you going?" in p["prompt"]
    assert "Playing Loki" in p["prompt"] and "Do not repeat that" in p["prompt"]
    p2 = build_ear_prompt([], {}, None)
    assert "(none)" in p2["prompt"] and "last told him" not in p2["prompt"]
    line = build_line_prompt({"kind": "multikill", "detail": "triple", "count": None},
                             {"god": "Ymir", "kda": [5, 0, 2]}, "PERSONA TEXT", max_words=20,
                             recent_chat=[{"display": "bob", "text": "LETS GO"}])
    assert line["system"].startswith("PERSONA TEXT") and "at most 20 words" in line["system"]
    assert "Event: multikill (triple)" in line["prompt"] and "bob: LETS GO" in line["prompt"]
    death = build_line_prompt({"kind": "death", "count": 7}, {}, "P")
    assert "Event: death" in death["prompt"] and "Count this session: 7" in death["prompt"]


def test_rate_limiter():
    rl = RateLimiter(45)
    assert rl.allow(now=1000.0)
    assert not rl.allow(now=1030.0)
    assert rl.allow(now=1046.0)


# ── voice plumbing (no audio) ─────────────────────────────────────────

def test_wav_to_array_and_device_lookup():
    d = _tmp()
    path = d / "t.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(struct.pack("<4h", 0, 16384, -16384, 32767))
    data, sr = wav_to_array(path)
    assert sr == 22050 and data.shape == (4, 1)
    assert abs(data[1, 0] - 0.5) < 1e-3 and abs(data[2, 0] + 0.5) < 1e-3
    devices = [
        {"name": "Microsoft Sound Mapper - Output", "max_output_channels": 2, "hostapi": 0},
        {"name": "Headphones (2- Elgato XLR Dock)", "max_output_channels": 2, "hostapi": 0},
        {"name": "Headphones (2- Elgato XLR Dock)", "max_output_channels": 2, "hostapi": 2},
        {"name": "Microphone (Elgato)", "max_output_channels": 0, "hostapi": 0},
    ]
    assert find_output_device("headphones", devices) == 1       # first host API wins
    assert find_output_device("Microphone", devices) is None     # input-only never matches
    assert find_output_device("", devices) is None
    assert find_output_device("nope", devices) is None


def test_voice_disabled_never_synthesizes():
    v = Voice("Headphones", enabled=False, work_dir=_tmp())
    assert v.speak("hello") is False and v.last_text == ""


# ── plugin logic with fakes ───────────────────────────────────────────

class _FakeBot:
    def __init__(self, enabled=True):
        self.features = {"cocaster": enabled}
        self.plugins = {}
        self.raw = []
        self.commands = {}
        self.replies = []

    def is_feature_enabled(self, name):
        return self.features.get(name, False)

    def register_raw_handler(self, h):
        self.raw.append(h)

    def register_command(self, name, handler, mod_only=False, **kw):
        self.commands[name] = handler

    def is_mod(self, chatter):
        return getattr(chatter, "mod", False)

    async def send_reply(self, message, text, whisper=False):
        self.replies.append(text)


class _FakeBackend:
    name = "fake"
    model = "fake-1"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, system, prompt, max_tokens=200):
        self.calls.append((system, prompt))
        return self.reply


class _FakeVoice:
    def __init__(self):
        self.spoken = []
        self.enabled = True
        self.device_substring = "fake"

    def speak(self, text):
        self.spoken.append(text)
        return True

    def resolve_device(self):
        return 7


class _Chatter:
    def __init__(self, name, mod=False):
        self.name = name
        self.display_name = name.title()
        self.mod = mod


class _Msg:
    def __init__(self, name, text, mod=False):
        self.chatter = _Chatter(name, mod)
        self.text = text


def _plugin(enabled=True, reply="Dyna wants to know your build."):
    d = _tmp()
    p = plugin_mod.CoCasterPlugin()
    bot = _FakeBot(enabled)
    p.setup(bot)
    p.chatlog = ChatLog(d / "chat.db")
    p.backend = _FakeBackend(reply)
    p.ear_voice = _FakeVoice()
    p.stream_voice = _FakeVoice()
    p.stream_voice.enabled = False
    p._lines_path = d / "lines.jsonl"
    p._last_ear_ts = 0.0
    return p, bot


def test_on_chat_logs_messages_and_flags():
    p, bot = _plugin()
    assert len(bot.raw) == 1 and "cocaster" in bot.commands
    asyncio.run(p._on_chat(_Msg("dyna", "hello")))
    asyncio.run(p._on_chat(_Msg("bob", "!sr song", mod=True)))
    asyncio.run(p._on_chat(_Msg("", "no user")))
    rows = p.chatlog.recent(include_commands=True)
    assert [(r["user"], r["is_command"], r["is_mod"]) for r in rows] == [("dyna", 0, 0), ("bob", 1, 1)]


def test_ear_tick_speaks_summary_and_respects_floor_and_toggle():
    p, bot = _plugin()
    p.chatlog.add("dyna", "Dyna", "what build?")
    # below the floor of 3 messages: silent
    assert asyncio.run(p.ear_tick()) is None
    p.chatlog.add("bob", "Bob", "hi")
    p.chatlog.add("cat", "Cat", "song pls")
    text = asyncio.run(p.ear_tick())
    assert text == "Dyna wants to know your build."
    assert p.ear_voice.spoken == [text] and p.stats["ear_summaries"] == 1
    system, prompt = p.backend.calls[-1]
    assert "Dyna: what build?" in prompt and "earpiece" in system
    # same messages are not re-read next tick
    assert asyncio.run(p.ear_tick()) is None
    # a written line record exists
    rec = json.loads(p._lines_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["channel"] == "ear" and rec["messages"] == 3
    # toggle off: nothing spoken, unread pointer advances
    bot.features["cocaster"] = False
    p.chatlog.add("dyna", "Dyna", "one"); p.chatlog.add("dyna", "Dyna", "two"); p.chatlog.add("dyna", "Dyna", "three")
    assert asyncio.run(p.ear_tick()) is None
    bot.features["cocaster"] = True
    assert asyncio.run(p.ear_tick()) is None                      # already consumed while off
    # "Nothing new in chat" from the model is swallowed
    p.backend.reply = "Nothing new in chat."
    p.chatlog.add("a", "A", "x"); p.chatlog.add("b", "B", "y"); p.chatlog.add("c", "C", "z")
    assert asyncio.run(p.ear_tick()) is None and len(p.ear_voice.spoken) == 1


def test_caster_line_rate_limit_and_stream_voice_switch():
    p, bot = _plugin(reply="**Triple kill.** Chat, breathe.")
    emitted = []

    class _Overlay:
        async def emit(self, name, data=None):
            emitted.append((name, data))
    p.overlay_manager = _Overlay()
    text = asyncio.run(p.caster_line({"kind": "multikill", "detail": "triple"}))
    assert text == "Triple kill. Chat, breathe."
    assert emitted and emitted[0][0] == "cocaster_line" and emitted[0][1]["text"] == text
    assert p.stream_voice.spoken == []                            # text only by default
    assert asyncio.run(p.caster_line({"kind": "death"})) is None  # inside cooldown
    p.stream_voice.enabled = True
    p._line_limiter.last_at = 0.0
    assert asyncio.run(p.caster_line({"kind": "death", "count": 4})) == text
    assert p.stream_voice.spoken == [text] and p.stats["lines"] == 2


def test_death_listener_fires_every_other_death():
    p, bot = _plugin(reply="Oof.")
    asyncio.run(p._on_death(1))
    assert p.stats["lines"] == 0
    asyncio.run(p._on_death(1))
    assert p.stats["lines"] == 1


def test_cocaster_command_and_status():
    p, bot = _plugin(reply="Chat says hi.")
    handler = bot.commands["cocaster"]
    asyncio.run(handler(None, "off"))
    assert p.muted and bot.replies[-1] == "co-caster muted"
    assert asyncio.run(p.ear_tick()) is None
    asyncio.run(handler(None, "on"))
    assert not p.muted
    asyncio.run(handler(None, "test hello there"))
    assert p.ear_voice.spoken[-1] == "hello there" and bot.replies[-1] == "earpiece test sent"
    p.chatlog.add("dyna", "Dyna", "hey")
    asyncio.run(handler(None, "now"))
    assert bot.replies[-1] == "ear: Chat says hi."
    asyncio.run(handler(None, "status"))
    assert bot.replies[-1].startswith("co-caster on")
    s = p.status()
    assert s["backend"] == "fake" and s["chat_messages"] == 1 and s["ear_summaries"] == 1
    assert s["ear_device_index"] == 7 and s["stream_voice"] is False


def test_persona_and_backend_selection_are_safe_without_config():
    p, bot = _plugin()
    assert "co-caster" in p._load_persona().lower()
    # backend factory never raises, even with no key: returns None
    real_key = getattr(plugin_mod.config, "CLAUDE_API_KEY", None)
    try:
        plugin_mod.config.CLAUDE_API_KEY = ""
        plugin_mod.config.COCASTER_LLM_BACKEND = "claude"
        assert p._make_backend() is None
        plugin_mod.config.COCASTER_LLM_BACKEND = "ollama"
        assert p._make_backend().name == "ollama"
    finally:
        plugin_mod.config.CLAUDE_API_KEY = real_key
        plugin_mod.config.COCASTER_LLM_BACKEND = "claude"


# ── chat stats tool ───────────────────────────────────────────────────

def test_chat_stats_summarize_and_load():
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import chat_stats
    d = _tmp()
    log = ChatLog(d / "chat.db")
    base = time.time() - 3600
    for i in range(6):
        log.add("dyna", "Dyna", f"msg {i}", ts=base + i)
    log.add("bob", "Bob", "!sr song", is_command=True, ts=base + 10)
    log.add("bob", "Bob", "!SR again", is_command=True, ts=base + 11)
    log.add("cat", "Cat", "hi", ts=base + 12)
    log.add("old", "Old", "ancient", ts=base - 40 * 86400)
    log.close()
    rows = chat_stats.load_rows(d / "chat.db", time.time() - 30 * 86400)
    assert len(rows) == 9                                   # the 40-day-old row is excluded
    s = chat_stats.summarize(rows, top=2)
    assert s["messages"] == 9 and s["users"] == 3 and s["days"] == 1
    assert s["commands"] == {"!sr": 2}
    assert s["top_users"] == [("dyna", 6), ("bob", 2)]
    assert s["users_5plus"] == 1 and s["one_message_users"] == 1
    assert abs(s["command_share"] - 2 / 9) < 1e-3 and abs(s["lurker_share"] - 1 / 3) < 1e-3  # rounded to 3 dp
    assert chat_stats.summarize([])["messages"] == 0


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
