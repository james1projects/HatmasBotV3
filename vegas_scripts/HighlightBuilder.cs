/*
 * ============================================================================
 *  HighlightBuilder.cs   —   Smite 2 highlight timeline builder for Vegas Pro
 * ============================================================================
 *
 *  WHAT IT DOES
 *      Picks the newest `*.events.json` from EVENTS_FOLDER (written
 *      by the Python event extractor), reads the source MP4 path and
 *      event list out of it, and combined with a FramePreset captured
 *      by TuneFrame.cs builds a Vegas timeline with:
 *          * N stacked video tracks per ACTIVE_PRESET — every track
 *            shows the same gameplay footage, but through its own
 *            Pan/Crop, producing the vertical TikTok frame (gameplay
 *            body + items-HUD overlay). The preset also sets project
 *            dimensions and frame rate (e.g. 1080x1920 @ 60fps).
 *          * N audio tracks, one per audio stream in the source MP4,
 *            each track correctly routed to ITS OWN source stream
 *            (gameplay / voice / music / combined mix).
 *      Each event's video + audio clips are grouped so they move together.
 *
 *      Presets come from TuneFrame.cs: hand-tune one event in a Vegas
 *      project the way you want it, run TuneFrame, save the .tune.json
 *      preset to vegas_presets\. HighlightBuilder reads PRESET_FILE (a
 *      constant near the top of this file) at runtime; no source edits.
 *
 *  WHY IT EXISTS
 *      The Vegas FCP-XML importer silently downgrades <trackindex> values
 *      greater than 1 to 1, so every audio track ends up playing the
 *      gameplay stream no matter what the XML says. Vegas's SCRIPTING API
 *      does not have this limitation — it can bind each audio event to a
 *      specific source stream, just like dragging the raw file in by hand.
 *
 *  HOW TO INSTALL
 *      1. Find your Vegas "Script Menu" folder. Default locations:
 *              C:\ProgramData\VEGAS Pro\<version>\Script Menu\
 *         or   %APPDATA%\VEGAS Pro\<version>\Script Menu\
 *         (In Vegas: Options -> Preferences -> General, look for
 *         "Script Menu folder" — that's the authoritative path.)
 *      2. Copy this file (HighlightBuilder.cs) into that folder.
 *      3. In Vegas: Tools -> Scripting -> Rescan Script Menu Folder
 *         (only needed the first time you add a new script).
 *
 *  HOW TO RUN
 *      Open Vegas. Start a new empty project (File -> New ... default
 *      settings are fine). Then:
 *          Tools -> Scripting -> HighlightBuilder
 *      An OpenFileDialog appears, starting in EVENTS_FOLDER and with the
 *      newest *.events.json pre-selected. Hit Enter to take that one, or
 *      navigate anywhere to pick a different file. To skip the picker
 *      and always auto-pick the newest, set SHOW_FILE_PICKER = false.
 *      A confirmation dialog will pop up when it finishes. Solo each
 *      audio track to verify routing:
 *          Track 1 -> Gameplay
 *          Track 2 -> Voice
 *          Track 3 -> Music / Other
 *          Track 4 -> Combined Mix
 *
 *  IF SOMETHING GOES WRONG
 *      The script shows any error in a popup dialog. Common snags:
 *        * Namespace mismatch — Vegas 13 and earlier used "Sony.Vegas",
 *          Vegas 14+ uses "ScriptPortal.Vegas". If you see a compile
 *          error on the "using" line, swap the namespace (see comment
 *          below where it's declared).
 *        * No `.events.json` found — check EVENTS_FOLDER below points
 *          at the folder where your Python event extractor writes JSON.
 *        * Source file missing — the `source_video` path inside the
 *          JSON must exist on disk.
 *
 *  C# QUICK NOTES (for readers new to the language)
 *      * C# is strongly typed; every variable has to declare its type
 *        (string, int, double, etc.) or use `var` to infer it.
 *      * `@"..."` is a verbatim string literal — backslashes are literal,
 *        not escape characters, which is why it's nice for file paths.
 *      * `const` declares a compile-time constant. `static readonly`
 *        declares a value that's fixed after initialization.
 *      * Lines end with semicolons. Braces delimit blocks.
 *      * `foreach (T item in collection)` iterates — like Python's `for`.
 *      * `//` starts a single-line comment. `/* ... *\/` is a block.
 *      * `throw new Exception("msg")` is the C# equivalent of Python's
 *        `raise Exception("msg")`.
 * ============================================================================
 */

using System;
using System.Collections.Generic;
using System.IO;                     // for File / Directory / FileInfo
using System.Reflection;             // for fallback String/Custom param setting
using System.Text.RegularExpressions;// for the mini JSON parser
using System.Windows.Forms;          // only for the diagnostic MessageBox
using ScriptPortal.Vegas;            // Vegas 14+. For Vegas 13 or earlier use:
                                     //   using Sony.Vegas;


public class EntryPoint
{
    // ────────────────────────────────────────────────────────────────────────
    //   CONFIGURATION
    // ────────────────────────────────────────────────────────────────────────

    // Default folder the file-picker opens in, and the folder we look in
    // to pre-select the newest *.events.json. You can navigate elsewhere
    // in the dialog — this is just the starting point. The actual
    // `source_video` path comes from inside whichever JSON you pick,
    // so the JSON + MP4 can live in any folder you like.
    const string EVENTS_FOLDER = @"C:\Users\james\Videos";

    // Set this to false to skip the picker and always auto-pick the
    // newest *.events.json in EVENTS_FOLDER — the old behaviour.
    //
    // `static readonly` instead of `const` on purpose: a const bool lets
    // the compiler fold away the branch we don't take in Run(), which
    // Vegas's strict compiler then flags as "unreachable code" (error
    // 0x80131600). static readonly has the same "set once, never changes
    // at runtime" semantics but isn't folded away, so both branches
    // remain live at compile time.
    static readonly bool SHOW_FILE_PICKER = true;

    // Default highlight window around each event, in seconds. These are
    // only used when an event's JSON entry omits `pre_sec` / `post_sec`.
    const double DEFAULT_PRE_SECONDS  = 5.0;
    const double DEFAULT_POST_SECONDS = 4.0;

    // Display names for the first N audio tracks. If the source file has
    // more audio streams than we have names, extra tracks will be called
    // "Audio 5", "Audio 6", etc. These are used ONLY as fallbacks when the
    // .tune.json preset doesn't name a track — the preset's names win.
    static readonly string[] AUDIO_TRACK_NAMES = new[] {
        "Gameplay",
        "Voice",
        "Music / Other",
        "Combined Mix",
    };

    // Which .tune.json to load. TuneFrame.cs writes presets here. Edit this
    // constant if you keep multiple presets (e.g. per-champion variants).
    // The orchestrator's ProcessVideo.cs overrides this via jobs/current.json;
    // this standalone path is for manual runs of HighlightBuilder.
    const string PRESET_FILE =
        @"C:\Users\james\HatmasBot\vegas_presets\vertical_tiktok.tune.json";


    // ────────────────────────────────────────────────────────────────────────
    //   GOD-FOLDER MODE
    //   ---------------------------------------------------------------------
    //   Three run modes:
    //     "events_file"   — original behaviour. Pick a single *.events.json,
    //                       build a vertical TikTok timeline using PRESET_FILE
    //                       (vertical_tiktok.tune.json). Intro + outro cards.
    //     "god_folder"    — Pick a god name from a list of
    //                       recordings\<God>\ subfolders, find every
    //                       *.events.json in that folder, filter to the
    //                       event types in GOD_FOLDER_EVENT_TYPES (default
    //                       kill + death), and lay them all back-to-back
    //                       on a single horizontal timeline using
    //                       HORIZONTAL_PRESET_FILE (1920x1080). No title
    //                       cards — this is a raw montage of one god.
    //     "rescan_append" — Sibling to god_folder, but for the rescan
    //                       workflow.  You ran tools/rescan_events.py on
    //                       a god folder after a detector fix; it wrote a
    //                       sidecar _rescan_diff.json listing the clips
    //                       whose event count changed.  This mode reads
    //                       that sidecar, loads ONLY the changed clips'
    //                       .events.json files, finds the end of your
    //                       CURRENT project's timeline (so it doesn't
    //                       disturb anything you've already edited), and
    //                       appends those fixed clips back-to-back starting
    //                       there.  Same preset + audio routing as
    //                       god_folder, no intro/outro cards.  Existing
    //                       video + audio tracks are reused — no new
    //                       tracks are created.  Duplicate footage in the
    //                       project is expected (and intended: the old
    //                       version stays where it was, the new version
    //                       lands at the end).
    //
    //   Same `static readonly` trick as SHOW_FILE_PICKER below: a `const`
    //   string lets the C# compiler fold the equality test in Run() and
    //   then flag the unused branch as unreachable code (Vegas's strict
    //   compiler error 0x80131600). `static readonly` keeps both branches
    //   alive at compile time with the same "set once" semantics.
    static readonly string SCRIPT_MODE = "god_folder";  // "events_file" | "god_folder" | "rescan_append"

    // Folder scanned in god-folder mode. Each immediate subfolder is a
    // selectable god name (e.g. recordings\Ymir\, recordings\Loki\, plus
    // recordings\mixed\ and recordings\unknown\ written by
    // process_recordings.py).
    const string RECORDINGS_FOLDER =
        @"C:\Users\james\HatmasBot\recordings";

    // 1920x1080 horizontal preset captured by TuneFrame.cs. Used only by
    // god-folder mode; events_file mode still loads PRESET_FILE.
    const string HORIZONTAL_PRESET_FILE =
        @"C:\Users\james\HatmasBot\vegas_presets\horizontal_full.tune.json";

    // Event types kept in god-folder mode. tools/vod_detector.py emits
    // exactly three type strings: "kill", "death", "assist". Multikills
    // are folded into "kill" (with the multi-kill label in the `note`
    // field). Edit this array to change what god-folder runs include.
    // Examples:
    //   { "kill" }                  — kill montages only (original behaviour)
    //   { "kill", "death" }         — kill + death montage (default)
    //   { "kill", "death", "assist" } — everything
    static readonly string[] GOD_FOLDER_EVENT_TYPES = new[] { "kill", "death" };


    // ────────────────────────────────────────────────────────────────────────
    //   BRAND TREATMENT  —  intro and outro title cards
    //
    //   The script adds a top-of-stack "Titles" video track that overlays
    //   the rest of the timeline. On that track:
    //     * INTRO: a SERIES_NAME card (e.g. "Smite Shorts") sits over the
    //       first highlight for INTRO_DURATION seconds, fading out over
    //       INTRO_FADE_OUT seconds so the gameplay underneath takes over.
    //     * OUTRO: an OUTRO_TEXT card (e.g. "Full gameplay available on
    //       YouTube") plays AFTER the last highlight ends, on its own,
    //       for OUTRO_DURATION seconds, with a fade-in of OUTRO_FADE_IN.
    //
    //   To rename the series, just edit SERIES_NAME below and re-run.
    //   Same for the outro CTA.
    // ────────────────────────────────────────────────────────────────────────

    const string SERIES_NAME    = "Smite Shorts";
    const string OUTRO_TEXT     = "Full gameplay available on YouTube";
    const double INTRO_DURATION = 1.5;   // seconds
    const double OUTRO_DURATION = 3.0;   // seconds
    const double INTRO_FADE_OUT = 0.5;   // seconds — fade at end of intro
    const double OUTRO_FADE_IN  = 0.5;   // seconds — fade at start of outro

    // Vegas exposes the title generator under different names depending on
    // the install. We try these in order and take the first one we find.
    // If none match, the script throws a diagnostic that lists every
    // generator on the install — paste that to me and I'll fix the list.
    static readonly string[] TITLE_GENERATOR_CANDIDATES = new[] {
        "(VEGAS) Titles & Text",
        "VEGAS Titles & Text",
        "Titles & Text",
        "(Legacy) Text",
        "Sony Text",
    };

    // OFX parameter that holds the displayed text. Same dance as above —
    // we try these names in order and use whichever exists. On failure
    // we dump every available parameter so we can see what to use.
    static readonly string[] TITLE_TEXT_PARAM_CANDIDATES = new[] {
        "Text",
        "TextString",
        "RichText",
        "String",
    };


    // ────────────────────────────────────────────────────────────────────────
    //   FRAME PRESET SYSTEM  (loaded from .tune.json at runtime)
    //
    //   A preset captures everything TuneFrame.cs reads from a manually-
    //   tuned project: the output frame size, frame rate, per-video-track
    //   Pan/Crop + composite mode, and per-audio-track volume + mute.
    //   HighlightBuilder loads the preset pointed at by PRESET_FILE, sets
    //   project properties to the preset dims, creates N video tracks with
    //   the preset's composite modes and names, applies Pan/Crop to each
    //   generated VideoEvent, and sets AudioTrack.Volume + AudioTrack.Mute
    //   from the preset's audio entries.
    //
    //   To add a new preset (e.g. per-champion variant):
    //     1. Tune the scene in Vegas.
    //     2. Run TuneFrame → save as vegas_presets\<name>.tune.json.
    //     3. Point PRESET_FILE at the new file (or run via the orchestrator
    //        which selects the preset via jobs/current.json).
    // ────────────────────────────────────────────────────────────────────────

