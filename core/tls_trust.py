"""
TLS trust shim — import this before anything that imports aiohttp.

Windows fills its root-CA store lazily: schannel (browsers, PowerShell)
downloads a missing root the first time it sees a chain, but Python's
OpenSSL can't trigger that download, so a host whose chain ends at a
not-yet-cached root fails with CERTIFICATE_VERIFY_FAILED even though every
browser loads it fine. This machine's store was missing "Starfield Root
Certificate Authority - G2" (Spotify's Certainly CA chains to it — broke
accounts.spotify.com on 2026-08-29) and "Amazon Root CA 1".

Pointing SSL_CERT_FILE at certifi's bundle is additive: OpenSSL loads the
bundle *and* Python still loads the Windows stores on top, so nothing that
verified before stops verifying.

Import order matters for aiohttp specifically: it builds and caches its
default SSL context at import time, so this module must run first.
urllib/requests build their contexts per request and pick the env var up
regardless of import order.
"""

import os

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass  # no certifi installed → previous behavior (Windows store only)
