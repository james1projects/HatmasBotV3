"""
VOD Detector
============
Replays the HatmasBot KDA detector over a recorded .mp4 file and returns
a list of event dicts ready to be serialized to the HighlightBuilder
``.events.json`` format.

Algorithm (Option B — coarse + binary search):

  1. Probe the video's duration with ffprobe.
  2. Sample one frame every ``coarse_interval`` seconds (default 5s) via
     ffmpeg.  For each frame that passes the gameplay / overlay checks,
     read (K, D, A).
  3. Between two successful coarse reads where at least one counter
     increased, binary-search inside that interval for the exact moment
     of each transition, to ``refine_precision`` seconds (default 0.2s).
  4. Kill → emit `{type: "kill"}`; death → `{type: "death"}`; assist →
     `{type: "assist"}`.  Multi-kills (within 10 s of a prior kill) get
     a human-readable note and a longer post-roll.

All the actual image processing is delegated to a shared ``KdaReader``
instance (``core/kda_reader.py``) — the same engine the live plugin
uses.  This module only owns the temporal / seeking / scheduling logic.

This module is framework-free: no asyncio, no OBS, no config imports.
It talks to ``ffmpeg`` and ``ffprobe`` via subprocess and hands PIL
images into the reader.
"""

from __future__ import annotations

import io
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from core.kda_reader import KDA_REGION, KdaReader


# --- Constants -------------------------------------------------------------

# Match live plugin: kills within this window count as a multi-kill.
MULTIKILL_WINDOW = 10.0

# Default per-event windows in the JSON output.  Bumped +2s on both sides
# (2026-04-18) so each Vegas clip captures more lead-in and follow-through —
# gives the highlight room to breathe and catches setup/reactions that the
# tighter 5/4 windows sometimes clipped off.
DEFAULT_PRE_SEC = 7.0
DEFAULT_POST_SEC = 6.0

# Multi-kills get extra post-roll because the highlight continues.
MULTIKILL_POST_SEC = 8.0

# Small offsets we try if a target frame is unreadable (menu overlay,
# scoreboard up, death screen, bad decode).  Kept tight so binary
# search stays honest.
READ_RETRY_OFFSETS = (0.0, 0.3, -0.3, 0.6, -0.6, 1.0, -1.0)

# Sanity check on KDA deltas between coarse reads — ported from the live
# How many consecutive identical reads we need before committing the
# first baseline.  Mirrors STARTUP_REQUIRED_READS in killdetector.py.
# Without this, a single OCR misread at scan start (e.g. the sword
# icon being read as "7" producing K=70) gets locked in as the
# baseline, and every subsequent legitimate frame then looks like a
# huge decrease — triggering expensive phantom-event refinement.
# A value of 2 is enough: real KDA stays constant for many seconds
# between events, so two consecutive 5s samples almost always agree.
# Misreads almost never repeat the exact same garbage twice in a row.
BASELINE_REQUIRED_READS = 2

# Plausibility cap on the very first baseline.  A fresh-match scan
# should see K+D+A small at scan start (the recording's coarse-scan
# first lands in lobby or early laning).  If the first candidate
# baseline has any field > BASELINE_MAX_FIELD or sum exceeds
# BASELINE_MAX_SUM, we refuse it as obviously misread and keep
# looking for a sane initial frame.  Penta-kill within 30s would
# put K=5, A could realistically reach 10 in long matches; anything
# above these is a clear icon-leaked-as-digit pattern.
BASELINE_MAX_FIELD = 15
BASELINE_MAX_SUM = 25

# plugin (killdetector.py).  Anything more than MAX_KDA_JUMP_BASE +
# elapsed_seconds * MAX_KDA_JUMP_PER_SEC is treated as an OCR misread and
# dropped without triggering refinement.  This prevents a single misread
# of e.g. K=21 from cascading into 20 phantom multi-kill events.
MAX_KDA_JUMP_BASE = 5
MAX_KDA_JUMP_PER_SEC = 1.0 / 3.0

# Extra safety in the refinement loop: even if a misread slips past the
# per-sample sanity check, never emit more than this many increments
# per coarse interval for any one KDA field.
MAX_EVENTS_PER_INTERVAL = 5

# --- ffmpeg-side crop --------------------------------------------------------
#
# The KDA reader only needs 4 regions from each frame.  Their union bounding
# box is Y=100..1080, X=0..1152 (driven by the overlay-check region, which is
# the widest; KDA/HUD/GAMEPLAY checks all live in the bottom strip).
#
# Cropping server-side in ffmpeg cuts per-frame payload from ~6.2 MB
# (1920x1080 rgb24) to ~3.4 MB (1152x980 rgb24) — a 45% reduction in the
# bytes we decode, pipe to Python, and wrap as a PIL Image.  Combined with
# the smaller numpy slices in is_gameplay_screen / is_overlay_open, the
# overall coarse-scan cost per sample drops noticeably.
#
# Values picked by taking max/min of KDA_REGION, OVERLAY_CHECK_REGION,
# HUD_CHECK_REGION, and GAMEPLAY_CHECK_REGION from core/kda_reader.py.
# The reader methods all take ``crop_origin=(VOD_CROP_X, VOD_CROP_Y)``
# when we pass them a cropped strip so their hardcoded region constants
# still point at the right pixels.
VOD_CROP_X = 0
VOD_CROP_Y = 100
VOD_CROP_W = 1152
VOD_CROP_H = 980

# Widened event windows used when --no-refine is on.  The default coarse
# interval is 5 s, so a detected event lands somewhere in the 5 s bucket;
# we widen pre/post by the half-interval plus a tiny bit of slack on top
# of the standard DEFAULT_PRE_SEC / DEFAULT_POST_SEC.
NO_REFINE_PRE_SEC = 9.5
NO_REFINE_POST_SEC = 8.5

# Lobby-skip: while we've never seen a gameplay-qualifying frame, only
# actually process 1 in LOBBY_STRIDE coarse samples.  At the default 5 s
# interval, 6 means we look every 30 s during pre-match / load screens.
# Flips to 1 (every sample) as soon as the first is_gameplay_screen read
# passes.
LOBBY_STRIDE = 6

# Post-match skip: once we've seen gameplay, if is_gameplay_screen fails
# for this many consecutive samples we stop scanning.  23 samples at 5 s
# interval = ~2 minutes of non-gameplay, which comfortably covers the
# end-of-match scoreboard + post-match lobby without false-positiving on
# a long death cam.
POST_MATCH_STOP_AFTER = 23


# --- Helpers ---------------------------------------------------------------

