// Alert kind: dividend — a god's dividend payout. Migrated from
// overlays/economy_dividend.html (TODO(alertbox): delete once switched).
// data = {god | god_name, rate (0.05 = 5%), holders, total_hats}.
HatmasAlerts.define('dividend', {
  render(el, d, ctx) {
    ctx.style('hm-kind-dividend', `
      .hmk-div { width: 380px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px;
        overflow: hidden; display: flex; align-items: center; gap: 14px; padding: 12px 16px; position: relative; font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); }
      .hmk-div::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent 10%, var(--hm-gold, #DFA06E) 50%, transparent 90%); opacity: 0.6; }
      .hmk-div .img { width: 48px; height: 48px; border-radius: 8px; overflow: hidden; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); flex-shrink: 0; }
      .hmk-div .img img { width: 100%; height: 100%; object-fit: cover; }
      .hmk-div .content { flex: 1; min-width: 0; }
      .hmk-div .tag { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 16px; color: var(--hm-gold, #DFA06E); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 1px; }
      .hmk-div .main { font-size: 14px; line-height: 1.3; }
      .hmk-div .main strong { font-weight: 700; }
      .hmk-div .sub { font-size: 11px; color: var(--hm-text-muted, #8FA6AF); margin-top: 2px; }
      .hmk-div .sub strong { color: var(--hm-text-secondary, #AFC2C9); }
      .hmk-div .payout { flex-shrink: 0; text-align: right; }
      .hmk-div .payout b { display: block; font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 18px; color: var(--hm-gold, #DFA06E); }
      .hmk-div .payout span { font-size: 9px; color: var(--hm-text-muted, #8FA6AF); text-transform: uppercase; letter-spacing: 0.5px; }
    `);
    const esc = ctx.esc;
    const god = d.god_name || d.god || '--';
    const pct = Math.round((Number(d.rate) || 0) * 100);
    const icon = ctx.godIcon(god);
    el.innerHTML = '<div class="hmk-div"><div class="img"><img src="' + icon.custom + '" alt=""></div>' +
      '<div class="content"><div class="tag">Dividend</div>' +
      '<div class="main"><strong>' + esc(god) + '</strong> shareholders paid <strong>' + pct + '%</strong></div>' +
      '<div class="sub"><strong>' + esc(d.holders || 0) + '</strong> holders &middot; ' + ctx.hats(d.total_hats || 0) + ' distributed</div></div>' +
      '<div class="payout"><b>0%</b><span>per share</span></div></div>';
    const img = el.querySelector('img'); img.onerror = () => { img.onerror = null; img.src = icon.fallback; };
    const out = el.querySelector('.payout b');
    if (ctx.preview) { out.textContent = pct + '%'; return null; }
    const t0 = performance.now(); let raf = 0;
    const step = (t) => { const p = Math.min((t - t0) / 600, 1); out.textContent = Math.floor(pct * p) + '%'; if (p < 1) raf = requestAnimationFrame(step); else out.textContent = pct + '%'; };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }
});
