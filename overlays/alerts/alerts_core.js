/**
 * HatmasAlerts — the kind registry shared by the alert box (alerts.html)
 * and the layout editor's previews (alerts_layout.html).
 *
 * A kind module (overlays/alerts/kinds/<kind>.js) registers itself:
 *
 *   HatmasAlerts.define('gamble', {
 *     render(el, data, ctx) { ...build markup into el...; return cleanupFn; }
 *   });
 *
 * ctx: { kind, box, duration (s), sound (bool), volume (0..1), width, height,
 *        preview (bool: the editor, no audio, no timers needed),
 *        done()            end the alert early (media finished, animation over)
 *        style(id, css)    add a <style> once per document (scope selectors!)
 *        audio(url)        an <audio> with the kind's volume applied (null when sound is off / preview)
 *        synth()           {ctx, gain}: a WebAudio context + master gain at the kind's volume
 *                          (gain 0 when sound is off / preview) for generated sounds
 *        hats(v, opts)     formatted Hats amount (HTML)
 *        godIcon(name)     {custom, fallback} icon URLs for a god }
 *
 * render() must not throw on partial data: sample data in the editor and
 * real events can differ in shape. Return a cleanup function to stop
 * audio / intervals when the alert ends early.
 */
(function () {
  'use strict';
  const kinds = {};
  let audioCtx = null;

  function getAudioCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) { audioCtx = null; }
    }
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    return audioCtx;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function style(id, css) {
    if (document.getElementById(id)) return;
    const st = document.createElement('style'); st.id = id; st.textContent = css;
    document.head.appendChild(st);
  }

  function makeCtx(opts) {
    const volume = Math.max(0, Math.min(1, opts.volume == null ? 1 : opts.volume));
    const audible = !!opts.sound && !opts.preview && volume > 0;
    const ctx = Object.assign({
      kind: '', box: 'main', duration: 8, sound: false, volume: 1, width: 480, height: 200, preview: false,
      done() {},
    }, opts, {volume, style, esc});
    ctx.audio = function (url) {
      if (!audible || !url) return null;
      const a = new Audio(url); a.volume = volume; a.preload = 'auto';
      return a;
    };
    // Hats amounts: the overlay client's formatter when the page has it
    // (the alert box), a plain number otherwise (the layout editor).
    ctx.hats = function (v, o) {
      if (window.HatmasOverlay && typeof window.HatmasOverlay.hatPrice === 'function') {
        try { return window.HatmasOverlay.hatPrice(v, o); } catch (_) {}
      }
      return Number(v || 0).toLocaleString() + ' Hats';
    };
    ctx.godIcon = function (god) {
      const g = String(god || '');
      return {custom: '/icons/custom/' + encodeURIComponent(g) + '.png',
              fallback: '/icons/gods/' + encodeURIComponent(g.toLowerCase().replace(/\s+/g, '-')) + '.png'};
    };
    ctx.synth = function () {
      const ac = audible ? getAudioCtx() : null;
      if (!ac) return null;
      const gain = ac.createGain(); gain.gain.value = volume; gain.connect(ac.destination);
      return {ctx: ac, gain};
    };
    return ctx;
  }

  function fallback(el, kind, data) {
    el.innerHTML = '<div class="hm-alert-panel"><div class="hm-alert-head"><span>' + esc(kind) + '</span></div>' +
      '<div class="hm-alert-body"><div class="hm-alert-fallback">' + esc(JSON.stringify(data || {}, null, 1).slice(0, 400)) + '</div></div></div>';
  }

  window.HatmasAlerts = {
    define(kind, mod) { kinds[kind] = mod; },
    has(kind) { return !!kinds[kind]; },
    get(kind) { return kinds[kind] || null; },
    /** A sticky kind stays up and takes new alerts of the same kind as updates (the rolling trade feed). */
    isSticky(kind) { const m = kinds[kind]; return !!(m && m.sticky && typeof m.update === 'function'); },
    update(kind, el, data, opts) {
      const mod = kinds[kind];
      if (mod && typeof mod.update === 'function') mod.update(el, data || {}, makeCtx(Object.assign({kind}, opts || {})));
    },
    kinds() { return Object.keys(kinds); },
    esc, style,
    /** Render `kind` into `el`; returns the module's cleanup function (or null). */
    render(kind, el, data, opts) {
      const ctx = makeCtx(Object.assign({kind}, opts || {}));
      const mod = kinds[kind];
      if (!mod || typeof mod.render !== 'function') { fallback(el, kind, data); return null; }
      const cleanup = mod.render(el, data || {}, ctx);
      return typeof cleanup === 'function' ? cleanup : null;
    },
    /** Editor preview: same renderer, no audio, no early end. */
    preview(kind, el, data, opts) {
      return this.render(kind, el, data, Object.assign({preview: true, sound: false, volume: 0, done() {}}, opts || {}));
    },
    /** Browsers gate audio behind a gesture; OBS does not. Try early and on any click. */
    unlockAudio() {
      getAudioCtx();
      document.addEventListener('click', () => getAudioCtx(), {once: false});
    },
  };
})();
