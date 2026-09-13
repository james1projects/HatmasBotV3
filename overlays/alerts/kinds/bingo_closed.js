// Alert kind: bingo_closed — the round was closed (manual, or after a claim). data = round summary + {reason}.
HatmasAlerts.define('bingo_closed', {
  render(el, d, ctx) {
    const esc = ctx.esc;
    const w = d.winner;
    el.innerHTML =
      '<div class="hm-alert-panel">' +
        '<div class="hm-alert-head"><span>Bingo round over</span><span class="hm-alert-sub">round ' + esc(d.round_id || '') + '</span></div>' +
        '<div class="hm-alert-body">' + (w && w.display ? '<small>Won by</small><b>' + esc(w.display) + '</b>' : '<small>No winner</small>Cards reset next round') + '</div>' +
      '</div>';
  }
});
