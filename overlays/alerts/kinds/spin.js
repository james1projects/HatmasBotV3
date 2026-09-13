// Alert kind: spin — the god pool spin reel. Migrated from
// overlays/god_pool_spin.html (TODO(alertbox): delete once switched).
// data = {candidates: [{god, aspect, votes}], chosen, chosen_aspect, chosen_votes, toast}.
// The reel is built in three segments (fast loops, one reveal pass of every
// non-winner, the winner, a short tail) so every god reads past the marker
// in the slow phase; ticks are scheduled from the inverse of the easing
// bezier so they stay in step with the reel; a two-note chime lands it.
// Sounds go through ctx.synth() so the volume slider applies. Ends ~6 s
// after landing (a toast ends after 5 s).
HatmasAlerts.define('spin', {
  render(el, d, ctx) {
    ctx.style('hm-kind-spin', `
      .hmk-spin { width: 680px; max-width: 100%; box-sizing: border-box; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 14px; padding: 18px 22px 22px; position: relative;
        box-shadow: 0 8px 40px rgba(0,0,0,0.55), 0 0 60px rgba(223,160,110,0.12); font-family: 'Inter', sans-serif; color: #fff; }
      .hmk-spin::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, transparent 5%, var(--hm-gold, #DFA06E) 50%, transparent 95%); border-radius: 14px 14px 0 0; opacity: 0.85; }
      .hmk-spin .title { text-align: center; margin-bottom: 14px; font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 30px; letter-spacing: 4px; -webkit-text-stroke: 2px #000; paint-order: stroke fill; text-shadow: 0 2px 6px rgba(0,0,0,0.85); text-transform: uppercase; }
      .hmk-spin .frame { position: relative; height: 130px; border-radius: 10px; border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); background: rgba(0,0,0,0.35); overflow: hidden; }
      .hmk-spin .frame::before, .hmk-spin .frame::after { content: ''; position: absolute; top: 0; bottom: 0; width: 90px; z-index: 2; pointer-events: none; }
      .hmk-spin .frame::before { left: 0; background: linear-gradient(90deg, rgba(32,44,57,0.95), rgba(32,44,57,0)); }
      .hmk-spin .frame::after { right: 0; background: linear-gradient(270deg, rgba(32,44,57,0.95), rgba(32,44,57,0)); }
      .hmk-spin .marker { position: absolute; top: 0; bottom: 0; left: 50%; width: 2px; transform: translateX(-1px); background: var(--hm-gold, #DFA06E); box-shadow: 0 0 18px var(--hm-gold-glow, rgba(223,160,110,0.18)); z-index: 3; opacity: 0.9; }
      .hmk-spin .marker::before, .hmk-spin .marker::after { content: ''; position: absolute; left: 50%; width: 0; height: 0; transform: translateX(-50%); border-left: 6px solid transparent; border-right: 6px solid transparent; }
      .hmk-spin .marker::before { top: 0; border-top: 8px solid var(--hm-gold, #DFA06E); } .hmk-spin .marker::after { bottom: 0; border-bottom: 8px solid var(--hm-gold, #DFA06E); }
      .hmk-spin .track { display: flex; align-items: center; height: 100%; transform: translateX(0); will-change: transform; }
      .hmk-spin .card { flex: 0 0 auto; width: 110px; height: 110px; margin: 0 6px; border-radius: 8px; background: var(--hm-icon-bg, rgba(223,160,110,0.1)); border: 1px solid var(--hm-border-subtle, rgba(223,160,110,0.08)); overflow: hidden; position: relative; display: flex; flex-direction: column; }
      .hmk-spin .card img.god { width: 100%; height: 78px; object-fit: cover; background: #000; }
      .hmk-spin .card .name { flex: 1; display: flex; align-items: center; justify-content: center; text-align: center; font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 14px; letter-spacing: 1.2px; -webkit-text-stroke: 1px #000; paint-order: stroke fill; text-transform: uppercase; padding: 0 4px; line-height: 1.05; background: rgba(0,0,0,0.45); }
      .hmk-spin .card .votes { position: absolute; top: 4px; right: 4px; font-family: 'JetBrains Mono', monospace; font-size: 9px; color: var(--hm-gold, #DFA06E); background: rgba(0,0,0,0.65); padding: 1px 5px; border-radius: 3px; border: 1px solid var(--hm-gold-glow, rgba(223,160,110,0.18)); }
      .hmk-spin .card .aspect { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; padding: 2px; border-radius: 4px; background: rgba(0,0,0,0.7); border: 1px solid rgba(255,145,40,0.8); object-fit: contain; }
      .hmk-spin .card.winner { transition: box-shadow 0.4s ease, transform 0.4s ease, border-color 0.4s ease; }
      .hmk-spin .track.landed .card.winner { border-color: var(--hm-gold, #DFA06E); box-shadow: 0 0 24px var(--hm-gold-glow, rgba(223,160,110,0.18)), 0 0 48px rgba(223,160,110,0.18); transform: scale(1.06); }
      .hmk-spin .result { margin-top: 16px; text-align: center; opacity: 0; transform: translateY(8px); transition: opacity 0.4s ease 0.1s, transform 0.4s ease 0.1s; }
      .hmk-spin .result.shown { opacity: 1; transform: translateY(0); }
      .hmk-spin .rlabel, .hmk-spin .rmeta { display: block; font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.4em; -webkit-text-stroke: 1px #000; paint-order: stroke fill; text-shadow: 0 1px 3px rgba(0,0,0,0.9); text-transform: uppercase; }
      .hmk-spin .rlabel { margin-bottom: 6px; } .hmk-spin .rmeta { margin-top: 8px; font-size: 12px; letter-spacing: 0.18em; }
      .hmk-spin .rname { font-family: 'BigNoodleTitling', 'Bebas Neue', 'Inter', sans-serif; font-size: 44px; letter-spacing: 5px; -webkit-text-stroke: 3px #000; paint-order: stroke fill; text-transform: uppercase; line-height: 1; text-shadow: 0 2px 0 #000, 0 0 12px rgba(0,0,0,0.95), 0 0 28px rgba(223,160,110,0.55); }
      .hmk-spin .atag { display: none; vertical-align: middle; margin-left: 10px; padding: 3px 8px 3px 5px; border-radius: 5px; background: rgba(0,0,0,0.7); border: 1px solid rgba(255,145,40,0.85); font-family: 'JetBrains Mono', monospace; font-size: 13px; letter-spacing: 0.2em; color: #ff9128; -webkit-text-stroke: 0; text-shadow: 0 1px 3px rgba(0,0,0,0.9); }
      .hmk-spin .atag img { width: 16px; height: 16px; vertical-align: -3px; margin-right: 5px; object-fit: contain; } .hmk-spin .atag.shown { display: inline-block; }
      .hmk-spin .toast { display: none; text-align: center; padding: 6px 6px 4px; font-size: 20px; font-weight: 600; line-height: 1.3; -webkit-text-stroke: 0.6px #000; paint-order: stroke fill; text-shadow: 0 2px 8px rgba(0,0,0,0.9); }
      .hmk-spin .toast .tl { display: block; font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.4em; text-transform: uppercase; color: var(--hm-gold, #DFA06E); -webkit-text-stroke: 0; text-shadow: none; margin-bottom: 10px; }
      .hmk-spin.toast-mode .title, .hmk-spin.toast-mode .frame, .hmk-spin.toast-mode .result { display: none; } .hmk-spin.toast-mode .toast { display: block; }
    `);
    const esc = ctx.esc;
    el.innerHTML = '<div class="hmk-spin"><div class="title">Spinning the wheel</div>' +
      '<div class="frame"><div class="marker"></div><div class="track"></div></div>' +
      '<div class="result"><span class="rlabel">Selected</span><span class="rname"><span class="rtext"></span><span class="atag"><img src="/icons/aspect.png" alt="">ASPECT</span></span><span class="rmeta"></span></div>' +
      '<div class="toast"><span class="tl">Spin</span><span class="tmsg"></span></div></div>';
    const root = el.firstChild, track = root.querySelector('.track'), result = root.querySelector('.result');
    root.querySelector('.atag img').onerror = function () { this.style.display = 'none'; };

    if (d.toast) {
      root.classList.add('toast-mode'); root.querySelector('.tmsg').textContent = d.toast;
      if (!ctx.preview) { const t = setTimeout(ctx.done, 5000); return () => clearTimeout(t); }
      return null;
    }
    const chosen = d.chosen, chosenAspect = !!d.chosen_aspect, votes = d.chosen_votes || 1;
    const same = (c, god, aspect) => c.god === god && !!c.aspect === !!aspect;
    const candidates = (Array.isArray(d.candidates) ? d.candidates.slice() : []).filter((c) => c && c.god);
    if (!chosen) { root.querySelector('.title').textContent = 'No spin data'; return null; }
    if (!candidates.some((c) => same(c, chosen, chosenAspect))) candidates.push({god: chosen, aspect: chosenAspect, votes});

    // ── build the reel ──
    const STRIDE = 122;
    const shuffle = (a) => { a = a.slice(); for (let j = a.length - 1; j > 0; j--) { const k = Math.floor(Math.random() * (j + 1)); [a[j], a[k]] = [a[k], a[j]]; } return a; };
    const N = candidates.length, loops = ctx.preview ? 1 : Math.max(2, Math.ceil(20 / Math.max(1, N)));
    const seq = [];
    for (let i = 0; i < loops; i++) for (const c of shuffle(candidates)) seq.push(c);
    const nonWinners = candidates.filter((c) => !same(c, chosen, chosenAspect));
    for (const c of shuffle(nonWinners)) seq.push(c);
    const chosenObj = candidates.find((c) => same(c, chosen, chosenAspect)) || {god: chosen, aspect: chosenAspect, votes};
    seq.push(chosenObj);
    const tail = Math.min(4, Math.max(2, nonWinners.length));
    for (let i = 0; i < tail; i++) seq.push(nonWinners.length ? nonWinners[Math.floor(Math.random() * nonWinners.length)] : chosenObj);
    const winnerIndex = seq.length - tail - 1;
    track.innerHTML = seq.map((c, i) => { const ic = ctx.godIcon(c.god);
      return '<div class="card' + (i === winnerIndex ? ' winner' : '') + '"><img class="god" src="' + ic.custom + '?v=2" data-fb="' + ic.fallback + '?v=2" alt=""><div class="name">' + esc(c.god) + '</div>' +
        (c.aspect ? '<img class="aspect" src="/icons/aspect.png" alt="Aspect">' : '') + (c.votes > 1 ? '<div class="votes">' + esc(c.votes) + '</div>' : '') + '</div>'; }).join('');
    track.querySelectorAll('img.god').forEach((img) => { img.onerror = () => { if (img.dataset.tried) { img.style.opacity = 0.25; } else { img.dataset.tried = '1'; img.src = img.dataset.fb; } }; });
    track.querySelectorAll('img.aspect').forEach((img) => { img.onerror = () => { img.style.display = 'none'; }; });

    const showResult = () => {
      track.classList.add('landed'); result.classList.add('shown');
      root.querySelector('.rtext').textContent = chosen;
      root.querySelector('.atag').classList.toggle('shown', chosenAspect);
      root.querySelector('.rmeta').textContent = votes + ' vote' + (votes === 1 ? '' : 's');
    };
    const frameWidth = Math.min(636, (ctx.width || 680) - 44);
    const winnerCenter = winnerIndex * STRIDE + STRIDE / 2;
    if (ctx.preview) { track.style.transform = 'translateX(' + (-(winnerCenter - frameWidth / 2)) + 'px)'; showResult(); return null; }

    // ── spin ──
    const DUR = 2200 + Math.max(0, N - 8) * 75;
    const X1 = 0.20, Y1 = 0.55, X2 = 0.25, Y2 = 1.0;
    const bez = (t, p1, p2) => 3 * (1 - t) * (1 - t) * t * p1 + 3 * (1 - t) * t * t * p2 + t * t * t;
    const tForY = (y) => { if (y <= 0) return 0; if (y >= 1) return 1; let lo = 0, hi = 1; for (let i = 0; i < 24; i++) { const m = (lo + hi) / 2; if (bez(m, Y1, Y2) < y) lo = m; else hi = m; } return (lo + hi) / 2; };
    const s = ctx.synth();
    const timers = [];
    const tick = () => { if (!s) return; const now = s.ctx.currentTime, o = s.ctx.createOscillator(), g = s.ctx.createGain(); o.type = 'sine'; o.frequency.setValueAtTime(820 + Math.random() * 360, now); g.gain.setValueAtTime(0.07, now); g.gain.exponentialRampToValueAtTime(0.001, now + 0.05); o.connect(g); g.connect(s.gain); o.start(now); o.stop(now + 0.06); };
    const land = () => { if (!s) return; const now = s.ctx.currentTime; [784, 1175].forEach((f, i) => { const o = s.ctx.createOscillator(), g = s.ctx.createGain(); o.type = 'triangle'; o.frequency.setValueAtTime(f, now + i * 0.14); g.gain.setValueAtTime(0, now + i * 0.14); g.gain.linearRampToValueAtTime(0.22, now + i * 0.14 + 0.02); g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.14 + 0.55); o.connect(g); g.connect(s.gain); o.start(now + i * 0.14); o.stop(now + i * 0.14 + 0.6); }); };
    const jitter = Math.random() * 12 - 6;
    const offset = -(winnerCenter - frameWidth / 2 + jitter);
    const dist = Math.abs(offset);
    track.style.transition = 'none'; track.style.transform = 'translateX(0)';
    void track.offsetWidth;
    requestAnimationFrame(() => {
      track.style.transition = 'transform ' + (DUR / 1000) + 's cubic-bezier(' + X1 + ', ' + Y1 + ', ' + X2 + ', ' + Y2 + ')';
      track.style.transform = 'translateX(' + offset + 'px)';
      const half = STRIDE / 2 - frameWidth / 2;
      for (let i = Math.max(0, Math.ceil(-half / STRIDE)); i <= Math.floor((dist - half) / STRIDE); i++) {
        const dd = i * STRIDE + half; if (dd <= 0 || dd > dist) continue;
        timers.push(setTimeout(tick, DUR * bez(tForY(dd / dist), X1, X2)));
      }
      timers.push(setTimeout(land, DUR));
    });
    timers.push(setTimeout(showResult, DUR + 100));
    timers.push(setTimeout(ctx.done, DUR + 6000));
    return () => timers.forEach(clearTimeout);
  }
});
