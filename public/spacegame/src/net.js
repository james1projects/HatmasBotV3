/* =======================================================================
   Net — live spawns from Twitch chat via the bot's overlay WebSocket.

   The bot broadcasts {overlay:"spacegame", action:"spawn", data:{type,user}}
   whenever a viewer types !red/!green/!yellow/!orange. We connect to the
   bot's overlay server (port 8069) regardless of how this page is served,
   and warp the ship in. If the bot is offline the game still works solo.
   ======================================================================= */

(function connectChatBridge() {
  const WS_PORT = 8069;
  const statusEl = document.getElementById('netStatus');
  let ws = null, retry = 0;
  const backoff = [1000, 2000, 3000, 5000, 8000, 12000, 15000];

  function setStatus(text, ok) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.style.color = ok ? '#56e06a' : '#8ea2cf';
  }
  function schedule() {
    const delay = backoff[Math.min(retry, backoff.length - 1)];
    retry++;
    setTimeout(connect, delay);
  }
  function connect() {
    const host = location.hostname || 'localhost';
    let sock;
    try { sock = new WebSocket(`ws://${host}:${WS_PORT}/ws/overlays?name=spacegame`); }
    catch (e) { setStatus('Chat: offline', false); schedule(); return; }
    ws = sock;
    sock.onopen = () => { retry = 0; setStatus('Chat: connected', true); };
    sock.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.overlay !== 'spacegame' || msg.action !== 'spawn' || !msg.data) return;
      const d = msg.data;
      if (window.SG && d.type) window.SG.spawn(d.type, d.user || '');
    };
    sock.onclose = () => { setStatus('Chat: offline', false); schedule(); };
    sock.onerror = () => { try { sock.close(); } catch (e) {} };
  }
  setStatus('Chat: connecting…', false);
  connect();
})();
