// Alert kind: gamble — a gamble result with the dice roll and its sound.
// Migrated from overlays/sound_alerts.html (TODO(alertbox): delete once
// switched). data = {player, type: jackpot|big_win|win|loss, roll, wager, winnings}.
// Sounds are synthesised (no assets) through ctx.synth() so the kind's
// volume slider applies. Ends itself after roll + reveal (~6.5 s).
HatmasAlerts.define('gamble', {
  render(el, d, ctx) {
    ctx.style('hm-kind-gamble', `
      .hmk-gamble { min-width: 280px; max-width: 100%; border-radius: 12px; padding: 16px 20px; border: 2px solid var(--hm-border-subtle, rgba(223,160,110,0.08));
        background: var(--hm-bg, rgba(32,44,57,0.94)); box-shadow: 0 4px 20px rgba(0,0,0,0.5); font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); }
      .hmk-gamble.jackpot { border-color: rgba(245,243,187,0.65); }
      .hmk-gamble.big_win { border-color: var(--hm-gold-bright, #E8B583); }
      .hmk-gamble.win { border-color: rgba(223,160,110,0.55); }
      .hmk-gamble.loss { border-color: rgba(223,41,53,0.55); }
      .hmk-gamble .label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 6px; min-height: 13px; opacity: 0; transition: opacity 0.3s; }
      .hmk-gamble .label.show { opacity: 1; }
      .hmk-gamble.jackpot .label, .hmk-gamble.jackpot .roll, .hmk-gamble.jackpot .value { color: #F5F3BB; }
      .hmk-gamble.big_win .label, .hmk-gamble.big_win .roll, .hmk-gamble.big_win .value { color: var(--hm-gold-bright, #E8B583); }
      .hmk-gamble.win .label, .hmk-gamble.win .roll, .hmk-gamble.win .value { color: var(--hm-gold, #DFA06E); }
      .hmk-gamble.loss .label, .hmk-gamble.loss .roll, .hmk-gamble.loss .value { color: var(--hm-red, #DF2935); }
      .hmk-gamble .player { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
      .hmk-gamble .details { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
      .hmk-gamble .roll { font-family: 'JetBrains Mono', monospace; font-size: 32px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; min-width: 60px; }
      .hmk-gamble .amount { font-size: 15px; font-weight: 600; color: var(--hm-text-secondary, #AFC2C9); text-align: right; opacity: 0; transition: opacity 0.3s; }
      .hmk-gamble .amount.show { opacity: 1; }
      .hmk-gamble .amount .value { font-family: 'JetBrains Mono', monospace; font-size: 20px; display: block; }
    `);
    const esc = ctx.esc;
    const type = String(d.type || 'win');
    const LABELS = {jackpot: 'JACKPOT', big_win: 'TRIPLE WIN', win: 'WIN', loss: 'LOSS'};
    el.innerHTML = '<div class="hmk-gamble rolling"><div class="label"></div><div class="player"></div>' +
      '<div class="details"><div class="roll"></div><div class="amount"><span class="alabel">won</span><span class="value"></span></div></div></div>';
    const root = el.firstChild, label = root.querySelector('.label'), player = root.querySelector('.player'),
      roll = root.querySelector('.roll'), amount = root.querySelector('.amount');
    const s = ctx.synth();   // null in the editor preview or with sound off

    const reveal = () => {
      root.classList.remove('rolling'); root.classList.add(type);
      roll.textContent = d.roll != null ? d.roll : '';
      player.textContent = d.player || '';
      label.textContent = LABELS[type] || type.toUpperCase(); label.classList.add('show');
      if (type === 'loss') { amount.querySelector('.alabel').textContent = 'lost'; amount.querySelector('.value').textContent = '-' + Number(d.wager || 0).toLocaleString() + ' hats'; }
      else { amount.querySelector('.alabel').textContent = 'won'; amount.querySelector('.value').textContent = '+' + Number(d.winnings || 0).toLocaleString() + ' hats'; }
      amount.classList.add('show');
      playResult(type);
    };

    // the editor preview shows the revealed state straight away
    if (ctx.preview) { reveal(); return null; }

    player.textContent = (d.player || '') + ' is rolling...';
    roll.textContent = Math.floor(Math.random() * 100) + 1;
    const tick = () => {
      if (!s) return;
      const now = s.ctx.currentTime, o = s.ctx.createOscillator(), g = s.ctx.createGain();
      o.type = 'sine'; o.frequency.setValueAtTime(800 + Math.random() * 400, now);
      g.gain.setValueAtTime(0.06, now); g.gain.exponentialRampToValueAtTime(0.001, now + 0.05);
      o.connect(g); g.connect(s.gain); o.start(now); o.stop(now + 0.06);
    };
    const start = Date.now(); let ticks = 0;
    const iv = setInterval(() => {
      ticks++; roll.textContent = Math.floor(Math.random() * 100) + 1;
      if (ticks % 2 === 0) tick();
      if (Date.now() - start >= 1500) { clearInterval(iv); reveal(); }
    }, 60);
    const endTimer = setTimeout(ctx.done, 6500);
    return () => { clearInterval(iv); clearTimeout(endTimer); };

    function tone(freq, at, len, type, peak, wobble) {
      const o = s.ctx.createOscillator(), g = s.ctx.createGain();
      o.type = type; o.frequency.setValueAtTime(freq, at);
      if (wobble) { const l = s.ctx.createOscillator(), lg = s.ctx.createGain(); l.frequency.value = 6; lg.gain.value = 8; l.connect(lg); lg.connect(o.frequency); l.start(at); l.stop(at + len); }
      g.gain.setValueAtTime(0, at); g.gain.linearRampToValueAtTime(peak, at + 0.02); g.gain.exponentialRampToValueAtTime(0.001, at + len);
      o.connect(g); g.connect(s.gain); o.start(at); o.stop(at + len + 0.05);
    }
    function playResult(t) {
      if (!s) return;
      const now = s.ctx.currentTime;
      if (t === 'jackpot') {
        const notes = [523, 659, 784, 1047, 1319, 1568];
        notes.forEach((f, i) => tone(f, now + i * 0.1, 0.6, 'sine', 0.3));
        const st = now + notes.length * 0.1 + 0.1;
        [1047, 1319, 1568, 2093].forEach((f) => tone(f, st, 1.5, 'triangle', 0.2, true));
        for (let i = 0; i < 20; i++) tone(2000 + Math.random() * 3000, st + 0.2 + Math.random(), 0.15, 'sine', 0.08 + Math.random() * 0.08);
      } else if (t === 'big_win') {
        [523, 659, 784, 1047].forEach((f, i) => tone(f, now + i * 0.12, 0.4, 'square', 0.15));
      } else if (t === 'win') {
        [784, 1047].forEach((f, i) => tone(f, now + i * 0.15, 0.4, 'sine', 0.2));
      } else {
        [440, 370, 330].forEach((f, i) => tone(f, now + i * 0.2, 0.5, 'sine', 0.15));
      }
    }
  }
});
