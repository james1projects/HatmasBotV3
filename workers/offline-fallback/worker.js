/*
 * hatmaster.tv offline fallback — Cloudflare Worker.
 *
 * Sits on the hatmaster.tv/* route at the Cloudflare edge, in front
 * of the tunnel. Normal traffic passes through untouched. When the
 * origin is unreachable — tunnel down (530/1033: PC asleep or
 * cloudflared stopped) or tunnel up but the bot process dead
 * (502/503/504 from cloudflared) — visitors get a branded offline
 * page instead of Cloudflare's error screen.
 *
 * The page is fully self-contained: when the origin is down,
 * /theme.css and /hat.png are unreachable too, so the needed design
 * tokens from public/theme.css are inlined here. If the brand
 * palette changes, update both.
 *
 * Responds 503 + Retry-After so uptime monitors still register the
 * outage, and no-store so nothing caches the outage page.
 *
 * Deploy: see README.md in this directory. NOT auto-deployed.
 */

const ORIGIN_DOWN_STATUSES = new Set([502, 503, 504, 521, 522, 523, 530]);

const OFFLINE_PAGE = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#202C39">
<title>Hatmaster.tv Offline</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@700&family=Bebas+Neue&display=swap');
  :root {
    --bg: #202C39;
    --panel: #2A3645;
    --panel-hi: #344252;
    --border: rgba(125, 152, 161, 0.22);
    --text: #FFFFFF;
    --text-dim: #9FB4BB;
    --accent: #DFA06E;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    --font-mono: 'JetBrains Mono', 'IBM Plex Mono', 'SF Mono', monospace;
    --font-display: 'Bebas Neue', 'Big Noodle Titling', Impact, sans-serif;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    min-height: 100vh;
  }
  .wrap {
    max-width: 600px;
    margin: 0 auto;
    padding: 24px 16px 60px;
  }
  .brand-band {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding: 8px 0 12px;
    margin-bottom: 64px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
  }
  .brand-band .left { color: var(--accent); font-weight: 700; }
  .err { text-align: center; }
  .err .kicker {
    margin-bottom: 18px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
  }
  .err .code {
    font-family: var(--font-mono);
    font-size: 96px;
    font-weight: 700;
    color: var(--accent);
    line-height: 1;
    letter-spacing: -0.05em;
    margin: 0 0 12px;
  }
  .err h1 {
    font-family: var(--font-display);
    font-size: 40px;
    margin: 0 0 18px;
    letter-spacing: 0.02em;
    font-weight: 400;
    line-height: 1;
    text-transform: uppercase;
  }
  .err p {
    color: var(--text-dim);
    font-size: 14px;
    line-height: 1.6;
    margin: 0 auto 28px;
    max-width: 440px;
  }
  .err .home-link {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 18px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 100px;
    color: var(--text);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    text-decoration: none;
    transition: border-color 0.12s, transform 0.12s, background 0.12s;
  }
  .err .home-link:hover {
    border-color: var(--accent);
    background: var(--panel-hi);
    transform: translateY(-1px);
  }
  @media (max-width: 480px) {
    .err .code { font-size: 72px; }
    .err h1 { font-size: 26px; }
    .brand-band { margin-bottom: 40px; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="brand-band">
      <div class="left">HATMASTER.TV</div>
      <div>OFFLINE</div>
    </div>
    <div class="err">
      <div class="kicker">Signal Lost</div>
      <div class="code">503</div>
      <h1>Hatmaster.tv Offline</h1>
      <p>Hat is performing maintenance or has his stream PC off,
         Hatmaster.tv is only on when his computer is on for now!</p>
      <a class="home-link" href="https://twitch.tv/hatmaster">Watch on Twitch &rarr;</a>
      <a class="home-link" href="/">Retry &rarr;</a>
    </div>
  </div>
</body>
</html>`;

function offlineResponse() {
  return new Response(OFFLINE_PAGE, {
    status: 503,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "retry-after": "120",
      "cache-control": "no-store",
    },
  });
}

export default {
  async fetch(request) {
    let response;
    try {
      response = await fetch(request);
    } catch (err) {
      return offlineResponse();
    }
    if (ORIGIN_DOWN_STATUSES.has(response.status)) {
      // Only dress up page loads; API callers and asset fetches keep
      // the raw status so nothing downstream misparses HTML as JSON.
      const accept = request.headers.get("accept") || "";
      if (request.method === "GET" && accept.includes("text/html")) {
        return offlineResponse();
      }
    }
    return response;
  },
};