    // FramePreset is the shape TuneFrame.cs emits. Classes (not structs)
    // so the initializer syntax `new FramePreset { Name = ..., Tracks = ... }`
    // works without a constructor.
    class FramePreset {
        public string Name;
        public string Kind;                  // "highlight" | "full_gameplay" | "unknown"
        public int    ProjectWidth;
        public int    ProjectHeight;
        public double FrameRate;
        public TrackPreset[] Tracks;         // video tracks
        public AudioPreset[] AudioTracks;    // audio tracks (volume + mute)
    }

    class TrackPreset {
        public string        Name;
        public CompositeMode Composite;
        // Pan/Crop bounds in source-pixel coordinates. Bounds may extend
        // outside the source rectangle — the area outside is transparent.
        public double TopLeftX,     TopLeftY;
        public double TopRightX,    TopRightY;
        public double BottomRightX, BottomRightY;
        public double BottomLeftX,  BottomLeftY;
        public double CenterX,      CenterY;
        public double Rotation;             // radians (Vegas's native unit)
        public double Smoothness;
        public VideoKeyframeType KeyframeType;
        // When true (Vegas default), the cropped region is scaled to fill
        // the output frame. When false, it keeps its source-pixel size
        // inside the output frame — essential for overlay tracks that
        // should stay small (e.g. items HUD as a strip, not fullscreen).
        public bool   ScaleToFill = true;
        // Per-event OFX effects (e.g. Bezier Masking). Re-applied to every
        // VideoEvent created on this track during the build.
        public EffectPreset[] Effects = new EffectPreset[0];
    }

    class AudioPreset {
        public string Name;
        public double Volume_dB;             // stored as dB; converted to linear at apply time
        public bool   Muted;
    }

    // OFX effects captured from a tuned event (e.g. Bezier Masking).
    // Applied to every VideoEvent created on the matching video track.
    class EffectPreset {
        public string PluginId;              // PlugInNode.UniqueID
        public string PluginName;            // for logging
        public bool   Bypass;
        public ParamPreset[] Parameters;
    }

    class ParamPreset {
        public string Name;
        public string Type;                  // "Boolean" | "Double" | "Integer" | "Choice" | "Double2D" | "String" | "Custom"
        public bool   BoolValue;
        public double DoubleValue;
        public int    IntValue;
        public string StringValue;           // used for Choice (choice name), String, Custom
        public double XValue, YValue;        // for Double2D
    }

    // The preset applied to this run. Loaded from a .tune.json file at the
    // top of Run() via LoadPreset(). Not readonly because it's assigned at
    // runtime (static readonly can only be assigned in the field initializer
    // or the static constructor).
    static FramePreset ACTIVE_PRESET;


    // A single highlight event. Using a small struct keeps it lightweight.
    struct HighlightEvent {
        public double TimestampSec;    // where in the source the event occurs
        public string Type;            // "kill" | "death" | "assist" — for your notes
        public string Note;            // free-text label
        public double PreSec;          // seconds to include BEFORE the timestamp
        public double PostSec;         // seconds to include AFTER the timestamp

        public HighlightEvent(double ts, string type, string note, double pre, double post) {
            TimestampSec = ts;
            Type = type;
            Note = note;
            PreSec = pre;
            PostSec = post;
        }
    }

