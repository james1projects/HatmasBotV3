// Alert kind: portfolio — a viewer's holdings on request. Migrated from
// overlays/economy_portfolio.html (TODO(alertbox): delete once switched).
// data = {username, display_name, profile_image_url, hat_balance, total_value, total_pnl,
//         holdings: [{god_name, shares, avg_cost, price, value, pnl, pnl_pct}]}.
HatmasAlerts.define('portfolio', {
  render(el, d, ctx) {
    ctx.style('hm-kind-portfolio', `
      .hmk-pf { width: 380px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 12px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.6); font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); }
      .hmk-pf .header { padding: 14px 18px 12px; border-bottom: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); }
      .hmk-pf .top { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
      .hmk-pf .pfp { width: 38px; height: 38px; border-radius: 50%; object-fit: cover; flex-shrink: 0; border: 2px solid var(--hm-border, rgba(223,160,110,0.18)); background: var(--hm-icon-bg, rgba(223,160,110,0.1)); }
      .hmk-pf .uname { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 22px; letter-spacing: 1px; line-height: 1.1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .hmk-pf .plabel, .hmk-pf .vlabel { font-size: 9px; font-weight: 500; color: var(--hm-text-muted, #8FA6AF); letter-spacing: 2px; text-transform: uppercase; }
      .hmk-pf .vals { display: flex; }
      .hmk-pf .vb { flex: 1; padding: 6px 0; }
      .hmk-pf .vb + .vb { border-left: 1px solid var(--hm-icon-bg, rgba(223,160,110,0.1)); padding-left: 16px; }
      .hmk-pf .amt { font-family: 'JetBrains Mono', monospace; font-size: 18px; font-weight: 500; line-height: 1.1; }
      .hmk-pf .amt.bal { color: var(--hm-gold, #DFA06E); }
      .hmk-pf .amt .sfx { font-size: 11px; color: var(--hm-text-secondary, #AFC2C9); font-weight: 400; margin-left: 2px; }
      .hmk-pf .pnl { font-family: 'JetBrains Mono', monospace; font-size: 11px; margin-top: 1px; }
      .hmk-pf .pos { color: var(--hm-green, #DFA06E); } .hmk-pf .neg { color: var(--hm-red, #DF2935); } .hmk-pf .neu { color: var(--hm-text-muted, #8FA6AF); }
      .hmk-pf .sub9 { font-size: 9px; color: var(--hm-text-muted, #8FA6AF); margin-top: 1px; }
      .hmk-pf .holdings { padding: 6px 0; max-height: 300px; overflow: hidden; }
      .hmk-pf .hrow { display: flex; align-items: center; padding: 8px 18px; gap: 10px; }
      .hmk-pf .hrow img { width: 36px; height: 36px; border-radius: 6px; object-fit: cover; flex-shrink: 0; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); border: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); }
      .hmk-pf .hinfo { flex: 1; min-width: 0; }
      .hmk-pf .gname { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.2; }
      .hmk-pf .shares { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--hm-text-secondary, #AFC2C9); }
      .hmk-pf .hval { text-align: right; flex-shrink: 0; }
      .hmk-pf .hcur { font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 500; line-height: 1.2; }
      .hmk-pf .hpnl { font-family: 'JetBrains Mono', monospace; font-size: 10px; }
      .hmk-pf .empty { padding: 30px 18px; text-align: center; font-size: 12px; color: var(--hm-text-muted, #8FA6AF); }
      .hmk-pf .foot { padding: 8px 18px 10px; border-top: 1px solid var(--hm-icon-bg, rgba(223,160,110,0.1)); display: flex; justify-content: space-between; }
      .hmk-pf .fs { display: flex; flex-direction: column; align-items: center; }
      .hmk-pf .fs span { font-size: 8px; font-weight: 500; color: var(--hm-text-muted, #8FA6AF); letter-spacing: 1.5px; text-transform: uppercase; }
      .hmk-pf .fs b { font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 400; color: var(--hm-text-secondary, #AFC2C9); }
      .hmk-pf .fs b.gold { color: var(--hm-gold, #DFA06E); font-weight: 500; }
    `);
    const esc = ctx.esc;
    const fmt = (v) => ctx.hats(Number(v) || 0, {compact: true, size: 'sm', label: false});
    const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu');
    const invested = Number(d.total_value) || 0, pnl = Number(d.total_pnl) || 0, bal = Number(d.hat_balance) || 0;
    const pnlPct = invested > 0 ? (pnl / invested * 100) : 0;
    const holdings = Array.isArray(d.holdings) ? d.holdings : [];
    const rows = holdings.map((h, i) => {
      const ic = ctx.godIcon(h.god_name), hp = Number(h.pnl) || 0, pct = Number(h.pnl_pct) || 0;
      return '<div class="hrow" style="animation-delay:' + (i * 0.05) + 's"><img src="' + ic.custom + '" data-fb="' + ic.fallback + '" alt="">' +
        '<div class="hinfo"><div class="gname">' + esc(h.god_name) + '</div><div class="shares">' + esc(h.shares) + ' shares' + (h.avg_cost != null ? ' @ ' + ctx.hats(Number(h.avg_cost) || 0, {size: 'sm', label: false}) : '') + '</div></div>' +
        '<div class="hval"><div class="hcur">' + fmt(h.value) + '</div><div class="hpnl ' + cls(hp) + '">' + (hp > 0 ? '+' : '') + fmt(hp) + ' (' + (pct > 0 ? '+' : '') + pct.toFixed(1) + '%)</div></div></div>';
    }).join('');
    const best = holdings.length ? holdings.reduce((b, h) => ((Number(h.pnl_pct) || 0) > (Number(b.pnl_pct) || 0) ? h : b)) : null;
    const shares = holdings.reduce((s, h) => s + (Number(h.shares) || 0), 0);
    el.innerHTML = '<div class="hmk-pf"><div class="header"><div class="top">' +
      (d.profile_image_url ? '<img class="pfp" src="' + esc(d.profile_image_url) + '" alt="">' : '') +
      '<div style="flex:1;min-width:0"><div class="uname">' + esc(d.display_name || d.username || '--') + '</div><div class="plabel">Portfolio</div></div></div>' +
      '<div class="vals"><div class="vb"><div class="vlabel">Invested</div><div class="amt">' + fmt(invested) + '<span class="sfx">HAT</span></div><div class="pnl ' + cls(pnl) + '">' + (pnl > 0 ? '+' : '') + fmt(pnl) + ' (' + (pnlPct > 0 ? '+' : '') + pnlPct.toFixed(1) + '%)</div></div>' +
      '<div class="vb"><div class="vlabel">Balance</div><div class="amt bal">' + fmt(bal) + '<span class="sfx">HAT</span></div><div class="sub9">Available</div></div></div></div>' +
      '<div class="holdings">' + (rows || '<div class="empty">No holdings yet</div>') + '</div>' +
      '<div class="foot"><div class="fs"><span>Net worth</span><b>' + fmt(invested + bal) + '</b></div><div class="fs"><span>Gods</span><b>' + holdings.length + '</b></div>' +
      '<div class="fs"><span>Shares</span><b>' + Math.round(shares) + '</b></div><div class="fs"><span>Best god</span><b class="gold">' + esc(best ? best.god_name : '--') + '</b></div></div></div>';
    el.querySelectorAll('img[data-fb]').forEach((img) => { img.onerror = () => { img.onerror = null; img.src = img.dataset.fb; }; });
    const pfp = el.querySelector('.pfp'); if (pfp) pfp.onerror = () => { pfp.style.display = 'none'; };
  }
});
