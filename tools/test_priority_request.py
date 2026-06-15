"""
test_priority_request.py — regression tests for the Stripe money path
=====================================================================

Exercises PriorityRequestPlugin.handle_webhook end-to-end against an
in-memory SQLite DB and a fake godrequest plugin, with Stripe's
signature verification stubbed at the module boundary (everything
after construct_event is real code).

Covers the failure modes that matter when real money is involved:
  - bad signature / malformed payload rejection
  - idempotent webhook replay (no duplicate queue entries)
  - crash-window recovery (claimed 'paid' but never queued —
    replaying the webhook must finish the job, exactly once)
  - refund + dispute events marking the row and unqueuing the god
  - played_at stamping via godrequest's history listener
  - plain-text chat announcements (Tone rule: no emojis)

Run:
    python tools/test_priority_request.py            # whole suite
    python tools/test_priority_request.py refund     # name filter

Exit 0 if every test passes, 1 otherwise. Same conventions as
tools/test_kda_fixture.py. No network, no real Stripe calls, no
files touched — safe to wire into a Stream Deck button or run
before any deploy that touches priority_request.py.
"""

import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import aiosqlite
except ImportError:
    print("Missing aiosqlite. Install with: pip install aiosqlite")
    sys.exit(1)

import plugins.priority_request as pr
from plugins.priority_request import PriorityRequestPlugin


# ──────────────────────────────────────────────────────────────────────
#   FAKES
# ──────────────────────────────────────────────────────────────────────

class FakeSigError(Exception):
    pass


def set_stripe(event=None, raise_sig=False, raise_val=False):
    """Install a stub stripe module into the plugin's namespace whose
    construct_event returns `event` (or raises). Everything downstream
    of signature verification runs the real code."""
    def construct_event(payload, signature, secret):
        if raise_val:
            raise ValueError("malformed payload")
        if raise_sig:
            raise FakeSigError("bad signature")
        return event

    pr.stripe = types.SimpleNamespace(
        Webhook=types.SimpleNamespace(construct_event=construct_event))
    pr.StripeSignatureError = FakeSigError
    pr.STRIPE_WEBHOOK_SECRET = "whsec_test"


class FakeGodReq:
    """Mirror of the godrequest surface priority_request touches."""

    KNOWN = {"ymir", "loki", "hou yi"}

    def __init__(self):
        self.queue = []
        self.history = []
        self._history_listeners = []
        self.save_count = 0

    def _match_god(self, raw):
        if raw and raw.lower() in self.KNOWN:
            return raw.title()
        return None

    def queue_add(self, god, requester, source="paid",
                  token_spent=False, position="end"):
        entry = {"god": god, "requester": requester,
                 "source": source, "token_spent": token_spent}
        if position == "head":
            self.queue.insert(0, entry)
        else:
            self.queue.append(entry)
        return entry

    def _save_data(self):
        self.save_count += 1

    def _update_web_state(self):
        pass

    async def _update_obs_display(self):
        pass

    def add_history_listener(self, fn):
        if fn not in self._history_listeners:
            self._history_listeners.append(fn)

    def resolve(self, entry, status):
        """Simulate the real _save_history: log + fire listeners."""
        final = {**entry, "status": status}
        self.history.append(final)
        for fn in self._history_listeners:
            fn(final)


class FakeBot:
    def __init__(self):
        self.plugins = {}
        self.chat = []

    async def send_chat(self, msg):
        self.chat.append(msg)


async def make_plugin(with_godreq=True):
    plugin = PriorityRequestPlugin()
    plugin._enabled = True
    db = await aiosqlite.connect(":memory:")
    await plugin._init_schema(db)  # also stores db on plugin._db
    bot = FakeBot()
    if with_godreq:
        godreq = FakeGodReq()
        bot.plugins["godrequest"] = godreq
        godreq.add_history_listener(plugin._on_queue_history)
    plugin.bot = bot
    return plugin, bot


def checkout_event(sid="cs_test_1", god="ymir", uname="viewer1",
                   msg="play him cold", amount=500, paid=True,
                   pi="pi_test_1"):
    return {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": sid,
            "payment_status": "paid" if paid else "unpaid",
            "amount_total": amount,
            "currency": "usd",
            "payment_intent": pi,
            "metadata": {"god": god, "twitch_username": uname,
                         "message": msg},
        }},
    }


def refund_event(pi="pi_test_1", dispute=False):
    return {
        "type": ("charge.dispute.created" if dispute
                 else "charge.refunded"),
        "data": {"object": {"payment_intent": pi}},
    }


async def fetch_row(plugin, sid="cs_test_1"):
    async with plugin._db.execute(
        "SELECT * FROM priority_payments WHERE stripe_session_id = ?",
        (sid,)
    ) as c:
        c.row_factory = aiosqlite.Row
        return await c.fetchone()


