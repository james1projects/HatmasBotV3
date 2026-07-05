# hatmaster.tv offline fallback (Cloudflare Worker)

When the tunnel is down (PC asleep, cloudflared stopped) or the tunnel
is up but the bot is dead, visitors currently get Cloudflare's raw
error screen (error 1033 / 502). This Worker sits on `hatmaster.tv/*`
at the edge and swaps those failures for a branded "Market closed"
page — same palette and type system as the 404 page, fully
self-contained (no origin assets), 503 + `Retry-After: 120` so uptime
monitors still see the outage, `no-store` so it never caches over the
real site.

Passthrough rules:

- Healthy responses: untouched.
- Origin-down statuses (502, 503, 504, 521, 522, 523, 530) on a GET
  with `Accept: text/html`: replaced by the offline page.
- Same statuses on API/asset requests: passed through raw, so JSON
  consumers never receive HTML.

## Deploy (one-time, ~5 minutes)

### Option A — dashboard (no tooling)

1. Cloudflare dashboard -> Workers & Pages -> Create -> Worker.
2. Name it `hatmaster-offline-fallback`, paste the contents of
   `worker.js`, Deploy.
3. On the worker -> Settings -> Domains & Routes -> Add -> Route:
   zone `hatmaster.tv`, route `hatmaster.tv/*`.
4. If a `www` DNS record exists, add `www.hatmaster.tv/*` too.

### Option B — wrangler CLI

```
npm install -g wrangler     # needs node; not installed on this PC yet
cd workers/offline-fallback
wrangler login
wrangler deploy
```

## Verify after deploying

1. With the bot and tunnel running: browse hatmaster.tv — site should
   behave exactly as before (worker passes through).
2. Stop the bot (`quit` in the console) but leave the cloudflared
   service running: hatmaster.tv should show the Market Closed page
   within seconds. `curl -si https://hatmaster.tv | head -5` should
   show `HTTP/2 503` and `retry-after: 120`.
3. Also stop the cloudflared service (`sc stop cloudflared`,
   elevated): same branded page instead of Cloudflare error 1033.
   Restart it after (`sc start cloudflared`).

## Notes

- Free plan allows 100k worker requests/day — orders of magnitude
  above this site's traffic. Every request to the zone now passes
  through the worker (that is how routes work); overhead is sub-ms.
- The page inlines its design tokens from `public/theme.css`. If the
  brand palette changes, update the copy in `worker.js`.
- The Twitch link points at https://twitch.tv/hatmaster (taken from
  public/landing.html).
