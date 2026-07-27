/*
 * ============================================================================
 *  ProcessVideo.cs  —  Orchestrator-driven "mega-script" for one VOD.
 * ============================================================================
 *
 *  WHAT IT DOES
 *      Launched by tools/process_vods.py via `vegas210.exe -SCRIPT:...`.
 *      Reads jobs/current.json for this video's paths + preset names, then:
 *
 *          Phase A — full gameplay (horizontal YouTube):
 *              1. New project at horizontal_full preset dims.
 *              2. One VideoEvent per preset video track, spanning the FULL
 *                 source duration. Pan/Crop + ScaleToFill + OFX effects.
 *              3. One AudioEvent per source audio stream spanning full
 *                 duration. Volume + Mute from preset.
 *              4. No intro/outro title cards. No event splitting.
 *              5. Poll jobs/go.flag — blocks until the orchestrator
 *                 writes it (triggered by the user pressing Enter in CMD).
 *              6. Render to rendered_dir/<basename>.<ext> via the
 *                 full_render_template named in the job.
 *
 *          Phase B — highlight (vertical TikTok):
 *              1. NewProject at vertical_tiktok preset dims.
 *              2. Lay down events per .events.json, grouped so video +
 *                 audio move together. Pan/Crop + ScaleToFill + OFX
 *                 effects per preset track (same code path as the
 *                 standalone HighlightBuilder.cs).
 *              3. Intro "Smite Shorts" title card + outro "YouTube" card
 *                 on a top-of-stack Titles track.
 *              4. Poll jobs/go.flag again.
 *              5. Render to highlight_dir/<basename>_highlight.<ext>.
 *
 *          Write jobs/phase_done.flag and exit.
 *
 *  WHY A "MEGA-SCRIPT"
 *      Verified 2026-04-24: Vegas's CLI `-SCRIPT:` ALWAYS spawns a new
 *      Vegas window, even when one is already open. Launching separate
 *      build + render scripts would leave the built project behind in
 *      Window #1 while Window #2 opened empty and rendered nothing. One
 *      script per video keeps both phases in the same Vegas session.
 *
 *  FILE HANDOFF (Python <-> script)
 *      jobs/current.json       Python writes on batch start; we read once.
 *      jobs/go.flag            Python writes on keypress; we poll + consume.
 *      jobs/phase_done.flag    We write when both phases finish.
 *      jobs/error.flag         We write on any exception with message+stack.
 *
 *  DUPLICATED CODE
 *      Preset loading, events.json parsing, title-card helpers, and
 *      effect application are copy-pasted from HighlightBuilder.cs per
 *      decision R-7 (can't share .cs files across Vegas scripts). Keep
 *      the two in sync when either is edited.
 * ============================================================================
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using ScriptPortal.Vegas;


public class EntryPoint
{
    // ────────────────────────────────────────────────────────────────────────
    //   PATHS
    // ────────────────────────────────────────────────────────────────────────

    const string JOBS_DIR        = @"C:\Users\james\HatmasBot\jobs";
    const string PRESET_DIR      = @"C:\Users\james\HatmasBot\vegas_presets";
    const string JOB_FILE         = @"C:\Users\james\HatmasBot\jobs\current.json";
    const string GO_FLAG          = @"C:\Users\james\HatmasBot\jobs\go.flag";
    const string PHASE_READY_FLAG = @"C:\Users\james\HatmasBot\jobs\phase_ready.flag";
    const string PHASE_DONE_FLAG  = @"C:\Users\james\HatmasBot\jobs\phase_done.flag";
    const string ERROR_FLAG       = @"C:\Users\james\HatmasBot\jobs\error.flag";


    // ────────────────────────────────────────────────────────────────────────
    //   CONSTANTS (mirror HighlightBuilder.cs)
    // ────────────────────────────────────────────────────────────────────────

    // Fallback audio track names when preset doesn't name a track.
    static readonly string[] AUDIO_TRACK_NAMES = new[] {
        "Gameplay",
        "Voice",
        "Music / Other",
        "Combined Mix",
    };

    // Default pre/post for events missing those fields. Matches
    // extract_events.py defaults.
    const double DEFAULT_PRE_SECONDS  = 7.0;
    const double DEFAULT_POST_SECONDS = 6.0;

    // Intro / outro card text. Mirror HighlightBuilder.cs.
    const string SERIES_NAME    = "Smite Shorts";
    const string OUTRO_TEXT     = "Full gameplay available on YouTube";
    const double INTRO_DURATION = 1.5;
    const double OUTRO_DURATION = 3.0;
    const double INTRO_FADE_OUT = 0.5;
    const double OUTRO_FADE_IN  = 0.5;

    static readonly string[] TITLE_GENERATOR_CANDIDATES = new[] {
        "(VEGAS) Titles & Text",
        "VEGAS Titles & Text",
        "Titles & Text",
        "(Legacy) Text",
        "Sony Text",
    };
    static readonly string[] TITLE_TEXT_PARAM_CANDIDATES = new[] {
        "Text", "TextString", "RichText", "String",
    };

    // Poll cadence for go.flag. 200ms balances responsiveness vs CPU.
    const int POLL_INTERVAL_MS = 200;


    // ────────────────────────────────────────────────────────────────────────
    //   DATA TYPES
    // ────────────────────────────────────────────────────────────────────────

    class JobSpec {
        public string SourceVideo;
        public string EventsJson;
        public string FullPreset;
        public string HighlightPreset;
        public string FullRenderTemplate;
        public string HighlightRenderTemplate;
        public string RenderedDir;
        public string HighlightDir;
    }

    class FramePreset {
        public string Name;
        public string Kind;
        public int    ProjectWidth;
        public int    ProjectHeight;
        public double FrameRate;
        public TrackPreset[] Tracks;
        public AudioPreset[] AudioTracks;
    }

    class TrackPreset {
        public string        Name;
        public CompositeMode Composite;
        public double TopLeftX,     TopLeftY;
        public double TopRightX,    TopRightY;
        public double BottomRightX, BottomRightY;
        public double BottomLeftX,  BottomLeftY;
        public double CenterX,      CenterY;
        public double Rotation;
        public double Smoothness;
        public VideoKeyframeType KeyframeType;
        public bool   ScaleToFill = true;
        public EffectPreset[] Effects = new EffectPreset[0];
    }

    class AudioPreset {
        public string Name;
        public double Volume_dB;
        public bool   Muted;
    }

    class EffectPreset {
        public string PluginId;
        public string PluginName;
        public bool   Bypass;
        public ParamPreset[] Parameters;
    }

    class ParamPreset {
        public string Name;
        public string Type;
        public bool   BoolValue;
        public double DoubleValue;
        public int    IntValue;
        public string StringValue;
        public double XValue, YValue;
    }

    struct HighlightEvent {
        public double TimestampSec;
        public string Type;
        public string Note;
        public double PreSec;
        public double PostSec;
        public HighlightEvent(double ts, string type, string note, double pre, double post) {
            TimestampSec = ts; Type = type; Note = note; PreSec = pre; PostSec = post;
        }
    }

    class EventsFile {
        public string SourceVideo;
        public List<HighlightEvent> Events;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   ENTRY POINT
    // ────────────────────────────────────────────────────────────────────────

    public void FromVegas(Vegas vegas)
    {
        try
        {
            Run(vegas);
        }
        catch (Exception ex)
        {
            TryWriteFlag(ERROR_FLAG,
                "ProcessVideo failed.\r\n" +
                ex.GetType().Name + ": " + ex.Message + "\r\n" +
                ex.StackTrace);
            MessageBox.Show(
                "ProcessVideo failed.\r\n\r\n" + ex.Message + "\r\n\r\n" + ex.StackTrace,
                "ProcessVideo error");
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   MAIN ORCHESTRATOR
    // ────────────────────────────────────────────────────────────────────────

    void Run(Vegas vegas)
    {
        // Clean stale coordination flags from a previous run.
        Directory.CreateDirectory(JOBS_DIR);
        DeleteIfExists(GO_FLAG);
        DeleteIfExists(PHASE_READY_FLAG);
        DeleteIfExists(PHASE_DONE_FLAG);
        DeleteIfExists(ERROR_FLAG);

        JobSpec job = LoadJob(JOB_FILE);
        ValidateJob(job);

        string baseName = Path.GetFileNameWithoutExtension(job.SourceVideo);

        // ── Phase A: full gameplay ──────────────────────────────────────────
        FramePreset fullPreset = LoadPreset(ResolvePresetPath(job.FullPreset));
        BuildFullGameplay(vegas, job, fullPreset);
        WaitForGoFlag("Phase A (full gameplay)\r\n" + ProjectDiagnostic(vegas.Project));
        string fullOut = Path.Combine(job.RenderedDir, baseName + ".mp4");
        RenderCurrent(vegas, job.FullRenderTemplate, fullOut);

        // ── Phase B: highlight ──────────────────────────────────────────────
        FramePreset highPreset = LoadPreset(ResolvePresetPath(job.HighlightPreset));
        EventsFile  ef         = LoadEventsFile(job.EventsJson);
        BuildHighlight(vegas, job, highPreset, ef);
        WaitForGoFlag("Phase B (highlight)\r\n" + ProjectDiagnostic(vegas.Project));
        string highOut = Path.Combine(job.HighlightDir, baseName + "_highlight.mp4");
        RenderCurrent(vegas, job.HighlightRenderTemplate, highOut);

        // Done. Python watches for this flag to move to the next video.
        TryWriteFlag(PHASE_DONE_FLAG,
            "Done at " + DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss",
                CultureInfo.InvariantCulture) +
            "\r\nSource:    " + job.SourceVideo +
            "\r\nFull:      " + fullOut +
            "\r\nHighlight: " + highOut);

        MessageBox.Show(
            "ProcessVideo complete for:\r\n  " + baseName + "\r\n\r\n" +
            "Full:      " + fullOut + "\r\n" +
            "Highlight: " + highOut,
            "ProcessVideo — done");
    }


    // ────────────────────────────────────────────────────────────────────────
    //   JOB FILE
    // ────────────────────────────────────────────────────────────────────────

    static JobSpec LoadJob(string path)
    {
        if (!File.Exists(path))
        {
            throw new Exception(
                "Job file not found: " + path +
                "\r\n\r\nThe orchestrator should write this before launching " +
                "the script. If you're running ProcessVideo manually, create " +
                "the file by hand — see ProcessVideo.cs header for the schema.");
        }
        string text = File.ReadAllText(path);
        JobSpec j = new JobSpec();
        j.SourceVideo             = RequireStringField(text, "source_video");
        j.EventsJson              = RequireStringField(text, "events_json");
        j.FullPreset              = RequireStringField(text, "full_preset");
        j.HighlightPreset         = RequireStringField(text, "highlight_preset");
        j.FullRenderTemplate      = RequireStringField(text, "full_render_template");
        j.HighlightRenderTemplate = RequireStringField(text, "highlight_render_template");
        j.RenderedDir             = RequireStringField(text, "rendered_dir");
        j.HighlightDir            = RequireStringField(text, "highlight_dir");
        return j;
    }

    static void ValidateJob(JobSpec j)
    {
        if (!File.Exists(j.SourceVideo))
            throw new Exception("source_video does not exist: " + j.SourceVideo);
        if (!File.Exists(j.EventsJson))
            throw new Exception("events_json does not exist: " + j.EventsJson);
        Directory.CreateDirectory(j.RenderedDir);
        Directory.CreateDirectory(j.HighlightDir);
    }

    static string ResolvePresetPath(string nameOrPath)
    {
        // If it already looks like a path, use it verbatim.
        if (nameOrPath.IndexOfAny(new[] { '\\', '/' }) >= 0)
            return nameOrPath;
        // Treat as a preset name under PRESET_DIR, append extension if missing.
        string p = Path.Combine(PRESET_DIR, nameOrPath);
        if (!p.EndsWith(".tune.json", StringComparison.OrdinalIgnoreCase))
            p += ".tune.json";
        return p;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   PHASE A — FULL GAMEPLAY
    // ────────────────────────────────────────────────────────────────────────

    void BuildFullGameplay(Vegas vegas, JobSpec job, FramePreset preset)
    {
        // Start a fresh project. false/false = no prompt to save current.
        vegas.NewProject(false, false);
        Project proj = vegas.Project;

        // Project dims from preset. Frame rate too — warn if source is
        // meaningfully different, but honour the preset.
        proj.Video.Width     = preset.ProjectWidth;
        proj.Video.Height    = preset.ProjectHeight;
        proj.Video.FrameRate = preset.FrameRate;

        Media media = proj.MediaPool.AddMedia(job.SourceVideo);
        MediaStream videoStream;
        List<MediaStream> audioStreams;
        SplitStreams(media, out videoStream, out audioStreams);

        // Create tracks.
        int trackIndex = proj.Tracks.Count;
        VideoTrack[] videoTracks = CreateVideoTracks(proj, preset, ref trackIndex);
        AudioTrack[] audioTracks = CreateAudioTracks(proj, preset, audioStreams,
            ref trackIndex);

        // One full-length event per video track + one full-length event per
        // audio stream. All start at cursor 0. No splitting.
        Timecode cursor = Timecode.FromSeconds(0.0);
        Timecode length = media.Length;   // full source duration

        List<TrackEvent> grouped = new List<TrackEvent>();
        List<string> buildWarnings = new List<string>();

        for (int i = 0; i < videoTracks.Length; i++)
        {
            VideoEvent ve = videoTracks[i].AddVideoEvent(cursor, length);
            ve.AddTake(videoStream, true);
            ve.ActiveTake.Offset = Timecode.FromSeconds(0.0);
            ApplyPanCrop(ve, preset.Tracks[i]);
            ApplyEffects(ve, preset.Tracks[i].Effects, vegas, buildWarnings);
            grouped.Add(ve);
        }
        for (int i = 0; i < audioStreams.Count; i++)
        {
            AudioEvent ae = audioTracks[i].AddAudioEvent(cursor, length);
            ae.AddTake(audioStreams[i], true);
            ae.ActiveTake.Offset = Timecode.FromSeconds(0.0);
            grouped.Add(ae);
        }

        // Group so drag/nudge preserves video/audio alignment.
        if (grouped.Count > 1)
        {
            TrackEventGroup group = new TrackEventGroup(proj);
            proj.Groups.Add(group);
            foreach (TrackEvent te in grouped) group.Add(te);
        }

        if (buildWarnings.Count > 0)
        {
            MessageBox.Show(
                "Phase A built with warnings:\r\n\r\n  " +
                string.Join("\r\n  ", buildWarnings.ToArray()),
                "ProcessVideo — Phase A warnings");
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   PHASE B — HIGHLIGHT
    // ────────────────────────────────────────────────────────────────────────

    void BuildHighlight(Vegas vegas, JobSpec job, FramePreset preset, EventsFile ef)
    {
        vegas.NewProject(false, false);
        Project proj = vegas.Project;

        proj.Video.Width     = preset.ProjectWidth;
        proj.Video.Height    = preset.ProjectHeight;
        proj.Video.FrameRate = preset.FrameRate;

        Media media = proj.MediaPool.AddMedia(job.SourceVideo);
        MediaStream videoStream;
        List<MediaStream> audioStreams;
        SplitStreams(media, out videoStream, out audioStreams);

        int trackIndex = proj.Tracks.Count;

        // Hardcoded Titles track FIRST (top of stack) for intro/outro cards.
        VideoTrack titlesTrack = new VideoTrack(proj, trackIndex++, "Titles");
        proj.Tracks.Add(titlesTrack);
        titlesTrack.CompositeMode = CompositeMode.SrcAlpha;

        // Then the preset's video tracks (Titles entries in preset are
        // filtered out by ParseVideoTracks).
        VideoTrack[] videoTracks = CreateVideoTracks(proj, preset, ref trackIndex);
        AudioTrack[] audioTracks = CreateAudioTracks(proj, preset, audioStreams,
            ref trackIndex);

        // Lay down each event as back-to-back clips.
        Timecode cursor = Timecode.FromSeconds(0.0);
        List<string> buildWarnings = new List<string>();

        foreach (HighlightEvent ev in ef.Events)
        {
            double srcStartSec = Math.Max(0.0, ev.TimestampSec - ev.PreSec);
            double srcEndSec   = ev.TimestampSec + ev.PostSec;
            double clipLenSec  = srcEndSec - srcStartSec;

            Timecode srcOffset = Timecode.FromSeconds(srcStartSec);
            Timecode clipLen   = Timecode.FromSeconds(clipLenSec);

            List<TrackEvent> linked = new List<TrackEvent>();

            for (int i = 0; i < videoTracks.Length; i++)
            {
                VideoEvent ve = videoTracks[i].AddVideoEvent(cursor, clipLen);
                ve.AddTake(videoStream, true);
                ve.ActiveTake.Offset = srcOffset;
                ApplyPanCrop(ve, preset.Tracks[i]);
                ApplyEffects(ve, preset.Tracks[i].Effects, vegas, buildWarnings);
                linked.Add(ve);
            }
            for (int i = 0; i < audioStreams.Count; i++)
            {
                AudioEvent ae = audioTracks[i].AddAudioEvent(cursor, clipLen);
                ae.AddTake(audioStreams[i], true);
                ae.ActiveTake.Offset = srcOffset;
                linked.Add(ae);
            }

            TrackEventGroup g = new TrackEventGroup(proj);
            proj.Groups.Add(g);
            foreach (TrackEvent te in linked) g.Add(te);

            cursor += clipLen;
        }

        // Intro + outro title cards.
        PlugInNode titleGen = FindTitleGenerator(vegas);

        AddTitleEvent(proj, titlesTrack, titleGen, SERIES_NAME,
            Timecode.FromSeconds(0.0),
            Timecode.FromSeconds(INTRO_DURATION),
            Timecode.FromSeconds(0.0),
            Timecode.FromSeconds(INTRO_FADE_OUT));

        AddTitleEvent(proj, titlesTrack, titleGen, OUTRO_TEXT,
            cursor,
            Timecode.FromSeconds(OUTRO_DURATION),
            Timecode.FromSeconds(OUTRO_FADE_IN),
            Timecode.FromSeconds(0.0));

        if (buildWarnings.Count > 0)
        {
            MessageBox.Show(
                "Phase B built with warnings:\r\n\r\n  " +
                string.Join("\r\n  ", buildWarnings.ToArray()),
                "ProcessVideo — Phase B warnings");
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   SHARED TRACK / EVENT HELPERS
    // ────────────────────────────────────────────────────────────────────────

    static void SplitStreams(Media media, out MediaStream videoStream,
                             out List<MediaStream> audioStreams)
    {
        videoStream  = null;
        audioStreams = new List<MediaStream>();
        foreach (MediaStream s in media.Streams)
        {
            if (s.MediaType == MediaType.Video && videoStream == null)
                videoStream = s;
            else if (s.MediaType == MediaType.Audio)
                audioStreams.Add(s);
        }
        if (videoStream == null)
            throw new Exception("No video stream in " + media.FilePath);
        if (audioStreams.Count == 0)
            throw new Exception("No audio streams in " + media.FilePath);
    }

    static VideoTrack[] CreateVideoTracks(Project proj, FramePreset preset,
                                          ref int trackIndex)
    {
        VideoTrack[] tracks = new VideoTrack[preset.Tracks.Length];
        for (int i = 0; i < preset.Tracks.Length; i++)
        {
            TrackPreset tp = preset.Tracks[i];
            string name = string.IsNullOrEmpty(tp.Name)
                ? "Video Track " + (i + 1)
                : tp.Name;
            tracks[i] = new VideoTrack(proj, trackIndex++, name);
            proj.Tracks.Add(tracks[i]);
            tracks[i].CompositeMode = tp.Composite;
        }
        return tracks;
    }

    static AudioTrack[] CreateAudioTracks(Project proj, FramePreset preset,
                                          List<MediaStream> audioStreams,
                                          ref int trackIndex)
    {
        AudioTrack[] tracks = new AudioTrack[audioStreams.Count];
        AudioPreset[] ap = preset.AudioTracks ?? new AudioPreset[0];
        for (int i = 0; i < audioStreams.Count; i++)
        {
            string name;
            if (i < ap.Length && !string.IsNullOrEmpty(ap[i].Name))
                name = ap[i].Name;
            else if (i < AUDIO_TRACK_NAMES.Length)
                name = AUDIO_TRACK_NAMES[i];
            else
                name = "Audio " + (i + 1);

            tracks[i] = new AudioTrack(proj, trackIndex++, name);
            proj.Tracks.Add(tracks[i]);

            if (i < ap.Length)
            {
                double linearVol = Math.Pow(10.0, ap[i].Volume_dB / 20.0);
                if (linearVol < 1e-5) linearVol = 1e-5;
                if (linearVol > 4.0)  linearVol = 4.0;
                tracks[i].Volume = (float)linearVol;
                tracks[i].Mute   = ap[i].Muted;
            }
        }
        return tracks;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   PAN/CROP APPLICATION (mirror of HighlightBuilder.ApplyPanCrop)
    // ────────────────────────────────────────────────────────────────────────

    static void ApplyPanCrop(VideoEvent ve, TrackPreset tp)
    {
        ve.VideoMotion.ScaleToFill = tp.ScaleToFill;

        VideoMotionKeyframe kf = ve.VideoMotion.Keyframes[0];
        kf.Bounds = new VideoMotionBounds(
            new VideoMotionVertex((float)tp.TopLeftX,     (float)tp.TopLeftY),
            new VideoMotionVertex((float)tp.TopRightX,    (float)tp.TopRightY),
            new VideoMotionVertex((float)tp.BottomRightX, (float)tp.BottomRightY),
            new VideoMotionVertex((float)tp.BottomLeftX,  (float)tp.BottomLeftY));
        kf.Center     = new VideoMotionVertex((float)tp.CenterX, (float)tp.CenterY);
        kf.Rotation   = (float)tp.Rotation;
        kf.Smoothness = (float)tp.Smoothness;
        kf.Type       = tp.KeyframeType;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   EFFECT APPLICATION (mirror of HighlightBuilder.ApplyEffects)
    // ────────────────────────────────────────────────────────────────────────

    static void ApplyEffects(VideoEvent ve, EffectPreset[] effects,
                             Vegas vegas, List<string> warnings)
    {
        if (effects == null || effects.Length == 0) return;
        foreach (EffectPreset ep in effects)
        {
            PlugInNode plugin = FindPluginByUniqueID(vegas.VideoFX, ep.PluginId);
            if (plugin == null)
            {
                warnings.Add("Plugin not found: " + ep.PluginName
                    + " (" + ep.PluginId + ")");
                continue;
            }
            Effect effect;
            try { effect = ve.Effects.AddEffect(plugin); }
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


    // ────────────────────────────────────────────────────────────────────────
    //   TITLE GENERATOR HELPERS (mirror of HighlightBuilder)
    // ────────────────────────────────────────────────────────────────────────

    static PlugInNode FindTitleGenerator(Vegas vegas)
    {
        foreach (string candidate in TITLE_GENERATOR_CANDIDATES)
        {
            PlugInNode node = FindNodeByName(vegas.Generators, candidate);
            if (node != null) return node;
        }
        throw new Exception(
            "No title generator found. Tried: " +
            string.Join(", ", TITLE_GENERATOR_CANDIDATES));
    }

    static PlugInNode FindNodeByName(PlugInNode parent, string name)
    {
        foreach (PlugInNode child in parent)
        {
            if (child.Name == name) return child;
            PlugInNode deep = FindNodeByName(child, name);
            if (deep != null) return deep;
        }
        return null;
    }

    static VideoEvent AddTitleEvent(Project proj, VideoTrack titlesTrack,
                                    PlugInNode titleGen, string text,
                                    Timecode start, Timecode length,
                                    Timecode fadeIn, Timecode fadeOut)
    {
        // Instantiate the generator as fresh Media. Registration is automatic.
        Media titleMedia = new Media(titleGen);

        // Pick the first VIDEO stream (Media.Streams may include audio).
        MediaStream titleStream = null;
        foreach (MediaStream s in titleMedia.Streams)
        {
            if (s.MediaType == MediaType.Video) { titleStream = s; break; }
        }
        if (titleStream == null)
            throw new Exception("Title generator produced no video stream: "
                + titleGen.Name);

        VideoEvent ve = titlesTrack.AddVideoEvent(start, length);
        ve.AddTake(titleStream, true);
        SetTitleText(titleMedia, text);

        Timecode zero = Timecode.FromSeconds(0.0);
        if (fadeIn  > zero) ve.FadeIn.Length  = fadeIn;
        if (fadeOut > zero) ve.FadeOut.Length = fadeOut;
        return ve;
    }

    static void SetTitleText(Media titleMedia, string text)
    {
        OFXEffect ofx = titleMedia.Generator.OFXEffect;
        foreach (string candidate in TITLE_TEXT_PARAM_CANDIDATES)
        {
            OFXParameter p = ofx.FindParameterByName(candidate);
            if (p != null && p is OFXStringParameter)
            {
                ((OFXStringParameter)p).Value = text;
                return;
            }
        }
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("Could not find a text parameter on the title generator.");
        sb.AppendLine("Tried: " + string.Join(", ", TITLE_TEXT_PARAM_CANDIDATES));
        sb.AppendLine("Available params:");
        foreach (OFXParameter p in ofx.Parameters)
            sb.AppendLine("  " + p.Name + "  (" + p.GetType().Name + ")");
        throw new Exception(sb.ToString());
    }


    // ────────────────────────────────────────────────────────────────────────
    //   RENDER
    // ────────────────────────────────────────────────────────────────────────

    static void RenderCurrent(Vegas vegas, string templateName, string outputPath)
    {
        RenderTemplate template = FindRenderTemplate(vegas, templateName);
        if (template == null)
        {
            throw new Exception("Render template not found: \"" + templateName
                + "\"\r\n\r\nAvailable templates: " + ListTemplateNames(vegas));
        }

        // Make sure the target directory exists.
        Directory.CreateDirectory(Path.GetDirectoryName(outputPath));

        // If an output file already exists, Vegas may prompt. Delete first.
        if (File.Exists(outputPath))
        {
            try { File.Delete(outputPath); }
            catch (Exception ex)
            {
                throw new Exception("Output file exists and could not be deleted: "
                    + outputPath + " (" + ex.Message + ")");
            }
        }

        RenderStatus status = vegas.Render(outputPath, template);
        if (status != RenderStatus.Complete)
        {
            throw new Exception("Render did not complete. Status: " + status
                + "\r\nTemplate: " + templateName + "\r\nOutput: " + outputPath);
        }
    }

    static RenderTemplate FindRenderTemplate(Vegas vegas, string name)
    {
        foreach (Renderer r in vegas.Renderers)
        {
            foreach (RenderTemplate t in r.Templates)
            {
                if (string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase))
                    return t;
            }
        }
        return null;
    }

    static string ListTemplateNames(Vegas vegas)
    {
        StringBuilder sb = new StringBuilder();
        foreach (Renderer r in vegas.Renderers)
        {
            sb.AppendLine();
            sb.AppendLine("  " + r.Name + ":");
            foreach (RenderTemplate t in r.Templates)
                sb.AppendLine("    " + t.Name);
        }
        return sb.ToString();
    }


    // ────────────────────────────────────────────────────────────────────────
    //   GO.FLAG POLL LOOP
    // ────────────────────────────────────────────────────────────────────────

    static void WaitForGoFlag(string phaseLabel)
    {
        // Signal Python that this phase's timeline is built and we're
        // now blocking on go.flag. Python reads the label + shows a
        // per-phase prompt ("Phase A timeline ready — press Enter...").
        TryWriteFlag(PHASE_READY_FLAG, phaseLabel);

        // A bare poll loop (Thread.Sleep + Application.DoEvents) doesn't
        // give Vegas's UI thread enough time to repaint script-added
        // tracks — the timeline stays blank until the script returns.
        // Fix: use a modal Form with a Timer. ShowDialog runs a proper
        // Windows message loop, which lets Vegas fully redraw the UI
        // while we wait. The Timer polls go.flag and closes the Form
        // when Python writes it. Visually, this also gives James a
        // progress indicator so he knows the script is alive.
        // We need TWO things simultaneously:
        //   (a) Vegas's UI thread to fully repaint script-added tracks —
        //       Application.DoEvents + Thread.Sleep in a loop doesn't
        //       cut it (observed 5+ minutes with tracks still invisible).
        //   (b) The user to remain free to interact with Vegas to tweak
        //       the timeline while we wait for their CMD Enter press.
        //
        // The fix: a MODELESS form driven by Application.Run(). Run()
        // installs a proper Win32 nested message loop on this thread,
        // which lets Vegas finish dispatching its deferred UI work AND
        // keeps the form non-blocking so Vegas stays interactive. When
        // the timer detects go.flag, form.Close() ends Application.Run
        // and we return to the script.
        Form form = new Form();
        form.Text            = "ProcessVideo — " + phaseLabel.Split('\r', '\n')[0];
        form.Width           = 460;
        form.Height          = 170;
        form.FormBorderStyle = FormBorderStyle.FixedToolWindow;
        form.StartPosition   = FormStartPosition.Manual;
        form.Location        = new System.Drawing.Point(60, 60);
        form.ShowInTaskbar   = false;
        form.TopMost         = true;   // stay on top so CMD instructions visible

        Label label = new Label();
        label.Text      = phaseLabel
                          + "\r\n\r\n"
                          + "Tweak the timeline in Vegas as needed, "
                          + "then press Enter in the CMD window.";
        label.Dock      = DockStyle.Fill;
        label.TextAlign = System.Drawing.ContentAlignment.MiddleCenter;
        form.Controls.Add(label);

        // Prevent accidental close (Alt+F4, taskbar right-click).
        form.FormClosing += delegate(object sender, FormClosingEventArgs e)
        {
            if (!File.Exists(GO_FLAG)) e.Cancel = true;
        };

        System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        timer.Interval = POLL_INTERVAL_MS;
        timer.Tick += delegate(object sender, EventArgs e)
        {
            if (File.Exists(GO_FLAG))
            {
                timer.Stop();
                form.Close();
            }
        };
        timer.Start();

        try
        {
            // Application.Run creates a nested message pump AND does not
            // block the owner window — Vegas stays fully interactive.
            Application.Run(form);
        }
        finally
        {
            timer.Dispose();
            form.Dispose();
        }

        // Sanity check — if we got here without go.flag present, something
        // unexpected closed the dialog and we should NOT proceed to render.
        if (!File.Exists(GO_FLAG))
        {
            throw new Exception(
                "ProcessVideo: wait form closed but go.flag was not set. "
                + "Aborting before render to avoid wasted output.");
        }

        // Consume the flags so the next phase starts clean.
        DeleteIfExists(PHASE_READY_FLAG);
        try { File.Delete(GO_FLAG); }
        catch { /* race with Python write: retry once */
            Thread.Sleep(100);
            try { File.Delete(GO_FLAG); } catch { }
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   PRESET LOADER  (mirror of HighlightBuilder.LoadPreset + parsers)
    // ────────────────────────────────────────────────────────────────────────

    static FramePreset LoadPreset(string path)
    {
        if (!File.Exists(path))
            throw new Exception("Preset file not found: " + path);

        string text = File.ReadAllText(path);
        FramePreset fp = new FramePreset();
        fp.Name = TryGetField(text, "name") ?? "(unnamed)";
        fp.Kind = TryGetField(text, "kind") ?? "unknown";

        int projKeyIdx = text.IndexOf("\"project\"");
        if (projKeyIdx < 0)
            throw new Exception("Preset is missing 'project' object: " + path);
        int projObjStart = text.IndexOf('{', projKeyIdx);
        int projObjEnd   = FindMatchingBracket(text, projObjStart);
        string projBody  = text.Substring(projObjStart, projObjEnd - projObjStart + 1);
        fp.ProjectWidth  = (int)ParseInvariantDouble(RequireField(projBody, "width"));
        fp.ProjectHeight = (int)ParseInvariantDouble(RequireField(projBody, "height"));
        fp.FrameRate     = ParseInvariantDouble(RequireField(projBody, "framerate"));

        fp.Tracks      = ParseVideoTracks(text);
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
            // Filter out preset "Titles" tracks — Phase B synthesizes its
            // own. Phase A never has a Titles track to worry about.
            if (string.Equals(tp.Name, "Titles", StringComparison.OrdinalIgnoreCase))
                continue;

            string compStr = GetStringFieldOr(objText, "composite", "SrcAlpha");
            tp.Composite = (CompositeMode)Enum.Parse(
                typeof(CompositeMode), compStr, true);

            ReadXY(objText, "top_left",     out tp.TopLeftX,     out tp.TopLeftY);
            ReadXY(objText, "top_right",    out tp.TopRightX,    out tp.TopRightY);
            ReadXY(objText, "bottom_right", out tp.BottomRightX, out tp.BottomRightY);
            ReadXY(objText, "bottom_left",  out tp.BottomLeftX,  out tp.BottomLeftY);
            ReadXY(objText, "center",       out tp.CenterX,      out tp.CenterY);

            tp.Rotation   = GetDoubleFieldOr(objText, "rotation_rad", 0.0);
            tp.Smoothness = GetDoubleFieldOr(objText, "smoothness",   0.0);
            string kfStr  = GetStringFieldOr(objText, "keyframe_type", "Linear");
            tp.KeyframeType = (VideoKeyframeType)Enum.Parse(
                typeof(VideoKeyframeType), kfStr, true);

            string scale = TryGetField(objText, "scale_to_fill");
            tp.ScaleToFill = (scale == null) ||
                             scale.Trim().Equals("true",
                                 StringComparison.OrdinalIgnoreCase);

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
            string m     = TryGetField(objText, "muted");
            ap.Muted     = (m != null) && m.Trim().Equals("true",
                StringComparison.OrdinalIgnoreCase);
            list.Add(ap);
        }
        return list.ToArray();
    }

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
            ep.Bypass     = (byp != null) && byp.Trim().Equals("true",
                StringComparison.OrdinalIgnoreCase);
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
                    pp.IntValue = rawValue != null
                        ? (int)ParseInvariantDouble(rawValue) : 0;
                    break;
                case "Double2D":
                    ReadXY(paramObj, "value", out pp.XValue, out pp.YValue);
                    break;
                default:
                    pp.StringValue = rawValue != null ? UnescapeJsonString(rawValue) : "";
                    break;
            }
            result.Add(pp);
        }
        return result.ToArray();
    }

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


    // ────────────────────────────────────────────────────────────────────────
    //   EVENTS.JSON LOADER (mirror of HighlightBuilder.LoadEventsFile)
    // ────────────────────────────────────────────────────────────────────────

    static EventsFile LoadEventsFile(string path)
    {
        string text = File.ReadAllText(path);
        EventsFile ef = new EventsFile();
        ef.Events = new List<HighlightEvent>();

        Match svm = Regex.Match(text,
            "\"source_video\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
        if (!svm.Success)
            throw new Exception("events.json missing 'source_video': " + path);
        ef.SourceVideo = UnescapeJsonString(svm.Groups[1].Value);

        int keyIdx = text.IndexOf("\"events\"");
        if (keyIdx < 0) throw new Exception("events.json missing 'events' array.");
        int arrStart = text.IndexOf('[', keyIdx);
        int arrEnd   = FindMatchingBracket(text, arrStart);
        string arrBody = text.Substring(arrStart + 1, arrEnd - arrStart - 1);

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
            throw new Exception("events.json 'events' is empty: " + path);
        return ef;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   JSON PARSING HELPERS  (mirror of HighlightBuilder — keep in sync)
    // ────────────────────────────────────────────────────────────────────────

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
                if (c == '\\') { i++; continue; }
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
        throw new Exception("Unmatched '" + open + "' in JSON.");
    }

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
            else i++;
        }
        return result;
    }

    static string TryGetField(string obj, string fieldName)
    {
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
            throw new Exception("Required field '" + fieldName + "' not found in:\n" + obj);
        return v;
    }

    static string RequireStringField(string obj, string fieldName)
    {
        return UnescapeJsonString(RequireField(obj, fieldName));
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

    static double ParseInvariantDouble(string raw)
    {
        return double.Parse(raw, CultureInfo.InvariantCulture);
    }

    static string UnescapeJsonString(string raw)
    {
        if (raw == null) return "";
        if (raw.IndexOf('\\') < 0) return raw;
        StringBuilder sb = new StringBuilder(raw.Length);
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


    // ────────────────────────────────────────────────────────────────────────
    //   FILE / FLAG UTILITIES
    // ────────────────────────────────────────────────────────────────────────

    static void DeleteIfExists(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    static void TryWriteFlag(string path, string content)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, content);
        }
        catch { /* best effort */ }
    }

    // Snapshot of the current project for the phase_ready.flag content.
    // Lets the orchestrator (and us, when debugging) see exactly what the
    // script believes it built before blocking on go.flag.
    static string ProjectDiagnostic(Project proj)
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendFormat("Project: {0}x{1} @ {2}fps",
            proj.Video.Width, proj.Video.Height,
            proj.Video.FrameRate.ToString("0.###",
                CultureInfo.InvariantCulture));
        sb.AppendLine();

        int videoTracks = 0, audioTracks = 0, videoEvents = 0, audioEvents = 0;
        foreach (Track t in proj.Tracks)
        {
            if (t is VideoTrack)
            {
                videoTracks++;
                foreach (TrackEvent te in t.Events)
                    if (te is VideoEvent) videoEvents++;
            }
            else if (t is AudioTrack)
            {
                audioTracks++;
                foreach (TrackEvent te in t.Events)
                    if (te is AudioEvent) audioEvents++;
            }
        }
        sb.AppendFormat("Tracks: {0} video, {1} audio  |  Events: {2} video, {3} audio",
            videoTracks, audioTracks, videoEvents, audioEvents);
        sb.AppendLine();

        sb.AppendFormat("MediaPool: {0} item(s)", proj.MediaPool.Count);
        sb.AppendLine();
        return sb.ToString();
    }
}