async def deliver(plugin, event=None, **kw):
    set_stripe(event, **kw)
    return await plugin.handle_webhook(b"{}", "t=1,v1=stub")


# ──────────────────────────────────────────────────────────────────────
#   TESTS
# ──────────────────────────────────────────────────────────────────────

async def test_bad_signature():
    plugin, _ = await make_plugin()
    res = await deliver(plugin, raise_sig=True)
    assert res == {"ok": False, "reason": "bad_signature"}, res


async def test_malformed_payload():
    plugin, _ = await make_plugin()
    res = await deliver(plugin, raise_val=True)
    assert res == {"ok": False, "reason": "bad_payload"}, res


async def test_ignored_event_type():
    plugin, _ = await make_plugin()
    res = await deliver(plugin, {"type": "payment_intent.created",
                                 "data": {"object": {}}})
    assert res["action"] == "ignored_event_type", res


async def test_unpaid_session():
    plugin, bot = await make_plugin()
    res = await deliver(plugin, checkout_event(paid=False))
    assert res["action"] == "unpaid_session", res
    assert not bot.plugins["godrequest"].queue


async def test_underpaid_rejected():
    plugin, bot = await make_plugin()
    res = await deliver(plugin, checkout_event(amount=100))
    assert res == {"ok": False, "reason": "underpaid"}, res
    assert not bot.plugins["godrequest"].queue


async def test_happy_path():
    plugin, bot = await make_plugin()
    res = await deliver(plugin, checkout_event())
    assert res["ok"] and res["action"] == "queued", res
    assert res["god"] == "Ymir", res

    godreq = bot.plugins["godrequest"]
    assert len(godreq.queue) == 1
    entry = godreq.queue[0]
    assert entry["god"] == "Ymir"
    assert entry["source"] == "paid_priority"
    assert entry["stripe_session_id"] == "cs_test_1"
    assert entry["message"] == "play him cold"
    assert godreq.save_count >= 1  # session id persisted to disk

    row = await fetch_row(plugin)
    assert row["status"] == "fulfilled", dict(row)
    assert row["paid_at"] and row["fulfilled_at"]
    assert row["payment_intent"] == "pi_test_1"

    await asyncio.sleep(0)  # let the shoutout task run
    assert bot.chat, "no chat announcement sent"
    shout = bot.chat[0]
    assert shout.startswith("Priority request:"), shout
    assert all(ord(ch) < 0x2000 for ch in shout), \
        f"non-plain-text characters in chat: {shout!r}"


async def test_replay_is_idempotent():
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    res = await deliver(plugin, checkout_event())  # Stripe retry
    assert res["action"] == "already_fulfilled", res
    assert len(bot.plugins["godrequest"].queue) == 1


async def test_crash_window_replay_requeues():
    """Row claimed 'paid' but the bot died before queue_add (the bug
    the two-phase rewrite fixes). Replaying the webhook must queue
    the god and complete the row."""
    plugin, bot = await make_plugin()
    await plugin._db.execute("""
        INSERT INTO priority_payments
               (stripe_session_id, twitch_username, god, message,
                amount_cents, currency, payment_intent, paid_at, status)
        VALUES ('cs_test_1', 'viewer1', 'Ymir', '', 500, 'usd',
                'pi_test_1', datetime('now'), 'paid')
    """)
    await plugin._db.commit()

    res = await deliver(plugin, checkout_event())
    assert res["action"] == "queued", res
    assert len(bot.plugins["godrequest"].queue) == 1
    row = await fetch_row(plugin)
    assert row["status"] == "fulfilled", dict(row)


async def test_crash_window_already_queued_no_dup():
    """Bot died between queue_add and the fulfilled UPDATE. The queue
    entry survived on disk; the replay must NOT queue a duplicate."""
    plugin, bot = await make_plugin()
    godreq = bot.plugins["godrequest"]
    await plugin._db.execute("""
        INSERT INTO priority_payments
               (stripe_session_id, twitch_username, god, message,
                amount_cents, currency, payment_intent, paid_at, status)
        VALUES ('cs_test_1', 'viewer1', 'Ymir', '', 500, 'usd',
                'pi_test_1', datetime('now'), 'paid')
    """)
    await plugin._db.commit()
    godreq.queue.append({"god": "Ymir", "requester": "viewer1",
                         "source": "paid_priority",
                         "stripe_session_id": "cs_test_1"})

    res = await deliver(plugin, checkout_event())
    assert res["action"] == "queued", res
    assert len(godreq.queue) == 1, godreq.queue  # no duplicate
    row = await fetch_row(plugin)
    assert row["status"] == "fulfilled", dict(row)


