// Alert kind: bingo_open — a Stream Bingo round just opened. data = round summary.
HatmasAlerts.define('bingo_open', {
  render(el, d, ctx) {
    const esc = ctx.esc;
    el.innerHTML =
      '<div class="hm-alert-panel">' +
        '<div class="hm-alert-head"><span>Stream Bingo</span><span class="hm-alert-sub">round ' + esc(d.round_id || '') + '</span></div>' +
        '<div class="hm-alert-body"><small>A round is open</small>Grab a free card at <b>hatmaster.tv/bingo</b></div>' +
        '<div class="hm-alert-row"><span class="k">Pot</span><span class="v">' + esc(d.pot != null ? d.pot + ' Hats' : '') + '</span></div>' +
      '</div>';
  }
});
