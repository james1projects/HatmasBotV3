"""
Test God Request system — queue management, OBS display, Smite detection.

Starts the webserver and walks through each scenario with timed steps.
Open http://localhost:8069 in your browser to see the control panel updating.

NOTE: These tests do NOT require MixItUp to be running. They test the queue
logic, OBS state updates, and web state independently. MixItUp-dependent
features (token spending/awarding) are stubbed.

Usage:
  python test_godrequest.py              — Run all tests sequentially
  python test_godrequest.py queue        — Test basic queue add/remove
  python test_godrequest.py matching     — Test god name fuzzy matching
  python test_godrequest.py detection    — Test Smite god detection auto-complete
  python test_godrequest.py webstate     — Test web state / control panel data
  python test_godrequest.py skip_clear   — Test skip and clear operations
  python test_godrequest.py donation     — Test donation token award logic
"""

import asyncio
import sys
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, ".")
from core.webserver import WebServer
from plugins.godrequest import GodRequestPlugin, SMITE2_GODS


# === HELPERS ===

def header(text):
    print(f"\n{'='*50}")
    print(f"  {text}")
    print(f"{'='*50}")


def step(text):
    print(f"\n  → {text}")


def check(label, condition):
    status = "✓" if condition else "✗ FAIL"
    color = "\033[92m" if condition else "\033[91m"
    print(f"    {color}{status}\033[0m {label}")
    return condition


async def wait(seconds, label=""):
    if label:
        print(f"    ⏱ Waiting {seconds}s — {label}")
    await asyncio.sleep(seconds)


def make_mock_bot(web_server=None):
    """Create a mock bot with the essentials for testing."""
    bot = MagicMock()
    bot.plugins = {}
    bot.features = {"god_requests": True}
    bot.is_feature_enabled = lambda f: bot.features.get(f, False)
    bot.web_server = web_server
    bot.send_chat = AsyncMock()
    bot.send_reply = AsyncMock()
    bot.is_mod = lambda c: getattr(c, "_is_mod", False)
    bot.register_command = MagicMock()
    return bot


def make_mock_message(username="testviewer", is_mod=False):
    """Create a mock chat message."""
    msg = MagicMock()
    msg.chatter.name = username
    msg.chatter.display_name = username
    msg.chatter._is_mod = is_mod
    return msg


# === TESTS ===

async def test_matching():
    """Test god name fuzzy matching."""
    header("TEST: God Name Matching")

    step("Exact match")
    check("'Sylvanus' → Sylvanus", GodRequestPlugin._match_god("Sylvanus") == "Sylvanus")
    check("'sylvanus' → Sylvanus", GodRequestPlugin._match_god("sylvanus") == "Sylvanus")
    check("'YMIR' → Ymir", GodRequestPlugin._match_god("YMIR") == "Ymir")

    step("Starts-with match")
    check("'syl' → Sylvanus", GodRequestPlugin._match_god("syl") == "Sylvanus")
    check("'kukulk' → Kukulkan", GodRequestPlugin._match_god("kukulk") == "Kukulkan")

    step("Contains match")
    check("'wukong' → Sun Wukong", GodRequestPlugin._match_god("wukong") == "Sun Wukong")
    check("'arthur' → King Arthur", GodRequestPlugin._match_god("arthur") == "King Arthur")

    step("No match")
    check("'asdfghjkl' → None", GodRequestPlugin._match_god("asdfghjkl") is None)
    check("'' → None", GodRequestPlugin._match_god("") is None)

    step("Ambiguous partial — should return None")
    # 'a' matches many gods, so no single match
    result = GodRequestPlugin._match_god("a")
    check("'a' → None (too many matches)", result is None)


