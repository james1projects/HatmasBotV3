// Alert kind: tradefeed_rolling — the legacy trade feed's behaviour: a
// panel that stays up and appends each trade (newest on top, last eight
// kept), hiding `duration` seconds after the last one. Migrated from
// overlays/economy_tradefeed.html (TODO(alertbox): delete once switched).
// A "sticky" kind: while it is on screen, a new alert of the same kind
// updates it instead of queueing behind it, and its timer restarts.
// data = {type: buy|sell|dividend|free, username, god | god_name, shares, total_hats, holders, viewers}.
HatmasAlerts.define('tradefeed_rolling', {
  sticky: true,
  render(el, d, ctx) {
    ctx.style('hm-kind-tradefeed-rolling', `
      .hmk-feed { width: 300px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px; overflow: hidden; font-family: 'Inter', sans-serif; }
      .hmk-feed .head { padding: 9px 14px; border-bottom: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 17px; color: var(--hm-text-secondary, #AFC2C9); letter-spacing: 2px; text-transform: uppercase; }
      .hmk-feed .list { padding: 4px 0; max-height: 260px; overflow: hidden; }
      .hmk-feed .trade { display: flex; align-items: center; gap: 10px; padding: 6px 14px; opacity: 0; transform: translateY(-8px); animation: hmkFeedIn 0.3s ease-out forwards; }
      .hmk-feed .trade + .trade { border-top: 1px solid var(--hm-separator, rgba(223,160,110,0.06)); }
      @keyframes hmkFeedIn { to { opacity: 1; transform: translateY(0); } }
      .hmk-feed .trade.dividend { background: var(--hm-bg-subtle, rgba(223,160,110,0.03)); } .hmk-feed .trade.free { background: var(--hm-blue-bg, rgba(125,152,161,0.1)); }
      .hmk-feed .icon { width: 24px; height: 24px; border-radius: 5px; overflow: hidden; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); flex-shrink: 0; }
      .hmk-feed .icon img { width: 100%; height: 100%; object-fit: cover; }
      .hmk-feed .text { flex: 1; min-width: 0; font-size: 11px; color: var(--hm-text-secondary, #AFC2C9); line-height: 1.3; }
      .hmk-feed .user, .hmk-feed .god { color: var(--hm-text, #fff); font-weight: 600; }
      .hmk-feed .buy { color: var(--hm-green, #DFA06E); font-weight: 600; } .hmk-feed .sell { color: var(--hm-red, #DF2935); font-weight: 600; }
      .hmk-feed .amount { font-family: 'JetBrains Mono', monospace; font-weight: 700; }
      .hmk-feed .dlabel { color: var(--hm-gold, #DFA06E); font-weight: 600; } .hmk-feed .flabel { color: var(--hm-blue, #7D98A1); font-weight: 600; }
      .hmk-feed .when { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: var(--hm-text-muted, #8FA6AF); flex-shrink: 0; }
    `);
    el.innerHTML = '<div class="hmk-feed"><div class="head">Trade feed</div><div class="list"></div></div>';
    this.update(el, d, ctx);
  },
  update(el, d, ctx) {
    const esc = ctx.esc, list = el.querySelector('.list');
    if (!list || !d || !d.type) return;
    const god = d.god || d.god_name || 'Unknown', user = d.display_name || d.username || 'someone';
    const n = Math.round(d.shares || 1), shares = '<span class="amount">' + n + (n === 1 ? ' share' : ' shares') + '</span>';
    let text;
    if (d.type === 'sell') text = '<span class="user">' + esc(user) + '</span> <span class="sell">sold</span> ' + shares + ' of <span class="god">' + esc(god) + '</span>';
    else if (d.type === 'dividend') text = '<span class="dlabel">Dividend:</span> <span class="god">' + esc(god) + '</span> paid <span class="amount">' + ctx.hats(d.total_hats || d.amount || 0, {compact: true, size: 'sm'}) + '</span> to <span class="amount">' + esc(d.holders || 0) + '</span> holders';
    else if (d.type === 'free') text = '<span class="flabel">Free share:</span> <span class="god">' + esc(god) + '</span> distributed to <span class="amount">' + esc(d.viewers || 0) + '</span> viewers';
    else text = '<span class="user">' + esc(user) + '</span> <span class="buy">bought</span> ' + shares + ' of <span class="god">' + esc(god) + '</span>';
    const icon = ctx.godIcon(god);
    list.insertAdjacentHTML('afterbegin', '<div class="trade ' + esc(d.type) + '"><div class="icon"><img src="' + icon.custom + '" alt=""></div><div class="text">' + text + '</div><span class="when">now</span></div>');
    const img = list.querySelector('img'); img.onerror = () => { img.onerror = null; img.src = icon.fallback; };
    while (list.children.length > 8) list.lastChild.remove();
  }
});
