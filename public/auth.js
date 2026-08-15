/*
 * auth.js — shared website login + trading client for hatmaster.tv
 * ================================================================
 * Hydrates auth state from GET /api/me (the static pages stay
 * cacheable; JS renders the login button / avatar chip into the
 * brand-band), and exposes HatmasAuth.trade() for the portfolio and
 * god pages. See WEBSITE_TRADING_DESIGN.md §5.
 *
 * Plain text everywhere per the repo Tone rule. All user-supplied
 * strings (display names, avatar URLs) are set via textContent /
 * properties, never innerHTML.
 */

window.HatmasAuth = (function () {
  let me = null;
  let readyResolve;
  const ready = new Promise(function (r) { readyResolve = r; });

  async function fetchMe() {
    try {
      const r = await fetch('/api/me', { cache: 'no-store' });
      me = await r.json(); // 401 body parses too (logged_in: false)
    } catch (e) {
      me = { logged_in: false, login_available: false };
    }
    return me;
  }

  function buildAuthChip() {
    const el = document.createElement('div');
    el.className = 'auth-chip';
    if (me && me.logged_in) {
      const isYt = me.prov === 'yt';
      if (me.img) {
        const img = document.createElement('img');
        img.className = 'auth-avatar';
        img.src = me.img;
        img.alt = '';
        el.appendChild(img);
      }
      const link = document.createElement('a');
      link.className = 'auth-name';
      // YouTube sessions have no Twitch login — their portfolio
      // lives under the channel id, unless the channel merged into
      // a Twitch account, in which case THAT portfolio holds the
      // shares and the chip should point there.
      link.href = isYt
        ? (me.yt_linked_to
            ? '/twitch/' + encodeURIComponent(me.yt_linked_to)
            : '/yt/' + encodeURIComponent(me.uid))
        : '/twitch/' + encodeURIComponent(me.login);
      link.textContent = me.name || me.login || 'You';
      link.title = 'Your portfolio';
      el.appendChild(link);
      if (!isYt && me.yt_login_available) {
        if (me.yt_linked) {
          // Linked state, made visible: comment shares from the
          // linked channel land in this portfolio.
          const badge = document.createElement('span');
          badge.className = 'linked-chip';
          badge.textContent = 'YT Linked';
          badge.title = 'Your YouTube channel is linked. Shares from '
            + 'your YouTube comments land in this portfolio.';
          el.appendChild(badge);
        } else {
          // One-time merge: prove YouTube ownership via Google, the
          // server folds that channel's shares into this account.
          const yt = document.createElement('a');
          yt.className = 'icon-toggle';
          yt.href = '/auth/google/login?link=1';
          yt.textContent = 'Link YouTube';
          yt.title = 'Move your YouTube comment shares into this account';
          el.appendChild(yt);
        }
      }
      if (isYt && me.yt_linked_to) {
        const badge = document.createElement('span');
        badge.className = 'linked-chip';
        badge.textContent = 'Linked to Twitch';
        badge.title = 'This channel is linked to '
          + me.yt_linked_to
          + '. Your shares live in that portfolio.';
        el.appendChild(badge);
      }
      const out = document.createElement('button');
      out.className = 'icon-toggle';
      out.textContent = 'Log out';
      out.addEventListener('click', async function () {
        try { await fetch('/auth/logout', { method: 'POST' }); }
        catch (e) { /* cookie clear is best-effort */ }
        location.reload();
      });
      el.appendChild(out);
      return el;
    }
    if (me && (me.login_available || me.yt_login_available)) {
      if (me.login_available) {
        const a = document.createElement('a');
        a.className = 'icon-toggle';
        a.href = '/auth/login';
        a.textContent = 'Log in with Twitch';
        a.title = 'Log in to trade from the site';
        el.appendChild(a);
      }
      if (me.yt_login_available) {
        const y = document.createElement('a');
        y.className = 'icon-toggle';
        y.href = '/auth/google/login';
        y.textContent = 'Log in with YouTube';
        y.title = 'See the shares your YouTube comments earned';
        el.appendChild(y);
      }
      return el;
    }
    return null; // login not configured — render nothing
  }

  function buildMarketChip() {
    // Only meaningful when trading is configured at all.
    if (!me || me.trading_enabled === undefined || !me.trading_enabled) {
      return null;
    }
    const el = document.createElement('span');
    el.className = 'market-chip ' + (me.market_open ? 'open' : 'closed');
    el.id = 'market-chip';
    el.textContent = me.market_open ? 'MARKET OPEN' : 'MARKET CLOSED';
    el.title = me.market_open
      ? 'The bot is up. Trades execute immediately'
      : 'Trades need the bot + MixItUp running';
    return el;
  }

  function buildSiteFooter() {
    // Shared footer for every page that loads auth.js. The landing
    // page ships its own static copy (Google OAuth verification wants
    // the /privacy link in the no-JS homepage HTML) — the
    // .site-footer guard below skips it there. Static trusted markup
    // only; keep the link list in sync with landing.html's footer.
    const wrap = document.querySelector('.wrap');
    if (!wrap || document.querySelector('.site-footer')) return;
    const el = document.createElement('footer');
    el.className = 'site-footer';
    el.innerHTML =
      '<div class="ft-grid">' +
        '<div>' +
          '<span class="ft-brand-mark">' +
            '<img src="/hat.png" alt="">Hatmaster.tv</span>' +
          '<p class="ft-tag">Smite 2 streams, videos, and the ' +
            'Hatmas Market.</p>' +
        '</div>' +
        '<div class="ft-col"><div class="ft-head">Site</div>' +
          '<a href="/market">Hatmas Market</a>' +
          '<a href="/community">Community</a>' +
          '<a href="/events">Events</a>' +
          '<a href="/live">Live match</a>' +
          '<a href="/FindIt">FindIt</a>' +
        '</div>' +
        '<div class="ft-col"><div class="ft-head">Follow</div>' +
          '<a href="https://twitch.tv/hatmaster" target="_blank" ' +
            'rel="noopener noreferrer">Twitch</a>' +
          '<a href="https://www.youtube.com/@Hatmaster" target="_blank" ' +
            'rel="noopener noreferrer">YouTube</a>' +
          '<a href="https://www.tiktok.com/@awfulmasterhat" ' +
            'target="_blank" rel="noopener noreferrer">TikTok</a>' +
          '<a href="https://bsky.app/profile/hatmasteryt.bsky.social" ' +
            'target="_blank" rel="noopener noreferrer">Bluesky</a>' +
        '</div>' +
      '</div>' +
      '<div class="ft-bottom">' +
        '<span>© 2026 Hatmaster</span>' +
        '<a href="/privacy">Privacy</a>' +
      '</div>';
    wrap.appendChild(el);
  }

  async function maybeLiveChip() {
    // Site-wide "stream is live" awareness: a red chip in the brand
    // band linking home to the embed. Live-only — the permanent
    // OFFLINE pill was removed July 2026 on purpose. Pages with
    // their own stream UI (#stream-section, i.e. the landing page)
    // handle live state themselves. CSS order:-1 pins the chip
    // leftmost however late this fetch resolves.
    if (document.getElementById('stream-section')) return;
    const cluster = document.querySelector('.brand-band .right-cluster')
      || document.querySelector('.brand-band');
    if (!cluster) return;
    try {
      const r = await fetch('/api/stream-status', { cache: 'no-store' });
      const data = await r.json();
      if (!data.is_live) return;
      const a = document.createElement('a');
      a.className = 'live-chip';
      a.href = '/';
      a.title = 'Hatmaster is live. Watch on the homepage.';
      const dot = document.createElement('span');
      dot.className = 'dot';
      a.appendChild(dot);
      a.appendChild(document.createTextNode('LIVE'));
      cluster.insertBefore(a, cluster.firstChild);
    } catch (e) { /* chip is a nicety — never block on it */ }
  }

  async function init() {
    buildSiteFooter();          // static — render before any fetch
    maybeLiveChip();            // fire-and-forget
    await fetchMe();
    const cluster = document.querySelector('.brand-band .right-cluster')
      || document.querySelector('.brand-band');
    if (cluster) {
      const chip = buildAuthChip();
      const market = buildMarketChip();
      if (market) cluster.insertBefore(market, cluster.firstChild);
      if (chip) cluster.insertBefore(chip, cluster.firstChild);
    }
    readyResolve(me);
    return me;
  }

  async function trade(action, god, amount) {
    try {
      const r = await fetch('/api/trade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, god: god, amount: amount }),
      });
      let data = null;
      try { data = await r.json(); } catch (e) { /* non-JSON error */ }
      if (data) return data;
      return { ok: false, error: 'Request failed (' + r.status + ')' };
    } catch (e) {
      return { ok: false, error: 'Network error - try again.' };
    }
  }

  async function balance() {
    try {
      const r = await fetch('/api/me/balance', { cache: 'no-store' });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  // Current position in one god: { ok, god, shares, avg_cost, price }.
  // Lets a page show "you own N" on load without scraping the portfolio.
  async function holding(god) {
    try {
      const r = await fetch('/api/me/holding/' + encodeURIComponent(god),
        { cache: 'no-store' });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    ready: ready,
    trade: trade,
    balance: balance,
    holding: holding,
    get me() { return me; },
  };
})();
