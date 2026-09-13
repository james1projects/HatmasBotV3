// Alert kind: burn — a viewer destroyed their own Hats (plugins/burn.py,
// event hats_burned). The number is the whole point, so it counts up big;
// a record (of the stream, or all-time) gets a ribbon and a bigger boom.
// data = {display_name, amount, balance_after, stream_total, stream_record: {display, amount} | null,
//         is_stream_record, alltime_record: {display, amount} | null, is_alltime_record,
//         user_total, user_burns, user_rank, burners}.
HatmasAlerts.define('burn', {
  render(el, d, ctx) {
    ctx.style('hm-kind-burn', `
      .hmk-burn { min-width: 420px; max-width: 100%; box-sizing: border-box; border-radius: 14px; padding: 18px 26px 16px;
        background: var(--hm-bg, rgba(32,44,57,0.94)); border: 2px solid rgba(223,41,53,0.55);
        box-shadow: 0 0 0 1px rgba(223,41,53,0.15), 0 6px 30px rgba(0,0,0,0.55), 0 0 40px rgba(223,41,53,0.18);
        font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); text-align: center; position: relative; overflow: hidden;
        animation: hmkBurnIn 0.35s cubic-bezier(0.16, 1, 0.3, 1); }
      .hmk-burn.record { border-color: var(--hm-gold-bright, #E8B583); box-shadow: 0 0 0 1px rgba(232,181,131,0.25), 0 6px 30px rgba(0,0,0,0.55), 0 0 48px rgba(232,181,131,0.22); }
      @keyframes hmkBurnIn { from { transform: scale(0.85); opacity: 0; } to { transform: scale(1); opacity: 1; } }
      .hmk-burn .head { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 22px; letter-spacing: 4px; text-transform: uppercase; color: var(--hm-red, #DF2935); }
      .hmk-burn.record .head { color: var(--hm-gold-bright, #E8B583); }
      .hmk-burn .who { font-size: 20px; font-weight: 600; margin-top: 4px; }
      .hmk-burn .amount { font-family: 'JetBrains Mono', monospace; font-size: 56px; font-weight: 700; line-height: 1.05; margin: 6px 0 2px; font-variant-numeric: tabular-nums; color: var(--hm-red, #DF2935); text-shadow: 0 0 18px rgba(223,41,53,0.45); }
      .hmk-burn.record .amount { color: var(--hm-gold-bright, #E8B583); text-shadow: 0 0 18px rgba(232,181,131,0.5); }
      .hmk-burn .unit { font-size: 13px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--hm-text-secondary, #AFC2C9); }
      .hmk-burn .ribbon { display: inline-block; margin-top: 10px; padding: 3px 12px; border-radius: 4px; font-size: 12px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
        background: var(--hm-gold-bright, #E8B583); color: var(--hm-bg, #202C39); }
      .hmk-burn .ribbon.stream { background: var(--hm-red, #DF2935); color: #fff; }
      .hmk-burn .foot { margin-top: 10px; font-size: 11px; color: var(--hm-text-muted, #8FA6AF); display: flex; justify-content: center; gap: 18px; }
      .hmk-burn .foot b { color: var(--hm-text-secondary, #AFC2C9); font-family: 'JetBrains Mono', monospace; }
      .hmk-burn .foot .rk { color: var(--hm-gold, #DFA06E); font-weight: 700; }
      /* a short slot (a shared lane of ~180px): tighter, no footer */
      .hmk-burn.compact { padding: 10px 20px 10px; min-width: 300px; }
      .hmk-burn.compact .head { font-size: 17px; letter-spacing: 3px; }
      .hmk-burn.compact .who { font-size: 16px; margin-top: 0; }
      .hmk-burn.compact .amount { font-size: 38px; margin: 2px 0 0; }
      .hmk-burn.compact .unit { font-size: 10px; }
      .hmk-burn.compact .ribbon { margin-top: 5px; padding: 2px 9px; font-size: 10px; }
      .hmk-burn.compact .foot { display: none; }
      .hmk-burn .ember { position: absolute; bottom: -6px; width: 5px; height: 5px; border-radius: 50%; background: var(--hm-gold, #DFA06E); opacity: 0; animation: hmkEmber 1.6s ease-out forwards; pointer-events: none; }
      @keyframes hmkEmber { 10% { opacity: 0.9; } 100% { transform: translateY(-120px) scale(0.3); opacity: 0; } }
    `);
    const esc = ctx.esc;
    const amount = Number(d.amount || 0);
    const record = !!d.is_alltime_record, streamRec = !!d.is_stream_record && !record;
    const ribbon = record ? '<div class="ribbon">New all-time record</div>'
                 : streamRec ? '<div class="ribbon stream">Biggest burn of the stream</div>' : '';
    const sr = d.stream_record, ar = d.alltime_record;
    const foot = [];
    if (sr && !streamRec && !record) foot.push('Tonight’s biggest <b>' + esc(sr.display) + ' ' + Number(sr.amount).toLocaleString() + '</b>');
    if (ar && !record) foot.push('All-time <b>' + esc(ar.display) + ' ' + Number(ar.amount).toLocaleString() + '</b>');
    if (d.user_total && d.user_burns > 1) foot.push(esc(d.display_name || 'They') + ' total <b>' + Number(d.user_total).toLocaleString() + '</b>' + (d.user_rank ? ' <span class="rk">#' + esc(d.user_rank) + '</span>' : ''));
    if (d.stream_total) foot.push('Burned tonight <b>' + Number(d.stream_total).toLocaleString() + '</b>');
    el.innerHTML =
      '<div class="hmk-burn' + (record ? ' record' : '') + ((ctx.height || 300) < 260 ? ' compact' : '') + '">' +
        '<div class="head">' + (record ? 'Record burn' : 'Burned') + '</div>' +
        '<div class="who">' + esc(d.display_name || 'someone') + '</div>' +
        '<div class="amount">0</div><div class="unit">Hats, gone forever</div>' +
        ribbon +
        (foot.length ? '<div class="foot">' + foot.map(f => '<span>' + f + '</span>').join('') + '</div>' : '') +
      '</div>';
    const root = el.firstChild, num = root.querySelector('.amount');

    if (ctx.preview) { num.textContent = amount.toLocaleString(); return null; }

    // count up over ~1.2 s, then embers
    const start = performance.now(), dur = 1200;
    let raf = 0;
    const step = (t) => {
      const p = Math.min(1, (t - start) / dur), e = 1 - Math.pow(1 - p, 3);
      num.textContent = Math.round(amount * e).toLocaleString();
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    const embers = setInterval(() => {
      const s = document.createElement('span'); s.className = 'ember';
      s.style.left = (10 + Math.random() * 80) + '%'; s.style.animationDelay = (Math.random() * 0.2) + 's';
      root.appendChild(s); setTimeout(() => s.remove(), 1800);
    }, 140);

    // sound: a low boom, then a rising shimmer for a record
    const s = ctx.synth();
    if (s) {
      const now = s.ctx.currentTime;
      const o = s.ctx.createOscillator(), g = s.ctx.createGain();
      o.type = 'sine'; o.frequency.setValueAtTime(110, now); o.frequency.exponentialRampToValueAtTime(38, now + 0.9);
      g.gain.setValueAtTime(0.0001, now); g.gain.exponentialRampToValueAtTime(0.7, now + 0.03); g.gain.exponentialRampToValueAtTime(0.001, now + 1.1);
      o.connect(g); g.connect(s.gain); o.start(now); o.stop(now + 1.2);
      const buf = s.ctx.createBuffer(1, s.ctx.sampleRate * 0.5, s.ctx.sampleRate), ch = buf.getChannelData(0);
      for (let i = 0; i < ch.length; i++) ch[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / ch.length, 2);
      const n = s.ctx.createBufferSource(), ng = s.ctx.createGain(); n.buffer = buf;
      ng.gain.setValueAtTime(0.25, now); ng.gain.exponentialRampToValueAtTime(0.001, now + 0.5);
      n.connect(ng); ng.connect(s.gain); n.start(now);
      if (record || streamRec) {
        [659, 784, 988, 1319].forEach((f, i) => {
          const o2 = s.ctx.createOscillator(), g2 = s.ctx.createGain();
          o2.type = 'triangle'; o2.frequency.setValueAtTime(f, now + 0.9 + i * 0.1);
          g2.gain.setValueAtTime(0, now + 0.9 + i * 0.1); g2.gain.linearRampToValueAtTime(0.3, now + 0.92 + i * 0.1);
          g2.gain.exponentialRampToValueAtTime(0.001, now + 1.7 + i * 0.1);
          o2.connect(g2); g2.connect(s.gain); o2.start(now + 0.9 + i * 0.1); o2.stop(now + 1.8 + i * 0.1);
        });
      }
    }
    return () => { cancelAnimationFrame(raf); clearInterval(embers); };
  }
});