async def test_queue(web):
    """Test basic queue operations."""
    header("TEST: Queue Add / Remove")

    plugin = GodRequestPlugin()
    plugin.queue = []  # fresh queue, don't load from file

    bot = make_mock_bot(web)
    # Stub out MixItUp (pretend connected)
    plugin._miu_connected = True
    plugin._get_token_balance = AsyncMock(return_value=5)
    plugin._spend_token = AsyncMock(return_value=True)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin
    # Stub OBS (no real OBS)
    plugin._update_obs_display = AsyncMock()

    step("Add god via !godrequest (viewer)")
    msg = make_mock_message("viewer1")
    await plugin.cmd_godrequest(msg, "sylvanus")
    check("Queue has 1 entry", len(plugin.queue) == 1)
    check("God is Sylvanus", plugin.queue[0]["god"] == "Sylvanus")
    check("Requester is viewer1", plugin.queue[0]["requester"] == "viewer1")
    check("Token was spent", plugin.queue[0]["token_spent"] is True)
    check("send_chat called", bot.send_chat.called)

    step("Add another god via !godreq (mod)")
    msg2 = make_mock_message("modperson", is_mod=True)
    await plugin.cmd_godreq(msg2, "ymir")
    check("Queue has 2 entries", len(plugin.queue) == 2)
    check("Second god is Ymir", plugin.queue[1]["god"] == "Ymir")
    check("No token spent (mod)", plugin.queue[1]["token_spent"] is False)

    step("Duplicate god rejected")
    bot.send_reply.reset_mock()
    msg3 = make_mock_message("viewer2")
    await plugin.cmd_godrequest(msg3, "sylvanus")
    check("Queue still has 2", len(plugin.queue) == 2)
    check("Reply sent about duplicate", bot.send_reply.called)

    step("Unknown god rejected")
    bot.send_reply.reset_mock()
    await plugin.cmd_godrequest(msg3, "nonexistentgod")
    check("Queue still has 2", len(plugin.queue) == 2)

    step("Show queue via !godqueue")
    bot.send_reply.reset_mock()
    await plugin.cmd_godqueue(msg, "")
    check("Reply sent with queue", bot.send_reply.called)

    step("Check web state")
    state = web._state.get("god_requests", {})
    check("Web state has queue", state.get("queue_length", 0) == 2)
    check("Next god is Sylvanus", state.get("next_god") == "Sylvanus")

    await wait(1, "letting you see the control panel")


async def test_skip_clear(web):
    """Test skip and clear commands."""
    header("TEST: Skip & Clear")

    plugin = GodRequestPlugin()
    plugin.queue = []
    bot = make_mock_bot(web)
    plugin._miu_connected = True
    plugin._get_token_balance = AsyncMock(return_value=10)
    plugin._spend_token = AsyncMock(return_value=True)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin
    plugin._update_obs_display = AsyncMock()

    # Add 3 gods
    for god in ["zeus", "athena", "ares"]:
        msg = make_mock_message(f"viewer_{god}")
        await plugin.cmd_godrequest(msg, god)

    check("Queue has 3 entries", len(plugin.queue) == 3)

    step("Skip first god")
    mod = make_mock_message("mod1", is_mod=True)
    await plugin.cmd_godskip(mod, "")
    check("Queue has 2 entries", len(plugin.queue) == 2)
    check("Zeus was removed", plugin.queue[0]["god"] == "Athena")

    step("Clear all")
    await plugin.cmd_godclear(mod, "")
    check("Queue is empty", len(plugin.queue) == 0)

    step("Skip on empty queue")
    bot.send_reply.reset_mock()
    await plugin.cmd_godskip(mod, "")
    check("Reply about empty queue", bot.send_reply.called)


async def test_detection(web):
    """Test Smite god detection auto-completing requests."""
    header("TEST: Smite God Detection")

    plugin = GodRequestPlugin()
    plugin.queue = []
    bot = make_mock_bot(web)
    plugin._miu_connected = True
    plugin._get_token_balance = AsyncMock(return_value=10)
    plugin._spend_token = AsyncMock(return_value=True)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin
    plugin._update_obs_display = AsyncMock()

    # Add Sylvanus to queue
    msg = make_mock_message("viewer1")
    await plugin.cmd_godrequest(msg, "sylvanus")
    check("Sylvanus in queue", plugin.queue[0]["god"] == "Sylvanus")

    step("Detect playing Sylvanus")
    bot.send_chat.reset_mock()
    await plugin._on_god_detected({"name": "Sylvanus", "team": "order"})
    check("Queue is now empty", len(plugin.queue) == 0)
    check("Completion message sent", bot.send_chat.called)
    check("Message mentions viewer1",
          any("viewer1" in str(c) for c in bot.send_chat.call_args_list))

    step("Add Ymir, detect wrong god (Athena)")
    await plugin.cmd_godrequest(msg, "ymir")
    bot.send_chat.reset_mock()
    await plugin._on_god_detected({"name": "Athena", "team": "chaos"})
    check("Ymir still in queue", len(plugin.queue) == 1)
    check("No completion message", not bot.send_chat.called)

    step("Now detect Ymir")
    await plugin._on_god_detected({"name": "Ymir", "team": "order"})
    check("Queue empty after Ymir detected", len(plugin.queue) == 0)


