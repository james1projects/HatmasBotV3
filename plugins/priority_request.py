"""
PriorityRequestPlugin
=====================
Lets viewers pay $5 on hatmaster.tv/community to push a god request
to the head of the godrequest queue. Stripe handles the card form on
their hosted Checkout page; this plugin only:

  1. Creates a Stripe Checkout Session with the god + twitch_username
     + message attached as `metadata` (so we get them back verbatim
     on the webhook, no DB round-trip required).
  2. Receives Stripe's signed webhook on payment success, verifies
     the signature, and calls godrequest.queue_add(...,
     source="paid_priority", position="head").

The plugin owns a small `priority_payments` SQLite table for two
things: idempotency (Stripe retries failed webhooks, so we must
remember which session IDs we've already processed) and history
(so the dashboard can show "James paid $5 for Hercules at 14:32").

This plugin doesn't register any chat commands — the entire surface
is HTTP endpoints exposed by core.public_webserver. See:
  - POST /api/priority-request/create
  - POST /api/stripe-webhook
  - GET  /priority-success
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

try:
    import stripe
except ImportError:
    stripe = None

# Stripe SDK v8+ exposes SignatureVerificationError at the top level
# (`stripe.SignatureVerificationError`); v7 and earlier put it under
# `stripe.error.SignatureVerificationError`. We resolve to whichever
# the installed version has and fall back to a sentinel that will
# never match if neither is available (so the broader `except
# Exception` later still catches verification failures, just less
# specifically).
if stripe is not None:
    StripeSignatureError = getattr(
        stripe, "SignatureVerificationError",
        getattr(getattr(stripe, "error", None),
                "SignatureVerificationError", Exception))
else:
    StripeSignatureError = Exception

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

from core import db as _shared_db
from core.config import (
    PRIORITY_REQUEST_ENABLED,
    PRIORITY_REQUEST_PRICE_CENTS,
    PRIORITY_REQUEST_CURRENCY,
    PRIORITY_REQUEST_PRODUCT_NAME,
    PRIORITY_REQUEST_SUCCESS_URL,
    PRIORITY_REQUEST_CANCEL_URL,
    PRIORITY_REQUEST_MAX_MESSAGE_LEN,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)


class PriorityRequestPlugin:
    """
    Stateless-ish plugin: most work is request-handling, no chat
    commands, no background loops. Survival of state across restarts
    is the responsibility of the priority_payments table, which the
    webhook handler writes BEFORE queuing — so a crash between
    "Stripe paid" and "queue mutated" can be replayed by re-driving
    the webhook from Stripe's dashboard.
    """

    def __init__(self):
        self.bot = None
        self._db: Optional["aiosqlite.Connection"] = None
        # Require BOTH keys to enable the feature. Accepting payments
        # we can't verify on the webhook side would mean the user paid
        # but their request never lands in the queue — strictly worse
        # than 503ing the form upfront. STRIPE_WEBHOOK_SECRET comes
        # from the Stripe dashboard (live) or the `stripe listen` CLI
        # session (dev); see config.py for setup instructions.
        self._enabled = bool(
            PRIORITY_REQUEST_ENABLED
            and stripe is not None
            and STRIPE_SECRET_KEY
            and STRIPE_WEBHOOK_SECRET
        )

    # ──────────────────────────────────────────────────────────────────
    #   LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        # No chat commands — entire surface is HTTP.
        if _shared_db.is_available():
            _shared_db.register_schema(self._init_schema)

        # Configure the Stripe SDK with our secret key. Safe to call
        # even when STRIPE_SECRET_KEY is empty (we just won't be able
        # to create sessions or verify webhooks, and is_enabled()
        # reflects that). Logged loudly so it's obvious in dev.
        if stripe is None:
            print("[PriorityRequest] stripe package not installed — "
                  "feature disabled. Run: pip install stripe")
        elif not STRIPE_SECRET_KEY:
            print("[PriorityRequest] STRIPE_SECRET_KEY not set — "
                  "feature disabled until config_local.py defines it")
        elif not STRIPE_WEBHOOK_SECRET:
            print("[PriorityRequest] STRIPE_WEBHOOK_SECRET not set — "
                  "feature disabled (would accept payments without "
                  "being able to verify the webhook). Run `stripe "
                  "listen --forward-to localhost:8070/api/stripe-webhook` "
                  "for local dev or copy from Stripe dashboard for live.")
            stripe.api_key = STRIPE_SECRET_KEY
        else:
            stripe.api_key = STRIPE_SECRET_KEY
            print("[PriorityRequest] Stripe SDK configured "
                  f"(${PRIORITY_REQUEST_PRICE_CENTS / 100:.2f} per request)")

    async def on_ready(self):
        if self._db is None and _shared_db.is_available():
            self._db = await _shared_db.get_db()
        # Subscribe to godrequest's history feed (append-only listener
        # list, same pattern as the kill detector hooks). Stamps
        # played_at on the payment row when the paid god is actually
        # played — fulfilled_at alone only proves we queued it.
        godreq = self._godreq()
        if godreq is not None and hasattr(godreq, "add_history_listener"):
            godreq.add_history_listener(self._on_queue_history)

    async def cleanup(self):
        # Shared DB is closed by main.py at shutdown.
        self._db = None

    # ──────────────────────────────────────────────────────────────────
    #   SCHEMA
    # ──────────────────────────────────────────────────────────────────

    async def _init_schema(self, conn):
        """Schema callback registered with core.db. Stores the shared
        connection on self._db so the webhook + create handlers can
        reach it without going through get_db() on every request."""
        self._db = conn
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS priority_payments (
                stripe_session_id  TEXT PRIMARY KEY,
                twitch_username    TEXT,
                god                TEXT,
                message            TEXT,
                amount_cents       INTEGER,
                currency           TEXT,
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                paid_at            TEXT,
                fulfilled_at       TEXT,
                status             TEXT NOT NULL DEFAULT 'pending'
                                   -- pending | paid | fulfilled | refunded
            );
            CREATE INDEX IF NOT EXISTS idx_priority_payments_status
                ON priority_payments(status);
        """)
        # Column-add migrations for DBs created before these fields
        # existed. PRAGMA-driven and idempotent.
        #   payment_intent — Stripe PaymentIntent id captured from the
        #       checkout webhook; needed to issue refunds and to match
        #       charge.refunded events back to a session.
        #   played_at — set via godrequest's history listener when the
        #       paid god is actually played. fulfilled_at only means
        #       "queued"; played_at means the viewer got what they
        #       paid for. fulfilled + no played_at = refund candidate
        #       (see tools/reconcile_stripe.py unplayed).
        #   refunded_at — set by the charge.refunded webhook or by
        #       tools/reconcile_stripe.py refund.
        async with self._db.execute(
                "PRAGMA table_info(priority_payments)") as c:
            cols = {row[1] for row in await c.fetchall()}
        for col in ("payment_intent", "played_at", "refunded_at"):
            if col not in cols:
                await self._db.execute(
                    f"ALTER TABLE priority_payments ADD COLUMN {col} TEXT")
        await self._db.commit()

    # ──────────────────────────────────────────────────────────────────
    #   PUBLIC HELPERS (called from public_webserver handlers)
    # ──────────────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """True if the feature can actually function right now.
        public_webserver checks this before invoking create_session /
        handle_webhook so we can 503 cleanly instead of throwing."""
        return self._enabled and self._db is not None

    def resolve_god(self, raw: str) -> Optional[str]:
        """Validate + canonicalize a god name using godrequest's
        fuzzy matcher. We re-use the existing matcher rather than
        keeping a parallel list — godrequest is the source of truth
        for valid names. Returns proper-cased name or None."""
        if not raw:
            return None
        godreq = self._godreq()
        if godreq is None:
            return None
        return godreq._match_god(raw)

    async def create_session(self, god: str, twitch_username: str,
                             message: str) -> dict:
        """Create a Stripe Checkout Session for a priority request.

        Validates inputs, persists a `pending` row in priority_payments
        so we can audit attempted-but-not-completed sessions, then
        hands back the Checkout Session URL the browser should
        redirect to.

        Raises ValueError on bad input. Raises RuntimeError on Stripe
        errors. The web handler maps these to 400 / 502.
        """
        if not self.is_enabled():
            raise RuntimeError("priority_request_disabled")

        # ── validate god ──
        canon = self.resolve_god(god)
        if not canon:
            raise ValueError(f"unknown_god:{god!r}")

        # ── validate twitch username ──
        # Twitch usernames are 4–25 chars, alnum + underscore.
        # We don't hit Twitch's API to confirm the user exists — the
        # cost of being wrong is just that the chat shoutout looks
        # weird. Letting unknown usernames through means viewers
        # without an existing Twitch ID can still buy (rare but real).
        uname = (twitch_username or "").strip().lstrip("@")
        if not uname or len(uname) > 25 or not all(
                c.isalnum() or c == "_" for c in uname):
            raise ValueError("bad_twitch_username")

        # ── truncate message ──
        msg = (message or "").strip()[:PRIORITY_REQUEST_MAX_MESSAGE_LEN]

        # ── create Stripe session ──
        # `metadata` survives untouched into the webhook payload, so
        # this is how we get the god + username back without a DB
        # lookup — and the webhook signature proves Stripe is the one
        # vouching for the metadata, so we can trust it.
        try:
            session = await asyncio.to_thread(
                stripe.checkout.Session.create,
                mode="payment",
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": PRIORITY_REQUEST_CURRENCY,
                        "product_data": {
                            "name": PRIORITY_REQUEST_PRODUCT_NAME,
                            "description": (
                                f"Skip the line: {canon} requested by "
                                f"{uname} on Hatmaster.tv"
                            ),
                        },
                        "unit_amount": PRIORITY_REQUEST_PRICE_CENTS,
                    },
                    "quantity": 1,
                }],
                success_url=PRIORITY_REQUEST_SUCCESS_URL,
                cancel_url=PRIORITY_REQUEST_CANCEL_URL,
                metadata={
                    "god": canon,
                    "twitch_username": uname,
                    "message": msg,
                },
            )
        except Exception as e:
            print(f"[PriorityRequest] Stripe session create failed: {e}")
            raise RuntimeError("stripe_session_create_failed") from e

        # ── persist pending row ──
        # Stored BEFORE returning the URL so a crash between session
        # create and the user reaching Stripe doesn't leave us with
        # an un-tracked attempt.
        try:
            await self._db.execute("""
                INSERT INTO priority_payments
                       (stripe_session_id, twitch_username, god, message,
                        amount_cents, currency, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(stripe_session_id) DO NOTHING
            """, (session.id, uname, canon, msg,
                  PRIORITY_REQUEST_PRICE_CENTS,
                  PRIORITY_REQUEST_CURRENCY))
            await self._db.commit()
        except Exception as e:
            # Non-fatal: the webhook handler will still queue from
            # Stripe metadata even if we never stored the pending row.
            print(f"[PriorityRequest] pending row insert failed: {e}")

        return {
            "session_id":   session.id,
            "checkout_url": session.url,
        }

    async def handle_webhook(self, payload: bytes,
                             signature: str) -> dict:
        """Verify + process a Stripe webhook POST.

        Returns a dict the HTTP handler can serialize:
            {"ok": True, "action": "..."} on success
            {"ok": False, "reason": "..."} on rejection
        Never raises — bad input becomes a {"ok": False} the caller
        translates to the right HTTP status.

        Idempotent: replaying the same checkout.session.completed
        event (Stripe retries on 5xx) will no-op after the first
        successful queue insert.
        """
        if not self.is_enabled():
            return {"ok": False, "reason": "disabled"}
        if not STRIPE_WEBHOOK_SECRET:
            return {"ok": False, "reason": "no_webhook_secret"}

        # ── verify signature ──
        # construct_event raises stripe.error.SignatureVerificationError
        # on bad signature and ValueError on malformed payload. Either
        # way we reject without doing anything.
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            print("[PriorityRequest] webhook: malformed payload")
            return {"ok": False, "reason": "bad_payload"}
        except StripeSignatureError:
            print("[PriorityRequest] webhook: bad signature — "
                  "rejecting. Check STRIPE_WEBHOOK_SECRET matches "
                  "the endpoint's signing secret in Stripe dashboard.")
            return {"ok": False, "reason": "bad_signature"}
        except Exception as e:
            print(f"[PriorityRequest] webhook verify error: {e}")
            return {"ok": False, "reason": "verify_failed"}

        # ── route by event type ──
        # Stripe sends a LOT of event types. We act on:
        #   checkout.session.completed — payment landed; queue the god
        #   charge.refunded            — money returned; mark + unqueue
        #   charge.dispute.created     — chargeback opened; treated like
        #                                a refund so a disputed payment
        #                                can't ride the queue for free
        # Everything else is acknowledged and ignored. The dashboard
        # webhook endpoint should be configured to send these three.
        etype = event.get("type", "")
        if etype == "checkout.session.completed":
            return await self._handle_checkout_completed(event)
        if etype in ("charge.refunded", "charge.dispute.created"):
            return await self._handle_refund_event(event)
        return {"ok": True, "action": "ignored_event_type"}

    async def _handle_checkout_completed(self, event) -> dict:
        """checkout.session.completed — the payment landed.

        Status lifecycle (two-phase, crash-safe):
            pending → paid       payment verified, not yet queued
            paid    → fulfilled  queue_add succeeded

        The old single-phase version marked the row 'fulfilled' BEFORE
        queuing, which silently broke Stripe's webhook replay as a
        recovery tool: a crash between the DB write and queue_add left
        a payment that looked handled but never hit the queue, and the
        replay no-op'd on the 'fulfilled' status. Now a replay of a
        'paid' row re-attempts the queue insert (with a queue-membership
        check on stripe_session_id so it can't double-queue), and only
        a successful queue_add flips the row to 'fulfilled'.
        """
        session = event["data"]["object"]
        session_id = session.get("id")
        if session.get("payment_status") != "paid":
            return {"ok": True, "action": "unpaid_session"}
        if session.get("amount_total", 0) < PRIORITY_REQUEST_PRICE_CENTS:
            # Underpaid (shouldn't happen with fixed-price line items,
            # but a defense in depth check costs nothing).
            print(f"[PriorityRequest] underpaid session {session_id}: "
                  f"{session.get('amount_total')} < "
                  f"{PRIORITY_REQUEST_PRICE_CENTS}")
            return {"ok": False, "reason": "underpaid"}

        metadata = session.get("metadata") or {}
        god = metadata.get("god")
        uname = metadata.get("twitch_username")
        msg = metadata.get("message", "")
        if not god or not uname:
            print(f"[PriorityRequest] session {session_id} missing "
                  f"metadata; cannot queue")
            return {"ok": False, "reason": "missing_metadata"}

        # payment_intent arrives as a string id unless the event was
        # fetched with expand=[...]; tolerate both shapes.
        payment_intent = session.get("payment_intent")
        if payment_intent is not None and not isinstance(payment_intent, str):
            payment_intent = (payment_intent.get("id")
                              if isinstance(payment_intent, dict)
                              else getattr(payment_intent, "id", None))

        # ── phase 1: claim → 'paid' ──
        row = await self._fetch_payment(session_id)
        if row is not None and row["status"] in ("fulfilled", "refunded"):
            return {"ok": True, "action": f"already_{row['status']}"}

        try:
            if row is None:
                # The pending insert in create_session failed or never
                # happened. Record the payment now so it can't get lost.
                await self._db.execute("""
                    INSERT INTO priority_payments
                           (stripe_session_id, twitch_username, god,
                            message, amount_cents, currency,
                            payment_intent, paid_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 'paid')
                    ON CONFLICT(stripe_session_id) DO NOTHING
                """, (session_id, uname, god, msg,
                      session.get("amount_total", 0),
                      session.get("currency", PRIORITY_REQUEST_CURRENCY),
                      payment_intent))
            else:
                await self._db.execute("""
                    UPDATE priority_payments
                       SET status = 'paid',
                           paid_at = COALESCE(paid_at, datetime('now')),
                           payment_intent = COALESCE(payment_intent, ?)
                     WHERE stripe_session_id = ?
                       AND status NOT IN ('fulfilled', 'refunded')
                """, (payment_intent, session_id))
            await self._db.commit()
        except Exception as e:
            # Don't bail — the viewer paid; we still owe them the
            # queue slot. reconcile_stripe.py audit will flag the
            # imperfect row.
            print(f"[PriorityRequest] paid-claim write failed: {e}")

        # Crash-recovery edge: the god was already played before a
        # replay arrived (row stuck at 'paid' but played_at was set by
        # the history listener). Just close out the row.
        if row is not None and row.get("played_at"):
            await self._mark_fulfilled(session_id)
            return {"ok": True, "action": "already_played"}

        # ── phase 2: queue at the head ──
        # We re-validate the god name because the canonical list might
        # have changed between create and webhook (unlikely but cheap).
        # If it fails to canonicalize NOW, fall back to the raw
        # metadata value rather than refusing — the viewer paid; we
        # owe them.
        godreq = self._godreq()
        if godreq is None:
            # Status stays 'paid' so a webhook replay (Stripe dashboard
            # → resend) or reconcile_stripe.py can finish the job later.
            print("[PriorityRequest] godrequest plugin missing — "
                  "payment recorded as 'paid' but not queued. Replay "
                  "the webhook once the plugin is back.")
            return {"ok": False, "reason": "godrequest_missing"}

        canon = godreq._match_god(god) or god

        # Source tagged "paid_priority" so the OBS overlay + chat
        # shoutout can flag it and the queue renderer can show a
        # badge. Position "head" matches !spin's behavior — paid
        # priority and spins both jump the line.
        already_queued = any(
            e.get("stripe_session_id") == session_id
            for e in getattr(godreq, "queue", []))
        if not already_queued:
            entry = godreq.queue_add(
                canon,
                uname,
                source="paid_priority",
                token_spent=False,
                position="head",
            )
            # queue_add doesn't accept extra fields; patch the entry it
            # returns and re-save. stripe_session_id is what lets the
            # history listener (played_at) and the refund handler find
            # this entry again later.
            entry["stripe_session_id"] = session_id
            if msg:
                entry["message"] = msg
            try:
                godreq._save_data()
            except Exception as e:
                print(f"[PriorityRequest] entry metadata save failed: {e}")

        # ── phase 3: only now mark fulfilled ──
        await self._mark_fulfilled(session_id)

        # Chat announcement — plain text per the repo Tone rule (no
        # emojis, no flair). Non-fatal if send_chat barfs.
        try:
            if self.bot and hasattr(self.bot, "send_chat"):
                amount = session.get("amount_total",
                                     PRIORITY_REQUEST_PRICE_CENTS) / 100
                shout = (f"Priority request: {uname} paid ${amount:.2f} "
                         f"for {canon} to jump the queue.")
                if msg:
                    shout += f' Message: "{msg}"'
                asyncio.create_task(self.bot.send_chat(shout))
        except Exception as e:
            print(f"[PriorityRequest] chat shoutout failed: {e}")

        print(f"[PriorityRequest] FULFILLED session {session_id}: "
              f"{uname} -> {canon} "
              f"(${session.get('amount_total', 0) / 100:.2f})")
        return {"ok": True, "action": "queued", "god": canon}

    async def _handle_refund_event(self, event) -> dict:
        """charge.refunded / charge.dispute.created — money went (or
        is going) back to the viewer. Mark the payment row 'refunded'
        and pull the god out of the request queue if it hasn't been
        played yet. Matched via payment_intent (captured on the
        checkout webhook); events for unrelated Stripe products on the
        same account won't match a row and are simply acknowledged."""
        obj = event["data"]["object"]
        payment_intent = obj.get("payment_intent")
        if not payment_intent:
            return {"ok": True, "action": "refund_no_payment_intent"}

        async with self._db.execute(
            "SELECT stripe_session_id, twitch_username, god, status "
            "FROM priority_payments WHERE payment_intent = ?",
            (payment_intent,)
        ) as c:
            row = await c.fetchone()
        if row is None:
            return {"ok": True, "action": "refund_unmatched"}

        session_id, uname, god, status = row
        is_dispute = event.get("type") == "charge.dispute.created"
        removed = await self._apply_refund_locally(session_id, uname, god)
        kind = "dispute" if is_dispute else "refund"
        print(f"[PriorityRequest] {kind.upper()} for session "
              f"{session_id} ({uname} -> {god}); "
              f"queue entry removed: {removed}")
        return {"ok": True, "action": f"{kind}_processed",
                "queue_entry_removed": removed}

    async def _apply_refund_locally(self, session_id, uname, god) -> bool:
        """Mark a payment row refunded + pull any still-queued entry.
        Shared by the charge.refunded webhook and the control panel's
        manual refund. Idempotent. Returns True if a queue entry was
        removed."""
        try:
            await self._db.execute("""
                UPDATE priority_payments
                   SET status = 'refunded',
                       refunded_at = COALESCE(refunded_at,
                                              datetime('now'))
                 WHERE stripe_session_id = ?
                   AND status != 'refunded'
            """, (session_id,))
            await self._db.commit()
        except Exception as e:
            print(f"[PriorityRequest] refund UPDATE failed: {e}")

        removed = False
        godreq = self._godreq()
        if godreq is not None:
            queue = getattr(godreq, "queue", [])
            for i, e in enumerate(queue):
                if e.get("stripe_session_id") == session_id:
                    queue.pop(i)
                    removed = True
                    try:
                        godreq._save_data()
                        godreq._update_web_state()
                        asyncio.create_task(godreq._update_obs_display())
                    except Exception as ex:
                        print(f"[PriorityRequest] queue cleanup after "
                              f"refund failed: {ex}")
                    break
        if removed:
            try:
                if self.bot and hasattr(self.bot, "send_chat"):
                    asyncio.create_task(self.bot.send_chat(
                        f"Priority request for {god} by {uname} was "
                        f"refunded and removed from the queue."))
            except Exception:
                pass
        return removed

    async def refund_session(self, session_id: str) -> dict:
        """Manual refund from the control panel. Creates a REAL Stripe
        refund, then applies the same local bookkeeping as the
        charge.refunded webhook (which will later arrive and no-op on
        the already-refunded status)."""
        if stripe is None or not STRIPE_SECRET_KEY:
            return {"ok": False, "error": "stripe_not_configured"}
        if self._db is None:
            return {"ok": False, "error": "db_unavailable"}
        async with self._db.execute(
            "SELECT twitch_username, god, status, payment_intent "
            "FROM priority_payments WHERE stripe_session_id = ?",
            (session_id,)
        ) as c:
            row = await c.fetchone()
        if row is None:
            return {"ok": False, "error": "not_found"}
        uname, god, status, pi = row
        if status == "refunded":
            return {"ok": True, "already_refunded": True}

        if not pi:
            # Pre-payment_intent-column rows: fetch it from Stripe.
            try:
                s = await asyncio.to_thread(
                    stripe.checkout.Session.retrieve, session_id)
                pi = s.get("payment_intent")
                if pi is not None and not isinstance(pi, str):
                    pi = (pi.get("id") if isinstance(pi, dict)
                          else getattr(pi, "id", None))
            except Exception as e:
                print(f"[PriorityRequest] session retrieve failed: {e}")
        if not pi:
            return {"ok": False, "error": "no_payment_intent"}

        try:
            refund = await asyncio.to_thread(
                stripe.Refund.create, payment_intent=pi)
        except Exception as e:
            print(f"[PriorityRequest] Stripe refund failed: {e}")
            return {"ok": False, "error": f"stripe_refund_failed: {e}"}

        removed = await self._apply_refund_locally(session_id, uname, god)
        print(f"[PriorityRequest] MANUAL REFUND {session_id} "
              f"({uname} -> {god}) refund_id={refund.get('id')}")
        return {"ok": True, "refund_id": refund.get("id"),
                "queue_entry_removed": removed}

    async def list_payments(self, limit: int = 50) -> list:
        """Recent payments for the control panel table."""
        if self._db is None:
            return []
        out = []
        async with self._db.execute("""
            SELECT stripe_session_id, twitch_username, god, message,
                   amount_cents, status, created_at, paid_at,
                   fulfilled_at, played_at, refunded_at
              FROM priority_payments
             ORDER BY created_at DESC LIMIT ?
        """, (limit,)) as c:
            async for r in c:
                out.append({
                    "session_id": r[0], "username": r[1], "god": r[2],
                    "message": r[3] or "",
                    "amount_cents": int(r[4] or 0),
                    "status": r[5], "created_at": r[6],
                    "paid_at": r[7], "fulfilled_at": r[8],
                    "played_at": r[9], "refunded_at": r[10],
                })
        return out

    # ── payment row helpers ──

    async def _fetch_payment(self, session_id):
        """Return {status, played_at} for a session, or None."""
        async with self._db.execute(
            "SELECT status, played_at FROM priority_payments "
            "WHERE stripe_session_id = ?",
            (session_id,)
        ) as c:
            row = await c.fetchone()
        if row is None:
            return None
        return {"status": row[0], "played_at": row[1]}

    async def _mark_fulfilled(self, session_id):
        try:
            await self._db.execute("""
                UPDATE priority_payments
                   SET status = 'fulfilled',
                       fulfilled_at = COALESCE(fulfilled_at,
                                               datetime('now'))
                 WHERE stripe_session_id = ?
                   AND status != 'refunded'
            """, (session_id,))
            await self._db.commit()
        except Exception as e:
            print(f"[PriorityRequest] fulfilled UPDATE failed: {e}")

    # ── godrequest history feed ──

    def _on_queue_history(self, entry: dict):
        """Sync listener invoked by godrequest._save_history. Stamps
        played_at when a paid entry's god was actually played. Skipped
        / removed paid entries stay unplayed on purpose — they're what
        `python tools/reconcile_stripe.py unplayed` lists as refund
        candidates."""
        sid = entry.get("stripe_session_id")
        if not sid or entry.get("status") != "played":
            return
        try:
            asyncio.create_task(self._mark_played(sid))
        except RuntimeError:
            pass  # no running loop (tests / shutdown)

    async def _mark_played(self, session_id):
        if self._db is None:
            return
        try:
            await self._db.execute("""
                UPDATE priority_payments
                   SET played_at = COALESCE(played_at, datetime('now'))
                 WHERE stripe_session_id = ?
            """, (session_id,))
            await self._db.commit()
            print(f"[PriorityRequest] PLAYED session {session_id}")
        except Exception as e:
            print(f"[PriorityRequest] played UPDATE failed: {e}")

    # ──────────────────────────────────────────────────────────────────
    #   INTERNALS
    # ──────────────────────────────────────────────────────────────────

    def _godreq(self):
        """Look up the godrequest plugin via the bot. Returns None
        when called before plugins are registered or if godrequest
        is disabled."""
        if not self.bot or not hasattr(self.bot, "plugins"):
            return None
        return self.bot.plugins.get("godrequest")
