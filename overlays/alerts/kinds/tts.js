// Alert kind: tts — a highlighted message read aloud. Migrated from
// overlays/tts.html (TODO(alertbox): delete once switched).
// data = {user, message, audio (url) | audio_url}. The card stays up while
// the clip plays (WebAudio through the kind's gain so the volume slider
// applies; <audio> fallback), then ends 1.5 s after it finishes. With no
// clip (a preview, or TTS disabled) it stays ~0.4 s per word.
HatmasAlerts.define('tts', {
  render(el, d, ctx) {
    ctx.style('hm-kind-tts', `
      .hmk-tts { width: 600px; max-width: 100%; background: var(--hm-bg, rgba(32,44,57,0.94)); border: 1px solid var(--hm-border, rgba(223,160,110,0.18)); border-radius: 10px; padding: 16px 20px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.4); font-family: 'Inter', sans-serif; color: var(--hm-text, #fff); box-sizing: border-box; }
      .hmk-tts.speaking { animation: hmkTtsPulse 1.5s ease-in-out infinite; }
      @keyframes hmkTtsPulse { 0%, 100% { border-color: var(--hm-border, rgba(223,160,110,0.18)); } 50% { border-color: rgba(245,243,187,0.45); } }
      .hmk-tts .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
      .hmk-tts .head svg { width: 20px; height: 20px; fill: var(--hm-gold, #DFA06E); }
      .hmk-tts .user { font-size: 16px; font-weight: 700; color: var(--hm-gold, #DFA06E); }
      .hmk-tts .tag { font-size: 11px; font-weight: 600; color: var(--hm-text-muted, #8FA6AF); text-transform: uppercase; letter-spacing: 0.07em; margin-left: auto; }
      .hmk-tts .msg { font-size: 18px; font-weight: 500; line-height: 1.4; overflow-wrap: anywhere; }
      .hmk-tts .bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 16px; margin-left: 6px; opacity: 0; transition: opacity 0.3s; }
      .hmk-tts.speaking .bars { opacity: 1; }
      .hmk-tts .bars i { display: block; width: 3px; background: #F5F3BB; border-radius: 1px; animation: hmkTtsBar 0.8s ease-in-out infinite; }
      .hmk-tts .bars i:nth-child(1) { height: 6px; } .hmk-tts .bars i:nth-child(2) { height: 12px; animation-delay: .15s; } .hmk-tts .bars i:nth-child(3) { height: 8px; animation-delay: .3s; }
      .hmk-tts .bars i:nth-child(4) { height: 14px; animation-delay: .1s; } .hmk-tts .bars i:nth-child(5) { height: 5px; animation-delay: .25s; }
      @keyframes hmkTtsBar { 0%, 100% { transform: scaleY(0.4); } 50% { transform: scaleY(1); } }
    `);
    const esc = ctx.esc;
    el.innerHTML = '<div class="hmk-tts speaking"><div class="head">' +
      '<svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2H3v2a9 9 0 0 0 8 8.94V23h2v-2.06A9 9 0 0 0 21 12v-2h-2z"/></svg>' +
      '<span class="user">' + esc(d.user || '') + '</span><span class="bars"><i></i><i></i><i></i><i></i><i></i></span><span class="tag">Highlighted message</span></div>' +
      '<div class="msg">' + esc(d.message || '') + '</div></div>';
    const root = el.firstChild;
    const url = d.audio || d.audio_url || '';
    let stop = null, finished = false;
    const finish = () => { if (finished) return; finished = true; root.classList.remove('speaking'); setTimeout(ctx.done, 1500); };

    if (ctx.preview) { root.classList.remove('speaking'); return null; }
    const synth = url ? ctx.synth() : null;
    if (synth) {
      (async () => {
        try {
          const buf = await synth.ctx.decodeAudioData(await (await fetch(url)).arrayBuffer());
          if (finished) return;
          const src = synth.ctx.createBufferSource(); src.buffer = buf; src.connect(synth.gain);
          src.onended = finish; src.start(0);
          stop = () => { try { src.stop(); } catch (_) {} };
        } catch (err) {
          console.warn('[alerts/tts] WebAudio failed, using <audio>', err);
          const a = ctx.audio(url);
          if (!a) return finish();
          a.onended = finish; a.onerror = finish; a.play().catch(finish);
          stop = () => { try { a.pause(); } catch (_) {} };
        }
      })();
    } else {
      // no clip to wait for: hold long enough to read it
      const words = String(d.message || '').split(/\s+/).filter(Boolean).length;
      const t = setTimeout(finish, Math.min(ctx.duration * 1000 - 1500, 2000 + words * 400));
      stop = () => clearTimeout(t);
    }
    return () => { finished = true; if (stop) stop(); };
  }
});