    // Container for a loaded events-file: the source video path plus
    // the events parsed out of the JSON.
    class EventsFile
    {
        public string SourceVideo;
        public List<HighlightEvent> Events;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   ENTRY POINT
    //   Vegas invokes FromVegas(Vegas) when this script is run from the
    //   Tools -> Scripting menu. `vegas` is the root API object.
    // ────────────────────────────────────────────────────────────────────────
    public void FromVegas(Vegas vegas)
    {
        try
        {
            Run(vegas);
        }
        catch (Exception ex)
        {
            // Pop a dialog with the full error — easier than hunting the
            // Vegas error list during development. You can remove this once
            // the script is stable.
            MessageBox.Show(
                "HighlightBuilder failed.\n\n" + ex.Message + "\n\n" + ex.StackTrace,
                "HighlightBuilder error");
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   MAIN LOGIC
    // ────────────────────────────────────────────────────────────────────────
    void Run(Vegas vegas)
    {
        Project proj = vegas.Project;

        // Warnings surface in the final summary dialog. Populated by
        // effect application (missing plugin, parameter apply failures,
        // audio-track count mismatch, etc.).
        List<string> buildWarnings = new List<string>();

        // ── 0) Mode switch ─────────────────────────────────────────────────
        // god-folder mode is its own self-contained build path (see
        // RunGodFolder below) — different preset, different file picker,
        // multiple recordings concatenated on one timeline, no title
        // cards. rescan-append mode is yet another self-contained path —
        // see RunRescanAppend.  events_file mode falls through to the
        // original logic unchanged.
        if (string.Equals(SCRIPT_MODE, "god_folder",
                          StringComparison.OrdinalIgnoreCase))
        {
            RunGodFolder(vegas, proj, buildWarnings);
            return;
        }
        if (string.Equals(SCRIPT_MODE, "rescan_append",
                          StringComparison.OrdinalIgnoreCase))
        {
            RunRescanAppend(vegas, proj, buildWarnings);
            return;
        }

        // ── 0a) Load the frame preset from .tune.json ──────────────────────
        // The preset dictates project dims, frame rate, video-track Pan/Crop
        // + composite mode, and audio-track volume + mute. Captured by
        // TuneFrame.cs. Fail early if the file is missing — no silent
        // fallback to a hardcoded default.
        if (!File.Exists(PRESET_FILE))
        {
            throw new Exception(
                "Preset file not found:\n  " + PRESET_FILE +
                "\n\nRun TuneFrame.cs first to capture a preset, " +
                "or edit PRESET_FILE at the top of this script.");
        }
        ACTIVE_PRESET = LoadPreset(PRESET_FILE);

        // ── 0b) Pick (or auto-pick) and parse the .events.json ─────────────
        // If SHOW_FILE_PICKER is true, we open an OpenFileDialog filtered to
        // *.events.json, starting in EVENTS_FOLDER and pre-selecting the
        // newest file there (so you can usually just hit Enter). Otherwise
        // we silently auto-pick that newest file. Either way, the chosen
        // JSON is parsed into an EventsFile (source mp4 path + events).
        // Anything bad here (cancel, no files, malformed JSON, missing
        // source video) throws and the outer try/catch shows a dialog.
        string eventsJsonPath = SHOW_FILE_PICKER
            ? PickEventsJsonViaDialog(EVENTS_FOLDER)
            : FindNewestEventsJson(EVENTS_FOLDER);
        EventsFile ef = LoadEventsFile(eventsJsonPath);

        if (!File.Exists(ef.SourceVideo))
        {
            throw new Exception(
                "source_video in JSON does not exist on disk:\n  " + ef.SourceVideo +
                "\n\nJSON file: " + eventsJsonPath);
        }

        // ── 1) Load the source file into the project media pool ────────────
        // AddMedia registers the file and returns a Media object describing
        // its contents (video + audio streams, duration, sample rate, etc).
        Media media = proj.MediaPool.AddMedia(ef.SourceVideo);

        // Walk the file's streams and categorise them. We only use the first
        // video stream; we keep ALL audio streams in order.
        MediaStream videoStream = null;
        List<MediaStream> audioStreams = new List<MediaStream>();

        foreach (MediaStream stream in media.Streams)
        {
            if (stream.MediaType == MediaType.Video && videoStream == null)
            {
                videoStream = stream;
            }
            else if (stream.MediaType == MediaType.Audio)
            {
                audioStreams.Add(stream);
            }
        }

        if (videoStream == null)
            throw new Exception("No video stream found in: " + ef.SourceVideo);
        if (audioStreams.Count == 0)
            throw new Exception("No audio streams found in: " + ef.SourceVideo);


        // ── 1.5) Apply the preset's project properties ──────────────────────
        //        Set output dims + frame rate to match what TuneFrame
        //        captured. Vegas's VideoProperties exposes Width/Height
        //        as ints and FrameRate as a double. (You can still
        //        override these by hand in File -> Properties afterwards.)
        proj.Video.Width     = ACTIVE_PRESET.ProjectWidth;
        proj.Video.Height    = ACTIVE_PRESET.ProjectHeight;
        proj.Video.FrameRate = ACTIVE_PRESET.FrameRate;


        // ── 2) Create the timeline tracks ───────────────────────────────────
        //      Titles track FIRST (top of stack), then N video tracks per
        //      ACTIVE_PRESET.Tracks (in preset order = Vegas UI order top-
        //      down), then one audio track per source stream. Appends to
        //      whatever tracks already exist (safe on an empty project;
        //      if the project already has tracks they stay above the
        //      newly-added ones in the UI).
        //
        //      Why titles first: the first track added gets the lowest
        //      index, which is the TOP of the composite stack in Vegas.
        //      Putting titles above everything means the intro card
        //      overlays the gameplay underneath instead of hiding below
        //      it. SrcAlpha composite mode lets the transparent regions
        //      of the title cards show the gameplay through.

        int trackIndex = proj.Tracks.Count;

        // Titles track — holds the intro "Smite Shorts" card and the
        // outro "Full gameplay on YouTube" card.
        VideoTrack titlesTrack = new VideoTrack(proj, trackIndex++, "Titles");
        proj.Tracks.Add(titlesTrack);
        // ORDER MATTERS (same gotcha as preset tracks): configure only
        // AFTER Tracks.Add, never before.
        titlesTrack.CompositeMode = CompositeMode.SrcAlpha;

        VideoTrack[] videoTracks = new VideoTrack[ACTIVE_PRESET.Tracks.Length];
        for (int i = 0; i < ACTIVE_PRESET.Tracks.Length; i++)
        {
            TrackPreset tp = ACTIVE_PRESET.Tracks[i];
            // Use the preset's track name; fall back to a generic if blank.
            string vtName = string.IsNullOrEmpty(tp.Name)
                ? "Video Track " + (i + 1)
                : tp.Name;
            videoTracks[i] = new VideoTrack(proj, trackIndex++, vtName);
            // ORDER MATTERS: a freshly-constructed Track is "invalid" until
            // it's actually in proj.Tracks. Setting CompositeMode (or any
            // other property) before Add() throws "Invalid Track object."
            // So: add first, configure second.
            proj.Tracks.Add(videoTracks[i]);
            videoTracks[i].CompositeMode = tp.Composite;
        }

        AudioTrack[] audioTracks = new AudioTrack[audioStreams.Count];
        AudioPreset[] audioPreset = ACTIVE_PRESET.AudioTracks ?? new AudioPreset[0];
        for (int i = 0; i < audioStreams.Count; i++)
        {
            // Track name precedence: preset > AUDIO_TRACK_NAMES fallback > generic.
            string trackName;
            if (i < audioPreset.Length && !string.IsNullOrEmpty(audioPreset[i].Name))
                trackName = audioPreset[i].Name;
            else if (i < AUDIO_TRACK_NAMES.Length)
                trackName = AUDIO_TRACK_NAMES[i];
            else
                trackName = "Audio " + (i + 1);

            audioTracks[i] = new AudioTrack(proj, trackIndex++, trackName);
            // ORDER MATTERS (same as video): add first, then configure.
            proj.Tracks.Add(audioTracks[i]);

            // Apply preset volume + mute if we have preset data for this track.
            if (i < audioPreset.Length)
            {
                AudioPreset ap = audioPreset[i];
                // Convert dB back to linear. Volumes at or below the -100 dB
                // "muted" sentinel map to ~1e-5 (effectively silent).
                double linearVol = Math.Pow(10.0, ap.Volume_dB / 20.0);
                if (linearVol < 1e-5) linearVol = 1e-5;
                if (linearVol > 4.0)  linearVol = 4.0;   // Vegas caps at +12 dB ≈ 3.98
                audioTracks[i].Volume = (float)linearVol;
                audioTracks[i].Mute   = ap.Muted;
            }
        }


        // ── 3) Lay down each event as back-to-back clips ────────────────────
        //      `cursor` is the position on the timeline where the next event
        //      will be placed. It advances by each clip's duration.

        Timecode cursor = Timecode.FromSeconds(0.0);

        foreach (HighlightEvent ev in ef.Events)
        {
            // Compute the window [srcStart, srcEnd] in the source file.
            // Clamp srcStart at 0 so we never try to read before the file begins.
            double srcStartSec = Math.Max(0.0, ev.TimestampSec - ev.PreSec);
            double srcEndSec   = ev.TimestampSec + ev.PostSec;
            double clipLenSec  = srcEndSec - srcStartSec;

            Timecode srcOffset = Timecode.FromSeconds(srcStartSec);
            Timecode clipLen   = Timecode.FromSeconds(clipLenSec);

            // We'll collect every event added for this highlight so we can
            // group them together afterwards.
            List<TrackEvent> linkedEvents = new List<TrackEvent>();

            // --- VIDEO EVENTS (one per video track in the preset) -------------
            // Each video track in the preset gets its own VideoEvent at the
            // same cursor/length, reading from the same video stream. We
            // then stamp each event's Pan/Crop from the matching preset
            // entry — this is what makes the stacked vertical frame look
            // like the tune capture instead of full-frame gameplay clips.
            for (int i = 0; i < videoTracks.Length; i++)
            {
                VideoEvent vEvent = videoTracks[i].AddVideoEvent(cursor, clipLen);
                vEvent.AddTake(videoStream, true /* set as active take */);
                vEvent.ActiveTake.Offset = srcOffset;
                ApplyPanCrop(vEvent, ACTIVE_PRESET.Tracks[i]);
                ApplyEffects(vEvent, ACTIVE_PRESET.Tracks[i].Effects, vegas, buildWarnings);
                linkedEvents.Add(vEvent);
            }

            // --- AUDIO EVENTS --------------------------------------------------
            // One audio event per source stream, each bound to its OWN stream.
            // This single line is the whole reason we're using scripting:
            //     aEvent.AddTake(audioStreams[i], true);
            // The XML importer collapses everything to stream 1; this call
            // binds the event to the specific MediaStream we pass in.
            for (int i = 0; i < audioStreams.Count; i++)
            {
                AudioEvent aEvent = audioTracks[i].AddAudioEvent(cursor, clipLen);
                aEvent.AddTake(audioStreams[i], true /* set as active take */);
                aEvent.ActiveTake.Offset = srcOffset;
                linkedEvents.Add(aEvent);
            }

            // --- GROUP ---------------------------------------------------------
            // A TrackEventGroup makes Vegas treat these events as a unit —
            // dragging one drags them all, which preserves the video/audio
            // alignment when you nudge a clip.
            TrackEventGroup group = new TrackEventGroup(proj);
            proj.Groups.Add(group);
            foreach (TrackEvent te in linkedEvents)
            {
                group.Add(te);
            }

            // Advance the cursor so the next event starts right after this one.
            cursor += clipLen;
        }


        // ── 3.5) Place the intro and outro title cards ──────────────────────
        //        The Titles track has been empty up to this point. We add
        //        the intro card overlapping the very first highlight, then
        //        the outro card AFTER the last highlight ends. The title
        //        generator is looked up once and reused for both.
        //
        //        If your install doesn't have the expected generator or
        //        OFX parameter names, the helpers throw a diagnostic
        //        listing what IS available — paste that error to me and
        //        I'll patch the candidate lists at the top of the file.

        PlugInNode titleGen = FindTitleGenerator(vegas);

        // Intro: starts at t=0, length = INTRO_DURATION, fade-OUT at end.
        AddTitleEvent(
            proj, titlesTrack, titleGen, SERIES_NAME,
            Timecode.FromSeconds(0.0),
            Timecode.FromSeconds(INTRO_DURATION),
            Timecode.FromSeconds(0.0),                 // no fade-in
            Timecode.FromSeconds(INTRO_FADE_OUT));

        // Outro: starts at end-of-last-highlight (= cursor), length =
        // OUTRO_DURATION, fade-IN at start so it eases on after the last
        // gameplay frame instead of cutting in hard.
        AddTitleEvent(
            proj, titlesTrack, titleGen, OUTRO_TEXT,
            cursor,
            Timecode.FromSeconds(OUTRO_DURATION),
            Timecode.FromSeconds(OUTRO_FADE_IN),
            Timecode.FromSeconds(0.0));                // no fade-out


        // ── 4) Confirmation ─────────────────────────────────────────────────
        // A small "done" dialog so you know it actually ran and can see
        // which tracks map to which sources.
        System.Text.StringBuilder summary = new System.Text.StringBuilder();
        summary.AppendLine("HighlightBuilder complete.");
        summary.AppendLine();
        summary.AppendFormat("Events file:   {0}\n", Path.GetFileName(eventsJsonPath));
        summary.AppendFormat("Source video:  {0}\n", Path.GetFileName(ef.SourceVideo));
        summary.AppendFormat("Preset file:   {0}\n", Path.GetFileName(PRESET_FILE));
        summary.AppendFormat("Preset applied: {0} [{4}] ({1}x{2} @ {3}fps)\n",
            ACTIVE_PRESET.Name,
            ACTIVE_PRESET.ProjectWidth,
            ACTIVE_PRESET.ProjectHeight,
            ACTIVE_PRESET.FrameRate,
            ACTIVE_PRESET.Kind);
        // Warn on audio-track count mismatch (preset vs source).
        int presetAudioCount = (ACTIVE_PRESET.AudioTracks != null)
            ? ACTIVE_PRESET.AudioTracks.Length : 0;
        if (presetAudioCount != audioStreams.Count)
        {
            summary.AppendFormat(
                "! Audio track count mismatch: preset has {0}, source has {1}. " +
                "First {2} applied; extras at unity gain.\n",
                presetAudioCount, audioStreams.Count,
                Math.Min(presetAudioCount, audioStreams.Count));
        }
        summary.AppendFormat("Events placed: {0}\n", ef.Events.Count);
        summary.AppendFormat("Video tracks:  {0} + 1 titles track\n", videoTracks.Length);
        summary.AppendFormat("Audio tracks:  {0} (one per source stream)\n", audioStreams.Count);
        summary.AppendFormat("Brand intro:   \"{0}\"\n", SERIES_NAME);
        summary.AppendFormat("Brand outro:   \"{0}\"\n", OUTRO_TEXT);
        summary.AppendLine();
        summary.AppendLine("Solo each audio track to verify routing:");
        for (int i = 0; i < audioTracks.Length; i++)
        {
            summary.AppendFormat("  Track {0} -> {1}\n", i + 1, audioTracks[i].Name);
        }

        if (buildWarnings.Count > 0)
        {
            summary.AppendLine();
            summary.AppendLine("Warnings:");
            foreach (string w in buildWarnings)
                summary.AppendFormat("  ! {0}\n", w);
        }

        MessageBox.Show(summary.ToString(), "HighlightBuilder — done");
    }


    // ────────────────────────────────────────────────────────────────────────
    //   PAN/CROP APPLICATION
    //   Vegas creates each new VideoEvent with a single default Pan/Crop
    //   keyframe at position 0 representing "no crop — show the full
    //   source frame." This helper overwrites that default keyframe's
    //   values with the ones captured by TuneFrame.cs for the matching
    //   preset track.
    //
    //   Vegas Pro 21 API details this code depends on:
    //     * The four corner vertices (TopLeft / TopRight / BottomLeft /
    //       BottomRight) are READ-ONLY on VideoMotionKeyframe. You can't
    //       assign them one by one. Instead, replace the whole rectangle
    //       via `kf.Bounds = new VideoMotionBounds(TL, TR, BR, BL)`.
    //       Constructor order is clockwise from top-left.
    //     * `Center` on the other hand IS settable directly.
    //     * `Rotation` is a FLOAT in Vegas 21 (not a double), so we cast.
    //     * The (float) casts on vertex coordinates match the
    //       VideoMotionVertex constructor's float parameters. Our preset
    //       stores doubles for readout precision; narrowing costs a
    //       sub-pixel of accuracy — well under Vegas rendering tolerance.
    // ────────────────────────────────────────────────────────────────────────
    static void ApplyPanCrop(VideoEvent vEvent, TrackPreset tp)
    {
        // ScaleToFill is a per-event (not per-keyframe) property of
        // VideoMotion. Set it before touching the keyframe so Vegas's
        // internal coordinate-mapping sees the correct mode when the
        // bounds get assigned.
        vEvent.VideoMotion.ScaleToFill = tp.ScaleToFill;

        VideoMotionKeyframe kf = vEvent.VideoMotion.Keyframes[0];

        // Replace the whole crop rectangle in one assignment.
        kf.Bounds = new VideoMotionBounds(
            new VideoMotionVertex((float)tp.TopLeftX,     (float)tp.TopLeftY),
            new VideoMotionVertex((float)tp.TopRightX,    (float)tp.TopRightY),
            new VideoMotionVertex((float)tp.BottomRightX, (float)tp.BottomRightY),
            new VideoMotionVertex((float)tp.BottomLeftX,  (float)tp.BottomLeftY));

        // Center is directly settable (unlike the corners).
        kf.Center = new VideoMotionVertex((float)tp.CenterX, (float)tp.CenterY);

        kf.Rotation   = (float)tp.Rotation;      // float in Vegas 21 — cast needed
        kf.Smoothness = (float)tp.Smoothness;    // also a float, same reason
        kf.Type       = tp.KeyframeType;
    }


    // ════════════════════════════════════════════════════════════════════════
    //   TITLE CARDS  (Smite Shorts intro + YouTube outro)
    //   ---------------------------------------------------------------------
    //   Vegas title generators are accessed through `vegas.Generators`, a
    //   tree of PlugInNodes organised roughly by category. We walk that
    //   tree looking for a name that matches TITLE_GENERATOR_CANDIDATES.
    //   Once found, we create a new Media from the generator, wire it to
    //   a VideoEvent on the Titles track, and set its displayed text via
    //   the generator's OFX parameters.
    //
    //   Why this many fallbacks: Vegas's title generator has been renamed
    //   across versions ("Sony Text" → "(Legacy) Text" → "(VEGAS) Titles
    //   & Text"), and its OFX parameter for the actual text string has
    //   also varied. The candidate lists at the top of the file give us
    //   a best-effort shot at working on James's specific install without
    //   an extra probe step — and if all candidates miss, the diagnostic
    //   dialog tells us exactly what the install DOES expose so we can
    //   patch the lists rather than guess again.
    // ════════════════════════════════════════════════════════════════════════

    // Walk the generators tree and return the first node whose name
    // matches one of our candidates. Throws with a full dump of what IS
    // available if nothing matches — paste that error back and I'll
    // update TITLE_GENERATOR_CANDIDATES.
    static PlugInNode FindTitleGenerator(Vegas vegas)
    {
        foreach (string candidate in TITLE_GENERATOR_CANDIDATES)
        {
            PlugInNode node = FindNodeByName(vegas.Generators, candidate);
            if (node != null) return node;
        }

        System.Text.StringBuilder sb = new System.Text.StringBuilder();
        sb.AppendLine("Could not find a Titles & Text generator. Tried:");
        foreach (string c in TITLE_GENERATOR_CANDIDATES)
            sb.AppendLine("  - " + c);
        sb.AppendLine();
        sb.AppendLine("Available generators on this install (full tree):");
        DumpPlugInTree(vegas.Generators, sb, 0);
        throw new Exception(sb.ToString());
    }

    // Recursive name-match search over a PlugInNode tree. Returns the
    // first matching node at any depth, or null if nothing matched.
    // Case-insensitive because Vegas's UI names sometimes drift in case.
    static PlugInNode FindNodeByName(PlugInNode parent, string name)
    {
        foreach (PlugInNode child in parent)
        {
            if (string.Equals(child.Name, name, StringComparison.OrdinalIgnoreCase))
                return child;
            PlugInNode deep = FindNodeByName(child, name);
            if (deep != null) return deep;
        }
        return null;
    }

    // Pretty-print every node in a PlugInNode tree, indented by depth.
    // Used when FindTitleGenerator can't find a match — the dump lands
    // in the error dialog so we can see what IS on this install.
    static void DumpPlugInTree(PlugInNode parent, System.Text.StringBuilder sb, int depth)
    {
        foreach (PlugInNode child in parent)
        {
            sb.Append(' ', depth * 2);
            sb.AppendLine(child.Name);
            DumpPlugInTree(child, sb, depth + 1);
        }
    }

    // Create a video event on `track` at `start`, sized `length`, with a
    // title-card take produced by `titleGen`. The text is set via the
    // first matching OFX string parameter (see TITLE_TEXT_PARAM_CANDIDATES).
    // FadeIn/FadeOut lengths are applied if > 0.
    //
    // Vegas notes:
    //   * `new Media(PlugInNode)` instantiates a generator → Media.
    //   * The new Media exposes at least one video stream; we take the
    //     first one (index 0) as the event's active take.
    //   * Generator media ISN'T auto-added to the project MediaPool in
    //     older Vegas versions — we call MediaPool.Add defensively.
    //     In Vegas 21 this may be a no-op; that's fine.
    static VideoEvent AddTitleEvent(Project proj, VideoTrack track,
                                    PlugInNode titleGen, string text,
                                    Timecode start, Timecode length,
                                    Timecode fadeIn, Timecode fadeOut)
    {
        // Instantiate the generator as a fresh Media object. In Vegas 21
        // this automatically registers the media with the project — we
        // don't (and CAN'T) call MediaPool.Add on it; that method only
        // accepts a string path for disk-backed media, not generator-
        // produced Media objects.
        Media titleMedia = new Media(titleGen);

        // Put an event on the timeline and bind it to the title media's
        // video stream. Media.Streams is mixed (video + audio in general),
        // so we pick the first entry with MediaType.Video.
        VideoEvent vEvent = track.AddVideoEvent(start, length);
        MediaStream titleStream = null;
        foreach (MediaStream s in titleMedia.Streams)
        {
            if (s.MediaType == MediaType.Video) { titleStream = s; break; }
        }
        if (titleStream == null)
            throw new Exception(
                "Title generator produced no video stream. " +
                "Generator name: " + titleGen.Name);
        vEvent.AddTake(titleStream, true);

        // Write the text into the generator's OFX parameters.
        SetTitleText(titleMedia, text);

        // Fades. Timecode comparisons against 0 work — Timecode implements
        // the usual comparison operators.
        Timecode zero = Timecode.FromSeconds(0.0);
        if (fadeIn  > zero) vEvent.FadeIn.Length  = fadeIn;
        if (fadeOut > zero) vEvent.FadeOut.Length = fadeOut;

        return vEvent;
    }

    // Find the OFX string parameter whose name matches one of our
    // candidates and assign `text` to it. Generator-produced Media has
    // a `Generator` handle that exposes an OFXEffect — that's where the
    // parameters live. If no candidate matches, dump every parameter
    // name and type for diagnosis.
    static void SetTitleText(Media titleMedia, string text)
    {
        OFXEffect ofx = titleMedia.Generator.OFXEffect;

        foreach (string paramName in TITLE_TEXT_PARAM_CANDIDATES)
        {
            OFXParameter p = ofx.FindParameterByName(paramName);
            if (p != null && p is OFXStringParameter)
            {
                ((OFXStringParameter)p).Value = text;
                return;
            }
        }

        System.Text.StringBuilder sb = new System.Text.StringBuilder();
        sb.AppendLine("Could not find a text parameter on the title");
        sb.AppendLine("generator. Tried:");
        foreach (string c in TITLE_TEXT_PARAM_CANDIDATES)
            sb.AppendLine("  - " + c);
        sb.AppendLine();
        sb.AppendLine("Available OFX parameters on this generator:");
        foreach (OFXParameter p in ofx.Parameters)
        {
            sb.AppendFormat("  {0}  ({1})\n", p.Name, p.GetType().Name);
        }
        throw new Exception(sb.ToString());
    }


    // ════════════════════════════════════════════════════════════════════════
    //   GOD-FOLDER MODE BUILD
    //   ---------------------------------------------------------------------
    //   Self-contained alternate build path. Activated by SCRIPT_MODE =
    //   "god_folder" at the top of the file. Run() delegates to this
    //   method and returns immediately, so events_file mode is bit-perfect
    //   identical to before.
    //
    //   Flow:
    //     1. Load HORIZONTAL_PRESET_FILE (1920x1080 .tune.json).
    //     2. Pop a Windows Form with a list of recordings\<God>\
    //        subfolders. User picks one.
    //     3. List every *.events.json in the chosen god folder, sort
    //        them in natural order (Ymir-1, Ymir-2, Ymir-10).
    //     4. Load each, FILTER events to type == "kill" only, drop any
    //        recording with zero kill events or a missing source mp4.
    //     5. Apply preset project dims/framerate.
    //     6. Create N video tracks per preset + M audio tracks (M = the
    //        first source's audio stream count). Apply preset audio
    //        volume + mute.
    //     7. For each kept recording: AddMedia, walk events, lay down
    //        clips at a SHARED cursor that advances across recordings.
    //        Same group/link logic as events_file mode.
    //     8. Show a summary dialog with kept/skipped counts and warnings.
    //
    //   No intro/outro title cards in this mode — the deliverable is a
    //   raw kill montage for a single god, not a branded TikTok short.
    // ════════════════════════════════════════════════════════════════════════
    void RunGodFolder(Vegas vegas, Project proj, List<string> buildWarnings)
    {
        // ── 1) Load the horizontal preset ──────────────────────────────────
        if (!File.Exists(HORIZONTAL_PRESET_FILE))
        {
            throw new Exception(
                "Horizontal preset not found:\n  " + HORIZONTAL_PRESET_FILE +
                "\n\nCapture one with TuneFrame.cs first, or edit " +
                "HORIZONTAL_PRESET_FILE at the top of this script.");
        }
        ACTIVE_PRESET = LoadPreset(HORIZONTAL_PRESET_FILE);

        // ── 2) Pick a god folder via list dialog ───────────────────────────
        string godFolder = PickGodFolder(RECORDINGS_FOLDER);
        string godName = new DirectoryInfo(godFolder).Name;

        // ── 3) Find every *.events.json in that god folder ─────────────────
        // Non-recursive — each god folder is flat (per
        // process_recordings.py's layout: <stem>-N.mp4 + <stem>-N.events.json
        // siblings). Sort naturally so Ymir-2 comes before Ymir-10.
        string[] jsonPaths = Directory.GetFiles(
            godFolder, "*.events.json", SearchOption.TopDirectoryOnly);
        if (jsonPaths.Length == 0)
        {
            throw new Exception(
                "No *.events.json files in god folder:\n  " + godFolder +
                "\n\nRun tools\\extract_events.py or " +
                "tools\\process_recordings.py first.");
        }
        Array.Sort(jsonPaths, delegate(string a, string b) {
            return NaturalCompare(Path.GetFileName(a), Path.GetFileName(b));
        });

        // ── 4) Load each JSON, filter to kills, validate source ────────────
        // Skip-with-note (rather than throw) so one bad recording in a
        // god folder doesn't abort the whole build. Reasons land in the
        // summary dialog so it's clear what got dropped.
        List<EventsFile> plans = new List<EventsFile>();
        List<string> skipNotes = new List<string>();
        foreach (string jp in jsonPaths)
        {
            EventsFile ef;
            try { ef = LoadEventsFile(jp); }
            catch (Exception ex)
            {
                skipNotes.Add(Path.GetFileName(jp) + ": parse failed — "
                              + ex.Message);
                continue;
            }

            // Filter by GOD_FOLDER_EVENT_TYPES (declared near the top of
            // this file). tools/vod_detector.py emits exactly three type
            // strings: "kill", "death", "assist". Multikills (double /
            // triple / quadra / penta) and batched player_kills are folded
            // into type "kill" with the multi-kill label captured in the
            // `note` field. Merged kill+death trades also anchor as type
            // "kill" (priority kill > death > assist).
            List<HighlightEvent> keep = new List<HighlightEvent>();
            foreach (HighlightEvent e in ef.Events)
            {
                foreach (string keepType in GOD_FOLDER_EVENT_TYPES)
                {
                    if (string.Equals(e.Type, keepType,
                                      StringComparison.OrdinalIgnoreCase))
                    {
                        keep.Add(e);
                        break;
                    }
                }
            }
            if (keep.Count == 0)
            {
                skipNotes.Add(Path.GetFileName(jp)
                    + ": no events of types ["
                    + string.Join(", ", GOD_FOLDER_EVENT_TYPES) + "]");
                continue;
            }
            if (!File.Exists(ef.SourceVideo))
            {
                skipNotes.Add(Path.GetFileName(jp)
                    + ": source missing — " + ef.SourceVideo);
                continue;
            }
            ef.Events = keep;
            plans.Add(ef);
        }
        if (plans.Count == 0)
        {
            throw new Exception(
                "No recordings with kill events found in:\n  " + godFolder +
                "\n\nReasons:\n  " + string.Join("\n  ",
                                                 skipNotes.ToArray()));
        }

        // ── 5) Apply preset project properties ─────────────────────────────
        proj.Video.Width     = ACTIVE_PRESET.ProjectWidth;
        proj.Video.Height    = ACTIVE_PRESET.ProjectHeight;
        proj.Video.FrameRate = ACTIVE_PRESET.FrameRate;

        // ── 6) Create tracks once, sized to the first recording's audio ───
        // All god-folder recordings share an OBS scene/setup, so audio
        // stream count should be identical across them. We size tracks
        // based on the first source and warn if a later one differs.
        Media firstMedia = proj.MediaPool.AddMedia(plans[0].SourceVideo);
        int firstAudioCount = 0;
        foreach (MediaStream s in firstMedia.Streams)
            if (s.MediaType == MediaType.Audio) firstAudioCount++;
        if (firstAudioCount == 0)
        {
            throw new Exception(
                "First recording has no audio streams:\n  "
                + plans[0].SourceVideo);
        }

        int trackIndex = proj.Tracks.Count;

        VideoTrack[] videoTracks = new VideoTrack[ACTIVE_PRESET.Tracks.Length];
        for (int i = 0; i < ACTIVE_PRESET.Tracks.Length; i++)
        {
            TrackPreset tp = ACTIVE_PRESET.Tracks[i];
            string vtName = string.IsNullOrEmpty(tp.Name)
                ? "Video Track " + (i + 1) : tp.Name;
            videoTracks[i] = new VideoTrack(proj, trackIndex++, vtName);
            // Same gotcha as events_file mode: add first, configure second.
            // Setting properties on an unparented track throws "Invalid
            // Track object."
            proj.Tracks.Add(videoTracks[i]);
            videoTracks[i].CompositeMode = tp.Composite;
        }

        AudioTrack[] audioTracks = new AudioTrack[firstAudioCount];
        AudioPreset[] audioPreset = ACTIVE_PRESET.AudioTracks
                                    ?? new AudioPreset[0];
        for (int i = 0; i < firstAudioCount; i++)
        {
            string trackName;
            if (i < audioPreset.Length
                && !string.IsNullOrEmpty(audioPreset[i].Name))
                trackName = audioPreset[i].Name;
            else if (i < AUDIO_TRACK_NAMES.Length)
                trackName = AUDIO_TRACK_NAMES[i];
            else
                trackName = "Audio " + (i + 1);

            audioTracks[i] = new AudioTrack(proj, trackIndex++, trackName);
            proj.Tracks.Add(audioTracks[i]);

            if (i < audioPreset.Length)
            {
                AudioPreset ap = audioPreset[i];
                double linearVol = Math.Pow(10.0, ap.Volume_dB / 20.0);
                if (linearVol < 1e-5) linearVol = 1e-5;
                if (linearVol > 4.0)  linearVol = 4.0;
                audioTracks[i].Volume = (float)linearVol;
                audioTracks[i].Mute   = ap.Muted;
            }
        }

        // ── 7) Lay down clips back-to-back across all recordings ──────────
        Timecode cursor = Timecode.FromSeconds(0.0);
        double totalSeconds = 0.0;
        int totalEvents = 0;

        foreach (EventsFile ef in plans)
        {
            // Reuse the already-loaded Media for the first plan, AddMedia
            // for the rest. (AddMedia is idempotent on the same path, but
            // skipping the call avoids a needless lookup.)
            Media srcMedia = (ef.SourceVideo == plans[0].SourceVideo)
                              ? firstMedia
                              : proj.MediaPool.AddMedia(ef.SourceVideo);

            MediaStream srcVideo = null;
            List<MediaStream> srcAudio = new List<MediaStream>();
            foreach (MediaStream s in srcMedia.Streams)
            {
                if (s.MediaType == MediaType.Video && srcVideo == null)
                    srcVideo = s;
                else if (s.MediaType == MediaType.Audio)
                    srcAudio.Add(s);
            }
            if (srcVideo == null)
            {
                buildWarnings.Add(Path.GetFileName(ef.SourceVideo)
                    + ": no video stream — recording skipped");
                continue;
            }
            if (srcAudio.Count != firstAudioCount)
            {
                buildWarnings.Add(string.Format(
                    "{0}: audio stream count {1} differs from first recording's {2}",
                    Path.GetFileName(ef.SourceVideo),
                    srcAudio.Count, firstAudioCount));
            }

            foreach (HighlightEvent ev in ef.Events)
            {
                double srcStartSec = Math.Max(0.0,
                                              ev.TimestampSec - ev.PreSec);
                double srcEndSec   = ev.TimestampSec + ev.PostSec;
                double clipLenSec  = srcEndSec - srcStartSec;
                Timecode srcOffset = Timecode.FromSeconds(srcStartSec);
                Timecode clipLen   = Timecode.FromSeconds(clipLenSec);

                List<TrackEvent> linked = new List<TrackEvent>();

                for (int i = 0; i < videoTracks.Length; i++)
                {
                    VideoEvent vEvent = videoTracks[i].AddVideoEvent(
                        cursor, clipLen);
                    vEvent.AddTake(srcVideo, true);
                    vEvent.ActiveTake.Offset = srcOffset;
                    ApplyPanCrop(vEvent, ACTIVE_PRESET.Tracks[i]);
                    ApplyEffects(vEvent,
                                 ACTIVE_PRESET.Tracks[i].Effects,
                                 vegas, buildWarnings);
                    linked.Add(vEvent);
                }

                int audioCount = Math.Min(srcAudio.Count, audioTracks.Length);
                for (int i = 0; i < audioCount; i++)
                {
                    AudioEvent aEvent = audioTracks[i].AddAudioEvent(
                        cursor, clipLen);
                    aEvent.AddTake(srcAudio[i], true);
                    aEvent.ActiveTake.Offset = srcOffset;
                    linked.Add(aEvent);
                }

                TrackEventGroup group = new TrackEventGroup(proj);
                proj.Groups.Add(group);
                foreach (TrackEvent te in linked) group.Add(te);

                cursor += clipLen;
                totalSeconds += clipLenSec;
                totalEvents++;
            }
        }

        // ── 8) Summary dialog ───────────────────────────────────────────────
        System.Text.StringBuilder summary = new System.Text.StringBuilder();
        summary.AppendLine("HighlightBuilder (god folder mode) complete.");
        summary.AppendLine();
        summary.AppendFormat("God folder:    {0}\n", godName);
        summary.AppendFormat("Recordings:    {0} of {1} contributed kills\n",
            plans.Count, jsonPaths.Length);
        summary.AppendFormat("Preset:        {0} ({1}x{2} @ {3}fps)\n",
            ACTIVE_PRESET.Name,
            ACTIVE_PRESET.ProjectWidth,
            ACTIVE_PRESET.ProjectHeight,
            ACTIVE_PRESET.FrameRate);
        summary.AppendFormat("Kill events:   {0}\n", totalEvents);
        summary.AppendFormat("Video tracks:  {0}\n", videoTracks.Length);
        summary.AppendFormat("Audio tracks:  {0}\n", audioTracks.Length);
        summary.AppendFormat("Total length:  {0:0.0}s\n", totalSeconds);

        if (skipNotes.Count > 0)
        {
            summary.AppendLine();
            summary.AppendLine("Skipped recordings:");
            foreach (string s in skipNotes)
                summary.AppendFormat("  - {0}\n", s);
        }
        if (buildWarnings.Count > 0)
        {
            summary.AppendLine();
            summary.AppendLine("Warnings:");
            foreach (string w in buildWarnings)
                summary.AppendFormat("  ! {0}\n", w);
        }

        MessageBox.Show(summary.ToString(), "HighlightBuilder — done");
    }


    // ════════════════════════════════════════════════════════════════════════
    //   RESCAN-APPEND MODE
    //   ---------------------------------------------------------------------
    //   Activated by SCRIPT_MODE = "rescan_append".  Mirrors god_folder mode
    //   in spirit (load horizontal preset, lay clips back-to-back, audio
    //   routed per source-stream), but with three critical differences:
    //
    //     1) It reads a sidecar _rescan_diff.json (written by
    //        tools/rescan_events.py) instead of walking a god folder.
    //        Only the clips listed in that sidecar's `clips` array get
    //        imported — so a montage with 30 Atlas clips can have just
    //        the 4 "fixed" clips appended without disturbing anything.
    //
    //     2) It REUSES existing tracks instead of creating new ones.
    //        The assumption is that you've already built the project
    //        using god_folder mode (or extended it manually with the
    //        same horizontal preset) and the tracks already match the
    //        preset's video composition + audio routing.  If track
    //        counts don't match what the preset wants, we warn and
    //        proceed with whatever fits.
    //
    //     3) The clips are appended AT THE END of the current timeline,
    //        not from cursor=0.  The insert point is computed by walking
    //        every TrackEvent on every Track and taking the max
    //        Start+Length — robust to manual edits that may have
    //        extended one track past the others.
    //
    //   Duplicate footage in the project is expected and intended: the
    //   old version of each clip stays where it was on the timeline, the
    //   new (fixed) version lands at the end so you can compare and
    //   choose which to keep.
    // ════════════════════════════════════════════════════════════════════════

    // One entry in the _rescan_diff.json sidecar's "clips" array.  Only
    // the fields we actually consume are deserialised; everything else
    // (prior_counts, new_counts, gods_seen) is ignored by the Vegas
    // import path.  The Python side guarantees these are present for
    // every entry it writes.
    struct RescanClipRecord
    {
        public string Clip;          // e.g. "Atlas-6.mp4"
        public string VideoPath;     // absolute path to the .mp4
        public string EventsJson;    // absolute path to the sibling .events.json
        public string Category;      // "added" | "removed" | "mixed" | "new" | ...
    }

    void RunRescanAppend(Vegas vegas, Project proj, List<string> buildWarnings)
    {
        // ── 1) Load the horizontal preset ──────────────────────────────────
        // Same preset god_folder mode uses — append must produce visually
        // identical clips to what's already on the timeline.
        if (!File.Exists(HORIZONTAL_PRESET_FILE))
        {
            throw new Exception(
                "Horizontal preset not found:\n  " + HORIZONTAL_PRESET_FILE +
                "\n\nCapture one with TuneFrame.cs first, or edit " +
                "HORIZONTAL_PRESET_FILE at the top of this script.");
        }
        ACTIVE_PRESET = LoadPreset(HORIZONTAL_PRESET_FILE);

        // ── 2) Pick the _rescan_diff*.json sidecar via file dialog ─────────
        // Defaults to the recordings folder so the user can navigate into
        // recordings\<God>\ and pick the _rescan_diff.json the Python tool
        // dropped there.
        string diffPath = PickRescanDiffViaDialog(RECORDINGS_FOLDER);

        // ── 3) Parse the sidecar — get list of fixed clips ─────────────────
        List<RescanClipRecord> diffClips = ParseRescanDiff(diffPath);
        if (diffClips.Count == 0)
        {
            throw new Exception(
                "Rescan diff sidecar has no changed clips:\n  " + diffPath +
                "\n\nIf the python tool reported all clips as 'unchanged' " +
                "there is nothing to append.");
        }

        // ── 4) Load each clip's .events.json, filter to wanted event types ─
        // Same filter contract as god_folder mode so the appended clips
        // match the rest of the montage.  Skip-with-note on per-clip
        // problems so one broken sidecar entry doesn't tank the whole run.
        List<EventsFile> plans = new List<EventsFile>();
        List<string> skipNotes = new List<string>();
        foreach (RescanClipRecord rec in diffClips)
        {
            if (string.IsNullOrEmpty(rec.EventsJson)
                || !File.Exists(rec.EventsJson))
            {
                skipNotes.Add(rec.Clip + ": .events.json missing — "
                              + rec.EventsJson);
                continue;
            }
            EventsFile ef;
            try { ef = LoadEventsFile(rec.EventsJson); }
            catch (Exception ex)
            {
                skipNotes.Add(rec.Clip + ": parse failed — " + ex.Message);
                continue;
            }

            List<HighlightEvent> keep = new List<HighlightEvent>();
            foreach (HighlightEvent e in ef.Events)
            {
                foreach (string keepType in GOD_FOLDER_EVENT_TYPES)
                {
                    if (string.Equals(e.Type, keepType,
                                      StringComparison.OrdinalIgnoreCase))
                    {
                        keep.Add(e);
                        break;
                    }
                }
            }
            if (keep.Count == 0)
            {
                // Could legitimately happen if the rescan removed every
                // event (e.g. all were misreads) — that's a "removed"
                // category entry with zero clips to append.  Skip with a
                // note rather than error.
                skipNotes.Add(rec.Clip
                    + ": no events of types ["
                    + string.Join(", ", GOD_FOLDER_EVENT_TYPES)
                    + "] (category=" + rec.Category + ")");
                continue;
            }
            if (!File.Exists(ef.SourceVideo))
            {
                skipNotes.Add(rec.Clip + ": source missing — "
                              + ef.SourceVideo);
                continue;
            }
            ef.Events = keep;
            plans.Add(ef);
        }
        if (plans.Count == 0)
        {
            throw new Exception(
                "No appendable clips found in:\n  " + diffPath +
                "\n\nReasons:\n  " + string.Join("\n  ",
                                                 skipNotes.ToArray()));
        }

        // ── 5) Find the end of the current timeline ────────────────────────
        // We append from this point forward.  Computed across ALL events
        // on ALL tracks so a manually-extended track doesn't get
        // overlapped by our new clips.
        Timecode insertAt = FindTimelineEnd(proj);

        // ── 6) Resolve existing tracks to reuse ────────────────────────────
        // We deliberately do NOT create new tracks.  Caller is expected
        // to have built the project via god_folder mode (or extended it
        // by hand using the same preset).  ResolveExistingTracks finds
        // the first N video tracks and the first M audio tracks, where
        // N + M come from the preset, and warns on mismatch.
        int firstAudioCount = 0;
        Media firstMedia = proj.MediaPool.AddMedia(plans[0].SourceVideo);
        foreach (MediaStream s in firstMedia.Streams)
            if (s.MediaType == MediaType.Audio) firstAudioCount++;
        if (firstAudioCount == 0)
        {
            throw new Exception(
                "First fixed clip has no audio streams:\n  "
                + plans[0].SourceVideo);
        }

        VideoTrack[] videoTracks;
        AudioTrack[] audioTracks;
        ResolveExistingTracks(proj, ACTIVE_PRESET, firstAudioCount,
                              out videoTracks, out audioTracks,
                              buildWarnings);

        // ── 7) Lay down clips back-to-back starting at insertAt ────────────
        // Mirrors god_folder mode's clip-placement loop verbatim except
        // the cursor starts at insertAt instead of zero, and we don't
        // touch project dims (the existing project already has them set).
        Timecode cursor = insertAt;
        double totalSeconds = 0.0;
        int totalEvents = 0;

        foreach (EventsFile ef in plans)
        {
            Media srcMedia = (ef.SourceVideo == plans[0].SourceVideo)
                              ? firstMedia
                              : proj.MediaPool.AddMedia(ef.SourceVideo);

            MediaStream srcVideo = null;
            List<MediaStream> srcAudio = new List<MediaStream>();
            foreach (MediaStream s in srcMedia.Streams)
            {
                if (s.MediaType == MediaType.Video && srcVideo == null)
                    srcVideo = s;
                else if (s.MediaType == MediaType.Audio)
                    srcAudio.Add(s);
            }
            if (srcVideo == null)
            {
                buildWarnings.Add(Path.GetFileName(ef.SourceVideo)
                    + ": no video stream — recording skipped");
                continue;
            }
            if (srcAudio.Count != firstAudioCount)
            {
                buildWarnings.Add(string.Format(
                    "{0}: audio stream count {1} differs from first " +
                    "recording's {2}",
                    Path.GetFileName(ef.SourceVideo),
                    srcAudio.Count, firstAudioCount));
            }

            foreach (HighlightEvent ev in ef.Events)
            {
                double srcStartSec = Math.Max(0.0,
                                              ev.TimestampSec - ev.PreSec);
                double srcEndSec   = ev.TimestampSec + ev.PostSec;
                double clipLenSec  = srcEndSec - srcStartSec;
                Timecode srcOffset = Timecode.FromSeconds(srcStartSec);
                Timecode clipLen   = Timecode.FromSeconds(clipLenSec);

                List<TrackEvent> linked = new List<TrackEvent>();

                for (int i = 0; i < videoTracks.Length; i++)
                {
                    VideoEvent vEvent = videoTracks[i].AddVideoEvent(
                        cursor, clipLen);
                    vEvent.AddTake(srcVideo, true);
                    vEvent.ActiveTake.Offset = srcOffset;
                    ApplyPanCrop(vEvent, ACTIVE_PRESET.Tracks[i]);
                    ApplyEffects(vEvent,
                                 ACTIVE_PRESET.Tracks[i].Effects,
                                 vegas, buildWarnings);
                    linked.Add(vEvent);
                }

                int audioCount = Math.Min(srcAudio.Count, audioTracks.Length);
                for (int i = 0; i < audioCount; i++)
                {
                    AudioEvent aEvent = audioTracks[i].AddAudioEvent(
                        cursor, clipLen);
                    aEvent.AddTake(srcAudio[i], true);
                    aEvent.ActiveTake.Offset = srcOffset;
                    linked.Add(aEvent);
                }

                TrackEventGroup group = new TrackEventGroup(proj);
                proj.Groups.Add(group);
                foreach (TrackEvent te in linked) group.Add(te);

                cursor += clipLen;
                totalSeconds += clipLenSec;
                totalEvents++;
            }
        }

        // ── 8) Summary dialog ───────────────────────────────────────────────
        System.Text.StringBuilder summary = new System.Text.StringBuilder();
        summary.AppendLine("HighlightBuilder (rescan-append mode) complete.");
        summary.AppendLine();
        summary.AppendFormat("Diff sidecar:  {0}\n", Path.GetFileName(diffPath));
        summary.AppendFormat("Changed clips: {0} listed, {1} appended\n",
            diffClips.Count, plans.Count);
        summary.AppendFormat("Insert point:  {0} "
                             + "(end of existing timeline)\n",
                             insertAt.ToString());
        summary.AppendFormat("Events added:  {0}\n", totalEvents);
        summary.AppendFormat("Video tracks:  {0} (reused)\n",
                             videoTracks.Length);
        summary.AppendFormat("Audio tracks:  {0} (reused)\n",
                             audioTracks.Length);
        summary.AppendFormat("Appended len:  {0:0.0}s\n", totalSeconds);

        if (skipNotes.Count > 0)
        {
            summary.AppendLine();
            summary.AppendLine("Skipped clips:");
            foreach (string s in skipNotes)
                summary.AppendFormat("  - {0}\n", s);
        }
        if (buildWarnings.Count > 0)
        {
            summary.AppendLine();
            summary.AppendLine("Warnings:");
            foreach (string w in buildWarnings)
                summary.AppendFormat("  ! {0}\n", w);
        }

        MessageBox.Show(summary.ToString(), "HighlightBuilder — done");
    }


    // Show an OpenFileDialog filtered to _rescan_diff*.json, defaulting
    // to the recordings folder + pre-selecting the newest match.  Same
    // UX shape as PickEventsJsonViaDialog.
    static string PickRescanDiffViaDialog(string defaultFolder)
    {
        using (OpenFileDialog dlg = new OpenFileDialog())
        {
            dlg.Title  = "Choose a _rescan_diff.json for HighlightBuilder";
            dlg.Filter =
                "Rescan diff (_rescan_diff*.json)|_rescan_diff*.json|"
                + "Any JSON (*.json)|*.json|All files (*.*)|*.*";
            dlg.CheckFileExists  = true;
            dlg.Multiselect      = false;
            dlg.RestoreDirectory = true;

            if (Directory.Exists(defaultFolder))
            {
                dlg.InitialDirectory = defaultFolder;
                FileInfo[] files = new DirectoryInfo(defaultFolder)
                                       .GetFiles("_rescan_diff*.json",
                                                 SearchOption.AllDirectories);
                if (files.Length > 0)
                {
                    FileInfo newest = files[0];
                    for (int i = 1; i < files.Length; i++)
                        if (files[i].LastWriteTime > newest.LastWriteTime)
                            newest = files[i];
                    dlg.FileName = newest.FullName;
                }
            }

            if (dlg.ShowDialog() != DialogResult.OK)
                throw new Exception(
                    "No file chosen — HighlightBuilder cancelled.");

            return dlg.FileName;
        }
    }


    // Parse _rescan_diff.json.  Reuses the existing JSON helpers
    // (FindMatchingBracket / SplitTopLevelObjects / TryGetField etc.)
    // so we don't depend on Newtonsoft.Json.  Refuses to load anything
    // with schema_version != 1 so a future format change can't silently
    // misimport.
    static List<RescanClipRecord> ParseRescanDiff(string path)
    {
        string text = File.ReadAllText(path);

        // Schema version guard — fail loudly on mismatch instead of
        // silently picking the wrong fields.
        string sv = TryGetField(text, "schema_version");
        if (sv != null && sv.Trim() != "1")
        {
            throw new Exception(
                "Rescan diff schema_version=" + sv + " is not supported by "
                + "this version of HighlightBuilder (expected 1).  Update "
                + "the script or downgrade tools/rescan_events.py.");
        }

        // Locate the "clips": [...] array body.
        int keyIdx = text.IndexOf("\"clips\"");
        if (keyIdx < 0)
            throw new Exception(
                "Rescan diff is missing required 'clips' array: " + path);
        int arrStart = text.IndexOf('[', keyIdx);
        if (arrStart < 0)
            throw new Exception(
                "Rescan diff 'clips' field is not an array: " + path);
        int arrEnd = FindMatchingBracket(text, arrStart);
        string body = text.Substring(arrStart + 1, arrEnd - arrStart - 1);

        List<RescanClipRecord> result = new List<RescanClipRecord>();
        foreach (string obj in SplitTopLevelObjects(body))
        {
            RescanClipRecord r = new RescanClipRecord();
            r.Clip       = GetStringFieldOr(obj, "clip", "");
            r.VideoPath  = GetStringFieldOr(obj, "video_path", "");
            r.EventsJson = GetStringFieldOr(obj, "events_json", "");
            r.Category   = GetStringFieldOr(obj, "category", "");
            result.Add(r);
        }
        return result;
    }


    // Walk every TrackEvent on every Track and return the max Start +
    // Length as a Timecode.  Empty project → Timecode.FromSeconds(0).
    // This is the "insert at the very end" anchor used by
    // RunRescanAppend so a manually-extended track (e.g. an audio
    // crossfade that runs past the last video clip) doesn't get
    // overlapped by the new clips.
    static Timecode FindTimelineEnd(Project proj)
    {
        Timecode end = Timecode.FromSeconds(0.0);
        foreach (Track t in proj.Tracks)
        {
            foreach (TrackEvent ev in t.Events)
            {
                Timecode evEnd = ev.Start + ev.Length;
                if (evEnd > end) end = evEnd;
            }
        }
        return end;
    }


    // Find the first N video tracks and first M audio tracks in the
    // current project, where N + M come from the preset.  Used by
    // RunRescanAppend so we reuse the project's existing tracks
    // instead of stacking duplicates.  Warns (does not throw) on track
    // count mismatch so the script can still do useful work on a
    // partially-set-up project.
    static void ResolveExistingTracks(
        Project proj,
        FramePreset preset,
        int audioCountNeeded,
        out VideoTrack[] videoTracks,
        out AudioTrack[] audioTracks,
        List<string> warnings)
    {
        // Collect every video + audio track in project order.
        List<VideoTrack> allVideo = new List<VideoTrack>();
        List<AudioTrack> allAudio = new List<AudioTrack>();
        foreach (Track t in proj.Tracks)
        {
            VideoTrack vt = t as VideoTrack;
            if (vt != null) { allVideo.Add(vt); continue; }
            AudioTrack at = t as AudioTrack;
            if (at != null) { allAudio.Add(at); continue; }
        }

        int videoNeed = preset.Tracks.Length;
        if (allVideo.Count < videoNeed)
        {
            throw new Exception(
                "Rescan-append needs " + videoNeed + " existing video "
                + "track(s) (one per preset video layer) but the project "
                + "only has " + allVideo.Count + ".\n\nRun god_folder "
                + "mode first to set up the tracks, then run "
                + "rescan_append on the same project.");
        }
        if (allAudio.Count < audioCountNeeded)
        {
            warnings.Add(string.Format(
                "Project has only {0} audio track(s) but the first fixed "
                + "clip has {1} audio stream(s).  Extra streams will be "
                + "dropped on the floor.",
                allAudio.Count, audioCountNeeded));
        }

        // Take the first N video / first M audio.  This matches how
        // god_folder mode built them (video first, then audio, appended
        // in preset order).
        videoTracks = new VideoTrack[videoNeed];
        for (int i = 0; i < videoNeed; i++) videoTracks[i] = allVideo[i];

        int audioTake = Math.Min(allAudio.Count, audioCountNeeded);
        audioTracks = new AudioTrack[audioTake];
        for (int i = 0; i < audioTake; i++) audioTracks[i] = allAudio[i];
    }


    // Show a simple list-of-folders dialog rooted at `recordingsRoot`.
    // We use a custom Form with a ListBox rather than FolderBrowserDialog
    // because the latter shows the full Windows tree and you'd have to
    // navigate; a ListBox of just the immediate subfolder names is one
    // click away from "pick Ymir."
    //
    // Returns the absolute path of the chosen subfolder. Throws on
    // cancel — the outer try/catch turns that into a polite dialog.
    static string PickGodFolder(string recordingsRoot)
    {
        if (!Directory.Exists(recordingsRoot))
        {
            throw new Exception(
                "RECORDINGS_FOLDER does not exist:\n  " + recordingsRoot);
        }

        DirectoryInfo[] subs = new DirectoryInfo(recordingsRoot).GetDirectories();
        if (subs.Length == 0)
        {
            throw new Exception(
                "No god folders found under:\n  " + recordingsRoot +
                "\n\nDrop recordings into recordings\\ and run " +
                "process_recordings.py to sort them into per-god subfolders.");
        }

        Array.Sort(subs, delegate(DirectoryInfo a, DirectoryInfo b) {
            return string.Compare(a.Name, b.Name,
                                  StringComparison.OrdinalIgnoreCase);
        });

        using (Form dlg = new Form())
        {
            dlg.Text             = "HighlightBuilder — pick a god folder";
            dlg.Width            = 360;
            dlg.Height           = 520;
            dlg.StartPosition    = FormStartPosition.CenterScreen;
            dlg.MinimizeBox      = false;
            dlg.MaximizeBox      = false;
            dlg.FormBorderStyle  = FormBorderStyle.FixedDialog;

            Label header = new Label();
            header.Text    = "Pick a god folder under recordings\\:";
            header.Dock    = DockStyle.Top;
            header.Height  = 26;
            header.Padding = new Padding(8, 6, 0, 0);

            ListBox lb = new ListBox();
            lb.Dock           = DockStyle.Fill;
            lb.IntegralHeight = false;
            foreach (DirectoryInfo sub in subs) lb.Items.Add(sub.Name);
            if (lb.Items.Count > 0) lb.SelectedIndex = 0;

            Panel buttonPanel = new Panel();
            buttonPanel.Dock   = DockStyle.Bottom;
            buttonPanel.Height = 44;

            Button ok = new Button();
            ok.Text         = "OK";
            ok.DialogResult = DialogResult.OK;
            ok.SetBounds(168, 8, 80, 28);
            ok.Anchor       = AnchorStyles.Right | AnchorStyles.Top;

            Button cancel = new Button();
            cancel.Text         = "Cancel";
            cancel.DialogResult = DialogResult.Cancel;
            cancel.SetBounds(258, 8, 80, 28);
            cancel.Anchor       = AnchorStyles.Right | AnchorStyles.Top;

            buttonPanel.Controls.Add(ok);
            buttonPanel.Controls.Add(cancel);

            // Double-click on a list item also accepts.
            lb.DoubleClick += delegate(object s, EventArgs e) {
                dlg.DialogResult = DialogResult.OK;
                dlg.Close();
            };

            // Add order: Fill must be added LAST relative to docked
            // siblings so it claims the remaining space correctly.
            dlg.Controls.Add(lb);
            dlg.Controls.Add(buttonPanel);
            dlg.Controls.Add(header);
            dlg.AcceptButton = ok;
            dlg.CancelButton = cancel;

            if (dlg.ShowDialog() != DialogResult.OK || lb.SelectedIndex < 0)
            {
                throw new Exception(
                    "No god folder chosen — HighlightBuilder cancelled.");
            }

            return Path.Combine(recordingsRoot, (string)lb.SelectedItem);
        }
    }


    // Natural-order string comparison so "Ymir-2" sorts before "Ymir-10".
    // Walks both strings in lockstep, comparing runs of digits as numeric
    // values and runs of non-digits as case-insensitive characters.
    // Lifted from the canonical implementation pattern; works for any
    // ASCII filename.
    static int NaturalCompare(string a, string b)
    {
        int ai = 0, bi = 0;
        while (ai < a.Length && bi < b.Length)
        {
            if (char.IsDigit(a[ai]) && char.IsDigit(b[bi]))
            {
                int aStart = ai;
                while (ai < a.Length && char.IsDigit(a[ai])) ai++;
                int bStart = bi;
                while (bi < b.Length && char.IsDigit(b[bi])) bi++;
                long anum = long.Parse(a.Substring(aStart, ai - aStart));
                long bnum = long.Parse(b.Substring(bStart, bi - bStart));
                if (anum != bnum) return anum.CompareTo(bnum);
            }
            else
            {
                int cmp = char.ToLowerInvariant(a[ai])
                              .CompareTo(char.ToLowerInvariant(b[bi]));
                if (cmp != 0) return cmp;
                ai++; bi++;
            }
        }
        return a.Length.CompareTo(b.Length);
    }


    // ════════════════════════════════════════════════════════════════════════
    //   EVENTS-FILE LOADING
    // ════════════════════════════════════════════════════════════════════════
    //
    //   The Python event extractor emits one `<videoname>.events.json`
    //   file per recording. HighlightBuilder scans EVENTS_FOLDER for
    //   these files, picks the newest, and parses it.
    //
    //   JSON shape:
    //     {
    //       "source_video": "C:\\Users\\james\\Videos\\match.mp4",
    //       "events": [
    //         {
    //           "timestamp_sec": 143.2,
    //           "type": "kill",
    //           "note": "first blood",
    //           "pre_sec": 5.0,
    //           "post_sec": 4.0
    //         },
    //         ...
    //       ]
    //     }
    //
    //   Why a hand-rolled parser instead of Newtonsoft.Json or
    //   System.Text.Json: Vegas's script runtime doesn't reliably have
    //   those libraries referenced, and wiring an external DLL into a
    //   Script Menu script is fiddly. The shape is small and fixed, so
    //   a few hundred lines of careful Regex + bracket-walking covers
    //   it without any dependency.
    //
    //   If the Python side ever adds a field, the parser will just
    //   ignore unknown keys — only `timestamp_sec` and `type` are
    //   required; `note`, `pre_sec`, `post_sec` fall back to defaults.
    // ════════════════════════════════════════════════════════════════════════

    // Scan folder (recursively, including all subfolders) for *.events.json
    // and return the most recently modified.  Throws if there aren't any —
    // the outer try/catch shows the message.
    //
    // Recursion is important because process_recordings.py sorts each
    // recording into a per-god subfolder (e.g. recordings/Ymir/Ymir-3.mp4
    // alongside Ymir-3.events.json).  After that reorg the root folder
    // is empty by design — the queue gets drained as files move into
    // their god buckets — so a non-recursive scan would always throw
    // "no events files."
    static string FindNewestEventsJson(string folder)
    {
        if (!Directory.Exists(folder))
            throw new Exception("EVENTS_FOLDER does not exist: " + folder);

        // DirectoryInfo.GetFiles with SearchOption.AllDirectories walks
        // every subfolder.  The filter "*.events.json" matches Python's
        // naming convention.
        DirectoryInfo dir = new DirectoryInfo(folder);
        FileInfo[] files = dir.GetFiles(
            "*.events.json", SearchOption.AllDirectories);

        if (files.Length == 0)
            throw new Exception(
                "No *.events.json files found anywhere under:\n  " + folder +
                "\n\nRun the Python event extractor first, or copy an " +
                "events file into that folder (or any of its subfolders).");

        // Manual max-by-LastWriteTime (avoids pulling in LINQ).
        FileInfo newest = files[0];
        for (int i = 1; i < files.Length; i++)
        {
            if (files[i].LastWriteTime > newest.LastWriteTime)
                newest = files[i];
        }
        return newest.FullName;
    }


    // Show a Windows OpenFileDialog filtered to *.events.json. We open it
    // in `defaultFolder` and pre-select the newest *.events.json there
    // (if any), so the common case is "just hit Enter to take the
    // newest one." If the user navigates elsewhere and picks a file, we
    // use that. If the user cancels, we throw — the outer try/catch
    // turns that into a polite "cancelled" dialog.
    //
    // Note on `using (...)`: this is C#'s deterministic-disposal block
    // (analogous to Python's `with`). When the block exits, dlg.Dispose()
    // is called automatically, releasing the unmanaged dialog resources.
    static string PickEventsJsonViaDialog(string defaultFolder)
    {
        using (OpenFileDialog dlg = new OpenFileDialog())
        {
            dlg.Title  = "Choose an events.json for HighlightBuilder";
            // Filter syntax: "Display name|*.pattern". Pipes separate
            // entries; a second entry "All files|*.*" is convenient.
            dlg.Filter = "Events JSON (*.events.json)|*.events.json|All files (*.*)|*.*";
            dlg.CheckFileExists  = true;
            dlg.Multiselect      = false;
            dlg.RestoreDirectory = true;

            // Pre-fill the dialog. If `defaultFolder` exists, start there;
            // and if it (or any subfolder) has any *.events.json files,
            // pre-select the newest one.  Recursive search is required
            // because process_recordings.py sorts each recording into a
            // per-god subfolder, so the newest events file will almost
            // always live one level down (e.g. EVENTS_FOLDER/Ymir/Ymir-7
            // .events.json).  Setting dlg.FileName to a path inside a
            // subfolder makes the dialog open in that subfolder with the
            // newest file already highlighted, which is exactly what the
            // user wants — Enter just works.
            if (Directory.Exists(defaultFolder))
            {
                dlg.InitialDirectory = defaultFolder;
                FileInfo[] files = new DirectoryInfo(defaultFolder)
                                       .GetFiles("*.events.json",
                                                 SearchOption.AllDirectories);
                if (files.Length > 0)
                {
                    FileInfo newest = files[0];
                    for (int i = 1; i < files.Length; i++)
                        if (files[i].LastWriteTime > newest.LastWriteTime)
                            newest = files[i];
                    dlg.FileName = newest.FullName;
                }
            }

            // ShowDialog returns DialogResult.OK on accept, Cancel on cancel.
            if (dlg.ShowDialog() != DialogResult.OK)
                throw new Exception(
                    "No file chosen — HighlightBuilder cancelled.");

            return dlg.FileName;
        }
    }


    // Read a .tune.json preset from disk. Fails loudly on missing required
    // fields or unknown enum values so we never silently apply a wrong
    // preset. Composite mode and keyframe type come back as strings and
    // are parsed with Enum.Parse(..., ignoreCase: true).
    static FramePreset LoadPreset(string path)
    {
        string text = File.ReadAllText(path);

        FramePreset fp = new FramePreset();
        fp.Name = TryGetField(text, "name") ?? "(unnamed)";
        fp.Kind = TryGetField(text, "kind") ?? "unknown";

        // project: { width, height, framerate }
        int projKeyIdx = text.IndexOf("\"project\"");
        if (projKeyIdx < 0)
            throw new Exception("Preset JSON is missing 'project' object: " + path);
        int projObjStart = text.IndexOf('{', projKeyIdx);
        int projObjEnd   = FindMatchingBracket(text, projObjStart);
        string projBody  = text.Substring(projObjStart, projObjEnd - projObjStart + 1);
        fp.ProjectWidth  = (int)ParseInvariantDouble(RequireField(projBody, "width"));
        fp.ProjectHeight = (int)ParseInvariantDouble(RequireField(projBody, "height"));
        fp.FrameRate     = ParseInvariantDouble(RequireField(projBody, "framerate"));

        // video_tracks: [ ... ]
        fp.Tracks = ParseVideoTracks(text);

        // audio_tracks: [ ... ]
        fp.AudioTracks = ParseAudioTracks(text);

        return fp;
    }

    static TrackPreset[] ParseVideoTracks(string text)
    {
        int keyIdx = text.IndexOf("\"video_tracks\"");
        if (keyIdx < 0) return new TrackPreset[0];
        int arrStart = text.IndexOf('[', keyIdx);
        int arrEnd   = FindMatchingBracket(text, arrStart);
        string body  = text.Substring(arrStart + 1, arrEnd - arrStart - 1);

        List<TrackPreset> list = new List<TrackPreset>();
        foreach (string objText in SplitTopLevelObjects(body))
        {
            TrackPreset tp = new TrackPreset();
            tp.Name = GetStringFieldOr(objText, "name", "");

            // Skip HighlightBuilder's synthesized "Titles" track if the
            // preset captured one (it's a full-frame passthrough created
            // for intro/outro cards, not part of the gameplay composition).
            // A preset's Titles track would stack above the gameplay tracks
            // and hide them. HighlightBuilder creates its own Titles track
            // independently — see Run()'s `titlesTrack` construction.
            if (string.Equals(tp.Name, "Titles", StringComparison.OrdinalIgnoreCase))
                continue;

            string compStr = GetStringFieldOr(objText, "composite", "SrcAlpha");
            tp.Composite = (CompositeMode)Enum.Parse(
                typeof(CompositeMode), compStr, /* ignoreCase */ true);

            // Each corner is a nested {x, y} object — extract by regex.
            ReadXY(objText, "top_left",     out tp.TopLeftX,     out tp.TopLeftY);
            ReadXY(objText, "top_right",    out tp.TopRightX,    out tp.TopRightY);
            ReadXY(objText, "bottom_right", out tp.BottomRightX, out tp.BottomRightY);
            ReadXY(objText, "bottom_left",  out tp.BottomLeftX,  out tp.BottomLeftY);
            ReadXY(objText, "center",       out tp.CenterX,      out tp.CenterY);

            tp.Rotation    = GetDoubleFieldOr(objText, "rotation_rad", 0.0);
            tp.Smoothness  = GetDoubleFieldOr(objText, "smoothness",   0.0);
            string kfStr   = GetStringFieldOr(objText, "keyframe_type", "Linear");
            tp.KeyframeType = (VideoKeyframeType)Enum.Parse(
                typeof(VideoKeyframeType), kfStr, /* ignoreCase */ true);

            // scale_to_fill — older presets (schema v1 without this field)
            // default to true, matching Vegas's native default.
            string scale = TryGetField(objText, "scale_to_fill");
            tp.ScaleToFill = (scale == null) ||
                             scale.Trim().Equals("true",
                                 StringComparison.OrdinalIgnoreCase);

            // effects array — older presets without it default to empty.
            tp.Effects = ParseEffects(objText);

            list.Add(tp);
        }
        return list.ToArray();
    }

    static AudioPreset[] ParseAudioTracks(string text)
    {
        int keyIdx = text.IndexOf("\"audio_tracks\"");
        if (keyIdx < 0) return new AudioPreset[0];
        int arrStart = text.IndexOf('[', keyIdx);
        int arrEnd   = FindMatchingBracket(text, arrStart);
        string body  = text.Substring(arrStart + 1, arrEnd - arrStart - 1);

        List<AudioPreset> list = new List<AudioPreset>();
        foreach (string objText in SplitTopLevelObjects(body))
        {
            AudioPreset ap = new AudioPreset();
            ap.Name      = GetStringFieldOr(objText, "name", "");
            ap.Volume_dB = GetDoubleFieldOr(objText, "volume_db", 0.0);
            string muted = TryGetField(objText, "muted");
            ap.Muted     = (muted != null &&
                            muted.Trim().Equals("true",
                                StringComparison.OrdinalIgnoreCase));
            list.Add(ap);
        }
        return list.ToArray();
    }

    // Parse the "effects": [...] array within a video-track object. Each
    // entry is { plugin_id, plugin_name, bypass, parameters: [...] }.
    // Older schema-v1 presets without the field return empty.
    static EffectPreset[] ParseEffects(string parentObj)
    {
        int keyIdx = parentObj.IndexOf("\"effects\"");
        if (keyIdx < 0) return new EffectPreset[0];
        int arrStart = parentObj.IndexOf('[', keyIdx);
        if (arrStart < 0) return new EffectPreset[0];
        int arrEnd   = FindMatchingBracket(parentObj, arrStart);
        string body  = parentObj.Substring(arrStart + 1, arrEnd - arrStart - 1);

        List<EffectPreset> result = new List<EffectPreset>();
        foreach (string effectObj in SplitTopLevelObjects(body))
        {
            EffectPreset ep = new EffectPreset();
            ep.PluginId   = GetStringFieldOr(effectObj, "plugin_id", "");
            ep.PluginName = GetStringFieldOr(effectObj, "plugin_name", "");
            string byp    = TryGetField(effectObj, "bypass");
            ep.Bypass     = (byp != null) &&
                            byp.Trim().Equals("true", StringComparison.OrdinalIgnoreCase);
            ep.Parameters = ParseParameters(effectObj);
            result.Add(ep);
        }
        return result.ToArray();
    }

    static ParamPreset[] ParseParameters(string effectObj)
    {
        int keyIdx = effectObj.IndexOf("\"parameters\"");
        if (keyIdx < 0) return new ParamPreset[0];
        int arrStart = effectObj.IndexOf('[', keyIdx);
        if (arrStart < 0) return new ParamPreset[0];
        int arrEnd   = FindMatchingBracket(effectObj, arrStart);
        string body  = effectObj.Substring(arrStart + 1, arrEnd - arrStart - 1);

        List<ParamPreset> result = new List<ParamPreset>();
        foreach (string paramObj in SplitTopLevelObjects(body))
        {
            ParamPreset pp = new ParamPreset();
            pp.Name = GetStringFieldOr(paramObj, "name", "");
            pp.Type = GetStringFieldOr(paramObj, "type", "String");

            string rawValue = TryGetField(paramObj, "value");
            switch (pp.Type)
            {
                case "Boolean":
                    pp.BoolValue = rawValue != null &&
                        rawValue.Trim().Equals("true", StringComparison.OrdinalIgnoreCase);
                    break;
                case "Double":
                    pp.DoubleValue = rawValue != null ? ParseInvariantDouble(rawValue) : 0.0;
                    break;
                case "Integer":
                    pp.IntValue = rawValue != null ?
                        (int)ParseInvariantDouble(rawValue) : 0;
                    break;
                case "Double2D":
                    // Nested {"x": ..., "y": ...} — reuse ReadXY logic.
                    ReadXY(paramObj, "value", out pp.XValue, out pp.YValue);
                    break;
                default:
                    // Choice / String / Custom / anything else — JSON string.
                    pp.StringValue = rawValue != null ? UnescapeJsonString(rawValue) : "";
                    break;
            }
            result.Add(pp);
        }
        return result.ToArray();
    }


    // ────────────────────────────────────────────────────────────────────────
    //   EFFECT APPLICATION
    // ────────────────────────────────────────────────────────────────────────

    // Apply the preset's Effects to a freshly-created VideoEvent. For each
    // EffectPreset we look up the plugin by UniqueID in vegas.VideoFX,
    // add it to the event's Effects collection, then set each parameter
    // by name. Missing plugins or parameters are logged via out-param
    // warnings but don't abort — the timeline still builds.
    static void ApplyEffects(VideoEvent vEvent, EffectPreset[] effects,
                             Vegas vegas, List<string> warnings)
    {
        if (effects == null || effects.Length == 0) return;

        foreach (EffectPreset ep in effects)
        {
            PlugInNode plugin = FindPluginByUniqueID(vegas.VideoFX, ep.PluginId);
            if (plugin == null)
            {
                warnings.Add(string.Format(
                    "Plugin not found — skipping effect: {0} ({1})",
                    ep.PluginName, ep.PluginId));
                continue;
            }

            Effect effect;
            try { effect = vEvent.Effects.AddEffect(plugin); }
            catch (Exception ex)
            {
                warnings.Add("AddEffect failed for " + ep.PluginName + ": " + ex.Message);
                continue;
            }
            effect.Bypass = ep.Bypass;

            OFXEffect ofx = effect.OFXEffect;
            if (ofx == null)
            {
                warnings.Add(ep.PluginName + " has no OFXEffect — parameters skipped.");
                continue;
            }

            Dictionary<string, OFXParameter> byName = new Dictionary<string, OFXParameter>();
            foreach (OFXParameter p in ofx.Parameters) byName[p.Name] = p;

            foreach (ParamPreset pp in ep.Parameters)
            {
                OFXParameter target;
                if (!byName.TryGetValue(pp.Name, out target)) continue;
                ApplyParam(target, pp, warnings);
            }
        }
    }

    // Recursive plugin-tree walk to find the OFX plugin with the given
    // UniqueID. Returns null if not found. UniqueID comes from the
    // .tune.json preset; TuneFrame.cs captured it from PlugInNode.UniqueID
    // at the time the preset was tuned.
    static PlugInNode FindPluginByUniqueID(PlugInNode root, string uniqueID)
    {
        if (string.IsNullOrEmpty(uniqueID) || root == null) return null;
        foreach (PlugInNode child in root)
        {
            if (child.UniqueID == uniqueID) return child;
            PlugInNode deep = FindPluginByUniqueID(child, uniqueID);
            if (deep != null) return deep;
        }
        return null;
    }

    // Set one OFXParameter from a ParamPreset. Dispatches on pp.Type
    // (captured by TuneFrame.cs) into the typed setter for each parameter
    // kind. Most are direct casts; OFXDouble2D is special-cased because
    // the SDK exposes its Value via .X and .Y FIELDS (not properties) on
    // some installs, so we go through reflection via TrySetXY to handle
    // both shapes.
    static void ApplyParam(OFXParameter p, ParamPreset pp, List<string> warnings)
    {
        try
        {
            switch (pp.Type)
            {
                case "Boolean":
                    ((OFXBooleanParameter)p).Value = pp.BoolValue;
                    return;
                case "Double":
                    ((OFXDoubleParameter)p).Value = pp.DoubleValue;
                    return;
                case "Integer":
                    ((OFXIntegerParameter)p).Value = pp.IntValue;
                    return;
                case "Choice":
                    OFXChoiceParameter cp = (OFXChoiceParameter)p;
                    foreach (OFXChoice ch in cp.Choices)
                    {
                        if (ch.Name == pp.StringValue) { cp.Value = ch; return; }
                    }
                    warnings.Add("Choice '" + pp.StringValue + "' not found for "
                        + pp.Name);
                    return;
                case "Double2D":
                    PropertyInfo valProp2D = p.GetType().GetProperty("Value",
                        BindingFlags.Public | BindingFlags.Instance);
                    if (valProp2D == null)
                    {
                        warnings.Add("No Value property on Double2D param " + pp.Name);
                        return;
                    }
                    object box;
                    try { box = valProp2D.GetValue(p, null); }
                    catch { box = null; }
                    if (box == null)
                        box = Activator.CreateInstance(valProp2D.PropertyType);
                    if (!TrySetXY(box, pp.XValue, pp.YValue))
                    {
                        warnings.Add("Could not set X/Y on OFXDouble2D for " + pp.Name);
                        return;
                    }
                    valProp2D.SetValue(p, box, null);
                    return;
                default:
                    PropertyInfo valProp = p.GetType().GetProperty(
                        "Value", BindingFlags.Public | BindingFlags.Instance);
                    if (valProp != null && valProp.CanWrite)
                        valProp.SetValue(p, pp.StringValue, null);
                    return;
            }
        }
        catch (Exception ex)
        {
            warnings.Add("Set " + pp.Name + " (" + pp.Type + ") failed: " + ex.Message);
        }
    }

    // Reflectively set X/Y on an OFXDouble2D-shaped object. Tries FIELDS
    // first (the standard layout: public double X, Y), then falls back
    // to PROPERTIES with several common name variants (X/Y, x/y, U/V,
    // u/v) for whatever the local SDK happens to expose. Returns false
    // only if neither path works; the caller logs that as a warning.
    static bool TrySetXY(object v, double x, double y)
    {
        if (v == null) return false;
        Type t = v.GetType();
        FieldInfo xf = t.GetField("X", BindingFlags.Public | BindingFlags.Instance);
        FieldInfo yf = t.GetField("Y", BindingFlags.Public | BindingFlags.Instance);
        if (xf != null && yf != null)
        {
            try
            {
                xf.SetValue(v, Convert.ChangeType(x, xf.FieldType));
                yf.SetValue(v, Convert.ChangeType(y, yf.FieldType));
                return true;
            }
            catch { }
        }
        string[] xNames = { "X", "x", "U", "u" };
        string[] yNames = { "Y", "y", "V", "v" };
        for (int i = 0; i < xNames.Length; i++)
        {
            PropertyInfo xp = t.GetProperty(xNames[i],
                BindingFlags.Public | BindingFlags.Instance);
            PropertyInfo yp = t.GetProperty(yNames[i],
                BindingFlags.Public | BindingFlags.Instance);
            if (xp == null || yp == null || !xp.CanWrite || !yp.CanWrite) continue;
            try
            {
                xp.SetValue(v, Convert.ChangeType(x, xp.PropertyType), null);
                yp.SetValue(v, Convert.ChangeType(y, yp.PropertyType), null);
                return true;
            }
            catch { continue; }
        }
        return false;
    }

    // Extract a nested {"x": ..., "y": ...} object from a JSON string by
    // regex. Used by ParseVideoTracks for the four Pan/Crop corner
    // vertices + center, and by ParseParameters for OFXDouble2D values.
    // Throws on a malformed match so a corrupted preset surfaces loudly
    // instead of silently zeroing the coordinate.
    static void ReadXY(string obj, string fieldName, out double x, out double y)
    {
        string pattern = "\"" + Regex.Escape(fieldName) + "\"\\s*:\\s*\\{"
                       + "\\s*\"x\"\\s*:\\s*(-?[0-9eE+\\-.]+)"
                       + "\\s*,\\s*\"y\"\\s*:\\s*(-?[0-9eE+\\-.]+)"
                       + "\\s*\\}";
        Match m = Regex.Match(obj, pattern);
        if (!m.Success)
            throw new Exception("Could not parse '" + fieldName + "' as {x,y}: " + obj);
        x = ParseInvariantDouble(m.Groups[1].Value);
        y = ParseInvariantDouble(m.Groups[2].Value);
    }


    // ════════════════════════════════════════════════════════════════════════
    //   EVENTS-FILE LOADING
    // ════════════════════════════════════════════════════════════════════════
    //
    //   The Python event extractor emits one `<videoname>.events.json`
    //   file per recording. HighlightBuilder scans EVENTS_FOLDER for
    //   these files, picks the newest, and parses it.
    //
    //   JSON shape:
    //     {
    //       "source_video": "C:\\Users\\james\\Videos\\match.mp4",
    //       "events": [
    //         {
    //           "timestamp_sec": 143.2,
    //           "type": "kill",
    //           "note": "first blood",
    //           "pre_sec": 5.0,
    //           "post_sec": 4.0
    //         },
    //         ...
    //       ]
    //     }
    //
    //   Why a hand-rolled parser instead of Newtonsoft.Json or
    //   System.Text.Json: Vegas's script runtime doesn't reliably have
    //   those libraries referenced, and wiring an external DLL into a
    //   Script Menu script is fiddly. The shape is small and fixed, so
    //   a few hundred lines of careful Regex + bracket-walking covers
    //   it without any dependency.
    //
    //   If the Python side ever adds a field, the parser will just
    //   ignore unknown keys — only `timestamp_sec` and `type` are
    //   required; `note`, `pre_sec`, `post_sec` fall back to defaults.
    // ════════════════════════════════════════════════════════════════════════

    // Read a JSON file from disk and parse it into an EventsFile.
    static EventsFile LoadEventsFile(string path)
    {
        string text = File.ReadAllText(path);
        EventsFile ef = new EventsFile();
        ef.Events = new List<HighlightEvent>();

        // --- source_video (top-level string field) -------------------------
        // Pattern: "source_video"<ws>:<ws>"<value>"   where <value> may
        // contain escaped characters like \\ or \".
        Match svm = Regex.Match(text,
            "\"source_video\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
        if (!svm.Success)
            throw new Exception("JSON is missing required 'source_video' field.");
        ef.SourceVideo = UnescapeJsonString(svm.Groups[1].Value);

        // --- events array --------------------------------------------------
        // Find the opening [ that follows the "events" key, then walk
        // character-by-character to find the matching ], honouring
        // brace/bracket depth and skipping over string literals.
        int keyIdx = text.IndexOf("\"events\"");
        if (keyIdx < 0)
            throw new Exception("JSON is missing required 'events' array.");
        int arrStart = text.IndexOf('[', keyIdx);
        if (arrStart < 0)
            throw new Exception("JSON 'events' field is not an array.");
        int arrEnd = FindMatchingBracket(text, arrStart);
        string arrBody = text.Substring(arrStart + 1, arrEnd - arrStart - 1);

        // Split the array body into individual {...} object strings, then
        // extract the known fields from each.
        foreach (string objText in SplitTopLevelObjects(arrBody))
        {
            double ts   = ParseInvariantDouble(RequireField(objText, "timestamp_sec"));
            string type = UnescapeJsonString(RequireStringField(objText, "type"));
            string note = GetStringFieldOr(objText, "note", "");
            double pre  = GetDoubleFieldOr(objText, "pre_sec",  DEFAULT_PRE_SECONDS);
            double post = GetDoubleFieldOr(objText, "post_sec", DEFAULT_POST_SECONDS);
            ef.Events.Add(new HighlightEvent(ts, type, note, pre, post));
        }

        if (ef.Events.Count == 0)
            throw new Exception("JSON 'events' array is empty: " + path);

        return ef;
    }


    // ──────────────── JSON parsing helpers ────────────────

    // Walk from startIdx (which must be '[' or '{') to the matching close
    // bracket, skipping over bracket characters that appear inside string
    // literals (so '"{" inside a string doesn't confuse us).
    static int FindMatchingBracket(string s, int startIdx)
    {
        char open = s[startIdx];
        char close = (open == '[') ? ']' : '}';
        int depth = 0;
        bool inString = false;

        for (int i = startIdx; i < s.Length; i++)
        {
            char c = s[i];
            if (inString)
            {
                if (c == '\\') { i++; continue; }  // skip escaped char
                if (c == '"') inString = false;
            }
            else
            {
                if (c == '"') inString = true;
                else if (c == open)  depth++;
                else if (c == close)
                {
                    depth--;
                    if (depth == 0) return i;
                }
            }
        }
        throw new Exception("Unmatched '" + open + "' while parsing JSON.");
    }

    // Given the body of a JSON array (everything between [ and ]), yield
    // each top-level {...} substring. Uses FindMatchingBracket to handle
    // nested objects correctly — though our schema doesn't nest, being
    // careful here means we won't break if the Python side adds nesting.
    static List<string> SplitTopLevelObjects(string body)
    {
        List<string> result = new List<string>();
        int i = 0;
        while (i < body.Length)
        {
            if (body[i] == '{')
            {
                int end = FindMatchingBracket(body, i);
                result.Add(body.Substring(i, end - i + 1));
                i = end + 1;
            }
            else
            {
                i++;
            }
        }
        return result;
    }

    // Extract the raw token value for a named field from inside a single
    // JSON object. Matches either "field": "quoted string" or
    // "field": number / bool / null. Returns null if not found.
    static string TryGetField(string obj, string fieldName)
    {
        // Group 1 captures a quoted string (escapes allowed).
        // Group 2 captures an unquoted token (number, bool, null).
        Match m = Regex.Match(obj,
            "\"" + Regex.Escape(fieldName) + "\"\\s*:\\s*" +
            "(?:\"((?:\\\\.|[^\"\\\\])*)\"|([^,\\}\\s][^,\\}]*))");
        if (!m.Success) return null;
        return m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value.Trim();
    }

    static string RequireField(string obj, string fieldName)
    {
        string v = TryGetField(obj, fieldName);
        if (v == null)
            throw new Exception("Event object is missing required field '" +
                                fieldName + "':\n" + obj);
        return v;
    }

    // For required string fields, we want the unescaped value.
    static string RequireStringField(string obj, string fieldName)
    {
        return RequireField(obj, fieldName);  // already unquoted by Regex
    }

    static string GetStringFieldOr(string obj, string fieldName, string fallback)
    {
        string v = TryGetField(obj, fieldName);
        return v == null ? fallback : UnescapeJsonString(v);
    }

    static double GetDoubleFieldOr(string obj, string fieldName, double fallback)
    {
        string v = TryGetField(obj, fieldName);
        if (v == null) return fallback;
        return ParseInvariantDouble(v);
    }

    // Parse a JSON number with `.` decimal separator, regardless of the
    // machine's regional settings. Without InvariantCulture, "1.5" on a
    // German-locale Windows parses as 15.0. Nightmare.
    static double ParseInvariantDouble(string raw)
    {
        return double.Parse(raw, System.Globalization.CultureInfo.InvariantCulture);
    }

    // Decode the common JSON string escapes. Our inputs only use \\, \",
    // \n, \r, \t in practice, so we don't bother with \uXXXX here.
    static string UnescapeJsonString(string raw)
    {
        if (raw.IndexOf('\\') < 0) return raw;  // fast path — no escapes
        System.Text.StringBuilder sb = new System.Text.StringBuilder(raw.Length);
        for (int i = 0; i < raw.Length; i++)
        {
            char c = raw[i];
            if (c == '\\' && i + 1 < raw.Length)
            {
                char next = raw[++i];
                switch (next)
                {
                    case '"':  sb.Append('"');  break;
                    case '\\': sb.Append('\\'); break;
                    case '/':  sb.Append('/');  break;
                    case 'n':  sb.Append('\n'); break;
                    case 'r':  sb.Append('\r'); break;
                    case 't':  sb.Append('\t'); break;
                    case 'b':  sb.Append('\b'); break;
                    case 'f':  sb.Append('\f'); break;
                    default:   sb.Append('\\').Append(next); break;
                }
            }
            else sb.Append(c);
        }
        return sb.ToString();
    }
}
