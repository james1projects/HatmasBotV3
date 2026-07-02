"""
Kill/Death Detector Plugin
===========================
Detects kills and deaths in Smite 2 by analyzing OBS screenshots.

Uses OBS WebSocket's GetSourceScreenshot to grab frames from the game
capture, then reads the K/D/A numbers from the HUD bar.  When the kill
count goes up → kill event (with multi-kill classification based on
timing).  When the death count goes up → death event.  When the assist
count goes up → assist event.

The KDA bar is always visible when alive or dead; hidden only when the
store or scoreboard overlay is open (detected via dark pixel ratio).

Architecture note (April 2026):
    The pure frame-analysis code (regions, binarization, digit matching,
    Tesseract fallback) now lives in ``core/kda_reader.py`` so the same
    engine powers both the live detector and the standalone VOD tool
    (``tools/extract_events.py``).  This plugin owns the *stateful*
    pieces — the async loop, match stats, god-identification gate,
    startup validation, sanity checks, multi-kill timing, state
    persistence, callbacks, and chat announcements.

Requires:
  - OBS WebSocket (obsws-python) — already configured for the bot
  - Tesseract OCR — optional, used as a fallback when the digit template
    library has a holdout.  pip install pytesseract + the Tesseract
    binary (see TESSERACT_PATH in config).
  - Pillow + OpenCV — pip install Pillow opencv-python
"""

import asyncio
import base64
import io
import logging
import time
from collections import deque

import numpy as np
from PIL import Image

from core.config import (
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
    DATA_DIR, TESSERACT_PATH,
)
from core.kda_reader import KdaReader


# --- File logger ---
# Writes all KillDetector output to data/killdetect.log for debugging.
# The log file is overwritten each session to avoid unbounded growth.
# Guard against duplicate handlers if module is imported more than once.
_log_path = DATA_DIR / "killdetect.log"
_kd_logger = logging.getLogger("KillDetector")
_kd_logger.setLevel(logging.DEBUG)
_kd_logger.propagate = False  # Don't duplicate to root logger
_file_handler = None

if not _kd_logger.handlers:
    _file_handler = logging.FileHandler(str(_log_path), mode="w", encoding="utf-8")
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
    )
    _kd_logger.addHandler(_file_handler)
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter("%(message)s"))
    _kd_logger.addHandler(_console_handler)
else:
    for _h in _kd_logger.handlers:
        if isinstance(_h, logging.FileHandler):
            _file_handler = _h
            break


def _log(msg: str):
    """Log a KillDetector message to both console and file."""
    _kd_logger.info(msg)
    if _file_handler is not None:
        _file_handler.flush()


# --- Loop / state config (live-specific — not shared with VOD tool) ---

# Screenshot interval — live OBS polling rate.
SCREENSHOT_INTERVAL = 0.8  # Seconds between screenshot grabs

# Non-gameplay screen detection — how many consecutive non-gameplay frames
# trigger a match-end reset.
NON_GAMEPLAY_FRAMES = 3

# Multi-kill timing: kills within this window count as multi-kills.
# Smite 2 multi-kill windows: ~10s for double, extends per kill.
MULTIKILL_WINDOW = 10.0  # Seconds

# Max plausible KDA jump per frame.  At ~0.8s intervals, even a penta kill
# won't produce more than 5 new kills in a single read.  Anything larger is
# almost certainly an OCR misread (e.g. "19" → "49").  If OCR fails for
# many frames, the real KDA can advance past this limit — scale the max
# jump based on seconds since last successful read.
MAX_KDA_JUMP_BASE = 5
MAX_KDA_JUMP_PER_SEC = 1.0 / 3.0

# State persistence — survive bot restarts mid-match.
STATE_STALE_SECONDS = 30 * 60  # 30 minutes
STATE_FILE = DATA_DIR / "kda_state.json"

# Startup validation — require multiple consistent reads before accepting
# the first KDA to avoid a single misread poisoning the baseline.
STARTUP_REQUIRED_READS = 3

# How many consecutive (0,0,0) KDA reads while prev_kda is non-zero
# before we treat it as "new match started" and reset. ~4 seconds at
# the default 0.8s/frame cadence. Without this, a stale prev_kda from
# the previous match (restored via state.json or carried across an
# undetected match boundary) makes the decrease-sanity check reject
# every read in the new match forever.
ZERO_READ_RESET_THRESHOLD = 5

# Loud-failure observability. When the god is identified and we're in
# gameplay but KDA reads keep failing, something is broken (region
# drift, binarization regression, missing dependency). Every
# READ_FAILURE_ALERT_EVERY consecutive failures (~20s at 0.8s/frame)
# we log a non-debug warning with the reader's failure_reason, and
# save a full-frame snapshot (rate-limited) for postmortem / fixture
# creation. Added June 2026 after a whole session failed silently
# because pytesseract was missing (ImportError swallowed by read_kda).
READ_FAILURE_ALERT_EVERY = 25
READ_FAILURE_SNAPSHOT_MIN_INTERVAL = 300.0  # seconds between snapshots


