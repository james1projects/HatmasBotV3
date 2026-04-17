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
      this.reconnectTimeouts = [1000, 2000, 4000, 8000]; // exponential backoff
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
     * Connect to WebSocket server
     */
    _connect() {
      const wsUrl = `ws://localhost:8069/ws/overlays?name=${encodeURIComponent(this.overlayName)}`;

      try {
        this.ws = new WebSocket(wsUrl);

        this.ws.addEventListener('open', () => {
          this.isConnected = true;
          this.reconnectAttempt = 0;
          this._log(`Connected: ${this.overlayName}`);
        });

        this.ws.addEventListener('message', (event) => {
          this._handleMessage(event.data);
        });

        this.ws.addEventListener('close', () => {
          this.isConnected = false;
          this._log(`Disconnected: ${this.overlayName}, reconnecting...`);
          this._scheduleReconnect();
        });

        this.ws.addEventListener('error', (error) => {
          this.isConnected = false;
          this._log(`Error: ${this.overlayName} - ${error.message || 'Unknown error'}`);
          this._scheduleReconnect();
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
     * Schedule reconnection with exponential backoff
     */
    _scheduleReconnect() {
      const delay = this.reconnectTimeouts[Math.min(this.reconnectAttempt, this.reconnectTimeouts.length - 1)];
      this.reconnectAttempt++;

      setTimeout(() => {
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
