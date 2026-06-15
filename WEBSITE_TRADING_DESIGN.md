# Hatmaster.tv — Twitch Login + Website Trading Design

**Status:** Draft for Hatmaster review
**Author:** Claude + Hatmaster
**Date:** June 9, 2026
**Companion to:** `HatmasBot.md` (v2.5/v2.6 sections), `HATMAS_MARKET_AIRTIGHT_DESIGN.md`

---

## 0. Decisions needed from Hatmaster before implementation

1. **Reuse the bot's Twitch application or create a second one?**
   Recommendation: reuse. Add `https://hatmaster.tv/auth/twitch/callback`
   to the existing app's OAuth Redirect URLs in the Twitch dev console.
   The website login requests ZERO scopes (identity only), so even a
   leaked website flow can't touch channel features. A separate app is
   marginally cleaner but doubles the credential-rotation surface.
2. **Trading while the stream is offline — allowed?**
   Recommendation: yes. Prices only move at tracker.gg settlement, so
   off-stream trades are value-neutral (no information asymmetry, no
   manipulation lever). "The market is open whenever the bot is up"
   is also the honest framing — see §4.5.
3. **Add a `channel` column to `transactions` ('chat' | 'web')?**
   Recommendation: yes — one ALTER TABLE, makes debugging and future
   stats ("40% of trades now come from the site") free.

---

## 1. Problem

Twitch viewers can SEE their portfolio on `hatmaster.tv` but can only
TRADE through chat commands while connected to Twitch chat. That makes
the website a stats page, not a destination. The goal: log in with
Twitch on hatmaster.tv, then buy/sell from the portfolio and god
pages — from a phone, between streams, without opening Twitch.

The hard part is that the public webserver (port 8070) is **read-only
by design** — that boundary is documented in `HatmasBot.md` as the
security rationale for exposing it through the Cloudflare Tunnel. This
design adds a *narrow, authenticated, rate-limited* write surface
without giving up what made the read-only stance valuable.

## 2. Design principles

1. **One money path.** Web trades call the exact same
   `execute_buy()` / `execute_sell()` in `plugins/economy/trading.py`
   that chat commands use. The web layer NEVER reimplements trade
   math, balance checks, or portfolio writes. If the web path and the
   chat path can disagree, the design has failed.
2. **The dashboard stays unreachable.** Port 8069 (control panel,
   `/api/action`) remains tunnel-invisible. Everything in this design
   lives on 8070.
3. **Smallest possible write surface.** Exactly three new
   state-changing routes: OAuth callback, logout, trade. Everything
   else stays GET.
4. **Identity, not access.** We use Twitch OAuth purely to prove "this
   browser belongs to Twitch user X". The Twitch access token is used
   once (to fetch id + login) and discarded. We never store viewer
   tokens, so there is nothing to leak.
5. **Fail closed, degrade loud.** No session secret configured → login
   disabled. MixItUp unreachable → trades 503 with "market closed",
   never a silent failure.

## 3. Authentication

### 3.1 OAuth flow (authorization code, zero scopes)

```
GET /auth/login
  → 302 to id.twitch.tv/oauth2/authorize
        ?response_type=code
        &client_id=<TWITCH_CLIENT_ID>
        &redirect_uri=https://hatmaster.tv/auth/twitch/callback
        &scope=                      (empty — identity only)
        &state=<32-byte urlsafe nonce>
     Set-Cookie: hatmas_oauth_state=<nonce>; Max-Age=600; HttpOnly;
                 Secure; SameSite=Lax

GET /auth/twitch/callback?code=...&state=...
  1. state param must equal the hatmas_oauth_state cookie (CSRF guard
     on the login flow itself). Mismatch → 403, no retry logic.
  2. POST id.twitch.tv/oauth2/token (server-side; uses the client
     secret from config_local.py) → access_token.
  3. GET api.twitch.tv/helix/users with that token → id, login,
     display_name, profile_image_url. Token is then dropped on the
     floor — never persisted.
  4. Issue session cookie (§3.2), 302 to /twitch/<login>.

POST /auth/logout
  → clears the session cookie. (POST, not GET, so a hostile <img>
     tag can't log viewers out.)
```

`redirect_uri` is pinned to the production hostname in config. For
local dev, `http://localhost:8070/auth/twitch/callback` is registered
as a second redirect URL on the Twitch app and selected via
`config_local.py`.

### 3.2 Session cookie (stateless, signed)

No server-side session table. The cookie is a signed claim:

```
hatmas_session = base64url(json payload) + "." + hex(hmac_sha256(payload, WEB_SESSION_SECRET))
payload = {"uid": "<twitch user id>", "login": "<login>",
           "name": "<display_name>", "img": "<avatar url>",
           "iat": <unix>, "exp": <unix, +30 days>}
```

