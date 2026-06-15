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
      if (me.img) {
        const img = document.createElement('img');
        img.className = 'auth-avatar';
        img.src = me.img;
        img.alt = '';
        el.appendChild(img);
      }
      const link = document.createElement('a');
      link.className = 'auth-name';
      link.href = '/twitch/' + encodeURIComponent(me.login);
      link.textContent = me.name || me.login;
      link.title = 'Your portfolio';
      el.appendChild(link);
      const out = document.createElement('button');
      out.className = 'icon-toggle';
      out.textContent = '[LOG OUT]';
      out.addEventListener('click', async function () {
        try { await fetch('/auth/logout', { method: 'POST' }); }
        catch (e) { /* cookie clear is best-effort */ }
        location.reload();
      });
      el.appendChild(out);
      return el;
    }
    if (me && me.login_available) {
      const a = document.createElement('a');
      a.className = 'icon-toggle';
      a.href = '/auth/login';
      a.textContent = '[LOG IN WITH TWITCH]';
      a.title = 'Log in to trade from the site';
      el.appendChild(a);
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
      ? 'The bot is up — trades execute immediately'
      : 'Trades need the bot + MixItUp running';
    return el;
  }

  async function init() {
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    ready: ready,
    trade: trade,
    balance: balance,
    get me() { return me; },
  };
})();