async def test_webstate(web):
    """Test that web state updates correctly for the control panel."""
    header("TEST: Web State")

    plugin = GodRequestPlugin()
    plugin.queue = []
    bot = make_mock_bot(web)
    plugin._miu_connected = True
    plugin._get_token_balance = AsyncMock(return_value=5)
    plugin._spend_token = AsyncMock(return_value=True)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin
    plugin._update_obs_display = AsyncMock()

    step("Empty queue state")
    plugin._update_web_state()
    state = web._state["god_requests"]
    check("next_god is None", state["next_god"] is None)
    check("queue_length is 0", state["queue_length"] == 0)
    check("mixitup_connected is True", state["mixitup_connected"] is True)

    step("Add gods and check state")
    msg = make_mock_message("viewer1")
    await plugin.cmd_godrequest(msg, "ra")
    await plugin.cmd_godrequest(msg, "thor")
    state = web._state["god_requests"]
    check("next_god is Ra", state["next_god"] == "Ra")
    check("queue_length is 2", state["queue_length"] == 2)
    check("queue has correct entries", len(state["queue"]) == 2)

    step("Open http://localhost:8069 to see the god request section!")
    await wait(2, "view control panel")


async def test_donation():
    """Test donation token award logic (no MixItUp needed)."""
    header("TEST: Donation Token Awards")

    plugin = GodRequestPlugin()
    plugin.queue = []
    bot = make_mock_bot()
    plugin._miu_connected = True
    plugin._award_token = AsyncMock(return_value=True)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin

    step("$5 donation → 1 token")
    await plugin.award_donation_tokens("donor1", 5.0)
    check("award_token called with 1", plugin._award_token.call_args[0] == ("donor1", 1))

    step("$12 donation → 2 tokens ($5 threshold)")
    plugin._award_token.reset_mock()
    await plugin.award_donation_tokens("donor2", 12.0)
    check("award_token called with 2", plugin._award_token.call_args[0] == ("donor2", 2))

    step("$3 donation → 0 tokens (below threshold)")
    plugin._award_token.reset_mock()
    await plugin.award_donation_tokens("donor3", 3.0)
    check("award_token not called", not plugin._award_token.called)

    step("$25 donation → 5 tokens")
    plugin._award_token.reset_mock()
    await plugin.award_donation_tokens("donor4", 25.0)
    check("award_token called with 5", plugin._award_token.call_args[0] == ("donor4", 5))


async def test_no_tokens(web):
    """Test what happens when viewer has no tokens."""
    header("TEST: No Tokens")

    plugin = GodRequestPlugin()
    plugin.queue = []
    bot = make_mock_bot(web)
    plugin._miu_connected = True
    plugin._get_token_balance = AsyncMock(return_value=0)
    plugin._spend_token = AsyncMock(return_value=False)
    plugin.setup(bot)
    bot.plugins["godrequest"] = plugin
    plugin._update_obs_display = AsyncMock()

    step("Request with 0 tokens")
    msg = make_mock_message("brokeviewer")
    await plugin.cmd_godrequest(msg, "zeus")
    check("Queue is empty", len(plugin.queue) == 0)
    check("Reply about insufficient tokens", bot.send_reply.called)
    check("Message mentions tokens",
          any("Token" in str(c) for c in bot.send_reply.call_args_list))


# === RUNNER ===

ALL_TESTS = {
    "matching": test_matching,
    "queue": test_queue,
    "skip_clear": test_skip_clear,
    "detection": test_detection,
    "webstate": test_webstate,
    "donation": test_donation,
    "no_tokens": test_no_tokens,
}

NEEDS_WEB = {"queue", "skip_clear", "detection", "webstate", "no_tokens"}


async def run():
    chosen = sys.argv[1] if len(sys.argv) > 1 else None

    if chosen and chosen not in ALL_TESTS:
        print(f"Unknown test: {chosen}")
        print(f"Available: {', '.join(ALL_TESTS.keys())}")
        return

    tests = {chosen: ALL_TESTS[chosen]} if chosen else ALL_TESTS

    # Start webserver if any test needs it
    web = None
    if any(t in NEEDS_WEB for t in tests):
        web = WebServer()
        await web.start()
        print(f"\n  Control panel: http://localhost:8069/")

    print(f"\n  Running {len(tests)} test(s)...")

    for name, test_fn in tests.items():
        try:
            if name in NEEDS_WEB:
                await test_fn(web)
            else:
                await test_fn()
        except Exception as e:
            print(f"\n  ✗ {name} CRASHED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  All tests complete!")
    print(f"{'='*50}\n")

    if web:
        await web.stop()


if __name__ == "__main__":
    asyncio.run(run())
