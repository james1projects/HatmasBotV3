"""
reconcile_stripe.py — audit + repair tooling for priority god requests
======================================================================

The priority-request money path has three places truth can live:
Stripe's records, the local priority_payments table, and the god
request queue/history. This tool diffs them and gives you the levers
to fix mismatches. Same end-to-end philosophy as check_stream_ready:
ask Stripe what actually happened, don't trust local state.

Subcommands:

    python tools/reconcile_stripe.py audit [--days 30]
        Diff Stripe checkout sessions against priority_payments.
        Catches: dropped webhooks (paid in Stripe, nothing queued),
        crash-window rows (claimed 'paid' but never 'fulfilled'),
        and refund mismatches (refunded in Stripe, still active
        locally). Exit 0 = clean, 1 = discrepancies found.

    python tools/reconcile_stripe.py unplayed
        List fulfilled payments whose god was never actually played
        (no played_at, not refunded). These are your refund
        candidates — e.g. stream ended before the god came up.

    python tools/reconcile_stripe.py refund <session_id> [--yes]
        Issue a real Stripe refund for a session and mark the local
        row 'refunded'. If the bot is running, its charge.refunded
        webhook also removes any still-queued entry automatically.

Run `audit` after any stream where the bot crashed or restarted, and
`unplayed` at end of stream to settle up before viewers have to ask.

Safe to run while the bot is up: the DB is WAL-mode and this tool
only writes the single row being refunded.
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import stripe
except ImportError:
    print("Missing stripe. Install with: pip install stripe")
    sys.exit(2)

from core.config import (
    ECONOMY_DB_PATH,
    STRIPE_SECRET_KEY,
    PRIORITY_REQUEST_PRICE_CENTS,
)

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ECONOMY_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def require_stripe_key():
    if not STRIPE_SECRET_KEY:
        print("STRIPE_SECRET_KEY is not set (config_local.py or env). "
              "Cannot talk to Stripe.")
        sys.exit(2)
    stripe.api_key = STRIPE_SECRET_KEY


def is_ours(session) -> bool:
    """A checkout session belongs to the priority-request product if it
    carries the god + twitch_username metadata we attach on create.
    Keeps the audit from flagging unrelated Stripe products on the
    same account."""
    md = session.get("metadata") or {}
    return bool(md.get("god") and md.get("twitch_username"))


def fetch_stripe_sessions(days: int) -> list:
    cutoff = int(time.time()) - days * 86400
    out = []
    sessions = stripe.checkout.Session.list(
        limit=100, created={"gte": cutoff})
    for s in sessions.auto_paging_iter():
        if is_ours(s):
            out.append(s)
    return out


def fetch_stripe_refunded_intents(days: int) -> set:
    cutoff = int(time.time()) - days * 86400
    refunded = set()
    refunds = stripe.Refund.list(limit=100, created={"gte": cutoff})
    for r in refunds.auto_paging_iter():
        if r.get("payment_intent"):
            refunded.add(r["payment_intent"])
    return refunded


# ──────────────────────────────────────────────────────────────────────
#   audit
# ──────────────────────────────────────────────────────────────────────

def cmd_audit(days: int) -> int:
    require_stripe_key()
    conn = open_db()
    try:
        local = {
            row["stripe_session_id"]: dict(row)
            for row in conn.execute("SELECT * FROM priority_payments")
        }
    finally:
        conn.close()

    print(f"Fetching Stripe checkout sessions (last {days} days)...")
    sessions = fetch_stripe_sessions(days)
    refunded_intents = fetch_stripe_refunded_intents(days)
    print(f"  {len(sessions)} priority-request sessions in Stripe, "
          f"{len(local)} rows in priority_payments\n")

    problems = 0

    for s in sessions:
        sid = s["id"]
        md = s.get("metadata") or {}
        who = f"{md.get('twitch_username')} -> {md.get('god')}"
        paid = s.get("payment_status") == "paid"
        pi = s.get("payment_intent")
        pi = pi if isinstance(pi, str) else (pi or {}).get("id")
        stripe_refunded = pi in refunded_intents if pi else False
        row = local.get(sid)

        if not paid:
            # Abandoned checkout — pending local row is expected noise.
            continue

        if row is None:
            print(f"{FAIL} {sid} ({who}): paid in Stripe but NO local "
                  f"row. Webhook never arrived. Fix: Stripe dashboard "
                  f"-> webhook -> resend checkout.session.completed.")
            problems += 1
            continue

        status = row["status"]
        if status == "pending":
            print(f"{FAIL} {sid} ({who}): paid in Stripe but local row "
                  f"still 'pending'. Webhook never arrived. Fix: resend "
                  f"the webhook from the Stripe dashboard.")
            problems += 1
        elif status == "paid":
            print(f"{FAIL} {sid} ({who}): claimed 'paid' but never "
                  f"queued (crash window). Fix: resend the webhook — "
                  f"the handler re-queues 'paid' rows safely.")
            problems += 1

        if stripe_refunded and status != "refunded":
            print(f"{FAIL} {sid} ({who}): refunded in Stripe but local "
                  f"status is '{status}'. The charge.refunded webhook "
                  f"was missed. Fix: resend it from the Stripe "
                  f"dashboard (the handler marks the row and removes "
                  f"any queued entry).")
            problems += 1
        elif status == "refunded" and not stripe_refunded:
            print(f"{WARN} {sid} ({who}): local row says refunded but "
                  f"no Stripe refund found in the window. Dispute, or "
                  f"refund older than {days} days?")

    # Reverse check: local rows that claim fulfillment for sessions
    # Stripe has no payment for (inside the window only).
    stripe_ids = {s["id"] for s in sessions}
    cutoff_iso = datetime.fromtimestamp(
        time.time() - days * 86400, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    for sid, row in local.items():
        if (row["status"] in ("fulfilled", "paid")
                and (row["created_at"] or "") >= cutoff_iso
                and sid not in stripe_ids):
            print(f"{FAIL} {sid} ({row['twitch_username']} -> "
                  f"{row['god']}): local row says '{row['status']}' but "
                  f"Stripe has no such paid session. Investigate.")
            problems += 1

    if problems == 0:
        print(f"{OK} Stripe and priority_payments agree. Nothing to do.")
        return 0
    print(f"\n{problems} discrepancies found.")
    return 1


# ──────────────────────────────────────────────────────────────────────
#   unplayed
# ──────────────────────────────────────────────────────────────────────

def cmd_unplayed() -> int:
    conn = open_db()
    try:
        rows = conn.execute("""
            SELECT stripe_session_id, twitch_username, god,
                   amount_cents, paid_at
              FROM priority_payments
             WHERE status = 'fulfilled'
               AND played_at IS NULL
               AND refunded_at IS NULL
             ORDER BY paid_at
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"{OK} No unplayed paid requests. All settled.")
        return 0

    print(f"{len(rows)} paid request(s) queued but never played:\n")
    now = datetime.now(timezone.utc)
    for r in rows:
        age = ""
        if r["paid_at"]:
            try:
                paid = datetime.strptime(
                    r["paid_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                age = f", {(now - paid).days}d ago"
            except ValueError:
                pass
        amt = (r["amount_cents"] or PRIORITY_REQUEST_PRICE_CENTS) / 100
        print(f"  {r['stripe_session_id']}  "
              f"{r['twitch_username']} -> {r['god']} (${amt:.2f}{age})")
    print(f"\nStill in the queue for a future stream is fine. If a "
          f"request can't be honored, settle up with:\n"
          f"  python tools/reconcile_stripe.py refund <session_id>")
    return 0


# ──────────────────────────────────────────────────────────────────────
#   refund
# ──────────────────────────────────────────────────────────────────────

def cmd_refund(session_id: str, skip_confirm: bool) -> int:
    require_stripe_key()
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT * FROM priority_payments WHERE stripe_session_id = ?",
            (session_id,)).fetchone()
        if row is None:
            print(f"No priority_payments row for {session_id}.")
            return 2
        if row["status"] == "refunded":
            print(f"{session_id} is already refunded. Nothing to do.")
            return 0

        pi = row["payment_intent"]
        if not pi:
            # Rows written before the payment_intent column existed —
            # fetch it from Stripe via the session.
            print("No payment_intent on the row; fetching from Stripe...")
            s = stripe.checkout.Session.retrieve(session_id)
            pi = s.get("payment_intent")
            pi = pi if isinstance(pi, str) else (pi or {}).get("id")
            if not pi:
                print("Stripe session has no payment_intent — was it "
                      "ever paid? Aborting.")
                return 2

        amt = (row["amount_cents"] or PRIORITY_REQUEST_PRICE_CENTS) / 100
        print(f"Refund ${amt:.2f} to {row['twitch_username']} for "
              f"{row['god']} (session {session_id})?")
        if not skip_confirm:
            answer = input("Type 'refund' to confirm: ").strip().lower()
            if answer != "refund":
                print("Aborted.")
                return 0

        refund = stripe.Refund.create(payment_intent=pi)
        print(f"Stripe refund created: {refund['id']} "
              f"(status: {refund['status']})")

        conn.execute("""
            UPDATE priority_payments
               SET status = 'refunded',
                   refunded_at = datetime('now'),
                   payment_intent = COALESCE(payment_intent, ?)
             WHERE stripe_session_id = ?
        """, (pi, session_id))
        conn.commit()
        print("Local row marked 'refunded'.")
        print("If the bot is running, its charge.refunded webhook will "
              "also remove any still-queued entry automatically. If the "
              "bot is down, check !godqueue on next launch.")
        return 0
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
#   CLI
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit + repair priority-request payments "
                    "(Stripe vs priority_payments vs god queue).")
    sub = parser.add_subparsers(dest="cmd")

    p_audit = sub.add_parser("audit", help="diff Stripe against the DB")
    p_audit.add_argument("--days", type=int, default=30,
                         help="lookback window (default 30)")

    sub.add_parser("unplayed",
                   help="list fulfilled-but-never-played payments")

    p_refund = sub.add_parser("refund", help="issue a Stripe refund")
    p_refund.add_argument("session_id")
    p_refund.add_argument("--yes", action="store_true",
                          help="skip confirmation prompt")

    args = parser.parse_args()
    if args.cmd == "unplayed":
        return cmd_unplayed()
    if args.cmd == "refund":
        return cmd_refund(args.session_id, args.yes)
    # default: audit
    days = getattr(args, "days", 30)
    return cmd_audit(days)


if __name__ == "__main__":
    sys.exit(main())
