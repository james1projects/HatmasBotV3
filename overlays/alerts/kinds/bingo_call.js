// Alert kind: bingo_call — a square was called. data = round summary + {call: {label, event_id, source}, changed_cards: [...]}.
HatmasAlerts.define('bingo_call', {
  render(el, d, ctx) {
    const esc = ctx.esc;
    const call = d.call || {};
    const n = (d.changed_cards || []).length;
    const lead = (d.leaders || [])[0];
    const tail = lead && lead.to_bingo <= 1 ? '<b>' + esc(lead.display) + '</b> needs ' + lead.to_bingo + ' more' : (n ? n + ' card' + (n === 1 ? '' : 's') + ' marked' : 'no cards marked');
    el.innerHTML =
      '<div class="hm-alert-panel">' +
        '<div class="hm-alert-head"><span>Bingo call</span><span class="hm-alert-sub">' + esc(d.pot != null ? d.pot + ' Hats' : '') + '</span></div>' +
        '<div class="hm-alert-body"><small>Called</small>' + esc(call.label || call.event_id || '') + '</div>' +
        '<div class="hm-alert-row"><span class="k">' + tail + '</span><span class="v">' + esc((d.called_ids || []).length || (d.calls || []).length || '') + (d.calls ? ' calls' : '') + '</span></div>' +
      '</div>';
  }
});
