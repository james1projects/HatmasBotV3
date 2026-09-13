// Alert kind: leaderboard — top portfolios on request. Migrated from
// overlays/economy_leaderboard.html (TODO(alertbox): delete once switched).
// data = {leaderboard: [{username | name, portfolio_value | value, change_pct, rank_change, top_gods: [...]}]}.
HatmasAlerts.define('leaderboard', {
  render(el, d, ctx) {
    ctx.style('hm-kind-leaderboard', `
      .hmk-lb { width: 260px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px; overflow: hidden; font-family: 'Inter', sans-serif; }
      .hmk-lb .head { padding: 10px 14px 8px; border-bottom: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); display: flex; align-items: center; justify-content: space-between; }
      .hmk-lb .title { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 17px; color: var(--hm-text-secondary, #AFC2C9); letter-spacing: 2px; text-transform: uppercase; }
      .hmk-lb .sub { font-size: 9px; color: var(--hm-text-muted, #8FA6AF); text-transform: uppercase; letter-spacing: 0.5px; }
      .hmk-lb .rows { padding: 6px 0; }
      .hmk-lb .row { display: flex; align-items: center; gap: 10px; padding: 7px 14px; opacity: 0; transform: translateX(-10px); animation: hmkLbEnter 0.3s ease-out forwards; }
      .hmk-lb.static .row { opacity: 1; transform: none; animation: none; }
      @keyframes hmkLbEnter { to { opacity: 1; transform: translateX(0); } }
      .hmk-lb .rank { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 13px; width: 18px; text-align: center; }
      .hmk-lb .row:nth-child(1) .rank { color: var(--hm-gold-bright, #E8B583); } .hmk-lb .row:nth-child(2) .rank { color: var(--hm-text-secondary, #AFC2C9); } .hmk-lb .row:nth-child(3) .rank { color: var(--hm-gold, #DFA06E); }
      .hmk-lb .row:nth-child(n+4) .rank { color: var(--hm-text-muted, #8FA6AF); }
      .hmk-lb .arrow { font-family: monospace; font-size: 10px; width: 8px; text-align: center; }
      .hmk-lb .arrow.up { color: var(--hm-green, #DFA06E); } .hmk-lb .arrow.down { color: var(--hm-red, #DF2935); } .hmk-lb .arrow.same { color: var(--hm-text-muted, #8FA6AF); }
      .hmk-lb .who { flex: 1; min-width: 0; }
      .hmk-lb .name { font-weight: 600; font-size: 12px; color: var(--hm-text, #fff); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .hmk-lb .gods { display: flex; gap: 3px; margin-top: 1px; }
      .hmk-lb .gods img { width: 14px; height: 14px; border-radius: 3px; object-fit: cover; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); }
      .hmk-lb .val { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 12px; color: var(--hm-text-secondary, #AFC2C9); text-align: right; flex-shrink: 0; }
      .hmk-lb .chg { font-family: 'JetBrains Mono', monospace; font-size: 10px; text-align: right; }
      .hmk-lb .chg.up { color: var(--hm-green, #DFA06E); } .hmk-lb .chg.down { color: var(--hm-red, #DF2935); }
      .hmk-lb .foot { padding: 7px 14px; border-top: 1px solid var(--hm-separator, rgba(223,160,110,0.08)); font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 11px; color: var(--hm-text-muted, #8FA6AF); letter-spacing: 1px; text-transform: uppercase; text-align: center; }
    `);
    const esc = ctx.esc;
    const rows = (d.leaderboard || []).slice(0, 5).map((inv, i) => {
      const change = Number(inv.change_pct) || 0, rc = Number(inv.rank_change) || 0;
      const gods = (inv.top_gods || []).map((g) => { const ic = ctx.godIcon(g); return '<img src="' + ic.custom + '" data-fb="' + ic.fallback + '" alt="">'; }).join('');
      return '<div class="row"><span class="rank">' + (i + 1) + '</span><span class="arrow ' + (rc > 0 ? 'up' : rc < 0 ? 'down' : 'same') + '">' + (rc > 0 ? '&#9650;' : rc < 0 ? '&#9660;' : '=') + '</span>' +
        '<div class="who"><div class="name">' + esc(inv.display_name || inv.username || inv.name || '') + '</div><div class="gods">' + gods + '</div></div>' +
        '<div><div class="val">' + ctx.hats(Number(inv.portfolio_value != null ? inv.portfolio_value : (inv.value != null ? inv.value : inv.total_value)) || 0, {compact: true, size: 'sm', label: false}) + '</div>' +
        '<div class="chg ' + (change >= 0 ? 'up' : 'down') + '">' + (change >= 0 ? '+' : '') + change.toFixed(1) + '%</div></div></div>';
    }).join('');
    el.innerHTML = '<div class="hmk-lb' + (ctx.preview ? ' static' : '') + '"><div class="head"><span class="title">Top investors</span><span class="sub">Portfolio value</span></div>' +
      '<div class="rows">' + (rows || '<div class="row"><span class="name" style="color:var(--hm-text-muted)">No portfolios yet</span></div>') + '</div><div class="foot">!portfolio &middot; !market</div></div>';
    el.querySelectorAll('img[data-fb]').forEach((img) => { img.onerror = () => { img.onerror = null; img.src = img.dataset.fb; }; });
    el.querySelectorAll('.row').forEach((r, i) => { r.style.animationDelay = (0.1 * (i + 1)) + 's'; });
  }
});