- Attributes: `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=30d`.
- Verification on every authed request: recompute HMAC
  (`hmac.compare_digest`), check `exp`. Invalid/expired → treated as
  logged out, never an error page.
- `WEB_SESSION_SECRET`: 64 random bytes in `config_local.py`
  (gitignored, same trust level as the Stripe keys). Rotating it
  invalidates every session — that's the global "log everyone out"
  lever, and it's the right cost/benefit for a fan site (no revocation
  table to maintain).
- Stdlib only (`hmac`, `hashlib`, `base64`, `json`) — no new
  dependencies. Lives in a new `core/web_session.py` (~80 lines),
  unit-testable in isolation.

### 3.3 Why not aiohttp-session / JWT libraries

aiohttp-session pulls in storage decisions we don't need; JWT
libraries add config surface (alg confusion pitfalls) for a single
issuer+consumer that lives in one process. HMAC-signed JSON is the
same construction minus the foot-guns.

## 4. The trade endpoint

### 4.1 Route

```
POST /api/trade        (port 8070, session required)
Body: {"action": "buy"|"sell", "god": "<name>", "amount": <int hats>}
                                 amount may also be "all"
→ 200 {"ok": true, "shares": ..., "price": ..., "balance": ...}
→ 4xx/503 {"ok": false, "error": "<human-readable>"}
```

The handler is a thin HTTP layer (same pattern as the Stripe
endpoints): parse → authenticate → guard → delegate to
`economy.execute_buy/execute_sell` with `username = session["login"]`
→ map result to status code. Business logic stays in the economy
plugin; `PublicWebServer.__init__` grows an `economy=None` parameter
(exactly how `priority_request` was wired in v2.6).

Identity note: MixItUp balances and `portfolios` rows are keyed on
Twitch login, and Helix gives us the canonical login for the OAuth'd
user — so a web trade lands on precisely the same balance and
portfolio rows as that viewer's chat trades. No mapping table needed.

### 4.2 Request guards (in order)

