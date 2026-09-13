// Alert kind: cocaster — the co-caster's lower-third line. Migrated from
// overlays/cocaster.html (TODO(alertbox): delete that file once switched).
// data = {text, event: {kind: "multikill" | "death" | ...}}.
HatmasAlerts.define('cocaster', {
  render(el, d, ctx) {
    ctx.style('hm-kind-cocaster', `
      .hmk-cocaster { display: flex; align-items: stretch; max-width: 100%; font-family: 'Inter', 'Segoe UI', sans-serif; }
      .hmk-cocaster .tag { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 22px; letter-spacing: 0.06em;
        text-transform: uppercase; padding: 12px 14px; background: var(--hm-gold, #DFA06E); color: #202C39; border-radius: 6px 0 0 6px; display: flex; align-items: center; white-space: nowrap; }
      .hmk-cocaster .tag.multikill { background: var(--hm-gold-bright, #E8B583); }
      .hmk-cocaster .tag.death { background: var(--hm-red, #DF2935); color: #fff; }
      .hmk-cocaster .line { font-size: 24px; line-height: 1.3; padding: 12px 18px; background: var(--hm-bg, rgba(32,44,57,0.94)); color: var(--hm-text, #fff);
        border-radius: 0 6px 6px 0; display: flex; align-items: center; }
    `);
    const kind = (d.event && d.event.kind) || '';
    const tag = kind === 'multikill' ? 'Multikill' : (kind === 'death' ? 'Death' : 'Co-caster');
    el.innerHTML = '<div class="hmk-cocaster"><div class="tag ' + ctx.esc(kind) + '">' + tag + '</div><div class="line">' + ctx.esc(d.text || '') + '</div></div>';
  }
});
