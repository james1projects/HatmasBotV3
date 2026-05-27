/**
 * HatmasOverlay - Shared client library for HatmasBot overlays
 * Handles WebSocket connection to overlay manager, message routing, and state management
 */

(function() {
  'use strict';

  // Embedded default styles for all overlays
  const STYLE_TAG = document.createElement('style');
  STYLE_TAG.textContent = `
    [data-hatmas-overlay] {
      opacity: 0;
      transition: opacity 0.3s;
    }
    [data-hatmas-overlay].visible {
      opacity: 1;
    }
  `;
  document.head.appendChild(STYLE_TAG);

  class OverlayManager {
    constructor() {
      this.ws = null;
      this.overlayName = null;
      this.options = {};
      this.isConnected = false;
      this.isCurrentlyVisible = false;
      this.reconnectAttempt = 0;
      this.maxReconnectDelay = 15000; // 15 seconds
      this.reconnectTimeouts = [1000, 2000, 4000, 8000, 15000]; // exponential backoff, capped at 15s
      // Handle for the pending reconnect setTimeout. Used as a
      // single-flight guard so we never schedule two reconnects for the
      // same failed connection (the WebSocket 'error' and 'close' events
      // both fire on a failed connect; without this guard each failure
      // would double the number of in-flight reconnects, eating memory
      // and CPU in OBS's browser-source processes whenever the bot is
      // offline).
      this.reconnectTimer = null;
      this.debugMode = false; // set true for console logging (no localStorage in OBS)
      this.container = null;

      // Ensure we have a DOMContentLoaded handler for container setup
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => this._ensureContainer());
      } else {
        this._ensureContainer();
      }
    }

    /**
     * Main API: Connect overlay to the overlay manager
     * @param {string} name - Unique overlay identifier (e.g., "chat_overlay")
     * @param {object} options - Configuration object
     *   - onShow(data) - callback when overlay should be shown
     *   - onUpdate(data) - callback for live data updates
     *   - onHide() - callback when overlay should be hidden
     *   - showClass - CSS class to add for visibility (default: "visible")
     *   - container - CSS selector or element for the container (default: first child of body)
     */
    connect(name, options = {}) {
      this.overlayName = name;
      this.options = {
        onShow: options.onShow || (() => {}),
        onUpdate: options.onUpdate || null,
        onHide: options.onHide || null,
        showClass: options.showClass || 'visible',
        container: options.container || null
      };

      // Set up container
      if (this.options.container) {
        if (typeof this.options.container === 'string') {
          this.container = document.querySelector(this.options.container);
        } else {
          this.container = this.options.container;
        }
      } else {
        this._ensureContainer();
      }

      // Mark container with data attribute and start hidden
      if (this.container) {
        this.container.setAttribute('data-hatmas-overlay', this.overlayName);
        this.container.style.display = 'none';
      }

      // Initiate WebSocket connection
      this._connect();
    }

    /**
     * Ensure container is set up (use first child of body by default)
     */
    _ensureContainer() {
      if (!this.container && document.body && document.body.firstElementChild) {
        this.container = document.body.firstElementChild;
      }
    }

    /**
     * Connect to WebSocket server.
     *
     * The 'close' event is the single source of truth for reconnect.
     * Per the WebSocket spec, 'close' always fires after a connection
     * lifecycle ends (success or failure) — including after 'error'.
     * Listening to both would schedule two reconnects per failure and
     * compound exponentially while the bot is offline.
     */
    _connect() {
      // Tear down any prior socket cleanly before opening a new one.
      // Without this, a stale socket can still fire 'close' after we've
      // already moved on, scheduling a phantom extra reconnect.
      if (this.ws) {
        try {
          this.ws.onopen = null;
          this.ws.onmessage = null;
          this.ws.onclose = null;
          this.ws.onerror = null;
          if (this.ws.readyState === WebSocket.OPEN ||
              this.ws.readyState === WebSocket.CONNECTING) {
            this.ws.close();
          }
        } catch (_) { /* ignore */ }
        this.ws = null;
      }

      const wsUrl = `ws://localhost:8069/ws/overlays?name=${encodeURIComponent(this.overlayName)}`;

      try {
        const ws = new WebSocket(wsUrl);
        this.ws = ws;

        ws.addEventListener('open', () => {
          // Stale-socket guard: if we've already moved on to a newer
          // connection attempt, ignore this open from a previous one.
          if (this.ws !== ws) return;
          this.isConnected = true;
          this.reconnectAttempt = 0;
          this._log(`Connected: ${this.overlayName}`);
        });

        ws.addEventListener('message', (event) => {
          if (this.ws !== ws) return;
          this._handleMessage(event.data);
        });

        ws.addEventListener('close', () => {
          if (this.ws !== ws) return;
          this.isConnected = false;
          this._log(`Disconnected: ${this.overlayName}, reconnecting...`);
          this._scheduleReconnect();
        });

        // Note: we deliberately do NOT call _scheduleReconnect from the
        // 'error' handler. 'close' will follow and handle it.
        ws.addEventListener('error', () => {
          if (this.ws !== ws) return;
          this.isConnected = false;
          this._log(`Error: ${this.overlayName} (waiting for close to schedule reconnect)`);
        });
      } catch (err) {
        this._log(`Failed to create WebSocket: ${err.message}`);
        this._scheduleReconnect();
      }
    }

    /**
     * Handle incoming WebSocket messages
     */
    _handleMessage(messageData) {
      try {
        const message = JSON.parse(messageData);

        // Filter for messages meant for this overlay
        if (message.overlay !== this.overlayName) {
          return;
        }

        const { action, data } = message;

        switch (action) {
          case 'show':
            this._handleShow(data);
            break;
          case 'hide':
            this._handleHide();
            break;
          case 'update':
            this._handleUpdate(data);
            break;
          default:
            this._log(`Unknown action: ${action}`);
        }
      } catch (err) {
        this._log(`Error parsing message: ${err.message}`);
      }
    }

    /**
     * Handle show action
     */
    _handleShow(data) {
      this.isCurrentlyVisible = true;

      if (this.container) {
        this.container.style.display = '';
        this.container.classList.add(this.options.showClass);
      }

      // Call user's onShow callback (populates overlay with data)
      try {
        this.options.onShow(data || {});
      } catch (err) {
        this._log(`Error in onShow callback: ${err.message}`);
      }

      // Replay CSS animations so entrance effects trigger properly
      // (animations may have fired or been skipped while container was display:none)
      if (this.container) {
        this._replayAnimations();
      }
    }

    /**
     * Force-restart all CSS animations on the container and its descendants.
     * Removing animation, forcing a reflow, then restoring it causes the
     * browser to treat it as a fresh animation start.
     */
    _replayAnimations() {
      const els = [this.container, ...this.container.querySelectorAll('*')];
      for (const el of els) {
        el.style.animation = 'none';
      }
      void this.container.offsetHeight;   // single reflow for the batch
      for (const el of els) {
        el.style.animation = '';
      }
    }

    /**
     * Handle hide action
     */
    _handleHide() {
      this.isCurrentlyVisible = false;

      if (this.container) {
        this.container.classList.remove(this.options.showClass);
        this.container.style.display = 'none';
      }

      // Call user's onHide callback if provided
      if (this.options.onHide) {
        try {
          this.options.onHide();
        } catch (err) {
          this._log(`Error in onHide callback: ${err.message}`);
        }
      }
    }

    /**
     * Handle update action
     */
    _handleUpdate(data) {
      // Only process updates if overlay is currently visible
      if (!this.isCurrentlyVisible) {
        return;
      }

      if (this.options.onUpdate) {
        try {
          this.options.onUpdate(data || {});
        } catch (err) {
          this._log(`Error in onUpdate callback: ${err.message}`);
        }
      }
    }

    /**
     * Schedule reconnection with exponential backoff.
     *
     * Single-flight: if a reconnect is already pending, this is a no-op.
     * That keeps OBS browser sources from spinning up an unbounded number
     * of WebSockets when the bot is offline (the previous behavior would
     * compound failures into a doubling chain of pending reconnects).
     */
    _scheduleReconnect() {
      if (this.reconnectTimer !== null) {
        return;
      }
      const delay = this.reconnectTimeouts[
        Math.min(this.reconnectAttempt, this.reconnectTimeouts.length - 1)
      ];
      this.reconnectAttempt++;

      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this._connect();
      }, delay);
    }

    /**
     * Get current visibility state
     */
    isVisible() {
      return this.isCurrentlyVisible;
    }

    /**
     * Send data back to server
     */
    send(data) {
      if (!this.isConnected || !this.ws) {
        this._log('Cannot send: WebSocket not connected');
        return false;
      }

      try {
        const message = {
          overlay: this.overlayName,
          data: data
        };
        this.ws.send(JSON.stringify(message));
        return true;
      } catch (err) {
        this._log(`Error sending message: ${err.message}`);
        return false;
      }
    }

    /**
     * Internal logging (console only in debug mode)
     */
    _log(message) {
      if (this.debugMode) {
        console.log(`[HatmasOverlay] ${message}`);
      }
    }
  }

  // Create singleton instance and expose global API
  const manager = new OverlayManager();

  window.HatmasOverlay = {
    connect(name, options) {
      return manager.connect(name, options);
    },

    isVisible() {
      return manager.isVisible();
    },

    send(data) {
      return manager.send(data);
    },

    /**
     * Get a CSS variable value from the current theme.
     * Useful for canvas drawing (sparklines, charts) that can't use CSS vars directly.
     */
    getColor(varName) {
      return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    },

    /**
     * Format a number as hat currency with inline hat icon.
     * Returns an HTML string: <img class="hat-icon" ...>118.20 hats
     *
     * @param {number} value - The numeric value to display
     * @param {object} opts - Options
     *   - decimals: decimal places (default 0)
     *   - size: 'sm' | 'md' | 'lg' (default 'md' — matches font size)
     *   - label: whether to append ' hats' (default true)
     *   - compact: shorten large numbers like 1.2k (default false)
     */
    hatPrice(value, opts = {}) {
      const decimals = opts.decimals !== undefined ? opts.decimals : 0;
      const size = opts.size || 'md';
      const label = opts.label !== false;
      const compact = opts.compact || false;

      const sizeClass = size === 'sm' ? 'hat-icon-sm' : size === 'lg' ? 'hat-icon-lg' : 'hat-icon';
      const img = `<img class="${sizeClass}" src="/overlays/hat.png" alt="hats">`;

      let numStr;
      if (compact) {
        if (Math.abs(value) >= 1000000) numStr = (value / 1000000).toFixed(1) + 'M';
        else if (Math.abs(value) >= 10000) numStr = (value / 1000).toFixed(1) + 'k';
        else numStr = value.toFixed(decimals);
      } else {
        numStr = value.toFixed(decimals);
      }

      return `${img}${numStr}${label ? ' hats' : ''}`;
    }
  };
})();
