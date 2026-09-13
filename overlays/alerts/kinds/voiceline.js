// Alert kind: voiceline — a redeemed god voiceline with its clip (and the
// animation video when there is one). Migrated from overlays/voicelines.html
// (TODO(alertbox): delete once switched).
// data = {god, type: god_joke|god_taunt|god_laugh, audio_url, video_url?}.
// Audio and video are buffered, then started together so they stay in sync
// (starting early clips the first few hundred ms). Ends 0.5 s after both finish.
HatmasAlerts.define('voiceline', {
  render(el, d, ctx) {
    ctx.style('hm-kind-voiceline', `
      .hmk-vl { text-align: center; font-family: 'Inter', sans-serif; max-width: 100%; }
      .hmk-vl video { max-width: 400px; max-height: 400px; border-radius: 10px; display: none; }
      .hmk-vl .label { margin-top: 12px; padding: 8px 24px; border-radius: 8px; font-size: 20px; font-weight: 600; display: inline-block;
        background: var(--hm-bg, rgba(32,44,57,0.94)); color: var(--hm-text, #fff); box-shadow: 0 4px 20px rgba(0,0,0,0.4); border: 1px solid transparent; }
      .hmk-vl .label.god_joke { border-color: rgba(223,160,110,0.55); color: var(--hm-gold, #DFA06E); }
      .hmk-vl .label.god_taunt { border-color: rgba(223,41,53,0.55); color: var(--hm-red, #DF2935); }
      .hmk-vl .label.god_laugh { border-color: rgba(125,152,161,0.55); color: var(--hm-blue, #7D98A1); }
    `);
    const esc = ctx.esc;
    const TYPES = {god_joke: 'Joke', god_taunt: 'Taunt', god_laugh: 'Laugh'};
    const type = String(d.type || 'god_joke');
    el.innerHTML = '<div class="hmk-vl"><video muted playsinline></video><div class="label ' + esc(type) + '">' +
      esc((d.god || '') + ' ' + (TYPES[type] || d.line || type)) + '</div></div>';
    if (ctx.preview) return null;
    const video = el.querySelector('video');
    const audio = ctx.audio(d.audio_url);
    let audioEnded = !audio, videoEnded = !d.video_url, finished = false;
    const finish = () => { if (finished) return; finished = true; setTimeout(ctx.done, 500); };
    const check = () => { if (audioEnded && videoEnded) finish(); };
    let audioReady = !audio, videoReady = !d.video_url;
    const tryStart = () => {
      if (!audioReady || !videoReady) return;
      if (d.video_url) video.play().catch(() => {});
      if (audio) audio.play().catch(() => { audioEnded = true; check(); });
    };
    if (audio) {
      audio.onended = () => { audioEnded = true; check(); };
      audio.onerror = () => { audioEnded = true; check(); };
      audio.oncanplay = () => { audioReady = true; tryStart(); };
      audio.load();
    }
    if (d.video_url) {
      video.style.display = 'block';
      video.onended = () => { videoEnded = true; check(); };
      video.onerror = () => { videoEnded = true; check(); };
      video.oncanplay = () => { videoReady = true; tryStart(); };
      video.src = d.video_url; video.load();
    }
    if (!audio && !d.video_url) setTimeout(finish, 3000);      // nothing to play: a short card
    check();
    return () => { finished = true; try { if (audio) audio.pause(); video.pause(); } catch (_) {} };
  }
});