def _read_exact(stream, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from ``stream``.

    Returns the bytes on success, ``None`` on EOF or short read.  Needed
    because ``stream.read(n)`` on a subprocess pipe can return fewer
    bytes than requested without meaning end-of-stream.
    """
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _classify_multikill(count: int) -> str:
    """Map a kill count within the multi-kill window to a label.

    Matches the live detector's classification.  ``count`` is the number
    of kills the player has landed inside the current window, including
    the kill being classified.
    """
    if count >= 5:
        return "penta_kill"
    if count == 4:
        return "quadra_kill"
    if count == 3:
        return "triple_kill"
    if count == 2:
        return "double_kill"
    return "player_kill"


@dataclass
class VodDetectorOptions:
    """Tuning knobs for VodDetector.  All optional.

    coarse_interval:    seconds between scan-pass frames (Option B step 2).
    refine_precision:   stop binary-searching when the interval is this small.
    include_deaths:     emit death events.  Default kills-only.
    include_assists:    emit assist events.  Default kills-only.
    enroll_templates:   save successful digit crops back into the shared
                        digit-template library.  Off by default so batch
                        runs don't pollute the live detector's templates.
    ffmpeg:             path to the ffmpeg binary (default: "ffmpeg").
    ffprobe:            path to the ffprobe binary (default: "ffprobe").
    frame_timeout:      seconds per single-frame extraction before giving up.
    probe_timeout:      seconds for ffprobe duration lookup before giving up.
    verbose:            print per-sample progress.
    progress_callback:  optional fn(scan_t, duration) called during the coarse
                        scan so callers can render a live progress line.  Off
                        by default.
    """

    coarse_interval: float = 5.0
    refine_precision: float = 0.2
    include_deaths: bool = False
    include_assists: bool = False
    enroll_templates: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    frame_timeout: float = 10.0
    probe_timeout: float = 15.0
    verbose: bool = False
    progress_callback: Optional[object] = None
    # Speed knobs:
    no_refine: bool = False       # skip binary-search refinement; emit events
                                   # at the coarse-scan timestamp with widened
                                   # pre/post windows.
    ffmpeg_crop: bool = True      # crop the HUD bounding box server-side in
                                   # ffmpeg before piping frames to Python.
    lobby_skip: bool = True       # sparse-sample until first gameplay frame.
    use_seek_scan: bool = True    # use input-side -ss to seek per sample
                                   # instead of streaming the whole file
                                   # through a fps filter. For AV1 / 4K
                                   # sources this is 3-10x faster because
                                   # ffmpeg only decodes from the nearest
                                   # keyframe to each sample instead of
                                   # decoding every frame and dropping
                                   # 4 out of 5. For HEVC / 1080p the
                                   # advantage is smaller (decode is
                                   # already cheap), and the per-sample
                                   # spawn overhead may make streaming
                                   # competitive. Set False to force the
                                   # legacy streaming path for A/B
                                   # comparison or fallback.
    hwaccel: Optional[str] = None  # ffmpeg -hwaccel value (e.g. "cuda",
                                   # "d3d11va", "auto").  None = software
                                   # decode, which is what the live plugin
                                   # uses.  HEVC from OBS decodes ~3-5x faster
                                   # on NVIDIA with "cuda" — this is usually
                                   # the biggest single speedup available.
    merge_overlaps: bool = True    # collapse events whose pre/post clip
                                   # windows overlap into a single wider
                                   # event.  Turns a "kill then die" pair
                                   # (windows overlap) into one Vegas clip
                                   # that covers both moments.
    # God identification:
    enable_god_detection: bool = True  # run the portrait matcher on each
                                       # gameplay frame so the scan can
                                       # surface which god(s) appeared in
                                       # the recording.  Output lands in
                                       # ``VodDetector.gods_seen`` as a
                                       # de-duplicated list ordered by
                                       # first-confirmation time.
    god_icons_dir: Optional[Path] = None  # path to the reference icon
                                          # library used by GodMatcher.
                                          # Defaults to ``<repo>/data/god_icons``
                                          # via core.god_matcher's own default.
    god_overlay_icons_dir: Optional[Path] = None  # path to the OBS-overlay
                                                  # custom god icons (covers
                                                  # the in-game portrait
                                                  # region in recordings).
                                                  # Defaults to
                                                  # ``<repo>/Custom God Icons``
                                                  # — auto-resolved in
                                                  # ``_ensure_god_matcher``
                                                  # if left None.  Set to
                                                  # an explicit empty Path
                                                  # to disable overlay
                                                  # matching.
    god_reference_icons_dir: Optional[Path] = None  # path to the pixel-accurate
                                                    # reference portrait crops
                                                    # captured from real
                                                    # recordings (via
                                                    # ``tools/capture_god_reference.py``).
                                                    # Defaults to
                                                    # ``<repo>/Portrait_Source``
                                                    # auto-resolved in
                                                    # ``_ensure_god_matcher``
                                                    # if left None.  Pass
                                                    # an empty Path to
                                                    # disable.  Distinct
                                                    # from the overlay
                                                    # folder so user-
                                                    # decorative art and
                                                    # matcher reference
                                                    # data don't mix.
    # Diagnostics:
    misread_debug_dir: Optional[Path] = None  # if set, every KDA-level
                                              # misread (partial decrease,
                                              # max-jump rejection) saves
                                              # the offending full frame +
                                              # raw KDA crop into this
                                              # folder.  Filenames encode
                                              # the timestamp, reason, and
                                              # before/after KDA values
                                              # so frames are immediately
                                              # diagnosable without
                                              # cross-referencing logs.
                                              # Off by default.
    failure_event_callback: Optional[object] = None  # fn(failure_dict) called
                                              # whenever read_kda returns None
                                              # on a confirmed gameplay frame.
                                              # The failure_dict contains the
                                              # raw KDA crop and binarised
                                              # debug image as base64, so the
                                              # /detector dashboard can show
                                              # exactly what the reader saw
                                              # without needing to scrape
                                              # disk-side debug folders.
                                              # Rate-limited internally; safe
                                              # to leave on for full scans.
    failure_callback_min_interval: float = 1.0  # minimum seconds between
                                              # failure_event_callback
                                              # invocations.  Stops the
                                              # detector from drowning the
                                              # dashboard when a long stretch
                                              # of overlay-open frames slips
                                              # through the gameplay check.


class VodDetectorError(RuntimeError):
    """Raised for unrecoverable problems (missing binary, corrupt file)."""


class VodDetector:
    """Runs the KDA reader against a recorded video and returns event dicts.

    Usage:
        reader = KdaReader(...)
        detector = VodDetector(reader, VodDetectorOptions(verbose=True))
        events = detector.detect("match.mp4")
    """

    # Same threshold the live detector uses (plugins/killdetector.py):
    # a candidate god must be returned by GodMatcher.identify() on this
    # many consecutive *gameplay* frames before we accept it as truly
    # seen.  Three is enough to reject lobby/menu false positives because
    # those screens won't consistently match the same god across multiple
    # frames.
    _GOD_CONFIRM_REQUIRED = 3

    def __init__(
        self,
        reader: KdaReader,
        options: Optional[VodDetectorOptions] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.reader = reader
        self.opts = options or VodDetectorOptions()
        self.log = logger or logging.getLogger("VodDetector")

        # God identification state.  Populated lazily on first detect()
        # call (so importing this module doesn't load the icon library).
        # ``gods_seen`` is the public output: each scan resets it, and
        # callers read it after detect() returns.
        self.gods_seen: list[str] = []
        self._god_matcher = None  # core.god_matcher.GodMatcher | None
        self._god_matcher_init_attempted: bool = False
        self._god_confirm_name: Optional[str] = None
        self._god_confirm_count: int = 0
        self._gods_seen_set: set = set()
        # Performance gate for _check_god — True until we've locked
        # in a god for the current match, then False until detect()
        # sees a 0/0/0 reset and calls rearm_god_check(). See
        # _check_god for the gate; see detect() for the rearm path.
        self._god_check_armed: bool = True
        # Per-god match counts across the whole scan.  Counts every
        # successful ``identify()`` regardless of whether the
        # 3-consecutive-frame rule then promoted that god into
        # gods_seen.  Used at end-of-scan as a frequency filter so
        # transient mid-gameplay false positives (e.g. Medusa
        # appearing 3 frames during Janus's portal animation) get
        # dropped while legitimate multi-match recordings keep both
        # gods.
        self._god_match_counts: dict[str, int] = {}

        # Diagnostic counters — incremented inside ``_read_from_image``
        # and surfaced in the scan summary at the end of ``detect()``.
        # When a scan reports "0 reads" the breakdown here tells you
        # which step is rejecting frames (gameplay-screen check,
        # overlay-open check, or KDA digit reading).  Reset at the
        # top of every ``detect()`` call.
        self._fail_counts: dict = {
            "gameplay_fail": 0,
            "overlay_open": 0,
            "kda_fail": 0,
        }

        # Rate-limit clock for the optional failure_event_callback
        # so a long stretch of unreadable frames doesn't flood the
        # dashboard.  See _read_from_image for the gate.
        self._last_failure_push_at: float = 0.0

    # --- Public API --------------------------------------------------------

    def detect(self, video_path) -> list[dict]:
        """Scan one video, return events in chronological order.

        Raises VodDetectorError for fatal problems.  Returns [] for a
        video that simply contained no detectable events (e.g. a menu
        recording).
        """
        video_path = str(video_path)
        t_start_scan = time.time()

        # Reset per-scan god identification state.  detect() can be called
        # back-to-back on multiple videos via a shared detector instance
        # (e.g. ProcessPoolExecutor worker reuse, batch CLI), so each
        # call must start with empty gods_seen + a fresh confirm counter.
        self.gods_seen = []
        self._gods_seen_set = set()
        self._god_confirm_name = None
        self._god_confirm_count = 0
        self._god_match_counts = {}
        # Re-arm the per-frame god check for the new scan. _check_god
        # disarms itself after the first confirmation to save CPU; we
        # need a clean slate here.
        self._god_check_armed = True
        self._fail_counts = {
            "gameplay_fail": 0,
            "overlay_open": 0,
            "kda_fail": 0,
        }
        self._ensure_god_matcher()

        duration, width, height = self._probe_video_info(video_path)
        if duration is None or duration <= 0:
            raise VodDetectorError(f"Could not probe duration of {video_path}")
        # Stash for failure-event tagging — the failure callback formats
        # this string into the dashboard payload so the user can tell a
        # 4K alignment issue (which would shouldn't happen, since
        # _coarse_frame_stream auto-inserts a scale=1920:1080 filter)
        # from a content issue (lobby/cinematic frame slipping through).
        self._current_source_resolution = (
            f"{width}x{height}" if width and height else None
        )

        if self.opts.verbose:
            self.log.info(
                f"Scanning {video_path} (duration: {duration:.1f}s, "
                f"coarse={self.opts.coarse_interval}s, "
                f"resolution={width}x{height})"
            )

        events: list[dict] = []

        # Running state during the coarse scan.
        prev_kda: Optional[tuple[int, int, int]] = None
        prev_t = 0.0
        # Baseline-validation state: holds the candidate baseline being
        # confirmed and how many consecutive reads have matched it.
        # The first read that survives the plausibility cap becomes the
        # candidate; once we have BASELINE_REQUIRED_READS consecutive
        # matches it's committed as prev_kda. See the prev_kda is None
        # branch below.
        #
        # ``baseline_candidate_t`` is the timestamp of the first sample
        # that produced the current candidate, used by the
        # "kill during baseline confirmation" recovery path so it can
        # bound the refinement window (see the prev_kda is None branch).
        baseline_candidate: Optional[tuple[int, int, int]] = None
        baseline_candidate_count = 0
        baseline_candidate_t = 0.0

        # Multi-kill timing & first-blood tracking.  These reset when we
        # observe a KDA decrease (the video likely contains multiple
        # matches; each gets its own first-blood).
        first_kill_emitted = False
        kill_timestamps: list[float] = []

        samples_taken = 0
        samples_processed = 0
        successful_reads = 0

        # Lobby / post-match accounting.  ``seen_gameplay`` flips the
        # first time is_gameplay_screen passes and never flips back — we
        # only use it to know whether to sparse-sample (lobby) or
        # stop-after-N-misses (post-match).
        seen_gameplay = False
        lobby_skip_enabled = bool(getattr(self.opts, "lobby_skip", True))
        miss_streak = 0

        # Progress heartbeat — fire ~every 10% of the video so the caller
        # can show forward motion even on a silent run.  Cap minimum stride
        # at 15s (won't spam short clips) and maximum at 120s (won't wait
        # forever on a 2-hour recording).
        progress_stride = max(15.0, min(120.0, duration / 10.0))
        next_progress_t = progress_stride
        progress_cb = getattr(self.opts, "progress_callback", None)

        # Iterate the streaming coarse-frame source.  ``t`` is driven by
        # the generator, not incremented inside the loop.
        for t, img in self._coarse_frame_stream(
            video_path, duration, width, height
        ):
            samples_taken += 1

            # Heartbeat at stride boundaries, regardless of read outcome.
            if progress_cb is not None and t >= next_progress_t:
                try:
                    progress_cb(t, duration)
                except Exception:
                    pass  # progress display errors must never abort a scan
                # Skip forward multiple strides if we fell behind (e.g.
                # a dense run where each frame took longer than one
                # stride worth of wall time).
                while next_progress_t <= t:
                    next_progress_t += progress_stride

            # Lobby skip: before we've ever seen gameplay, only process
            # 1 in LOBBY_STRIDE samples.  The ffmpeg frame still lands
            # here (we can't cheaply tell it to emit sparser frames
            # conditionally), but we skip the numpy/PIL conversion and
            # the scene-classifier calls, which is where most of the
            # per-sample cost lives.
            if (
                lobby_skip_enabled
                and not seen_gameplay
                and samples_taken % LOBBY_STRIDE != 1
            ):
                continue

            samples_processed += 1
            kda = (
                self._read_from_image(img, video_t=t)
                if img is not None else None
            )

            if kda is None:
                if seen_gameplay:
                    miss_streak += 1
                    if miss_streak >= POST_MATCH_STOP_AFTER:
                        if self.opts.verbose:
                            self.log.info(
                                f"  t={t:.1f}s — {miss_streak} consecutive "
                                f"non-gameplay samples; stopping scan early "
                                f"(post-match)."
                            )
                        break
                if self.opts.verbose and samples_taken % 20 == 1:
                    self.log.info(f"  t={t:.1f}s — no read")
                continue

            miss_streak = 0
            if not seen_gameplay:
                seen_gameplay = True
                # First confirmed gameplay frame.  Discard any god-ID
                # state that built up during lobby / god-select —
                # those screens show portrait icons in the same region
                # we read from in-match, so the matcher can perfectly
                # well "confirm" a god you were just hovering over in
                # the picker.  Resetting here means only post-match-
                # start god IDs accumulate into ``gods_seen``.  The
                # current sample is also a gameplay frame, so its
                # _check_god call (which already ran above) gets the
                # confirm counter back to 1 right after the reset
                # and the 3-frame rule resumes from there.
                self.gods_seen = []
                self._gods_seen_set = set()
                self._god_confirm_name = None
                self._god_confirm_count = 0
                # Match-count tracking also resets — anything counted
                # during lobby could include the picker's god rotation
                # and would skew the end-of-scan frequency filter.
                self._god_match_counts = {}
                if self.opts.verbose:
                    self.log.info(
                        f"  t={t:.1f}s — first gameplay frame; "
                        f"switching to full-rate scanning "
                        f"(god-ID state reset to discard any lobby "
                        f"false positives)"
                    )
            successful_reads += 1

            if prev_kda is None:
                # Initial baseline candidate. To prevent a single OCR
                # misread from poisoning the rest of the scan (e.g.
                # icon-as-digit producing K=70), we require:
                #   1. The candidate must pass the plausibility cap
                #      (K, D, A each <= BASELINE_MAX_FIELD and
                #      K+D+A <= BASELINE_MAX_SUM).
                #   2. BASELINE_REQUIRED_READS consecutive identical
                #      reads must agree before commit.
                # Misreads almost never repeat the exact same garbage
                # twice in a row, so the second check kills most
                # cascade scenarios.

                # Plausibility check first.
                if (max(kda) > BASELINE_MAX_FIELD
                        or sum(kda) > BASELINE_MAX_SUM):
                    self.log.warning(
                        f"  t={t:.1f}s — implausible baseline candidate "
                        f"K={kda[0]}/D={kda[1]}/A={kda[2]} "
                        f"(max field >{BASELINE_MAX_FIELD} or "
                        f"sum >{BASELINE_MAX_SUM}); likely OCR misread "
                        f"(icon read as digit) — skipping and waiting "
                        f"for a sane initial frame."
                    )
                    self._commit_sample(trustworthy=False)
                    self._push_kda_rejection(
                        img, t, None, kda, "implausible_baseline",
                    )
                    # Don't update baseline_candidate — keep looking.
                    continue

                # First-ever read of the scan — seed the candidate.
                if baseline_candidate is None:
                    baseline_candidate = kda
                    baseline_candidate_count = 1
                    baseline_candidate_t = t
                    if self.opts.verbose:
                        self.log.info(
                            f"  t={t:.1f}s — baseline candidate "
                            f"K={kda[0]}/D={kda[1]}/A={kda[2]} "
                            f"(needs {BASELINE_REQUIRED_READS - 1} "
                            f"more identical read(s) to commit)"
                        )
                    self._commit_sample(trustworthy=False)
                    continue

                # Same read as the running candidate — accumulate toward
                # confirmation.
                if kda == baseline_candidate:
                    baseline_candidate_count += 1
                    if baseline_candidate_count < BASELINE_REQUIRED_READS:
                        if self.opts.verbose:
                            self.log.info(
                                f"  t={t:.1f}s — baseline candidate confirm "
                                f"{baseline_candidate_count}/"
                                f"{BASELINE_REQUIRED_READS} "
                                f"K={kda[0]}/D={kda[1]}/A={kda[2]}"
                            )
                        self._commit_sample(trustworthy=False)
                        continue

                    # Commit. Both plausibility + consecutive checks passed.
                    self._commit_sample(trustworthy=True)
                    prev_kda = kda
                    prev_t = t
                    if self.opts.verbose:
                        self.log.info(
                            f"  t={t:.1f}s — baseline committed "
                            f"K={kda[0]}/D={kda[1]}/A={kda[2]} "
                            f"(confirmed over {BASELINE_REQUIRED_READS} reads)"
                        )
                    continue

                # Disagreement between consecutive reads while baseline
                # is still un-confirmed.  Before declaring this a
                # mismatch and resetting the candidate, check whether
                # the new read is a sane positive monotonic delta from
                # the candidate.  If so, the most likely explanation is
                # that a real event (kill / death / assist) happened
                # inside the baseline-confirmation window — the
                # candidate IS the pre-event baseline, the new read IS
                # the post-event KDA.
                #
                # Without this branch, the detector silently swallows
                # every event that happens in the first ~coarse_interval
                # seconds of a clip — exactly the symmetric counterpart
                # to the end-of-clip blind spot fixed in
                # ``_coarse_frame_stream_seek``.  Together they close
                # both ends of the recording.
                #
                # Safety:
                #   * delta must be monotonic non-negative on all three
                #     fields AND positive on at least one (KDA never
                #     decreases mid-match, and a real event always
                #     bumps something).
                #   * delta on every field must stay under the same
                #     ``max_jump`` budget the live path uses — anything
                #     larger looks like an OCR misread.
                #   * the new read itself must pass the plausibility
                #     cap, so an icon-as-digit blowup can't bootstrap a
                #     bogus event sequence.
                elapsed = max(0.0, t - baseline_candidate_t)
                max_jump = int(
                    MAX_KDA_JUMP_BASE + elapsed * MAX_KDA_JUMP_PER_SEC
                )
                deltas = [kda[i] - baseline_candidate[i] for i in range(3)]
                positive_monotonic = (
                    all(d >= 0 for d in deltas)
                    and any(d > 0 for d in deltas)
                    and all(d <= max_jump for d in deltas)
                )
                new_read_plausible = (
                    max(kda) <= BASELINE_MAX_FIELD
                    and sum(kda) <= BASELINE_MAX_SUM
                )

                if positive_monotonic and new_read_plausible:
                    # Promote candidate to committed baseline at its
                    # first-seen time, then emit the delta as real
                    # events via the standard refinement path.  Note:
                    # we set prev_kda = kda (the post-event state)
                    # rather than baseline_candidate, because the
                    # detector loop continues from here and any future
                    # changes are measured against this new committed
                    # value.
                    self._commit_sample(trustworthy=True)
                    if self.opts.verbose:
                        self.log.info(
                            f"  t={t:.1f}s — baseline candidate "
                            f"K={baseline_candidate[0]}/"
                            f"D={baseline_candidate[1]}/"
                            f"A={baseline_candidate[2]} → "
                            f"K={kda[0]}/D={kda[1]}/A={kda[2]} during "
                            f"confirm window (Δ={tuple(deltas)}, "
                            f"elapsed {elapsed:.1f}s, max_jump={max_jump}); "
                            f"treating candidate as committed baseline "
                            f"and refining the delta as real event(s)."
                        )
                    new_events, first_kill_emitted = (
                        self._emit_events_for_interval(
                            video_path,
                            baseline_candidate_t,
                            baseline_candidate,
                            t,
                            kda,
                            first_kill_emitted,
                            kill_timestamps,
                        )
                    )
                    events.extend(new_events)
                    prev_kda = kda
                    prev_t = t
                    continue

                # Otherwise: this is a genuinely different read.  Treat
                # the previous candidate as untrusted and start over
                # with this read as the new candidate.  Same warning
                # path as the original "two non-matching reads" case.
                if self.opts.verbose:
                    self.log.info(
                        f"  t={t:.1f}s — baseline candidate reset "
                        f"K={baseline_candidate[0]}/"
                        f"D={baseline_candidate[1]}/"
                        f"A={baseline_candidate[2]} → "
                        f"K={kda[0]}/D={kda[1]}/A={kda[2]} (not a sane "
                        f"positive delta — Δ={tuple(deltas)})"
                    )
                baseline_candidate = kda
                baseline_candidate_count = 1
                baseline_candidate_t = t
                self._commit_sample(trustworthy=False)
                continue

            # KDA only goes up within a single match.  A decrease means
            # either an OCR misread or a new match started inside the
            # same recording (James runs long OBS sessions).
            #
            # Every Smite 2 match ALWAYS starts at 0/0/0, so we only
            # accept "(non-zero) → (0, 0, 0)" as a real new-match signal
            # and re-baseline then.  A partial decrease on just one
            # component (e.g. K drops while D is mid-read) is almost
            # certainly an OCR misread — we treat it as a bad sample and
            # skip without updating prev_kda, so the next legitimate read
            # still compares against the last known-good baseline.
            decreased = any(kda[i] < prev_kda[i] for i in range(3))
            if decreased and kda == (0, 0, 0) and prev_kda != (0, 0, 0):
                # Clean match-reset to 0/0/0 — trustworthy baseline for
                # the next match.
                self._commit_sample(trustworthy=True)
                if self.opts.verbose:
                    self.log.info(
                        f"  t={t:.1f}s — KDA reset to 0/0/0 (was {prev_kda}), "
                        f"re-baselining for new match"
                    )
                prev_kda = kda
                prev_t = t
                first_kill_emitted = False
                kill_timestamps = []
                # New match → re-enable per-frame god identification so
                # we can pick up a different god this match. Without this
                # the post-confirmation disarm would persist for the rest
                # of the recording.
                self.rearm_god_check()
                continue
            if decreased:
                # Partial decrease — OCR misread, don't trust this sample
                # and don't enroll its digits (which would poison the
                # template library if this ever fires with --enroll).
                self._dump_misread(
                    img, t, prev_kda, kda,
                    reason="partial_decrease",
                    video_path=video_path,
                )
                self._commit_sample(trustworthy=False)
                self.log.warning(
                    f"  t={t:.1f}s — partial KDA decrease "
                    f"{prev_kda} → {kda} (not a clean 0/0/0 reset) "
                    f"— treating as misread, skipping"
                )
                self._push_kda_rejection(
                    img, t, prev_kda, kda, "partial_decrease",
                )
                continue

            if kda == prev_kda:
                # No change — advance.  The re-read agrees with the
                # previously-confirmed KDA, so these digit crops are as
                # trustworthy as the last committed sample.
                self._commit_sample(trustworthy=True)
                prev_t = t
                continue

            # Sanity check: reject implausibly large jumps.  A 5s coarse
            # interval allows up to ~6 extra kills/deaths/assists;
            # anything larger is almost certainly an OCR misread (most
            # commonly the match-end scoreboard, a cinematic, or a
            # transient where the reader picked up the wrong digits).
            # We deliberately DO NOT update prev_kda / prev_t here —
            # that way, when the next legitimate read comes in, we
            # compare against the last known-good KDA instead of a
            # corrupted baseline.
            time_elapsed = max(0.0, t - prev_t)
            max_jump = int(
                MAX_KDA_JUMP_BASE + time_elapsed * MAX_KDA_JUMP_PER_SEC
            )
            dk = kda[0] - prev_kda[0]
            dd = kda[1] - prev_kda[1]
            da = kda[2] - prev_kda[2]
            if dk > max_jump or dd > max_jump or da > max_jump:
                # Always log rejections — this is a signal, not noise.
                # If this fires a lot, something's wrong with the reader
                # or a recording contains a weird overlay we don't skip.
                # These digits absolutely must not enroll (e.g. "3/4/97"
                # from today's CUDA scan at t=935s).
                self._dump_misread(
                    img, t, prev_kda, kda,
                    reason="max_jump",
                    video_path=video_path,
                )
                self._commit_sample(trustworthy=False)
                self._push_kda_rejection(
                    img, t, prev_kda, kda, "max_jump",
                )
                self.log.warning(
                    f"  t={t:.1f}s — KDA jump too large "
                    f"({prev_kda} → {kda}, Δ={dk}/{dd}/{da}, "
                    f"max={max_jump} after {time_elapsed:.1f}s) "
                    f"— treating as misread, skipping"
                )
                continue

            # Something changed in [prev_t, t].  Refine each field that
            # moved and emit per-event timestamps.
            if self.opts.verbose:
                self.log.info(
                    f"  t={t:.1f}s — change {prev_kda} → {kda}, refining "
                    f"[{prev_t:.1f}, {t:.1f}]"
                )
            # Commit this sample as trustworthy BEFORE refinement — the
            # binary search will overwrite the reader's stashed crops
            # with mid-interval samples that may be transient.
            self._commit_sample(trustworthy=True)
            new_events, first_kill_emitted = self._emit_events_for_interval(
                video_path,
                prev_t,
                prev_kda,
                t,
                kda,
                first_kill_emitted,
                kill_timestamps,
            )
            events.extend(new_events)

            prev_kda = kda
            prev_t = t

        # Sort chronologically, then merge overlapping windows, then
        # apply the include-flag filter.  Merge-before-filter is
        # important: a kill+death trade with include_deaths=False
        # should still surface as a single kill-anchored clip that
        # covers the death context, not a lone kill event.
        # Lobby-only recordings: if we never saw a single confirmed
        # gameplay frame, discard any god identifications that may
        # have accumulated during lobby — there's no in-match content
        # to corroborate them and they'd just route the recording to
        # the wrong god folder.  Pre-match recordings legitimately
        # belong in either ``unknown/`` or (via the interactive
        # sorter) ``Other/``.
        if not seen_gameplay and self.gods_seen:
            if self.opts.verbose:
                self.log.info(
                    f"No gameplay frames confirmed; clearing "
                    f"{len(self.gods_seen)} lobby-only god ID(s) so "
                    f"the recording routes to unknown/."
                )
            self.gods_seen = []
            self._gods_seen_set = set()
        elif len(self.gods_seen) > 1 and self._god_match_counts:
            # Multi-god gods_seen on a real-gameplay scan: filter out
            # any god whose total match-frame count is dwarfed by the
            # most-common god's count.  Catches the "transient false
            # positive sustained for ~3 frames during an ability
            # animation" case that the 3-consecutive rule alone can't
            # reject.  Threshold is a relative one (10% of the top
            # god's count) so legitimate back-to-back matches with
            # different gods both survive.
            counts = self._god_match_counts
            max_count = max(counts.get(g, 0) for g in self.gods_seen)
            # Floor of 1 keeps the comparison meaningful even on tiny
            # recordings; the 3-consecutive rule already requires at
            # least 3 frames to confirm a god in the first place.
            threshold = max(1, int(max_count * 0.10))
            kept: list[str] = []
            dropped: list[tuple[str, int]] = []
            for god in self.gods_seen:
                c = counts.get(god, 0)
                if c >= threshold:
                    kept.append(god)
                else:
                    dropped.append((god, c))
            if dropped and self.opts.verbose:
                detail = ", ".join(
                    f"{g} ({c}/{max_count} frames, "
                    f"{100 * c / max_count:.1f}%)"
                    for g, c in dropped
                )
                self.log.info(
                    f"Filtered out likely false-positive god ID(s): "
                    f"{detail} — below 10% of dominant god's frame count."
                )
            self.gods_seen = kept
            self._gods_seen_set = set(kept)

        events.sort(key=lambda e: e["timestamp_sec"])
        raw_count = len(events)
        if getattr(self.opts, "merge_overlaps", True):
            events = self._merge_overlapping_events(events)
        merged_collapsed = raw_count - len(events)

        filtered = [
            e for e in events
            if e["type"] == "kill"
            or (e["type"] == "death" and self.opts.include_deaths)
            or (e["type"] == "assist" and self.opts.include_assists)
        ]

        elapsed = time.time() - t_start_scan
        if self.opts.verbose:
            if merged_collapsed > 0:
                events_line = (
                    f"{raw_count} raw events → "
                    f"{len(events)} after merging "
                    f"{merged_collapsed} overlap(s) → "
                    f"{len(filtered)} kept"
                )
            else:
                events_line = (
                    f"{raw_count} raw events → {len(filtered)} kept"
                )
            self.log.info(
                f"Scan complete in {elapsed:.1f}s: "
                f"{samples_taken} samples "
                f"({samples_processed} processed, "
                f"{samples_taken - samples_processed} lobby-skipped), "
                f"{successful_reads} reads, "
                f"{events_line}"
            )
            # Surface the rejection breakdown so "0 reads" runs are
            # diagnosable without further instrumentation.  Most useful
            # when nearly all processed samples failed; identifies
            # which pipeline step is the bottleneck.
            total_fails = sum(self._fail_counts.values())
            if total_fails > 0 and successful_reads == 0:
                self.log.info(
                    f"  read failures: "
                    f"gameplay-check failed: {self._fail_counts['gameplay_fail']}, "
                    f"overlay-open: {self._fail_counts['overlay_open']}, "
                    f"kda-read failed: {self._fail_counts['kda_fail']}"
                )

        return filtered

    # --- God identification ------------------------------------------------

    def _ensure_god_matcher(self) -> None:
        """Lazy-init the GodMatcher on first detect() call.

        Done lazily so ``import tools.vod_detector`` doesn't pay the icon
        library load cost (and so a missing ``data/god_icons`` directory
        doesn't crash anything that just imports this module).  If the
        matcher fails to load (no icons, OpenCV missing, etc.) we leave
        ``self._god_matcher = None`` and the rest of the scan path
        silently no-ops on god identification — events still detect.
        """
        if not getattr(self.opts, "enable_god_detection", True):
            self._god_matcher = None
            return
        if self._god_matcher_init_attempted:
            return
        self._god_matcher_init_attempted = True

        # Resolve the overlay-icons directory.  ``None`` means "use the
        # repo's default location"; setting it to a Path that doesn't
        # exist is non-fatal (load_icons() just skips the overlay step).
        # Pass an empty Path("") if a caller really wants overlay
        # matching disabled — we treat that explicitly.
        repo_root = Path(__file__).resolve().parent.parent
        overlay_dir = self.opts.god_overlay_icons_dir
        if overlay_dir is None:
            overlay_dir = repo_root / "Custom God Icons"
        elif str(overlay_dir) == "":
            overlay_dir = None  # explicit opt-out

        # Same resolution rules for the reference-icons directory.
        reference_dir = self.opts.god_reference_icons_dir
        if reference_dir is None:
            reference_dir = repo_root / "Portrait_Source"
        elif str(reference_dir) == "":
            reference_dir = None  # explicit opt-out

        try:
            from core.god_matcher import GodMatcher
            matcher = GodMatcher(
                icons_dir=self.opts.god_icons_dir,
                overlay_icons_dir=overlay_dir,
                reference_icons_dir=reference_dir,
            )
            if matcher.load_icons():
                self._god_matcher = matcher
            else:
                self.log.warning(
                    "GodMatcher loaded zero icons — gods_seen will be empty. "
                    "Run download_god_icons.py to populate data/god_icons/."
                )
                self._god_matcher = None
        except Exception as e:
            self.log.warning(
                f"GodMatcher init failed: {type(e).__name__}: {e} — "
                f"continuing without god identification"
            )
            self._god_matcher = None

    def _check_god(
        self,
        img: Image.Image,
        crop_origin: tuple[int, int],
    ) -> None:
        """Run portrait identification on one gameplay frame.

        Performance gate (May 2026): once a god has been confirmed for
        the current match (added to ``gods_seen``), we skip the per-
        frame histogram comparison until ``detect()`` re-arms us via
        ``rearm_god_check()`` on a KDA reset (signals new match
        started). Saves ~5ms per frame × hundreds of frames per
        single-match recording.

        Mirrors the live detector's confirmation rule
        (plugins/killdetector.py): a candidate has to be returned by
        ``GodMatcher.identify()`` on ``_GOD_CONFIRM_REQUIRED`` consecutive
        gameplay frames before we accept it.  This rejects lobby /
        cinematic / scoreboard false positives because those screens
        won't consistently match the same god across multiple frames.

        On confirmation the god is appended to ``self.gods_seen`` (if not
        already present) and the confirm counter resets to zero so the
        same loop can pick up a *different* god later in the recording —
        important for multi-match files.

        ``crop_origin`` is forwarded to GodMatcher.identify() so its
        absolute PORTRAIT_REGION coords resolve correctly inside the
        ffmpeg-side HUD-cropped strip we typically feed it.
        """
        if self._god_matcher is None:
            return
        # Skip if we already locked in a god for the current match.
        # detect() will call rearm_god_check() on a 0/0/0 reset to
        # re-enable us for the next match.
        if self._gods_seen_set and not self._god_check_armed:
            return
        try:
            god_name, confidence = self._god_matcher.identify(
                img, crop_origin=crop_origin
            )
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(f"God match error: {e}")
            return

        if god_name is None:
            # No match this frame.  Reset the running confirm candidate so
            # a transient cinematic / overlay frame doesn't keep alive a
            # stale partial confirmation across an unrelated gap.
            self._god_confirm_name = None
            self._god_confirm_count = 0
            return

        # Track per-god frequency for the end-of-scan false-positive
        # filter.  Every accepted matcher result counts once, whether
        # or not it eventually confirms via the 3-consecutive rule.
        self._god_match_counts[god_name] = (
            self._god_match_counts.get(god_name, 0) + 1
        )

        if god_name == self._god_confirm_name:
            self._god_confirm_count += 1
        else:
            self._god_confirm_name = god_name
            self._god_confirm_count = 1

        if self._god_confirm_count >= self._GOD_CONFIRM_REQUIRED:
            if god_name not in self._gods_seen_set:
                self._gods_seen_set.add(god_name)
                self.gods_seen.append(god_name)
                if self.opts.verbose:
                    self.log.info(
                        f"  god identified: {god_name} "
                        f"(confidence: {confidence:.3f}, confirmed over "
                        f"{self._god_confirm_count} frames)"
                    )
            # Disarm per-frame god checking now that we've locked in a
            # god for this match.  Histogram matching is the single
            # most expensive operation per frame (~5ms), and re-running
            # it 300+ times across a match when the answer can't
            # change is the biggest waste in the scan loop.  detect()
            # re-arms us via rearm_god_check() on a clean 0/0/0 reset.
            self._god_check_armed = False
            # Reset so a subsequent match in the same recording can
            # confirm a different god from scratch.  (If the player
            # picks the same god twice back-to-back we'll just re-confirm
            # and the set-dedupe quietly absorbs it.)
            self._god_confirm_name = None
            self._god_confirm_count = 0

    def rearm_god_check(self) -> None:
        """Re-enable per-frame god identification for the next match.

        Called by detect() on a clean 0/0/0 KDA reset (the only
        unambiguous "new match started" signal in a recording that
        spans multiple matches).  Without this the disarm in
        _check_god would lock us out of identifying the second god
        for the rest of the recording.
        """
        self._god_check_armed = True
        self._god_confirm_name = None
        self._god_confirm_count = 0

    # --- Misread diagnostics ----------------------------------------------

    def _dump_misread(
        self,
        img: Image.Image,
        t: float,
        prev_kda: Optional[tuple[int, int, int]],
        kda: tuple[int, int, int],
        reason: str,
        video_path: str,
    ) -> None:
        """Save the offending frame + KDA crop to disk for diagnosis.

        No-op unless ``self.opts.misread_debug_dir`` is set.  Output goes
        into ``<misread_debug_dir>/<video_basename>/`` so misreads from
        different scans never collide and per-recording cleanup is easy.

        Filenames encode timestamp, reason, and before/after KDA so a
        glance at the folder shows what's stuck without opening anything:

            t0935.0_max_jump_prev3-4-0_read3-4-97.png        ← full frame
            t0935.0_max_jump_prev3-4-0_read3-4-97.kda.png    ← raw 125x22 crop

        Best-effort — any I/O exception is caught and logged so a write
        failure can never abort a scan.
        """
        if not self.opts.misread_debug_dir:
            return
        try:
            base = Path(video_path).stem
            out_dir = Path(self.opts.misread_debug_dir) / base
            out_dir.mkdir(parents=True, exist_ok=True)

            pk = (
                f"{prev_kda[0]}-{prev_kda[1]}-{prev_kda[2]}"
                if prev_kda is not None
                else "none"
            )
            nk = f"{kda[0]}-{kda[1]}-{kda[2]}"
            # Pad timestamp so directory listings sort chronologically.
            # Width 6 covers up to ~9999.9s (~2h45m), which is far longer
            # than any realistic single recording.
            stem = f"t{t:06.1f}_{reason}_prev{pk}_read{nk}"

            # 1) Full frame (whatever ``img`` actually is — may already be
            #    the ffmpeg-cropped HUD strip; that's still informative).
            img.save(str(out_dir / f"{stem}.png"))

            # 2) The KDA region crop the reader actually saw.  Translate
            #    KDA_REGION by the same crop_origin we'd use for reading
            #    so the slice lines up regardless of whether ``img`` is
            #    the cropped strip or a full 1920x1080 frame.
            origin = (
                (VOD_CROP_X, VOD_CROP_Y)
                if img.size == (VOD_CROP_W, VOD_CROP_H)
                else (0, 0)
            )
            ox, oy = origin
            x1, y1, x2, y2 = KDA_REGION
            kda_crop = img.crop((x1 - ox, y1 - oy, x2 - ox, y2 - oy))
            kda_crop.save(str(out_dir / f"{stem}.kda.png"))
        except Exception as e:
            # Diagnostic dumping must never abort a scan.
            self.log.warning(
                f"misread dump failed at t={t:.1f}s: {type(e).__name__}: {e}"
            )

    # --- Sample commit -----------------------------------------------------

    def _commit_sample(self, trustworthy: bool) -> None:
        """Commit or discard the reader's last-read digit crops.

        Called once per main-loop sample after the KDA-level sanity
        guards have decided whether this sample is real or a misread.

        ``trustworthy=True`` + ``opts.enroll_templates=True`` → enroll.
        Anything else → discard (safely idempotent).

        Refinement reads never reach this helper — they always discard
        inline in ``_read_at`` / ``_read_with_offsets``.
        """
        if trustworthy and self.opts.enroll_templates:
            self.reader.enroll_last_read()
        else:
            self.reader.discard_last_read()

    # --- Event merging -----------------------------------------------------
    #
    # When a kill is followed closely by a death (or vice versa) — the
    # classic "traded" fight — the two events' pre/post clip windows
    # typically overlap.  Importing them into Vegas as two separate
    # clips produces back-to-back copies of the same footage, and the
    # viewer sees the same moment twice.
    #
    # We collapse overlapping windows into a single wider event so the
    # downstream importer gets one clip that naturally covers both
    # moments.  The highest-priority event in each group becomes the
    # anchor (kill > death > assist), which also means the include-
    # filter still keeps a merged kill+death group even in kills-only
    # mode — the user still sees the death context around the kill.

    # Ordering used to pick an anchor within a merge group.  Higher
    # wins; a kill always beats a death, which always beats an assist.
    _MERGE_PRIORITY = {"kill": 3, "death": 2, "assist": 1}

    def _merge_overlapping_events(self, events: list[dict]) -> list[dict]:
        """Collapse events whose pre/post time windows overlap.

        ``events`` must be sorted by ``timestamp_sec``.  Groups chain:
        if A+B overlap and B+C overlap, all three collapse even if A
        and C don't touch on their own.

        Returns a new list; does not mutate inputs.
        """
        if len(events) < 2:
            return [dict(e) for e in events]

        # Walk once, greedy-grouping by window overlap.
        groups: list[list[dict]] = [[events[0]]]
        cur_end = events[0]["timestamp_sec"] + events[0]["post_sec"]
        for ev in events[1:]:
            ev_start = ev["timestamp_sec"] - ev["pre_sec"]
            ev_end = ev["timestamp_sec"] + ev["post_sec"]
            if ev_start <= cur_end:
                groups[-1].append(ev)
                cur_end = max(cur_end, ev_end)
            else:
                groups.append([ev])
                cur_end = ev_end

        merged: list[dict] = []
        for group in groups:
            if len(group) == 1:
                merged.append(dict(group[0]))
                continue

            # Anchor on the highest-priority type in the group so the
            # output type reads correctly (a kill+death trade should
            # surface as a kill event, not a death).
            anchor = max(
                group,
                key=lambda e: self._MERGE_PRIORITY.get(e["type"], 0),
            )
            group_start = min(
                e["timestamp_sec"] - e["pre_sec"] for e in group
            )
            group_end = max(
                e["timestamp_sec"] + e["post_sec"] for e in group
            )

            new_ev = dict(anchor)
            new_ev["pre_sec"] = round(
                anchor["timestamp_sec"] - group_start, 1
            )
            new_ev["post_sec"] = round(
                group_end - anchor["timestamp_sec"], 1
            )
            types_chrono = [
                e["type"]
                for e in sorted(group, key=lambda e: e["timestamp_sec"])
            ]
            new_ev["note"] = self._format_merge_note(
                anchor.get("note", ""), types_chrono
            )
            merged.append(new_ev)

        return merged

    @staticmethod
    def _format_merge_note(anchor_note: str, types_chrono: list[str]) -> str:
        """Build a human-readable note for a merged event group.

        Single-event groups keep their original note unchanged.  For
        multi-event groups, join the chronological type sequence with
        " + " and prepend the anchor's original note in parentheses if
        it had one (e.g. "first blood (kill + death)").
        """
        if len(types_chrono) == 1:
            return anchor_note
        summary = " + ".join(types_chrono)
        if anchor_note:
            return f"{anchor_note} ({summary})"
        return summary

    # --- Refinement --------------------------------------------------------

    def _emit_events_for_interval(
        self,
        video_path: str,
        lo_t: float,
        lo_kda: tuple[int, int, int],
        hi_t: float,
        hi_kda: tuple[int, int, int],
        first_kill_emitted: bool,
        kill_timestamps: list[float],
    ) -> tuple[list[dict], bool]:
        """Refine changes in (lo_t, hi_t] and return the resulting events.

        Mutates ``kill_timestamps`` in-place with any new kill timestamps
        so subsequent intervals classify multi-kills against them.

        Returns (events, updated_first_kill_emitted).
        """
        events: list[dict] = []

        # --- Timestamp sourcing ---------------------------------------------
        #
        # Normal path: binary-search for each field's precise change time.
        # --no-refine path: skip the search and spread N events evenly
        # across the coarse interval, widening pre_sec / post_sec so the
        # resulting clip still comfortably covers the real moment.
        no_refine = bool(getattr(self.opts, "no_refine", False))
        pre_sec = NO_REFINE_PRE_SEC if no_refine else DEFAULT_PRE_SEC
        coarse_post_sec = NO_REFINE_POST_SEC if no_refine else DEFAULT_POST_SEC

        def _emit_times(field_idx: int, delta: int) -> list[float]:
            """Pick timestamps for ``delta`` events in (lo_t, hi_t]."""
            if delta <= 0:
                return []
            if no_refine:
                # Spread evenly at quarter-points inside the interval.
                # For delta=1 this is hi_t (newest confirmed read) which
                # is what we'd typically land near anyway; for delta=2
                # it's 2/3 and 3/3; for delta=3 it's 2/4, 3/4, 4/4; etc.
                span = hi_t - lo_t
                return [lo_t + span * (i + 1) / (delta + 1)
                        for i in range(delta)]
            return self._refine_field(
                video_path, lo_t, lo_kda[field_idx],
                hi_t, hi_kda[field_idx], field_idx=field_idx
            )

        # Kills first — that's the primary signal + needs multi-kill logic.
        if hi_kda[0] > lo_kda[0]:
            kill_times = _emit_times(0, hi_kda[0] - lo_kda[0])
            for ts in kill_times:
                # Trim the window to kills still active at this timestamp.
                kill_timestamps[:] = [
                    k for k in kill_timestamps if ts - k <= MULTIKILL_WINDOW
                ]
                kill_timestamps.append(ts)
                kill_type = _classify_multikill(len(kill_timestamps))

                note = ""
                if not first_kill_emitted:
                    note = "first blood"
                    first_kill_emitted = True
                elif kill_type != "player_kill":
                    note = kill_type.replace("_", " ")

                post = (
                    MULTIKILL_POST_SEC
                    if kill_type != "player_kill"
                    else coarse_post_sec
                )
                events.append({
                    "timestamp_sec": round(ts, 1),
                    "type": "kill",
                    "note": note,
                    "pre_sec": pre_sec,
                    "post_sec": post,
                })

        if hi_kda[1] > lo_kda[1]:
            death_times = _emit_times(1, hi_kda[1] - lo_kda[1])
            for ts in death_times:
                events.append({
                    "timestamp_sec": round(ts, 1),
                    "type": "death",
                    "note": "",
                    "pre_sec": pre_sec,
                    "post_sec": coarse_post_sec,
                })

        if hi_kda[2] > lo_kda[2]:
            assist_times = _emit_times(2, hi_kda[2] - lo_kda[2])
            for ts in assist_times:
                events.append({
                    "timestamp_sec": round(ts, 1),
                    "type": "assist",
                    "note": "",
                    "pre_sec": pre_sec,
                    "post_sec": coarse_post_sec,
                })

        return events, first_kill_emitted

    def _refine_field(
        self,
        video_path: str,
        lo_t: float,
        lo_val: int,
        hi_t: float,
        hi_val: int,
        field_idx: int,
    ) -> list[float]:
        """Find each individual increment timestamp for one KDA field
        inside (lo_t, hi_t].  Uses sequential binary searches: find when
        the field first became ``lo_val + 1``, then search the remainder
        for ``+2``, and so on.

        Returns a list of timestamps with ``hi_val - lo_val`` entries.
        """
        timestamps: list[float] = []
        search_lo_t = lo_t
        search_lo_val = lo_val

        remaining = hi_val - lo_val
        # Defense in depth: even with the per-sample max-jump check, cap
        # how many events we're willing to emit for a single coarse
        # interval.  A legitimate penta kill is 5 within 10 seconds, so
        # anything more than MAX_EVENTS_PER_INTERVAL in one ≤5 s coarse
        # window is almost certainly a misread we didn't catch upstream.
        if remaining > MAX_EVENTS_PER_INTERVAL:
            self.log.warning(
                f"  Capping refinement at {MAX_EVENTS_PER_INTERVAL} "
                f"events (requested {remaining} in "
                f"[{lo_t:.1f}, {hi_t:.1f}], field_idx={field_idx}) "
                f"— likely misread upstream."
            )
            remaining = MAX_EVENTS_PER_INTERVAL

        for _ in range(remaining):
            ts = self._binary_search_single(
                video_path, search_lo_t, search_lo_val, hi_t, field_idx
            )
            timestamps.append(ts)
            search_lo_t = ts
            search_lo_val += 1

        return timestamps

    def _binary_search_single(
        self,
        video_path: str,
        lo_t: float,
        lo_val: int,
        hi_t: float,
        field_idx: int,
    ) -> float:
        """Find the timestamp at which ``kda[field_idx]`` first exceeded
        ``lo_val`` inside ``[lo_t, hi_t]``.  Precision is bounded by
        ``self.opts.refine_precision`` but degrades gracefully if a
        midpoint frame is unreadable.
        """
        precision = self.opts.refine_precision
        while hi_t - lo_t > precision:
            mid_t = (lo_t + hi_t) / 2.0
            # Allow small offsets if the exact midpoint can't be read,
            # but keep them well inside the current interval so they
            # don't cross the change point we're hunting.
            max_off = max(0.0, (hi_t - lo_t) / 4.0)
            kda, actual_t = self._read_with_offsets(
                video_path, mid_t, max_offset=max_off
            )
            if kda is None:
                # Couldn't read anywhere near the midpoint.  Stop
                # refining; emit at the current midpoint with whatever
                # precision we have.
                break
            mid_val = kda[field_idx]
            if mid_val <= lo_val:
                # The change is after the frame we actually read.
                lo_t = actual_t
            else:
                # The change is at or before the frame we actually read.
                hi_t = actual_t
        return round((lo_t + hi_t) / 2.0, 3)

    # --- Frame I/O ---------------------------------------------------------

    def _read_at(
        self, video_path: str, t: float
    ) -> Optional[tuple[int, int, int]]:
        """Extract one frame and try to read KDA from it (no fallback).

        Refinement callers go through here.  The reader's stashed crops
        are always discarded — refinement reads are transient midpoints
        that can legitimately catch a frame mid-digit-tick (the "5"
        half-rendered as the 4→5 transition fires) and we never want
        those to enroll.
        """
        img = self._extract_frame(video_path, t)
        if img is None:
            return None
        kda = self._read_from_image(img)
        if kda is not None:
            self.reader.discard_last_read()
        return kda

    def _read_with_offsets(
        self, video_path: str, t: float, max_offset: float
    ) -> tuple[Optional[tuple[int, int, int]], float]:
        """Try the target time, then a handful of small offsets.

        Returns (kda_tuple_or_None, actual_timestamp_read).
        """
        for off in READ_RETRY_OFFSETS:
            if abs(off) > max_offset + 1e-9:
                continue
            t_try = max(0.0, t + off)
            img = self._extract_frame(video_path, t_try)
            if img is None:
                continue
            kda = self._read_from_image(img)
            if kda is not None:
                # Refinement reads never enroll — see _read_at.
                self.reader.discard_last_read()
                return kda, t_try
        return None, t

    def _read_from_image(
        self, img: Image.Image, video_t: Optional[float] = None
    ) -> Optional[tuple[int, int, int]]:
        """Apply the scene classifier + KDA reader to a PIL frame.

        If the frame is the ffmpeg-cropped HUD strip (matching VOD_CROP_W x
        VOD_CROP_H), translate the reader's region constants by the crop
        origin so they index correctly into the smaller image.  Otherwise
        (full 1920x1080 frame coming from the per-frame fallback path)
        use the default (0, 0) origin.

        ``video_t`` is the timestamp into the source video for the frame
        (seconds).  Only used to tag failure-event payloads; safe to omit.
        """
        origin = (
            (VOD_CROP_X, VOD_CROP_Y)
            if img.size == (VOD_CROP_W, VOD_CROP_H)
            else (0, 0)
        )
        img_array = np.array(img)
        if not self.reader.is_gameplay_screen(img_array, crop_origin=origin):
            self._fail_counts["gameplay_fail"] += 1
            return None
        # We have a confirmed gameplay frame — call the god identifier
        # here regardless of whether the overlay or KDA read fail
        # downstream.  Lobby / god-select false positives are handled
        # structurally in detect() instead: god-ID state is RESET the
        # moment we see the first KDA-readable gameplay frame, so any
        # candidates that accumulated during pre-match lobby get
        # discarded the moment we know we're really in a match.
        self._check_god(img, crop_origin=origin)
        if self.reader.is_overlay_open(img_array, crop_origin=origin):
            self._fail_counts["overlay_open"] += 1
            return None
        kda = self.reader.read_kda(img, crop_origin=origin)
        if kda is None:
            self._fail_counts["kda_fail"] += 1
            # Surface what the reader actually saw to the dashboard.
            # Re-runs the (slower) diagnostic pipeline only on confirmed
            # failures, rate-limited so a long stretch of unreadable
            # frames doesn't flood the dashboard.
            self._push_kda_failure(img, origin, video_t=video_t)
        # NOTE: we deliberately do NOT commit the reader's last-read digit
        # crops here.  The per-digit matcher can successfully label a bad
        # KDA (e.g. "64/0/4" when the ones column glued into the next
        # field, or "3/4/97" when an overlay slipped through) — those get
        # rejected at the KDA level by detect()'s sanity guards.
        # Enrolling them here would poison the template library with
        # misread digits.  Instead, detect() calls _commit_sample() after
        # the guards pass; refinement reads always discard (see _read_at
        # and _read_with_offsets).
        return kda

    def _push_kda_failure(
        self,
        img: Image.Image,
        origin: tuple[int, int],
        video_t: Optional[float] = None,
    ) -> None:
        """Surface a read-time failure (read_kda returned None) to the
        dashboard.  Thin wrapper around :meth:`_push_kda_diagnostic` —
        see that for the actual logic.
        """
        self._push_kda_diagnostic(
            img, origin, video_t=video_t, kind="read_failure",
        )

    def _push_kda_rejection(
        self,
        img: Image.Image,
        video_t: Optional[float],
        prev_kda: Optional[tuple[int, int, int]],
        read_kda: tuple[int, int, int],
        reason: str,
    ) -> None:
        """Surface a post-read rejection (sanity guard tripped) to the
        dashboard so the user can see the actual crop + per-digit OCR
        scoring that produced the bad read.

        ``read_kda`` is the value the OCR produced (and would have been
        emitted if the guards hadn't tripped); ``prev_kda`` is the last
        trusted baseline (or None for an implausible initial read);
        ``reason`` is one of "implausible_baseline", "partial_decrease",
        "max_jump" — matching the labels used in :meth:`detect`.
        """
        origin = (
            (VOD_CROP_X, VOD_CROP_Y)
            if img.size == (VOD_CROP_W, VOD_CROP_H)
            else (0, 0)
        )
        extra = {
            "prev_kda": list(prev_kda) if prev_kda is not None else None,
            "read_kda": list(read_kda),
        }
        self._push_kda_diagnostic(
            img, origin, video_t=video_t,
            kind=f"rejected:{reason}", extra=extra,
        )

    def _push_kda_diagnostic(
        self,
        img: Image.Image,
        origin: tuple[int, int],
        video_t: Optional[float] = None,
        kind: str = "read_failure",
        extra: Optional[dict] = None,
    ) -> None:
        """Generalized diagnostic-push.  Captures the raw KDA strip, the
        binarised 8x view, and the per-digit OCR top-N scoring for any
        frame, then hands the payload to the dashboard callback.

        Used by both:
        - :meth:`_push_kda_failure` — frames where ``read_kda`` returned
          None (the reader gave up).
        - :meth:`_push_kda_rejection` — frames where ``read_kda`` produced
          a value but :meth:`detect` then rejected it (implausible
          baseline, partial decrease, max jump).

        Rate-limited via ``failure_callback_min_interval`` shared with
        :meth:`_push_kda_failure` — we don't want a stretch of
        consecutive rejections drowning the dashboard.  Best-effort:
        any exception (callback errors, PIL/encoding issues, missing
        dependencies) is swallowed so a debug hook can never abort a
        scan.
        """
        cb = getattr(self.opts, "failure_event_callback", None)
        if cb is None:
            return
        now = time.time()
        min_int = float(getattr(
            self.opts, "failure_callback_min_interval", 1.0))
        if now - self._last_failure_push_at < min_int:
            return
        self._last_failure_push_at = now

        try:
            details = self.reader.read_kda_with_details(
                img, crop_origin=origin)
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(f"diagnostic-detail capture failed: {e}")
            return

        # Slim down the per-group OCR info for the dashboard payload —
        # the full ``groups`` structure carries numpy arrays and other
        # non-serializable bits we don't want in JSON.  Keep only what's
        # useful for diagnosis: the digit's best label, its top-3
        # competitor labels with distances, and the margin (how
        # confidently it beat the runner-up).  A low margin on a digit
        # whose verdict was "accepted" is the smoking gun for an
        # ambiguous OCR.
        groups_slim = []
        for g in details.get("groups", []) or []:
            digits_slim = []
            for d in g.get("digits", []) or []:
                top_n = d.get("top_n") or []
                top_n_slim = []
                for tn in top_n[:3]:
                    # tn is typically (label, distance) or similar
                    try:
                        lbl, dist = tn[0], tn[1]
                        top_n_slim.append([str(lbl), float(dist)])
                    except Exception:
                        pass
                digits_slim.append({
                    "best": str(d.get("best", "?")),
                    "margin": float(d.get("margin", 0.0) or 0.0),
                    "verdict": str(d.get("verdict", "?")),
                    "method": str(d.get("method", "?")),
                    "top_n": top_n_slim,
                })
            groups_slim.append({
                "label": str(g.get("label", "?")),
                "value": str(g.get("concatenated", "")),
                "digits": digits_slim,
            })

        try:
            import base64
            import io as _io

            def _enc(im):
                if im is None:
                    return None
                buf = _io.BytesIO()
                im.save(buf, format="PNG")
                return base64.b64encode(buf.getvalue()).decode("ascii")

            payload = {
                "ts": now,
                "video_t": video_t,
                "source_resolution": getattr(
                    self, "_current_source_resolution", None),
                "kind": kind,
                "failure_reason": details.get("failure_reason"),
                "crop_b64": _enc(details.get("crop")),
                "binary_b64": _enc(details.get("binary_8x")),
                "elapsed_ms": float(details.get("elapsed_ms", 0.0)),
                "groups": groups_slim,
            }
            if extra:
                payload.update(extra)
            cb(payload)
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(f"diagnostic-callback dispatch failed: {e}")

    # --- ffmpeg / ffprobe --------------------------------------------------

    def _extract_frame(self, video_path: str, t: float) -> Optional[Image.Image]:
        """Pull a single PNG frame at time ``t`` from ``video_path``.

        We place ``-ss`` before ``-i`` for fast seek.  Modern ffmpeg
        builds do a decode from the preceding keyframe up to ``t`` even
        in fast-seek mode, so the seek is reasonably accurate.  Seek
        imprecision is bounded by GOP length (typically ≤ 2 s on OBS
        HEVC recordings), but the coarse scan tolerates that and the
        binary search just needs monotonic ordering to converge.
        """
        # Fast-seek + per-frame decode.  ``-hwaccel`` goes before the input
        # URL so the seek itself can use the GPU decoder.  Keeping ``-ss``
        # before ``-i`` preserves fast-seek behavior.
        cmd = [self.opts.ffmpeg, "-v", "error"]
        if self.opts.hwaccel:
            cmd.extend(["-hwaccel", self.opts.hwaccel])
        cmd.extend([
            "-ss", f"{max(0.0, t):.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-c:v", "png",
            "-",
        ])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.opts.frame_timeout,
            )
        except subprocess.TimeoutExpired:
            if self.opts.verbose:
                self.log.warning(f"ffmpeg timed out at t={t:.2f}s")
            return None
        except FileNotFoundError as e:
            raise VodDetectorError(
                f"Could not run ffmpeg ({self.opts.ffmpeg!r}): {e}"
            )
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(f"ffmpeg error at t={t:.2f}s: {e}")
            return None

        if result.returncode != 0 or not result.stdout:
            if self.opts.verbose:
                err = result.stderr.decode("utf-8", errors="ignore")[:200]
                self.log.warning(f"ffmpeg failed at t={t:.2f}s: {err}")
            return None

        try:
            return Image.open(io.BytesIO(result.stdout)).convert("RGB")
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(f"Bad PNG from ffmpeg at t={t:.2f}s: {e}")
            return None

    def _probe_duration(self, video_path: str) -> Optional[float]:
        """Return the video's duration in seconds (via ffprobe)."""
        duration, _, _ = self._probe_video_info(video_path)
        return duration

    def _probe_video_info(
        self, video_path: str
    ) -> tuple[Optional[float], Optional[int], Optional[int]]:
        """Return (duration_sec, width, height) in a single ffprobe call.

        Any of the three fields may be ``None`` on failure.  Raises
        :class:`VodDetectorError` only if ffprobe itself can't be spawned.
        """
        cmd = [
            self.opts.ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1",
            video_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.opts.probe_timeout,
            )
        except FileNotFoundError as e:
            raise VodDetectorError(
                f"Could not run ffprobe ({self.opts.ffprobe!r}): {e}"
            )
        except subprocess.TimeoutExpired:
            return None, None, None
        except Exception as e:
            self.log.warning(f"ffprobe error: {e}")
            return None, None, None

        if result.returncode != 0:
            self.log.warning(f"ffprobe failed: {result.stderr.strip()}")
            return None, None, None

        width = height = None
        duration = None
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            try:
                if key == "width":
                    width = int(val)
                elif key == "height":
                    height = int(val)
                elif key == "duration":
                    duration = float(val)
            except ValueError:
                continue
        return duration, width, height

    # --- Streaming frame extraction ---------------------------------------

    def _coarse_frame_stream(
        self,
        video_path: str,
        duration: float,
        width: Optional[int],
        height: Optional[int],
    ):
        """Yield ``(timestamp, PIL.Image)`` for a whole coarse scan.

        Two backends:
        - **Seek-based** (default, ``opts.use_seek_scan=True``): spawn one
          ffmpeg per sample with ``-ss <t>`` before ``-i`` so ffmpeg
          seeks to the nearest keyframe and only decodes from there.
          For AV1 / 4K sources this is 3-10x faster than streaming
          because we skip 95%+ of the decode work.  Per-sample spawn
          overhead is ~50-100ms on Windows, which is dominated by the
          decode savings on any non-trivial recording.
        - **Streaming** (``opts.use_seek_scan=False``): one ffmpeg
          invocation emits raw RGB frames at the coarse sampling rate
          via an ``fps=1/N`` filter.  Lower fixed cost (no per-sample
          spawn) but ffmpeg has to decode every frame to compute
          timestamps, so 4K AV1 ends up bottlenecked on decode.

        If streaming can't start (missing resolution, spawn failure, or
        ffmpeg exits before producing a single frame), fall back to the
        original per-frame extraction path.  If streaming succeeds for
        some frames and then dies, we stop — the events captured so far
        are still valid.
        """
        interval = self.opts.coarse_interval
        scan_end = max(0.0, duration - 0.5)

        # Seek-based backend: one ffmpeg per sample, input-side -ss.
        # See _coarse_frame_stream_seek for the details.  Caller-side
        # control flow is identical (yields (t, img) tuples), so the
        # rest of detect() doesn't care which backend is in use.
        if getattr(self.opts, "use_seek_scan", True):
            yield from self._coarse_frame_stream_seek(
                video_path, duration, width, height
            )
            return

        streamed_any = False

        if width and height:
            try:
                for t, img in self._stream_raw_frames(
                    video_path, width, height, interval, scan_end
                ):
                    streamed_any = True
                    yield t, img
            except Exception as e:
                if not streamed_any:
                    self.log.warning(
                        f"Streaming extraction failed ({e}), "
                        f"falling back to per-frame ffmpeg seeks."
                    )
                else:
                    # Partial success — just stop.  The detect() loop will
                    # emit whatever events we've accumulated up to this point.
                    self.log.warning(
                        f"Streaming cut short ({e}); "
                        f"keeping events found so far."
                    )
                    return
            if streamed_any:
                return

        # Fallback: one ffmpeg per sample, classic path.
        t = 0.0
        while t < scan_end:
            img = self._extract_frame(video_path, t)
            yield t, img
            t += interval

    def _coarse_frame_stream_seek(
        self,
        video_path: str,
        duration: float,
        width: Optional[int],
        height: Optional[int],
    ):
        """Coarse-frame iterator that seeks per sample.

        For every sample timestamp ``t`` in ``[0, duration)`` stepping
        by ``coarse_interval``, spawn one ffmpeg with input-side ``-ss``
        and pipe back a single frame.  ffmpeg seeks to the nearest
        keyframe at or before ``t`` and decodes only from there, so the
        total decode work is roughly ``(samples * frames_per_keyframe)``
        instead of ``(duration * fps)``.  On 4K AV1 sources this is the
        single biggest decode-time speedup available.

        Output format matches the streaming backend exactly:
        ``(width*height*3)`` rgb24 bytes per frame, scaled to 1920x1080
        and optionally cropped to the VOD_CROP_* bounding box.  Yields
        ``(t, PIL.Image)`` so the caller is backend-agnostic.

        Bad samples (ffmpeg failure, short read, timeout) are skipped
        silently — one missed coarse sample at t=300 doesn't matter
        since the next legitimate read at t=305 still compares against
        the prior baseline.  This matches the resilience of the
        streaming path, which similarly tolerates intermittent failures.
        """
        interval = self.opts.coarse_interval
        scan_end = max(0.0, duration - 0.5)
        do_crop = bool(getattr(self.opts, "ffmpeg_crop", True))

        # Build the scale+crop filter chain.  No fps filter here — we
        # control the sample cadence ourselves via the -ss loop.
        if width == 1920 and height == 1080:
            scale_filter = ""
        else:
            scale_filter = "scale=1920:1080,"
            if self.opts.verbose:
                self.log.info(
                    f"[VodDetector] Source is {width}x{height} — adding "
                    "scale=1920:1080 to the seek-based ffmpeg pipeline."
                )

        if do_crop:
            out_w, out_h = VOD_CROP_W, VOD_CROP_H
            crop_filter = (
                f"crop={VOD_CROP_W}:{VOD_CROP_H}:{VOD_CROP_X}:{VOD_CROP_Y},"
            )
        else:
            out_w, out_h = 1920, 1080
            crop_filter = ""

        vf = (scale_filter + crop_filter).rstrip(",")
        frame_bytes = out_w * out_h * 3

        # Track the last successfully-yielded timestamp so we can decide
        # whether the end-of-clip extra sample is needed.  See the
        # "End-of-clip top-up" block at the bottom of this method.
        last_yielded_t = -1.0

        t = 0.0
        while t < scan_end:
            img = self._seek_extract_one(
                video_path, t, vf, out_w, out_h, frame_bytes
            )
            if img is not None:
                yield t, img
                last_yielded_t = t
            t += interval

        # End-of-clip top-up.  The regular cadence stops as soon as
        # ``t < scan_end`` fails, which routinely leaves a 1–coarse-
        # interval (up to ~5s) blind spot at the very end of the clip
        # — and user-trimmed short highlight clips put the climactic
        # kill in that exact window because that's where the action
        # peaks.  We fix this by taking ONE extra sample at scan_end
        # whenever the last regular sample landed meaningfully before
        # it.  The detect() loop then sees the new (kda, t) pair and
        # binary-searches inside (last_regular_t, scan_end] for the
        # precise event timestamp, exactly like a normal change.
        #
        # Threshold of 0.5s: if the cadence already landed close to
        # scan_end (e.g. clip duration is an exact multiple of the
        # interval) the extra sample is redundant and we skip it.
        if scan_end > 0 and last_yielded_t < scan_end - 0.5:
            if self.opts.verbose:
                self.log.info(
                    f"  end-of-clip top-up: last regular sample at "
                    f"t={last_yielded_t:.2f}s, taking extra sample at "
                    f"t={scan_end:.2f}s to catch trailing events."
                )
            img = self._seek_extract_one(
                video_path, scan_end, vf, out_w, out_h, frame_bytes
            )
            if img is not None:
                yield scan_end, img

    def _seek_extract_one(
        self,
        video_path: str,
        t: float,
        vf: str,
        out_w: int,
        out_h: int,
        frame_bytes: int,
    ) -> Optional[Image.Image]:
        """Pull one raw RGB frame at ``t`` via input-side -ss.

        Factored out of ``_coarse_frame_stream_seek`` so the per-sample
        ffmpeg work can be called both from the regular cadence loop
        and from the end-of-clip top-up after the loop ends.  Returns
        a PIL Image on success or ``None`` for any per-sample failure
        (timeout, short read, decode error) — those are recoverable
        and never abort a scan.
        """
        cmd = [self.opts.ffmpeg, "-v", "error"]
        if self.opts.hwaccel:
            cmd.extend(["-hwaccel", self.opts.hwaccel])
        # ``-ss`` BEFORE ``-i`` triggers fast input-side seek.
        # Modern ffmpeg still decodes from the preceding keyframe
        # to land at an accurate timestamp, but it skips all
        # frames before that keyframe.  For OBS recordings with
        # 2-5s keyframe intervals, this means ~2-5s of decode per
        # sample instead of (sample_t) seconds of decode.
        cmd.extend([
            "-ss", f"{max(0.0, t):.3f}",
            "-i", video_path,
            "-frames:v", "1",
        ])
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend([
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-",
        ])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.opts.frame_timeout,
            )
        except subprocess.TimeoutExpired:
            if self.opts.verbose:
                self.log.warning(
                    f"ffmpeg timeout at t={t:.2f}s (seek backend)"
                )
            return None
        except FileNotFoundError as e:
            # No ffmpeg installed — fatal.  Unlike a per-sample
            # failure this can't be recovered from by skipping.
            raise VodDetectorError(
                f"Could not run ffmpeg ({self.opts.ffmpeg!r}): {e}"
            )
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(
                    f"ffmpeg error at t={t:.2f}s (seek backend): {e}"
                )
            return None

        if result.returncode != 0 or not result.stdout:
            if self.opts.verbose:
                err = result.stderr.decode("utf-8", errors="ignore")[:200]
                self.log.warning(
                    f"ffmpeg failed at t={t:.2f}s (seek): {err}"
                )
            return None

        if len(result.stdout) != frame_bytes:
            # Short read — likely near end of file or a black-frame
            # padding from the codec.  Skip and continue.
            if self.opts.verbose:
                self.log.warning(
                    f"short frame at t={t:.2f}s "
                    f"({len(result.stdout)}/{frame_bytes} bytes); "
                    f"skipping."
                )
            return None

        try:
            arr = np.frombuffer(result.stdout, dtype=np.uint8).reshape(
                out_h, out_w, 3
            )
            return Image.fromarray(arr, "RGB")
        except Exception as e:
            if self.opts.verbose:
                self.log.warning(
                    f"frame decode failed at t={t:.2f}s: {e}"
                )
            return None
