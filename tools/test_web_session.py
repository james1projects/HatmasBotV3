"""
test_web_session.py — unit tests for core/web_session.py
========================================================

The session token is the entire auth story for website trading, so
every failure mode gets a test: round-trip, tampered body, tampered
mac, truncation, wrong secret, expiry, missing exp, garbage input,
and login normalization.

Run:
    python tools/test_web_session.py

Exit 0 on full pass. Same conventions as test_priority_request.py.
No network, no files, no bot imports.
"""

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import web_session as ws

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


def test_round_trip():
    token = ws.issue("12345", "Viewer1", "Viewer1", "http://img", SECRET)
    payload = ws.verify(token, SECRET)
    assert payload is not None
    assert payload["uid"] == "12345"
    assert payload["login"] == "viewer1"  # lowercased
    assert payload["name"] == "Viewer1"
    assert payload["exp"] > time.time()


def test_tampered_body_rejected():
    token = ws.issue("12345", "viewer1", secret=SECRET)
    body, mac = token.split(".")
    forged = ws._b64e(b'{"exp":9999999999,"login":"hatmaster","uid":"1"}')
    assert ws.verify(f"{forged}.{mac}", SECRET) is None


def test_tampered_mac_rejected():
    token = ws.issue("12345", "viewer1", secret=SECRET)
    body, mac = token.split(".")
    bad_mac = ("0" if mac[0] != "0" else "1") + mac[1:]
    assert ws.verify(f"{body}.{bad_mac}", SECRET) is None


def test_wrong_secret_rejected():
    token = ws.issue("12345", "viewer1", secret=SECRET)
    assert ws.verify(token, "different-secret") is None


def test_expired_rejected():
    token = ws.issue("12345", "viewer1", secret=SECRET, max_age=10)
    assert ws.verify(token, SECRET) is not None
    assert ws.verify(token, SECRET, now=time.time() + 11) is None


def test_missing_exp_rejected():
    token = ws.sign({"uid": "1", "login": "viewer1"}, SECRET)
    assert ws.verify(token, SECRET) is None


def test_non_numeric_exp_rejected():
    token = ws.sign({"uid": "1", "login": "x", "exp": "never"}, SECRET)
    assert ws.verify(token, SECRET) is None


def test_garbage_inputs_return_none():
    for garbage in (None, "", ".", "a.b.c", "a.b", "....",
                    "%%%.deadbeef", "ab" * 5000,
                    ws._b64e(b"[1,2,3]") + "." + "0" * 64):
        assert ws.verify(garbage, SECRET) is None, repr(garbage)


def test_non_dict_payload_rejected():
    body = ws._b64e(b'["not","a","dict"]')
    token = f"{body}.{ws._mac(body, SECRET)}"
    assert ws.verify(token, SECRET) is None


def test_empty_secret_refuses_to_sign():
    try:
        ws.sign({"x": 1}, "")
    except ValueError:
        pass
    else:
        raise AssertionError("sign() accepted an empty secret")
    # verify with empty secret is also a hard no
    token = ws.issue("1", "x", secret=SECRET)
    assert ws.verify(token, "") is None


def test_state_nonce_unique_and_urlsafe():
    a, b = ws.make_state(), ws.make_state()
    assert a != b and len(a) >= 40
    assert all(c.isalnum() or c in "-_" for c in a)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
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