async def test_godrequest_missing_stays_replayable():
    plugin, _ = await make_plugin(with_godreq=False)
    res = await deliver(plugin, checkout_event())
    assert res == {"ok": False, "reason": "godrequest_missing"}, res
    row = await fetch_row(plugin)
    # 'paid', NOT 'fulfilled' — so a later replay can still queue it.
    assert row["status"] == "paid", dict(row)


async def test_played_listener_stamps_played_at():
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    godreq = bot.plugins["godrequest"]
    godreq.resolve(godreq.queue.pop(0), "played")
    await asyncio.sleep(0.01)  # let _mark_played task run
    row = await fetch_row(plugin)
    assert row["played_at"], dict(row)
    assert row["status"] == "fulfilled"


async def test_skipped_entry_stays_unplayed():
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    godreq = bot.plugins["godrequest"]
    godreq.resolve(godreq.queue.pop(0), "skipped")
    await asyncio.sleep(0.01)
    row = await fetch_row(plugin)
    assert row["played_at"] is None, dict(row)  # refund candidate


async def test_refund_marks_and_unqueues():
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    godreq = bot.plugins["godrequest"]
    assert len(godreq.queue) == 1

    res = await deliver(plugin, refund_event())
    assert res["action"] == "refund_processed", res
    assert res["queue_entry_removed"] is True, res
    assert not godreq.queue
    row = await fetch_row(plugin)
    assert row["status"] == "refunded", dict(row)
    assert row["refunded_at"]


async def test_refund_unmatched_payment_intent():
    plugin, _ = await make_plugin()
    res = await deliver(plugin, refund_event(pi="pi_someone_elses"))
    assert res["action"] == "refund_unmatched", res


async def test_dispute_treated_like_refund():
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    res = await deliver(plugin, refund_event(dispute=True))
    assert res["action"] == "dispute_processed", res
    row = await fetch_row(plugin)
    assert row["status"] == "refunded", dict(row)
    assert not bot.plugins["godrequest"].queue


async def test_checkout_after_refund_does_not_requeue():
    """A replayed checkout webhook arriving AFTER a refund must not
    resurrect the queue entry."""
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())
    await deliver(plugin, refund_event())
    res = await deliver(plugin, checkout_event())  # late retry
    assert res["action"] == "already_refunded", res
    assert not bot.plugins["godrequest"].queue
    row = await fetch_row(plugin)
    assert row["status"] == "refunded", dict(row)


async def test_manual_refund_session():
    """Control-panel refund: real Stripe call faked, local bookkeeping
    shared with the webhook path."""
    plugin, bot = await make_plugin()
    await deliver(plugin, checkout_event())  # fulfilled + queued

    refunds = []

    def fake_refund_create(payment_intent=None):
        refunds.append(payment_intent)
        return {"id": "re_test_1", "status": "succeeded"}

    pr.STRIPE_SECRET_KEY = "sk_test"
    pr.stripe = types.SimpleNamespace(
        Refund=types.SimpleNamespace(create=fake_refund_create))

    res = await plugin.refund_session("cs_test_1")
    assert res["ok"] and res["refund_id"] == "re_test_1", res
    assert refunds == ["pi_test_1"], refunds
    assert not bot.plugins["godrequest"].queue
    row = await fetch_row(plugin)
    assert row["status"] == "refunded", dict(row)

    # second call is a no-op, no double Stripe refund
    res2 = await plugin.refund_session("cs_test_1")
    assert res2.get("already_refunded"), res2
    assert len(refunds) == 1

    # unknown session
    res3 = await plugin.refund_session("cs_nope")
    assert res3 == {"ok": False, "error": "not_found"}, res3


async def test_list_payments():
    plugin, _ = await make_plugin()
    await deliver(plugin, checkout_event())
    rows = await plugin.list_payments()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "cs_test_1"
    assert rows[0]["status"] == "fulfilled"
    assert rows[0]["amount_cents"] == 500


# ──────────────────────────────────────────────────────────────────────
#   RUNNER
# ──────────────────────────────────────────────────────────────────────

TESTS = [
    test_bad_signature,
    test_malformed_payload,
    test_ignored_event_type,
    test_unpaid_session,
    test_underpaid_rejected,
    test_happy_path,
    test_replay_is_idempotent,
    test_crash_window_replay_requeues,
    test_crash_window_already_queued_no_dup,
    test_godrequest_missing_stays_replayable,
    test_played_listener_stamps_played_at,
    test_skipped_entry_stays_unplayed,
    test_refund_marks_and_unqueues,
    test_refund_unmatched_payment_intent,
    test_dispute_treated_like_refund,
    test_checkout_after_refund_does_not_requeue,
    test_manual_refund_session,
    test_list_payments,
]


def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = 0
    for fn in TESTS:
        if name_filter and name_filter not in fn.__name__:
            continue
        try:
            asyncio.run(fn())
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
        else:
            print(f"PASS  {fn.__name__}")
            passed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
