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

  // ── sound samples (assets/sounds/README.md) ──
  // Real recordings instead of synthesised beeps: CC0 packs by Kenney
  // (kenney.nl), served from /assets/sounds/. Every kind plays a ROLE
  // from this table and one of the role's variants is picked at random,
  // so swapping a sound is a one-line edit here. A role that fails to
  // load falls back to the kind's synthesised sound.
  const SOUNDS_BASE = '/assets/sounds/kenney/';
  const SOUNDS = {
    // James's pick on /sounds (2026-09-13): a chip stack per tick, rate 0.9-1.15, gain 0.45 (set in spin.js)
    spin_tick:      ['casino-audio/chips-stack-5'],
    spin_land:      ['interface-sounds/confirmation_002'],
    gamble_shake:   ['casino-audio/dice-shake-1', 'casino-audio/dice-shake-2', 'casino-audio/dice-shake-3'],
    gamble_throw:   ['casino-audio/dice-throw-1', 'casino-audio/dice-throw-2', 'casino-audio/dice-throw-3'],
    gamble_jackpot: ['casino-audio/chips-stack-1', 'casino-audio/chips-stack-2', 'casino-audio/chips-stack-3',
                     'casino-audio/chips-stack-4', 'casino-audio/chips-stack-5', 'casino-audio/chips-stack-6'],
    gamble_fanfare: ['interface-sounds/confirmation_004'],
    gamble_big_win: ['casino-audio/chips-collide-1', 'casino-audio/chips-collide-2', 'casino-audio/chips-collide-3'],
    gamble_win:     ['casino-audio/chip-lay-1', 'casino-audio/chip-lay-2', 'casino-audio/chip-lay-3'],
    gamble_win_tone: ['interface-sounds/select_003'],
    gamble_loss:    ['interface-sounds/error_004'],
    gamble_loss_card: ['casino-audio/card-shove-1', 'casino-audio/card-shove-2'],
    bingo_claim:    ['interface-sounds/confirmation_003'],
    bingo_prize:    ['casino-audio/chips-stack-4', 'casino-audio/chips-stack-5'],
    burn_boom:      ['impact-sounds/impactSoft_heavy_000', 'impact-sounds/impactSoft_heavy_001', 'impact-sounds/impactSoft_heavy_002'],
    burn_crackle:   ['impact-sounds/impactMetal_heavy_000', 'impact-sounds/impactMetal_heavy_003'],
    burn_record:    ['interface-sounds/confirmation_004'],
  };
  const loaded = {};    // name -> AudioBuffer (decoded, ready to play now)
  const loading = {};   // name -> Promise
  function loadSample(name) {
    if (loaded[name]) return Promise.resolve(loaded[name]);
    if (!loading[name]) {
      const ac = getAudioCtx();
      loading[name] = (!ac ? Promise.resolve(null)
        : fetch(SOUNDS_BASE + name + '.ogg')
            .then(r => r.ok ? r.arrayBuffer() : Promise.reject(new Error('HTTP ' + r.status)))
            .then(b => ac.decodeAudioData(b))
            .then(buf => { loaded[name] = buf; return buf; }))
        .catch(e => { console.warn('[alerts] sound missing:', name, e && e.message); return null; });
    }
    return loading[name];
  }
  function preloadRoles(roles) {
    (roles || Object.keys(SOUNDS)).forEach(role => (SOUNDS[role] || []).forEach(loadSample));
  }

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
    /** Play one variant of a SOUNDS role through the kind's volume.
     *  o = {gain (0..1, default 1), rate (playback rate, default 1), at (seconds from now)}.
     *  Returns true when a decoded sample played NOW; false when sound is
     *  off, the sample is not loaded yet (a load is started for next time)
     *  or the role is unknown, so the caller can fall back to synth. */
    ctx.play = function (role, o) {
      o = o || {};
      const names = SOUNDS[role] || [];
      if (!audible || !names.length) return false;
      const ac = getAudioCtx();
      if (!ac) return false;
      const name = names[Math.floor(Math.random() * names.length)];
      const buf = loaded[name];
      if (!buf) { loadSample(name); return false; }
      const src = ac.createBufferSource(); src.buffer = buf;
      src.playbackRate.value = o.rate || 1;
      const g = ac.createGain(); g.gain.value = volume * (o.gain == null ? 1 : o.gain);
      src.connect(g); g.connect(ac.destination);
      src.start(ac.currentTime + Math.max(0, o.at || 0));
      return true;
    };
    ctx.hasSound = function (role) { return (SOUNDS[role] || []).some(n => !!loaded[n]); };
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
    /** The sound table (role -> sample names under /assets/sounds/kenney/). */
    SOUNDS, SOUNDS_BASE,
    /** Decode samples ahead of the first alert; kinds call this at load. No-op without WebAudio. */
    preload(roles) { try { preloadRoles(roles); } catch (_) {} },
    soundsLoaded() { return Object.keys(loaded).length; },
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
