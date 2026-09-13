// Alert kind: bingo_claim — someone pressed Bingo! and the server accepted it
// (also fires on the legacy bingo_win event). data = round summary + {winner: {display, prize, paid}}.
HatmasAlerts.define('bingo_claim', {
  render(el, d, ctx) {
    const esc = ctx.esc;
    const w = d.winner || {};
    const prize = w.prize != null ? w.prize : d.pot;
    el.innerHTML =
      '<div class="hm-alert-panel is-win is-big">' +
        '<div class="hm-alert-head"><span>BINGO!</span><span class="hm-alert-sub">' + esc(prize != null ? prize + ' Hats' : '') + '</span></div>' +
        '<div class="hm-alert-body"><small>Winner</small><b>' + esc(w.display || w.login || 'someone') + '</b>' +
          (w.paid === false ? '<br><small>Hats owed (balance service down)</small>' : '') + '</div>' +
        '<div class="hm-alert-row"><span class="k">Cards in play</span><span class="v">' + esc(d.cards != null ? d.cards : '') + '</span></div>' +
      '</div>';
    // a short rising chime, generated: no asset to ship
    const s = ctx.synth();
    if (s) {
      const now = s.ctx.currentTime;
      [523, 659, 784, 1047, 1319].forEach((f, i) => {
        const o = s.ctx.createOscillator(); const g = s.ctx.createGain();
        o.type = 'triangle'; o.frequency.setValueAtTime(f, now + i * 0.11);
        g.gain.setValueAtTime(0, now + i * 0.11);
        g.gain.linearRampToValueAtTime(0.35, now + i * 0.11 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.11 + 0.7);
        o.connect(g); g.connect(s.gain); o.start(now + i * 0.11); o.stop(now + i * 0.11 + 0.75);
      });
    }
  }
});
