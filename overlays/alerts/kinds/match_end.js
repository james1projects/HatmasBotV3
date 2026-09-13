// Alert kind: match_end — the economy's match report. Migrated from
// overlays/economy_match_end.html (TODO(alertbox): delete once switched).
// data = {god, outcome: win|loss, mode, kda: [k, d, a], streak, old_price, new_price, change_pct,
//         free_shares: {shares_each, viewer_count} | {}, movers: [{name, change_pct}]}.
HatmasAlerts.define('match_end', {
  render(el, d, ctx) {
    ctx.style('hm-kind-match-end', `
      .hmk-me { width: 380px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px; overflow: hidden; font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); }
      .hmk-me .result { display: flex; align-items: center; gap: 12px; padding: 12px 16px; }
      .hmk-me .result.win { border-bottom: 1px solid var(--hm-green-bg, rgba(223,160,110,0.1)); } .hmk-me .result.loss { border-bottom: 1px solid var(--hm-red-bg, rgba(223,41,53,0.1)); }
      .hmk-me .img { width: 44px; height: 44px; border-radius: 8px; overflow: hidden; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); flex-shrink: 0; }
      .hmk-me .img img { width: 100%; height: 100%; object-fit: cover; }
      .hmk-me .info { flex: 1; min-width: 0; }
      .hmk-me .tag { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 20px; letter-spacing: 2px; text-transform: uppercase; }
      .hmk-me .win .tag { color: var(--hm-green, #DFA06E); } .hmk-me .loss .tag { color: var(--hm-red, #DF2935); }
      .hmk-me .sub { font-size: 12px; color: var(--hm-text-muted, #8FA6AF); }
      .hmk-me .kda { display: flex; align-items: center; gap: 8px; }
      .hmk-me .kda b { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 18px; text-align: right; }
      .hmk-me .streak { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 11px; padding: 3px 6px; border-radius: 4px; white-space: nowrap; text-transform: uppercase; }
      .hmk-me .streak.win { background: var(--hm-green-bg, rgba(223,160,110,0.1)); color: var(--hm-green, #DFA06E); } .hmk-me .streak.loss { background: var(--hm-red-bg, rgba(223,41,53,0.1)); color: var(--hm-red, #DF2935); }
      .hmk-me .move { padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; }
      .hmk-me .prices { display: flex; align-items: center; gap: 10px; }
      .hmk-me .old { font-family: 'JetBrains Mono', monospace; font-size: 18px; color: var(--hm-text-muted, #8FA6AF); text-decoration: line-through; }
      .hmk-me .arr { color: var(--hm-border-subtle, rgba(223,160,110,0.3)); font-size: 14px; }
      .hmk-me .new { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 24px; }
      .hmk-me .up { color: var(--hm-green, #DFA06E); } .hmk-me .down { color: var(--hm-red, #DF2935); }
      .hmk-me .badge { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 16px; padding: 3px 10px; border-radius: 5px; }
      .hmk-me .badge.up { background: var(--hm-green-bg, rgba(223,160,110,0.1)); } .hmk-me .badge.down { background: var(--hm-red-bg, rgba(223,41,53,0.1)); }
      .hmk-me .shares { margin: 0 16px 12px; padding: 9px 12px; background: var(--hm-separator, rgba(223,160,110,0.06)); border: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); border-radius: 6px; font-size: 12px; color: var(--hm-text-secondary, #AFC2C9); }
      .hmk-me .movers { padding: 0 16px 12px; display: flex; gap: 6px; }
      .hmk-me .mover { flex: 1; display: flex; align-items: center; gap: 6px; background: var(--hm-separator, rgba(223,160,110,0.06)); border: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); border-radius: 5px; padding: 5px 8px; min-width: 0; }
      .hmk-me .mover img { width: 20px; height: 20px; border-radius: 4px; object-fit: cover; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); }
      .hmk-me .mover .n { font-size: 11px; font-weight: 600; color: var(--hm-text-secondary, #AFC2C9); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .hmk-me .mover .p { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; }
      .hmk-me .foot { padding: 7px 16px; border-top: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 13px; color: var(--hm-text-muted, #8FA6AF); display: flex; justify-content: space-between; letter-spacing: 1px; text-transform: uppercase; }
    `);
    const esc = ctx.esc;
    const win = String(d.outcome || '').toLowerCase() === 'win';
    const kda = Array.isArray(d.kda) ? d.kda : String(d.kda || '0/0/0').split('/');
    const streak = Number(d.streak) || 0;
    const oldP = Number(d.old_price) || 0, newP = Number(d.new_price) || 0, chg = Number(d.change_pct) || 0, up = chg >= 0;
    const fs = d.free_shares && typeof d.free_shares === 'object' ? d.free_shares : {};
    const icon = ctx.godIcon(d.god);
    const movers = (Array.isArray(d.movers) ? d.movers : []).slice(0, 3).map((m) => {
      const ic = ctx.godIcon(m.name), mu = (Number(m.change_pct) || 0) >= 0;
      return '<div class="mover"><img src="' + ic.custom + '" data-fb="' + ic.fallback + '" alt=""><span class="n">' + esc(m.name) + '</span><span class="p ' + (mu ? 'up' : 'down') + '">' + (mu ? '+' : '') + (Number(m.change_pct) || 0).toFixed(1) + '%</span></div>';
    }).join('');
    el.innerHTML = '<div class="hmk-me"><div class="result ' + (win ? 'win' : 'loss') + '"><div class="img"><img src="' + icon.custom + '" data-fb="' + icon.fallback + '" alt=""></div>' +
      '<div class="info"><div class="tag">' + (win ? 'Victory' : 'Defeat') + '</div><div class="sub">' + esc(d.god || '') + ' : ' + esc(d.mode || 'Conquest') + '</div></div>' +
      '<div class="kda"><b>' + esc(kda[0] || 0) + ' / ' + esc(kda[1] || 0) + ' / ' + esc(kda[2] || 0) + '</b>' +
      (Math.abs(streak) >= 2 ? '<span class="streak ' + (streak > 0 ? 'win' : 'loss') + '">' + (streak > 0 ? 'W' : 'L') + Math.abs(streak) + '</span>' : '') + '</div></div>' +
      '<div class="move"><div class="prices"><span class="old">' + ctx.hats(oldP, {label: false}) + '</span><span class="arr">&rarr;</span><span class="new ' + (up ? 'up' : 'down') + '">' + ctx.hats(newP, {label: true}) + '</span></div>' +
      '<span class="badge ' + (up ? 'up' : 'down') + '">' + (up ? '+' : '') + chg.toFixed(1) + '%</span></div>' +
      (fs.shares_each ? '<div class="shares">' + esc(fs.shares_each) + ' free share' + (fs.shares_each > 1 ? 's' : '') + ' to ' + esc(fs.viewer_count || 0) + ' viewers worth ' + ctx.hats(newP) + '</div>' : '') +
      (movers ? '<div class="movers">' + movers + '</div>' : '') +
      '<div class="foot"><span>Hatmas Market</span><span>!portfolio &middot; !price &middot; !buy &middot; !sell</span></div></div>';
    el.querySelectorAll('img[data-fb]').forEach((img) => { img.onerror = () => { img.onerror = null; img.src = img.dataset.fb; }; });
  }
});