class KillDeathDetector:
    """
    Analyzes OBS game screenshots for kill/death events via KDA number tracking.

    Lifecycle:
      1. Created and registered as a plugin.
      2. Connects to OBS WebSocket on_ready.  Loads the god matcher.
         (The KdaReader — which owns the digit matcher — is constructed
         in ``__init__`` so all its state is ready immediately.)
      3. ``start_detection()`` launches the async loop.
      4. Each iteration: grab screenshot, classify the frame (gameplay?
         overlay open? god identified?), read KDA, run sanity checks,
         fire kill / death / assist callbacks.
    """

    def __init__(self, debug=False):
        self.bot = None
        self.obs_client = None
        self._running = False
        self._task = None
        self._debug = debug
        # Operator pause from /detector. When True the scan loop
        # short-circuits — no new screenshot, no per-frame processing,
        # _last_screenshot + debug_state stay frozen on whatever the
        # bot last saw. Used by the "save portrait as reference"
        # workflow so the dashboard frame and the frame written to
        # disk are guaranteed to be the same one.
        self._paused = False
        self._paused_at: float | None = None

        # Debug output directory — holds saved frames when debug is on.
        # Shared with the reader so both can use it.
        self._debug_dir = DATA_DIR / "killdetect_debug"
        if self._debug:
            self._debug_dir.mkdir(exist_ok=True)

        # The shared frame analyzer.  This handles OCR, template matching,
        # and gameplay/overlay heuristics.  It is stateless across frames
        # beyond the per-read digit-crop stash used for auto-enrollment.
        self._reader = KdaReader(
            data_dir=DATA_DIR,
            tesseract_path=TESSERACT_PATH,
            debug=self._debug,
            debug_dir=self._debug_dir,
            logger=_kd_logger,
        )

        # KDA tracking — the sole detection method.
        self._prev_kda = None          # (kills, deaths, assists) from last successful read
        self._kda_read_failures = 0    # consecutive failed reads
        self._last_kda_read_time = 0   # timestamp of last successful read
        self._recent_kill_times = []   # timestamps for multi-kill detection
        self._is_dead = False
        self._manual_mode = False
        self._announce_chat = False    # Announce K/D/A changes in Twitch chat
        self._non_gameplay_count = 0   # Consecutive non-gameplay frames seen

        # Auto-reset signals so stale prev_kda from a previous match
        # cannot poison new-match reads forever.
        # _last_match_god: most-recently confirmed god identity. When
        # a different god is later confirmed, we treat that as a new
        # match and reset stats inline. _zero_read_count: consecutive
        # (0,0,0) reads while prev_kda is non-zero - once it crosses
        # ZERO_READ_RESET_THRESHOLD we accept (0,0,0) as the new
        # baseline instead of rejecting it as "KDA decreased."
        self._last_match_god = None
        self._zero_read_count = 0

        # Stats for current match
        self.match_kills = 0
        self.match_deaths = 0
        self.match_assists = 0
        self.match_kill_types = {}

        # Event listeners — registered by the bot/webserver/plugins during
        # integration. Use the add_*_listener() methods rather than
        # assigning these lists directly. Multiple listeners are supported
        # per event; they fire in registration order. Each call signature:
        #
        #   kill         async fn(kill_type: str, count: int)         (fired via create_task)
        #   multikill    async fn(kill_type: str)                     (fired via create_task)
        #   death        async fn(count: int)                         (fired via create_task)
        #   assist       async fn(count: int)                         (fired via create_task)
        #   god_id       async fn(god_name: str)                      (awaited inline)
        #   gameplay_end async fn()                                   (awaited inline)
        self._kill_listeners = []
        self._multikill_listeners = []
        self._death_listeners = []
        self._assist_listeners = []
        self._god_identified_listeners = []
        self._gameplay_ended_listeners = []

        # God portrait matcher — identifies the god from the in-game
        # portrait before tracker.gg API responds (which has a 2-5 min
        # delay).  Lives on the detector (not the reader) because it
        # is driven by the live match lifecycle.
        self._god_matcher = None
        self._god_identified = False

        # God identification validation — require multiple consecutive
        # frames matching the SAME god before accepting, to prevent lobby
        # false positives.
        self._god_confirm_name = None
        self._god_confirm_count = 0
        self._GOD_CONFIRM_REQUIRED = 3

        # Startup validation — multiple consistent reads before accepting.
        self._startup_reads = []
        self._startup_validated = False

        # ── Debug observability ──────────────────────────────────────────
        # Populated at the end of every detection-loop tick. Consumed by
        # the /detector debug page via the dashboard webserver. Stays
        # in-memory (no disk, no persistence between bot restarts). The
        # debug snapshot is a single dict that the webserver atomically
        # reads; it never tries to mutate while we're writing because
        # asyncio gives us a single task scheduler.
        self._debug_state = {
            "frame": 0,
            "timestamp": 0.0,
            "last_scan_ms": 0.0,
        }
        self._last_screenshot: Image.Image | None = None  # most-recent grab
        self._last_screenshot_ts: float = 0.0
        # Rolling 50-event log feeding the page's "recent events" panel.
        # Newest events at the right end; oldest fall off automatically.
        self._debug_events: deque = deque(maxlen=50)

        # Log reader readiness so the bot console reflects OCR status.
        if self._reader.has_digit_templates:
            matcher = self._reader.digit_matcher
            coverage = sorted(matcher.digit_coverage)
            _log(
                f"[KillDetector] Digit matcher ready "
                f"({matcher.template_count} templates, "
                f"digits: {','.join(coverage)})"
            )
        else:
            _log(
                "[KillDetector] Digit matcher has no templates — "
                "will auto-collect from confirmed OCR reads"
            )
        if self._reader.ocr_available:
            _log("[KillDetector] Tesseract OCR available")
        else:
            _log(
                "[KillDetector] Tesseract OCR not available — "
                "running on digit templates only"
            )

    # --- Back-compat accessors -------------------------------------------
    # Other plugins / external code referenced these on older builds.  Keep
    # them as thin pass-throughs to the reader so nothing breaks.

    @property
    def _ocr_available(self) -> bool:
        return self._reader.ocr_available

    @property
    def _digit_matcher(self):
        return self._reader.digit_matcher

    # --- Listener registration ------------------------------------------
    # Plugins / the main wiring register interest in detection events here.
    # Registration is additive: any number of listeners can subscribe to
    # the same event and they all fire when it occurs. Listeners are
    # async callables; signatures are documented above the listener-list
    # fields in __init__.

    def add_kill_listener(self, fn):
        """Register an async callback for kill events. fn(kill_type, count)."""
        self._kill_listeners.append(fn)

    def add_multikill_listener(self, fn):
        """Register an async callback for multikill events. fn(kill_type).

        Fires *in addition* to the kill listener — a triple kill produces
        one kill event AND one multikill event, in that order, on the
        same frame.
        """
        self._multikill_listeners.append(fn)

    def add_death_listener(self, fn):
        """Register an async callback for death events. fn(count)."""
        self._death_listeners.append(fn)

    def add_assist_listener(self, fn):
        """Register an async callback for assist events. fn(count)."""
        self._assist_listeners.append(fn)

    def add_god_identified_listener(self, fn):
        """Register an async callback for portrait-identified-the-god events.
        fn(god_name). Awaited inline — keep it short or schedule your own
        background work."""
        self._god_identified_listeners.append(fn)

    def add_gameplay_ended_listener(self, fn):
        """Register an async callback fired when N consecutive non-gameplay
        frames are seen (i.e., the game ended / minimized). fn() — no args.
        Awaited inline before reset_match_stats() so listeners can read
        the final stats. Keep it short."""
        self._gameplay_ended_listeners.append(fn)

    # --- Listener dispatch ----------------------------------------------
    # Two flavors. _fire_listeners uses asyncio.create_task so the loop
    # doesn't block on slow callbacks (kill / death / assist / multikill).
    # _await_listeners awaits sequentially — used where the kill detector
    # needs to know the listeners are done before it changes state
    # (god_identified, gameplay_ended).

    def _fire_listeners(self, listeners, *args):
        """Schedule each listener as a fire-and-forget task. Errors are
        logged but never propagate back into the detection loop."""
        for fn in listeners:
            try:
                asyncio.create_task(self._safe_call(fn, *args))
            except RuntimeError:
                # No running event loop (shouldn't happen — the loop is
                # what's calling us — but defensive).
                pass

    async def _await_listeners(self, listeners, *args):
        """Await each listener in registration order. Errors are logged
        and swallowed so one bad listener doesn't break the others."""
        for fn in listeners:
            await self._safe_call(fn, *args)

    # --- Debug observability --------------------------------------------
    # Methods used by the /detector debug page. They never mutate
    # detection state; they only collect data the loop has already
    # computed and a small amount of extra observability info.

    @property
    def debug_state(self) -> dict:
        """Return the most recent debug snapshot. Single-task scheduler
        means no need to lock — the caller (webserver) and the writer
        (detection loop) run on the same event loop."""
        return self._debug_state

    @property
    def debug_events(self) -> list:
        """Return the rolling event log as a list (newest last)."""
        return list(self._debug_events)

    def _log_debug_event(self, level: str, msg: str) -> None:
        """Push an event to the rolling debug log.

        level is one of: 'info', 'accept', 'reject', 'state'.
        UI colours them differently. msg should be short — one line.
        """
        self._debug_events.append({
            "ts": time.time(),
            "level": level,
            "msg": msg,
        })

    def _build_debug_state(
        self,
        img: Image.Image,
        img_array: np.ndarray,
        frame_count: int,
        scan_started_at: float,
    ) -> None:
        """Recompute self._debug_state from the current frame.

        Cost: one scene_classification pass (cheap — numpy mean/std on a
        few small ROIs), optionally one identify_top_n call (cheap),
        optionally one read_kda_with_details call (the expensive part —
        roughly the same as read_kda(), so a duplicate of the per-frame
        KDA cost).

        Called at the very end of each scan iteration, after the live
        path has done its work. Never mutates anything the live path
        cares about.
        """
        self._last_screenshot = img
        self._last_screenshot_ts = time.time()

        # --- Scene classification with all the numbers behind it ---
        try:
            scene = self._reader.scene_classification_with_details(img_array)
        except Exception as e:
            scene = {"error": f"{type(e).__name__}: {e}"}

        # --- God identification panel ---
        god_panel = {
            "identified": self._god_identified,
            "current": self._god_confirm_name if self._god_identified else None,
            "confirm_name": self._god_confirm_name,
            "confirm_count": self._god_confirm_count,
            "confirm_required": self._GOD_CONFIRM_REQUIRED,
            "top_n": [],
            "threshold": None,
            "verdict": "not_run",
        }
        if self._god_matcher is not None and self._god_matcher.is_loaded:
            try:
                # Pull top 5 candidates regardless of identification state
                # so the page always shows what the matcher would pick.
                # Use the with-sources variant when available so the page
                # can render "(overlay)" / "(base)" tags; fall back to
                # the plain variant if a stale matcher build doesn't
                # have the method yet.
                if hasattr(self._god_matcher,
                           "identify_top_n_with_sources"):
                    top5 = self._god_matcher.identify_top_n_with_sources(
                        img, n=5)
                    god_panel["top_n"] = [
                        (name, round(float(score), 4), src)
                        for name, score, src in top5
                    ]
                else:
                    top5 = self._god_matcher.identify_top_n(img, n=5)
                    god_panel["top_n"] = [
                        (name, round(float(score), 4), "base")
                        for name, score in top5
                    ]
                # Threshold info — pulled from god_matcher module constants
                # via the matcher instance if it exposes them, otherwise
                # left None (UI can hide the row).
                try:
                    from core import god_matcher as _gm
                    god_panel["threshold"] = float(_gm.MIN_CONFIDENCE)
                    god_panel["margin_threshold"] = float(_gm.MIN_CONFIDENCE_MARGIN)
                    god_panel["margin_gap"] = float(_gm.MARGIN_GAP)
                except Exception:
                    pass
                if top5:
                    # top5[0] may be a 2-tuple (name, score) from the old
                    # identify_top_n or a 3-tuple (name, score, source)
                    # from identify_top_n_with_sources. Handle either.
                    best_row = top5[0]
                    score = best_row[1]
                    if score >= (god_panel.get("threshold") or 0.80):
                        god_panel["verdict"] = "above_threshold"
                    else:
                        god_panel["verdict"] = "below_threshold"
            except Exception as e:
                god_panel["error"] = f"{type(e).__name__}: {e}"

        # --- KDA pipeline details ---
        # Only run the heavy with_details pipeline when we'd actually
        # attempt a live read (god identified, in gameplay, no overlay).
        # Cheap to skip otherwise; respects the gating the live path
        # uses so the debug page reflects the same logic.
        kda_panel = {
            "ran": False,
            "gated_by": None,        # 'god_not_identified' | 'overlay_open' | 'not_gameplay'
            "details": None,
        }
        if not self._god_identified:
            kda_panel["gated_by"] = "god_not_identified"
        elif not scene.get("is_gameplay", True):
            kda_panel["gated_by"] = "not_gameplay"
        elif scene.get("overlay_open", False):
            kda_panel["gated_by"] = "overlay_open"
        else:
            try:
                kda_panel["details"] = self._reader.read_kda_with_details(img)
                kda_panel["ran"] = True
            except Exception as e:
                kda_panel["error"] = f"{type(e).__name__}: {e}"

        # --- Match state ---
        match_panel = {
            "kills": self.match_kills,
            "deaths": self.match_deaths,
            "assists": self.match_assists,
            "prev_kda": (list(self._prev_kda)
                         if self._prev_kda is not None else None),
            "startup_validated": self._startup_validated,
            "non_gameplay_count": self._non_gameplay_count,
            "manual_mode": self._manual_mode,
            "running": self._running,
            "announce_chat": self._announce_chat,
            "last_kda_read_time": self._last_kda_read_time,
            "kda_read_failures": self._kda_read_failures,
        }

        # Assemble the snapshot. The page reads this whole dict atomically.
        self._debug_state = {
            "frame": frame_count,
            "timestamp": self._last_screenshot_ts,
            "last_scan_ms": (time.time() - scan_started_at) * 1000,
            "scene": scene,
            "god": god_panel,
            "kda": kda_panel,
            "match": match_panel,
            "obs_connected": self.obs_client is not None,
            "events": list(self._debug_events),  # snapshot — newest last
        }

    @staticmethod
    async def _safe_call(fn, *args):
        try:
            await fn(*args)
        except Exception as e:
            _log(f"[KillDetector] listener {getattr(fn, '__name__', fn)!r} error: {e}")

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        """Connect to OBS WebSocket and load the god portrait matcher."""
        await self._connect_obs()
        await self._load_god_matcher()

    async def _connect_obs(self):
        """Create a dedicated OBS WebSocket connection for screenshots."""
        try:
            import obsws_python as obs
            self.obs_client = obs.ReqClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
            )
            _log("[KillDetector] Connected to OBS WebSocket")
        except (ConnectionRefusedError, OSError):
            _log("[KillDetector] OBS not reachable — is OBS running with its "
                 "WebSocket server enabled? (kill detection will stay idle)")
            self.obs_client = None
        except Exception as e:
            _log(f"[KillDetector] OBS connection failed: {e}")
            self.obs_client = None

    async def _load_god_matcher(self):
        """Load the god portrait matcher for early god detection.

        Loads up to three fingerprint sources per god where each exists:
          * data/god_icons/<slug>.png     — the tracker.gg/wiki in-game
            portrait reference. Matches the raw game capture before any
            OBS overlay sits on top.
          * Custom God Icons/<Name>.png   — the streamer's custom OBS
            overlay art. Matches when the screenshot includes the
            overlay composited over the in-game portrait region (which
            happens whenever the bot's OBS source is a scene/group with
            the overlay enabled, not the bare game capture).
          * Portrait_Source/<Name>.png    — pixel-accurate reference
            crops captured from /detector's "save as reference" button
            (or tools/capture_god_reference.py on recordings). These
            correlate near-1.0 with future scans because they come from
            the same render pipeline as the live screenshots. The
            matcher picks the best score across all loaded fingerprints
            per god, so a borderline-confidence god (e.g. Ymir at 0.798)
            jumps to ~1.0 the next poll after a reference capture.
        Called fresh after save_portrait_reference so new captures take
        effect immediately without restarting the bot.
        """
        try:
            from core.god_matcher import GodMatcher
            from pathlib import Path
            repo_root = Path(__file__).resolve().parent.parent
            custom_dir = repo_root / "Custom God Icons"
            overlay_dir = str(custom_dir) if custom_dir.is_dir() else None
            reference_dir = repo_root / "Portrait_Source"
            reference_dir_str = (
                str(reference_dir) if reference_dir.is_dir() else None
            )
            self._god_matcher = GodMatcher(
                overlay_icons_dir=overlay_dir,
                reference_icons_dir=reference_dir_str,
            )
            if self._god_matcher.load_icons():
                msg = (f"[KillDetector] God portrait matcher ready "
                       f"({self._god_matcher.icon_count} icons)")
                if overlay_dir:
                    msg += " + Custom God Icons overlay fingerprints"
                if reference_dir_str:
                    msg += " + Portrait_Source references"
                _log(msg)
            else:
                _log(
                    "[KillDetector] God portrait matcher has no icons — "
                    "run download_god_icons.py"
                )
                self._god_matcher = None
        except ImportError as e:
            _log(f"[KillDetector] God matcher not available: {e}")
            self._god_matcher = None
        except Exception as e:
            _log(f"[KillDetector] God matcher load error: {e}")
            self._god_matcher = None

    # === SCREENSHOT CAPTURE ===

    def _grab_screenshot(self) -> Image.Image | None:
        """Grab a screenshot of the Smite 2 game source from OBS."""
        if not self.obs_client:
            return None
        try:
            resp = self.obs_client.get_source_screenshot(
                name="Smite 2",
                img_format="png",
                width=1920,
                height=1080,
                quality=-1,
            )
            img_data = resp.image_data
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            img_bytes = base64.b64decode(img_data)
            img = Image.open(io.BytesIO(img_bytes))
            self._screenshot_failures = 0
            return img.convert("RGB")
        except Exception as e:
            self._screenshot_failures = getattr(self, "_screenshot_failures", 0) + 1
            if self._screenshot_failures <= 3 or self._screenshot_failures % 50 == 0:
                _log(f"[KillDetector] Screenshot failed: {e}")
            # If many failures in a row, OBS likely disconnected — reset client
            # so the loop will attempt reconnection
            if self._screenshot_failures >= 10:
                self.obs_client = None
                self._screenshot_failures = 0
                _log(
                    "[KillDetector] Too many screenshot failures — "
                    "will retry OBS connection"
                )
            return None

    # === MULTI-KILL CLASSIFICATION ===

    def _classify_multikill(self, now: float) -> str:
        """Classify the multi-kill type based on recent kill timestamps."""
        recent = [
            t for t in self._recent_kill_times if now - t <= MULTIKILL_WINDOW
        ]
        self._recent_kill_times = recent

        count = len(recent)
        if count >= 5:
            return "penta_kill"
        elif count == 4:
            return "quadra_kill"
        elif count == 3:
            return "triple_kill"
        elif count == 2:
            return "double_kill"
        else:
            return "player_kill"

    # === DETECTION LOOP ===

    def _archive_debug_dir(self):
        """Archive previous session's debug images into a timestamped subfolder.

        Preserves all frames for offline testing / regression analysis.
        Only moves .png files from the top-level debug dir; existing
        archive subfolders are left untouched.
        """
        if not self._debug_dir.exists():
            return
        pngs = [
            f for f in self._debug_dir.iterdir()
            if f.suffix == ".png" and f.is_file()
        ]
        if not pngs:
            return

        import datetime
        oldest_mtime = min(f.stat().st_mtime for f in pngs)
        ts = datetime.datetime.fromtimestamp(oldest_mtime).strftime("%Y%m%d_%H%M%S")
        archive = self._debug_dir / ts
        archive.mkdir(exist_ok=True)

        moved = 0
        for f in pngs:
            try:
                f.rename(archive / f.name)
                moved += 1
            except OSError:
                pass
        if moved:
            _log(f"[KillDetector] Archived {moved} debug images → {ts}/")

    async def start_detection(self, manual=False):
        """Start the detection loop.

        manual=True bypasses the in-match check (for testing in jungle
        practice, etc.)
        """
        if self._running:
            return
        if not self._reader.is_ready:
            _log(
                "[KillDetector] ERROR: Cannot start — no Tesseract OCR or "
                "digit templates available"
            )
            return
        if self._debug:
            self._archive_debug_dir()
        self._running = True
        self._manual_mode = manual
        self._task = asyncio.create_task(self._detection_loop())
        mode = "MANUAL" if manual else "AUTO"
        _log(f"[KillDetector] Detection started ({mode})")

    async def stop_detection(self):
        """Stop the detection loop."""
        self._running = False
        self._manual_mode = False
        if self._task:
            self._task.cancel()
            self._task = None
        _log("[KillDetector] Detection stopped")

    def reset_match_stats(self):
        """Reset stats for a new match."""
        self.match_kills = 0
        self.match_deaths = 0
        self.match_assists = 0
        self.match_kill_types = {}
        self._is_dead = False
        self._prev_kda = None
        self._kda_read_failures = 0
        self._last_kda_read_time = 0
        self._recent_kill_times = []
        self._non_gameplay_count = 0
        self._god_identified = False
        self._god_confirm_name = None
        self._god_confirm_count = 0
        self._reader.discard_last_read()
        self._startup_reads = []
        self._startup_validated = False
        self._zero_read_count = 0
        self._last_match_god = None
        self._save_state()

    # === STATE PERSISTENCE ===

    def _save_state(self):
        """Save current KDA + match stats to disk for restart recovery."""
        import json
        state = {
            "kda": list(self._prev_kda) if self._prev_kda else None,
            "match_kills": self.match_kills,
            "match_deaths": self.match_deaths,
            "match_assists": self.match_assists,
            "match_kill_types": self.match_kill_types,
            "timestamp": time.time(),
        }
        try:
            from core.atomic_io import atomic_write_json
            atomic_write_json(STATE_FILE, state, indent=None)
        except OSError as e:
            if self._debug:
                _log(f"[KillDetector] Failed to save state: {e}")

    def _load_state(self) -> bool:
        """Load saved KDA state from disk if it exists and is recent enough.

        Returns True if state was restored, False if starting fresh.
        """
        import json
        if not STATE_FILE.exists():
            _log("[KillDetector] No saved state found — starting fresh")
            return False
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log(f"[KillDetector] Could not read saved state: {e}")
            return False

        saved_time = state.get("timestamp", 0)
        age = time.time() - saved_time
        if age > STATE_STALE_SECONDS:
            _log(
                f"[KillDetector] Saved state is {age/60:.0f} min old "
                f"(>{STATE_STALE_SECONDS/60:.0f} min) — starting fresh"
            )
            return False

        saved_kda = state.get("kda")
        if saved_kda is None:
            _log("[KillDetector] Saved state has no KDA — starting fresh")
            return False

        self._prev_kda = tuple(saved_kda)
        self.match_kills = state.get("match_kills", 0)
        self.match_deaths = state.get("match_deaths", 0)
        self.match_assists = state.get("match_assists", 0)
        self.match_kill_types = state.get("match_kill_types", {})
        self._last_kda_read_time = saved_time

        _log(
            f"[KillDetector] Restored state from {age:.0f}s ago: "
            f"KDA={saved_kda[0]}/{saved_kda[1]}/{saved_kda[2]}, "
            f"stats={self.match_kills}K/{self.match_deaths}D/"
            f"{self.match_assists}A"
        )
        return True

    def _save_failure_snapshot(self, img: Image.Image, reason) -> None:
        """Save a full frame + reason for postmortem when KDA reads fail
        persistently during identified gameplay. Rate-limited via
        READ_FAILURE_SNAPSHOT_MIN_INTERVAL so a broken reader doesn't
        fill the disk. Snapshots land in data/detector_snapshots/
        readfail_<ts>/fullframe.png — the same layout fixtures are
        built from, so a failing frame can be promoted straight into
        data/test_fixtures/kda/ once the truth is known."""
        now = time.time()
        last = getattr(self, "_last_failure_snapshot_ts", 0.0)
        if now - last < READ_FAILURE_SNAPSHOT_MIN_INTERVAL:
            return
        self._last_failure_snapshot_ts = now
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_dir = DATA_DIR / "detector_snapshots" / f"readfail_{ts}"
        try:
            snap_dir.mkdir(parents=True, exist_ok=True)
            img.save(str(snap_dir / "fullframe.png"))
            (snap_dir / "reason.txt").write_text(
                str(reason), encoding="utf-8"
            )
            _log(f"[KillDetector] Saved failure snapshot -> {snap_dir}")
        except OSError as e:
            _log(f"[KillDetector] Failed to save failure snapshot: {e}")

    async def _detection_loop(self):
        """Main loop: grab screenshots, read KDA, detect changes.

        Always runs while active — does not require the Smite plugin to
        report an in-match state.  ``is_gameplay_screen()`` ensures we
        only process frames from actual gameplay, so this safely idles
        on menus, god select, lobby, etc.  This lets god portrait
        identification work instantly (even in jungle practice) without
        waiting for the tracker.gg API.
        """
        _log("[KillDetector] Detection loop running (always-on)")
        _frame_count = 0
        _obs_retry_interval = 10  # seconds between OBS reconnect attempts

        # Try to restore state from a previous session
        self._load_state()
        while self._running:
            # Per-iteration markers so the finally clause at the end of
            # this try/except can decide whether to refresh debug state.
            # They stay None until we've grabbed a screenshot and bumped
            # the frame counter — before that there's nothing to show.
            _img_for_debug: Image.Image | None = None
            _img_array_for_debug: np.ndarray | None = None
            try:
                # Honor the dashboard kill_detection feature toggle.
                # We don't tear down the OBS connection or task — flipping
                # back on resumes the loop on the next tick — but we
                # short-circuit the per-frame work so the loop is cheap
                # while disabled.
                if self.bot is not None and not self.bot.is_feature_enabled("kill_detection"):
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                # Honor operator pause from /detector. Skip everything —
                # no screenshot, no portrait, no KDA — so debug_state +
                # _last_screenshot stay locked on the frozen frame and
                # the dashboard's "save portrait as reference" button is
                # guaranteed to capture exactly what's being displayed.
                if self._paused:
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                if not self.obs_client:
                    await self._connect_obs()
                    if not self.obs_client:
                        await asyncio.sleep(_obs_retry_interval)
                        continue

                img = await asyncio.get_event_loop().run_in_executor(
                    None, self._grab_screenshot
                )
                if img is None:
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                img_array = np.array(img)
                _frame_count += 1

                _frame_start = time.time()

                # Mark this iteration as having data the debug page can
                # render — the finally clause refreshes the dashboard
                # snapshot regardless of which early-exit branch we take.
                _img_for_debug = img
                _img_array_for_debug = img_array

                if _frame_count % 20 == 1:
                    _log(f"[KillDetector] Scanning... (frame {_frame_count})")

                if self._debug and _frame_count % 10 == 1:
                    img.save(str(self._debug_dir / f"frame_{_frame_count}.png"))

                # --- NON-GAMEPLAY DETECTION ---
                if not self._reader.is_gameplay_screen(img_array):
                    self._non_gameplay_count += 1
                    if self._debug:
                        _log(
                            f"[KillDetector] Non-gameplay screen "
                            f"(count: {self._non_gameplay_count}/"
                            f"{NON_GAMEPLAY_FRAMES})"
                        )
                    if self._non_gameplay_count >= NON_GAMEPLAY_FRAMES:
                        if self._prev_kda is not None:
                            # Full match-end: we validated KDA reads, so
                            # there are downstream consumers (economy
                            # settlement, dashboard stats, etc.) that
                            # need the gameplay_ended notification.
                            prev = self._prev_kda
                            _log(
                                f"[KillDetector] Game ended (non-gameplay "
                                f"screen detected). Final KDA: "
                                f"{prev[0]}/{prev[1]}/{prev[2]}. Resetting."
                            )
                            self._log_debug_event(
                                "state",
                                f"game ended — final KDA "
                                f"{prev[0]}/{prev[1]}/{prev[2]}",
                            )
                            # Notify listeners BEFORE resetting stats,
                            # so callbacks can read final stats.
                            await self._await_listeners(
                                self._gameplay_ended_listeners
                            )
                            self.reset_match_stats()
                        elif (self._god_identified
                              or self._god_confirm_name is not None):
                            # Player identified a god but never produced
                            # a validated KDA read — typical of jungle
                            # practice / 1v1 lobby / champ-select
                            # back-out. Clear ONLY the god identification
                            # so the next real match re-identifies
                            # cleanly. Don't fire gameplay_ended_listeners:
                            # downstream consumers were never told a
                            # match was in progress, so they have nothing
                            # to wrap up. Don't call reset_match_stats():
                            # _save_state() would thrash disk every few
                            # frames while idle in a lobby otherwise.
                            cleared = self._god_confirm_name
                            _log(
                                f"[KillDetector] Left gameplay (no KDA "
                                f"validated this session) — clearing "
                                f"god identification ({cleared})"
                            )
                            self._log_debug_event(
                                "state",
                                f"god id cleared ({cleared}) "
                                f"— left gameplay without KDA reads",
                            )
                            # Even without a validated KDA read,
                            # visual-only consumers (the economy
                            # plugin's cosmetic overlay arming, the
                            # smite plugin's OBS god portrait) DO
                            # need to know we left gameplay. Fire
                            # listeners so they can clear visual state.
                            await self._await_listeners(
                                self._gameplay_ended_listeners
                            )
                            self._god_identified = False
                            self._god_confirm_name = None
                            self._god_confirm_count = 0
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue
                self._non_gameplay_count = 0

                # --- GOD PORTRAIT IDENTIFICATION ---
                if (
                    not self._god_identified
                    and self._god_matcher
                    and self._god_matcher.is_loaded
                ):
                    try:
                        god_name, confidence = self._god_matcher.identify(img)
                        if god_name:
                            if god_name == self._god_confirm_name:
                                self._god_confirm_count += 1
                            else:
                                self._god_confirm_name = god_name
                                self._god_confirm_count = 1

                            if self._god_confirm_count >= self._GOD_CONFIRM_REQUIRED:
                                self._god_identified = True

                                # AUTO-RESET on god identity change.
                                # A god identity cannot change inside a
                                # single match, so seeing a different god
                                # means a new game started. Without this,
                                # the saved prev_kda from the previous
                                # match would make every read in the new
                                # one fail the decrease check.
                                # Inline reset preserves _god_identified
                                # so we stay locked in on the new god.
                                if (self._last_match_god is not None
                                        and self._last_match_god != god_name):
                                    _log(
                                        f"[KillDetector] God changed: "
                                        f"{self._last_match_god} -> {god_name} "
                                        f"- resetting match state for new game"
                                    )
                                    self._log_debug_event(
                                        "state",
                                        f"new match auto-detected: god "
                                        f"changed {self._last_match_god} "
                                        f"-> {god_name}",
                                    )
                                    self.match_kills = 0
                                    self.match_deaths = 0
                                    self.match_assists = 0
                                    self.match_kill_types = {}
                                    self._is_dead = False
                                    self._prev_kda = None
                                    self._kda_read_failures = 0
                                    self._last_kda_read_time = 0
                                    self._recent_kill_times = []
                                    self._startup_reads = []
                                    self._startup_validated = False
                                    self._zero_read_count = 0
                                    self._reader.discard_last_read()
                                    self._save_state()
                                self._last_match_god = god_name

                                _log(
                                    f"[KillDetector] God identified from portrait: "
                                    f"{god_name} (confidence: {confidence:.3f}, "
                                    f"confirmed over "
                                    f"{self._god_confirm_count} frames)"
                                )
                                self._log_debug_event(
                                    "state",
                                    f"god identified: {god_name} "
                                    f"(conf {confidence:.3f}, "
                                    f"{self._god_confirm_count} frames)",
                                )
                                await self._await_listeners(
                                    self._god_identified_listeners, god_name
                                )
                            elif self._debug:
                                _log(
                                    f"[KillDetector] God candidate: {god_name} "
                                    f"({confidence:.3f}) — confirming "
                                    f"{self._god_confirm_count}/"
                                    f"{self._GOD_CONFIRM_REQUIRED}"
                                )
                        else:
                            self._god_confirm_name = None
                            self._god_confirm_count = 0
                            if self._debug:
                                top = self._god_matcher.identify_top_n(img, n=1)
                                if top:
                                    _log(
                                        f"[KillDetector] God match below "
                                        f"threshold: best={top[0][0]} "
                                        f"({top[0][1]:.3f})"
                                    )
                    except Exception as e:
                        if self._debug:
                            _log(f"[KillDetector] God match error: {e}")

                # Don't attempt KDA reading until god portrait is identified.
                if not self._god_identified:
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                # Skip if store or scoreboard is open — KDA bar is hidden.
                if self._reader.is_overlay_open(img_array):
                    if self._debug:
                        _log("[KillDetector] Overlay detected, skipping")
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                now = time.time()

                # --- KDA NUMBER TRACKING ---
                kda = self._reader.read_kda(img)
                if kda is not None:
                    self._kda_read_failures = 0

                    # --- Startup validation ---
                    if not self._startup_validated:
                        self._startup_reads.append(kda)
                        if len(self._startup_reads) < STARTUP_REQUIRED_READS:
                            if self._debug:
                                _log(
                                    f"[KillDetector] Startup read "
                                    f"{len(self._startup_reads)}/"
                                    f"{STARTUP_REQUIRED_READS}: {kda}"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        if all(
                            r == self._startup_reads[0]
                            for r in self._startup_reads
                        ):
                            validated_kda = self._startup_reads[0]
                            self._startup_validated = True
                            self._startup_reads = []

                            # How many kills/deaths/assists should we fire
                            # listener events for to bring downstream
                            # consumers (economy plugin, overlays) in sync
                            # with the actual game state? The math is the
                            # same as for normal incremental reads — a
                            # delta from "where we last knew we were."
                            #
                            # Fresh boot:    last known = (0,0,0). Catch
                            #                up the entire baseline.
                            # Restart with
                            # saved state:   last known = saved snapshot.
                            #                Catch up only what happened
                            #                while the bot was down.
                            # Saved-state
                            # mismatch:      we treat it as a fresh start
                            #                (per the existing branch
                            #                below), so catch up the
                            #                entire baseline.
                            catchup_k = catchup_d = catchup_a = 0

                            if self._prev_kda is not None:
                                saved = self._prev_kda
                                v_k, v_d, v_a = validated_kda
                                s_k, s_d, s_a = saved
                                if (
                                    v_k >= s_k
                                    and v_d >= s_d
                                    and v_a >= s_a
                                ):
                                    dk = v_k - s_k
                                    dd = v_d - s_d
                                    da = v_a - s_a
                                    self.match_kills += dk
                                    self.match_deaths += dd
                                    self.match_assists += da
                                    self._prev_kda = validated_kda
                                    self._last_kda_read_time = now
                                    _log(
                                        f"[KillDetector] Startup validated "
                                        f"with saved state: "
                                        f"{s_k}/{s_d}/{s_a} → "
                                        f"{v_k}/{v_d}/{v_a} "
                                        f"(+{dk}K/+{dd}D/+{da}A while down)"
                                    )
                                    self._save_state()
                                    catchup_k, catchup_d, catchup_a = dk, dd, da
                                else:
                                    _log(
                                        f"[KillDetector] Startup mismatch: "
                                        f"saved {s_k}/{s_d}/{s_a} but "
                                        f"read {v_k}/{v_d}/{v_a} — "
                                        f"starting fresh"
                                    )
                                    self.match_kills = 0
                                    self.match_deaths = 0
                                    self.match_assists = 0
                                    self.match_kill_types = {}
                                    self._prev_kda = validated_kda
                                    self._last_kda_read_time = now
                                    self._save_state()
                                    catchup_k, catchup_d, catchup_a = validated_kda
                            else:
                                self._prev_kda = validated_kda
                                self._last_kda_read_time = now
                                _log(
                                    f"[KillDetector] Startup validated "
                                    f"(fresh): {validated_kda}"
                                )
                                self._save_state()
                                catchup_k, catchup_d, catchup_a = validated_kda

                            # Fire listener catch-up events so plugins that
                            # care about KDA (economy live ticks, overlay
                            # updates, death counter) sync to the current
                            # game state. The events are dispatched as
                            # bulk (count > 1) where applicable so a 3-kill
                            # baseline produces a single price tick of
                            # +4.5%, not three staggered ones, and the
                            # overlay sees a single update message.
                            #
                            # We use kill_type="player_kill" deliberately
                            # — multikill classification only makes sense
                            # for kills observed in real time within the
                            # MULTIKILL_WINDOW. A baseline catch-up isn't
                            # a multikill regardless of count.
                            if catchup_k or catchup_d or catchup_a:
                                _log(
                                    f"[KillDetector] Catching listeners up "
                                    f"on baseline: "
                                    f"+{catchup_k}K/+{catchup_d}D/+{catchup_a}A"
                                )
                                if catchup_k:
                                    self._fire_listeners(
                                        self._kill_listeners,
                                        "player_kill", int(catchup_k),
                                    )
                                if catchup_d:
                                    self._fire_listeners(
                                        self._death_listeners,
                                        int(catchup_d),
                                    )
                                if catchup_a:
                                    self._fire_listeners(
                                        self._assist_listeners,
                                        int(catchup_a),
                                    )
                                # Also bump the local match-stats counter
                                # in the FRESH case (saved-state case
                                # already did this above via match_*).
                                if self._prev_kda == validated_kda and \
                                        self.match_kills == 0 and \
                                        self.match_deaths == 0 and \
                                        self.match_assists == 0:
                                    self.match_kills = int(catchup_k)
                                    self.match_deaths = int(catchup_d)
                                    self.match_assists = int(catchup_a)

                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue
                        else:
                            self._startup_reads.pop(0)
                            if self._debug:
                                _log(
                                    f"[KillDetector] Startup reads "
                                    f"inconsistent, retrying: "
                                    f"{self._startup_reads}"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                    if self._prev_kda is not None:
                        prev_k, prev_d, prev_a = self._prev_kda
                        cur_k, cur_d, cur_a = kda

                        # AUTO-RESET on extended (0,0,0) reads. If the
                        # bot carried a stale prev_kda from a previous
                        # match across an undetected boundary (spectator
                        # screen, custom lobby, post-match scoreboard),
                        # the decrease check below would reject every
                        # (0,0,0) read in the new match forever.
                        # Counting consecutive zeros gives a clean self-
                        # healing signal: once we see ZERO_READ_RESET_
                        # THRESHOLD of them, accept (0,0,0) as baseline.
                        if kda == (0, 0, 0) and self._prev_kda != (0, 0, 0):
                            self._zero_read_count += 1
                            if self._zero_read_count >= ZERO_READ_RESET_THRESHOLD:
                                _log(
                                    f"[KillDetector] {self._zero_read_count} "
                                    f"consecutive (0,0,0) reads - new match "
                                    f"detected, resetting from "
                                    f"{self._prev_kda}"
                                )
                                self._log_debug_event(
                                    "state",
                                    f"new match auto-detected: "
                                    f"{self._zero_read_count} consecutive "
                                    f"0/0/0 reads, was {self._prev_kda}",
                                )
                                self.match_kills = 0
                                self.match_deaths = 0
                                self.match_assists = 0
                                self.match_kill_types = {}
                                self._is_dead = False
                                self._prev_kda = kda
                                self._kda_read_failures = 0
                                self._last_kda_read_time = now
                                self._recent_kill_times = []
                                self._zero_read_count = 0
                                self._save_state()
                                await asyncio.sleep(SCREENSHOT_INTERVAL)
                                continue
                            if self._debug:
                                _log(
                                    f"[KillDetector] (0,0,0) read "
                                    f"{self._zero_read_count}/"
                                    f"{ZERO_READ_RESET_THRESHOLD} "
                                    f"- may be a new match"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue
                        else:
                            # Reset counter on any non-(0,0,0) read so
                            # we only fire on a SUSTAINED 0/0/0 burst.
                            self._zero_read_count = 0

                        # Sanity check: KDA should only go up during a match.
                        if (
                            cur_k < prev_k
                            or cur_d < prev_d
                            or cur_a < prev_a
                        ):
                            if self._debug:
                                _log(
                                    f"[KillDetector] KDA decreased "
                                    f"({self._prev_kda} → {kda}), "
                                    f"likely OCR misread — skipping"
                                )
                            self._log_debug_event(
                                "reject",
                                f"KDA decreased "
                                f"{prev_k}/{prev_d}/{prev_a} → "
                                f"{cur_k}/{cur_d}/{cur_a} — misread",
                            )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Sanity check: reject implausible jumps.
                        dk = cur_k - prev_k
                        dd = cur_d - prev_d
                        da = cur_a - prev_a
                        time_since_read = now - self._last_kda_read_time
                        max_jump = int(
                            MAX_KDA_JUMP_BASE
                            + time_since_read * MAX_KDA_JUMP_PER_SEC
                        )
                        if dk > max_jump or dd > max_jump or da > max_jump:
                            if self._debug:
                                _log(
                                    f"[KillDetector] KDA jump too large "
                                    f"({self._prev_kda} → {kda}, "
                                    f"Δ={dk}/{dd}/{da}, "
                                    f"max={max_jump} after "
                                    f"{time_since_read:.1f}s) — skipping"
                                )
                            self._log_debug_event(
                                "reject",
                                f"KDA jump too large "
                                f"{prev_k}/{prev_d}/{prev_a} → "
                                f"{cur_k}/{cur_d}/{cur_a} "
                                f"(Δ={dk}/{dd}/{da}, max={max_jump})",
                            )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        self._last_kda_read_time = now

                        # Kill increase
                        kill_type = "player_kill"
                        if cur_k > prev_k:
                            new_kills = cur_k - prev_k
                            if new_kills == 1:
                                self._recent_kill_times.append(now)
                                self.match_kills += 1
                                kill_type = self._classify_multikill(now)
                            else:
                                self._recent_kill_times.clear()
                                self.match_kills += new_kills
                                kill_type = "player_kill"

                            self.match_kill_types[kill_type] = (
                                self.match_kill_types.get(kill_type, 0) + 1
                            )
                            _log(
                                f"[KillDetector] KILL: {prev_k}→{cur_k} "
                                f"(+{new_kills}, type={kill_type}, "
                                f"total={self.match_kills})"
                            )
                            self._log_debug_event(
                                "accept",
                                f"KILL +{new_kills} ({kill_type}) — "
                                f"total {self.match_kills}",
                            )

                            if self._debug:
                                img.save(str(
                                    self._debug_dir
                                    / f"event_kill_{self.match_kills}"
                                    f"_f{_frame_count}.png"
                                ))

                            if kill_type in (
                                "double_kill", "triple_kill",
                                "quadra_kill", "penta_kill",
                            ):
                                self._fire_listeners(
                                    self._multikill_listeners, kill_type
                                )
                            self._fire_listeners(
                                self._kill_listeners, kill_type, new_kills
                            )

                        # Death increase
                        if cur_d > prev_d:
                            new_deaths = cur_d - prev_d
                            self.match_deaths += new_deaths
                            self._is_dead = True
                            _log(
                                f"[KillDetector] DEATH: {prev_d}→{cur_d} "
                                f"(+{new_deaths}, total={self.match_deaths})"
                            )
                            self._log_debug_event(
                                "accept",
                                f"DEATH +{new_deaths} — "
                                f"total {self.match_deaths}",
                            )

                            if self._debug:
                                img.save(str(
                                    self._debug_dir
                                    / f"event_death_{self.match_deaths}"
                                    f"_f{_frame_count}.png"
                                ))

                            self._fire_listeners(
                                self._death_listeners, new_deaths
                            )

                        # Assist increase
                        if cur_a > prev_a:
                            new_assists = cur_a - prev_a
                            self.match_assists += new_assists
                            _log(
                                f"[KillDetector] ASSIST: {prev_a}→{cur_a} "
                                f"(+{new_assists}, total={self.match_assists})"
                            )
                            self._log_debug_event(
                                "accept",
                                f"ASSIST +{new_assists} — "
                                f"total {self.match_assists}",
                            )
                            self._fire_listeners(
                                self._assist_listeners, new_assists
                            )

                        # Chat announcements
                        if self._announce_chat and self.bot:
                            kda_str = f"{cur_k}/{cur_d}/{cur_a}"
                            if cur_k > prev_k:
                                label = kill_type.replace("_", " ").title()
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"{label}! KDA: {kda_str}"
                                    )
                                )
                            elif cur_d > prev_d:
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"Hatmaster died. KDA: {kda_str}"
                                    )
                                )
                            elif cur_a > prev_a:
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"Assist! KDA: {kda_str}"
                                    )
                                )

                        if cur_d == prev_d and self._is_dead:
                            self._is_dead = False
                            _log("[KillDetector] Respawned (KDA stable)")

                    # Update _prev_kda and persist state
                    self._prev_kda = kda
                    self._save_state()

                    # Auto-collect digit templates from confirmed reads.
                    added = self._reader.enroll_last_read()
                    if added and self._debug:
                        _log(
                            f"[KillDetector] Auto-collected {added} new "
                            f"digit template(s)"
                        )

                else:
                    self._kda_read_failures += 1
                    if self._debug and self._kda_read_failures % 10 == 1:
                        _log(
                            f"[KillDetector] KDA read failed "
                            f"(consecutive: {self._kda_read_failures})"
                        )
                    # Loud, non-debug alert on sustained failure during
                    # identified gameplay. This state used to be silent
                    # — a fully-broken reader looked identical to lobby
                    # idling in the logs. Run the with_details pipeline
                    # once per alert (~10-20ms) to capture WHY it fails.
                    if self._kda_read_failures % READ_FAILURE_ALERT_EVERY == 0:
                        reason = None
                        try:
                            details = self._reader.read_kda_with_details(img)
                            reason = details.get("failure_reason")
                        except Exception as e:
                            reason = f"details_error:{type(e).__name__}:{e}"
                        _log(
                            f"[KillDetector] WARNING: "
                            f"{self._kda_read_failures} consecutive KDA "
                            f"read failures during gameplay "
                            f"(god={self._god_confirm_name}, "
                            f"reason={reason})"
                        )
                        self._log_debug_event(
                            "reject",
                            f"{self._kda_read_failures} consecutive read "
                            f"failures (reason={reason})",
                        )
                        self._save_failure_snapshot(img, reason)

                _frame_elapsed = (time.time() - _frame_start) * 1000
                if _frame_count % 20 == 1:
                    _log(
                        f"[KillDetector] Frame {_frame_count} total: "
                        f"{_frame_elapsed:.0f}ms"
                    )

                await asyncio.sleep(SCREENSHOT_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _log(f"[KillDetector] Error in detection loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(SCREENSHOT_INTERVAL * 2)
            finally:
                # Refresh the dashboard debug snapshot every iteration.
                # Wrapped in its own try so any error inside the
                # observability path can never break the live loop.
                if _img_for_debug is not None:
                    try:
                        self._build_debug_state(
                            _img_for_debug,
                            _img_array_for_debug,
                            _frame_count,
                            _frame_start,
                        )
                    except Exception as e:
                        _log(
                            f"[KillDetector] Debug state update "
                            f"error: {e}"
                        )

        _log("[KillDetector] Detection loop stopped")

    def _is_in_match(self) -> bool:
        """Check if the smite plugin reports an active match."""
        if self.bot and "smite" in self.bot.plugins:
            return self.bot.plugins["smite"].is_in_match
        return False

    def get_match_stats(self):
        """Return current match kill/death/assist stats."""
        return {
            "kills": self.match_kills,
            "deaths": self.match_deaths,
            "assists": self.match_assists,
            "is_dead": self._is_dead,
            "kill_types": dict(self.match_kill_types),
            "kda_failures": self._kda_read_failures,
        }

    def set_debug(self, enabled: bool):
        """Toggle debug mode (saves OCR crops to killdetect_debug/)."""
        self._debug = enabled
        self._reader.debug = enabled
        if self._debug:
            self._debug_dir.mkdir(exist_ok=True)

    def set_announce_chat(self, enabled: bool):
        """Toggle chat announcements for kill/death/assist events."""
        self._announce_chat = enabled
        _log(
            f"[KillDetector] Chat announcements "
            f"{'enabled' if enabled else 'disabled'}"
        )

    def pause_scanning(self) -> dict:
        """Freeze the scan loop. ``_last_screenshot`` and ``debug_state``
        stay locked on the most recent frame so /detector's frozen view
        and the frame captured by save_portrait_reference are guaranteed
        to be identical.

        Returns a small dict describing the paused frame so the caller
        can echo it back to the dashboard.
        """
        self._paused = True
        self._paused_at = time.time()
        _log("[KillDetector] Scanning paused (operator)")
        return {
            "paused": True,
            "paused_at": self._paused_at,
            "frame_ts": self._last_screenshot_ts,
            "has_screenshot": getattr(self, "_last_screenshot", None) is not None,
        }

    def resume_scanning(self) -> dict:
        """Un-freeze the scan loop. Next iteration grabs a fresh frame."""
        was = self._paused
        self._paused = False
        self._paused_at = None
        if was:
            _log("[KillDetector] Scanning resumed (operator)")
        return {"paused": False, "was_paused": was}

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def save_portrait_reference(self, god_name: str) -> dict:
        """Crop the current frame's portrait region and save it as a
        Portrait_Source reference for ``god_name``. Overwrites any
        existing flat-file reference for that god (subfolder references
        from earlier captures are left alone — the matcher already
        picks the best score across all files).

        After the save, the matcher is reloaded from disk so the new
        reference takes effect on the very next frame.
        """
        import base64
        import io
        from pathlib import Path
        from core.god_matcher import PORTRAIT_REGION

        if not god_name or not god_name.strip():
            return {"ok": False, "error": "god_name is required"}
        god_name = god_name.strip()

        img = getattr(self, "_last_screenshot", None)
        if img is None:
            return {"ok": False, "error": "no screenshot available yet"}

        try:
            x1, y1, x2, y2 = PORTRAIT_REGION
            crop = img.convert("RGB").crop((x1, y1, x2, y2))
        except Exception as e:
            return {"ok": False, "error": f"crop failed: {e}"}

        repo_root = Path(__file__).resolve().parent.parent
        ref_dir = repo_root / "Portrait_Source"
        ref_dir.mkdir(parents=True, exist_ok=True)
        # Operator picked overwrite semantics — one flat file per god.
        # Any subfolder Portrait_Source/<God>/*.png references stay in
        # place; the matcher uses the best score across all files.
        out_path = ref_dir / f"{god_name}.png"
        try:
            crop.save(out_path, format="PNG")
        except Exception as e:
            return {"ok": False, "error": f"write failed: {e}"}

        try:
            await self._load_god_matcher()
        except Exception as e:
            return {"ok": False,
                    "error": f"saved {out_path.name} but matcher reload failed: {e}"}

        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        portrait_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        icon_count = (
            self._god_matcher.icon_count
            if self._god_matcher is not None else 0
        )
        _log(f"[KillDetector] Saved portrait reference: {out_path} "
             f"(matcher now {icon_count} icons)")
        return {
            "ok": True,
            "god": god_name,
            "path": str(out_path),
            "icon_count": icon_count,
            "captured_at": self._last_screenshot_ts or __import__("time").time(),
            "portrait_b64": portrait_b64,
        }

    async def cleanup(self):
        """Shutdown the detector and flush logs."""
        _log("[KillDetector] Cleaning up...")
        await self.stop_detection()
        if self.obs_client:
            try:
                self.obs_client.disconnect()
            except Exception:
                pass
        for handler in _kd_logger.handlers:
            handler.flush()
