// Alert kind: tradefeed — one trade (buy / sell / dividend / free share) as
// it happens. Migrated from overlays/economy_tradefeed.html (TODO(alertbox):
// delete once switched). The legacy source kept a rolling list of eight;
// as an alert each trade is its own card and bursts queue in the lane.
// data = {type: buy|sell|dividend|free, username, god | god_name, shares, total_hats, holders, viewers}.
HatmasAlerts.define('tradefeed', {
  render(el, d, ctx) {
    ctx.style('hm-kind-tradefeed', `
      .hmk-trade { width: 380px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px; overflow: hidden; font-family: 'Inter', sans-serif; }
      .hmk-trade .head { padding: 7px 14px; border-bottom: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 15px; color: var(--hm-text-secondary, #AFC2C9); letter-spacing: 2px; text-transform: uppercase; }
      .hmk-trade .row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; }
      .hmk-trade .row.dividend { background: var(--hm-bg-subtle, rgba(223,160,110,0.03)); }
      .hmk-trade .row.free { background: var(--hm-blue-bg, rgba(125,152,161,0.1)); }
      .hmk-trade .icon { width: 28px; height: 28px; border-radius: 5px; overflow: hidden; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); flex-shrink: 0; }
      .hmk-trade .icon img { width: 100%; height: 100%; object-fit: cover; }
      .hmk-trade .text { flex: 1; min-width: 0; font-size: 13px; color: var(--hm-text-secondary, #AFC2C9); line-height: 1.3; }
      .hmk-trade .user, .hmk-trade .god { color: var(--hm-text, #fff); font-weight: 600; }
      .hmk-trade .buy { color: var(--hm-green, #DFA06E); font-weight: 600; }
      .hmk-trade .sell { color: var(--hm-red, #DF2935); font-weight: 600; }
      .hmk-trade .amount { font-family: 'JetBrains Mono', monospace; font-weight: 700; }
      .hmk-trade .dlabel { color: var(--hm-gold, #DFA06E); font-weight: 600; }
      .hmk-trade .flabel { color: var(--hm-blue, #7D98A1); font-weight: 600; }
    `);
    const esc = ctx.esc;
    const god = d.god || d.god_name || 'Unknown';
    const user = d.display_name || d.username || 'someone';
    const n = Math.round(d.shares || 1), shares = '<span class="amount">' + n + (n === 1 ? ' share' : ' shares') + '</span>';
    let text, cls = d.type || 'buy';
    if (d.type === 'sell') text = '<span class="user">' + esc(user) + '</span> <span class="sell">sold</span> ' + shares + ' of <span class="god">' + esc(god) + '</span>';
    else if (d.type === 'dividend') text = '<span class="dlabel">Dividend:</span> <span class="god">' + esc(god) + '</span> paid <span class="amount">' + ctx.hats(d.total_hats || d.amount || 0, {compact: true, size: 'sm'}) + '</span> to <span class="amount">' + esc(d.holders || 0) + '</span> holders';
    else if (d.type === 'free') text = '<span class="flabel">Free share:</span> <span class="god">' + esc(god) + '</span> distributed to <span class="amount">' + esc(d.viewers || 0) + '</span> viewers';
    else text = '<span class="user">' + esc(user) + '</span> <span class="buy">bought</span> ' + shares + ' of <span class="god">' + esc(god) + '</span>';
    const icon = ctx.godIcon(god);
    el.innerHTML = '<div class="hmk-trade"><div class="head">Trade feed</div><div class="row ' + esc(cls) + '"><div class="icon"><img src="' + icon.custom + '" alt=""></div><div class="text">' + text + '</div></div></div>';
    const img = el.querySelector('img'); img.onerror = () => { img.onerror = null; img.src = icon.fallback; };
  }
});