| # | Guard | Failure |
|---|---|---|
| 1 | `WEB_TRADING_ENABLED` config + feature toggle `web_trading` | 503 disabled |
| 2 | Valid session cookie | 401 not_logged_in |
| 3 | `Origin` header is exactly `https://hatmaster.tv` (or the dev origin) | 403 bad_origin |
| 4 | Login not in `ECONOMY_EXCLUDED_USERNAMES` | 403 excluded |
| 5 | Per-user cooldown: 1 trade per `WEB_TRADE_COOLDOWN` (default 3s, same as chat) | 429 with retry-after |
| 6 | Per-IP token bucket: 30 requests/min on /api/trade and /auth/* | 429 |
| 7 | Body shape: action ∈ {buy,sell}, god resolves via `_resolve_god_name`, amount int ≥ 1 or "all" | 400 |
| 8 | Per-user `asyncio.Lock` held across the `execute_*` call | (serializes, no failure) |
| 9 | Economy ready: `economy._db` set AND MixItUp `_connected` | 503 market_closed |

Guard 3 is the CSRF story: `SameSite=Strict` already prevents the
session cookie from riding on cross-site POSTs in every modern
browser, and the Origin check backstops older ones. A double-submit
token adds complexity for no additional coverage here.

Guard 8 closes a web-specific gap: `execute_buy` has a
check-balance-then-deduct sequence that chat never races (one message
at a time per user), but two simultaneous browser tabs could. One
`defaultdict(asyncio.Lock)` keyed on login serializes each user's
trades; different users stay concurrent.

### 4.3 What deliberately does NOT change

- `execute_buy` / `execute_sell` — untouched (besides the optional
  `channel` column tag from §0.3).
- Chat commands, cooldown semantics, the trade-feed overlay (web
  trades emit the same `trade_executed` event — viewers see them on
  stream, which is half the fun).
- The dividend/settlement/free-share machinery — trades were always
  viewer-initiated; this only adds a second front door.

### 4.4 Rate limiting at the edge

Add a Cloudflare WAF rate rule on `hatmaster.tv/api/trade` and
`/auth/*` (e.g. 60 req/min/IP) so junk floods die at the edge before
reaching the tunnel. Defense in depth on top of guard 6, not instead
of it.

### 4.5 Market hours

Trades require the bot process up and MixItUp running, because hats
live in MixItUp (`localhost:8911`). That's not a limitation to paper
over — surface it: `/api/stream-status` already exists; add
`"market_open": true|false` (bot reachable + MixItUp connected) and
show OPEN/CLOSED in the site header. When James's PC is off, the
market is closed; portfolio pages still render (read paths hit
economy.db directly, unchanged).

## 5. UI changes (public/)

- **Site header (all pages):** "Log in with Twitch" button → avatar +
  display name + logout when authed. Session state comes from a new
  `GET /api/me` (200 with identity, or 401) so the static HTML pages
  stay cacheable and JS hydrates auth state — same pattern the pages
  already use for `/api/stream-status`.
- **Portfolio page:** when viewing YOUR portfolio, each holding gets
  BUY/SELL controls (amount input + all/half presets mirroring chat
  syntax). Current hat balance shown next to net worth (needs a small
  `GET /api/me/balance` → MixItUp read).
- **God detail page:** trade box under the chart when logged in.
  Logged out, the same box renders the login button — every page sells
  the login.
- **Community page synergy:** when logged in, the $5 priority-request
  form locks `twitch_username` to the session login. Kills the
  typo'd-username failure mode in the Stripe metadata path for free.
- Plain-text everything per the Tone rule. The terminal theme already
  has button/input styles in `theme.css`.

## 6. Config additions

| Key | Notes |
|---|---|
| `WEB_SESSION_SECRET` | 64 random bytes, config_local.py only. Empty → login + trading disabled, loud startup print. |
| `WEB_TRADING_ENABLED` | Master switch, default False until tested live. Also a `web_trading` feature toggle for mid-stream cutoff from the dashboard. |
| `WEB_TRADE_COOLDOWN` | Default 3 (seconds), mirrors chat. |
| `WEB_OAUTH_REDIRECT_URI` | Prod default; localhost override in config_local.py. |
| `WEB_TRADE_MAX_PER_MIN` | Per-IP bucket size, default 30. |

Reuses existing `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` (per §0.1).

## 7. Security review summary

| Threat | Mitigation |
|---|---|
| Stolen/forged session | HMAC over server secret; `compare_digest`; 30-day expiry; HttpOnly+Secure+Strict cookie |
| CSRF trade | SameSite=Strict + Origin allowlist (guards 3) |
| Login-flow CSRF / code injection | `state` nonce bound to a short-lived cookie |
| Token leakage | Viewer tokens never stored; zero scopes requested |
| Trade spam / scripting | Per-user cooldown + per-IP bucket + Cloudflare edge rule; trades are value-neutral at static prices so scripting yields nothing anyway |
| Race on balance check | Per-user lock (guard 8) |
| Bot impersonating a viewer | Excluded-usernames filter (guard 4) |
| Secret on disk | Same posture as Stripe keys: config_local.py, gitignored |
| Cloudflare caching an authed response | All authed JSON responses set `Cache-Control: private, no-store` |
| Dashboard exposure | Unchanged — 8069 never enters this design |

Residual risk accepted: a compromised viewer browser can trade as that
viewer for up to 30 days. Blast radius is that viewer's hats — fake
currency, fee-free, reversible by hand in MixItUp. Acceptable.

## 8. Implementation plan

1. `core/web_session.py` — sign/verify/issue/clear + unit tests
   (`tools/test_web_session.py`: round-trip, tamper, expiry, bad
   secret). No bot dependencies; test first, wire second.
2. OAuth routes on `PublicWebServer` + `GET /api/me`. Test against
   localhost redirect URI before touching DNS-facing config.
3. `POST /api/trade` handler + guards + per-user locks; wire
   `economy=` into `PublicWebServer` in main.py. Optional
   `transactions.channel` migration.
4. `tools/test_web_trade.py` — fake economy + forged/valid sessions:
   every guard in §4.2 gets a test, same style as
   `test_priority_request.py`.
5. UI: header auth state → portfolio trade controls → god page box →
   priority-form lock-in.
6. `market_open` in `/api/stream-status` + header indicator.
7. Cloudflare WAF rate rule; flip `WEB_TRADING_ENABLED` on a live
   stream with announcement; watch the trade feed.
8. Update `HatmasBot.md` (v2.7 section) + `check_stream_ready.py`
   gains a "web session secret configured" probe.

Estimated effort: steps 1–4 are one solid session; 5–6 a second; 7–8
cleanup. Each step lands independently — the site keeps working with
the feature half-built because everything is gated on
`WEB_TRADING_ENABLED`.

## 9. Future work (explicitly out of scope)

- Linking YouTube portfolios to Twitch identities (cross-platform
  merge is a data-model question, not an auth question).
- Web-initiated gambling/wagers, gifting shares, limit orders.
- Account deletion/GDPR-style export — revisit if the site grows
  beyond the community.
