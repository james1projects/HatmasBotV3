/*
 * ============================================================================
 *  TuneFrame.cs   —   Capture Pan/Crop + audio volumes into a .tune.json preset
 * ============================================================================
 *
 *  WHAT IT DOES
 *      You open a tuned Vegas project (the output of HighlightBuilder, or a
 *      raw .mp4 dragged into an empty 1920x1080 project for full-gameplay),
 *      manually dial in:
 *          * Pan/Crop on each video track (composition)
 *          * Volume faders on each audio track (mix)
 *          * Mute on any track you want silenced
 *      Then run this script (Tools -> Scripting -> TuneFrame) and it writes
 *      a JSON preset to vegas_presets\<name>.tune.json via a SaveFileDialog.
 *      HighlightBuilder.cs + ProcessVideo.cs read these presets at runtime,
 *      so you never have to paste C# code into source files.
 *
 *      The old .txt archive is also written next to your recordings for
 *      diff history (per champion variants, before/after tune comparisons).
 *
 *  CAPTURES (per track, top-down in Vegas's UI order)
 *      Video:
 *          * Composite mode (SrcAlpha, Add, ...)
 *          * Pan/Crop bounds (four corner vertices), center, rotation (rad),
 *            smoothness, keyframe interpolation type.
 *          * First keyframe only (static crops have 1; animation isn't captured).
 *      Audio:
 *          * Track name (empty-string safe).
 *          * Volume in dB (converted from Vegas's 0.0–4.0 linear scalar).
 *          * Muted state (read via AudioTrack.Mute if the property exists;
 *            falls back to volume_db <= -99 as a sentinel).
 *
 *  KIND INFERENCE
 *      1080x1920  -> "highlight"       (vertical TikTok/Shorts)
 *      1920x1080  -> "full_gameplay"   (horizontal YouTube)
 *      other      -> "unknown"         (warning in dialog)
 *
 *  HOW TO RUN
 *      1. Tune a project to your liking (Pan/Crop, audio faders, mute).
 *      2. Tools -> Scripting -> TuneFrame.
 *      3. Summary dialog confirms what was captured.
 *      4. SaveFileDialog opens at vegas_presets\. Pick an existing
 *         .tune.json to overwrite, or type a new name.
 *
 *  AUDIO DIAGNOSTIC (first run, or when AUDIO_DIAGNOSTIC = true)
 *      The summary dialog prints every public property of AudioTrack it
 *      found. Look through the list for "Mute" — if it's there, we're good.
 *      If it isn't, muted tracks will be recorded as volume_db = -100 (a
 *      linear gain of 1e-5, effectively silent). Set AUDIO_DIAGNOSTIC = false
 *      once you've verified the behaviour to clean up the dialog.
 * ============================================================================
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using ScriptPortal.Vegas;   // Vegas 14+. (Sony.Vegas for Vegas 13.)


public class EntryPoint
{
    // ────────────────────────────────────────────────────────────────────────
    //   CONFIGURATION
    // ────────────────────────────────────────────────────────────────────────

    // Where .tune.json presets live — the SaveFileDialog opens here.
    const string PRESET_OUTPUT_DIR = @"C:\Users\james\HatmasBot\vegas_presets";

    // Where the timestamped .txt archive goes (diff history / sanity log).
    const string ARCHIVE_OUTPUT_DIR = @"C:\Users\james\Videos";

    // First run: leave this on so the summary dialog lists every AudioTrack
    // property it sees. Turn off once you've confirmed Mute behaviour.
    // Verified 2026-04-24: AudioTrack.Mute exists on Vegas 21 (Boolean).
    // `static readonly` (not `const`) — otherwise the compiler folds the
    // branch and flags our unused path as "unreachable code".
    static readonly bool AUDIO_DIAGNOSTIC = false;

    // Similar diagnostic for VideoEvent / VideoMotion / VideoMotionKeyframe.
    // We need to find where Vegas exposes the Pan/Crop "Mask" checkbox so
    // we can capture + reapply it. Turn off once baked in.
    // Verified 2026-04-24: Pan/Crop Mask is not exposed in ScriptPortal.Vegas.dll.
    // Workaround: use Bezier Masking OFX (an Effect). Capture/apply via Effects.
    static readonly bool VIDEO_DIAGNOSTIC = false;

    // Muted tracks get this dB value in the preset when we can't read
    // AudioTrack.Mute directly. Anything <= -99 dB is treated as muted on load.
    const double MUTED_SENTINEL_DB = -100.0;

    // Vegas's audio fader caps at +12 dB (linear ≈ 3.98). Preset clamps here.
    const double MAX_VOLUME_DB = 12.0;

    // JSON schema version — bump when the schema changes in a breaking way.
    const int SCHEMA_VERSION = 1;

    // Every run writes the summary + diagnostic + generated JSON here,
    // overwriting any previous run. Useful when MessageBox text isn't
    // selectable — paste from this file instead.
    const string LAST_RUN_LOG = @"C:\Users\james\HatmasBot\vegas_presets\_tuneframe_last_run.txt";


    // Reflected AudioTrack.Mute (may be null on older SDKs). Resolved once.
    static readonly PropertyInfo MUTE_PROP =
        typeof(AudioTrack).GetProperty("Mute",
            BindingFlags.Public | BindingFlags.Instance);


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
            MessageBox.Show(
                "TuneFrame failed.\r\n\r\n" + ex.Message + "\r\n\r\n" + ex.StackTrace,
                "TuneFrame error");
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   MAIN LOGIC
    // ────────────────────────────────────────────────────────────────────────
    void Run(Vegas vegas)
    {
        Project proj = vegas.Project;

        int    projWidth     = proj.Video.Width;
        int    projHeight    = proj.Video.Height;
        double projFrameRate = proj.Video.FrameRate;

        string kind = InferKind(projWidth, projHeight);

        List<string> warnings = new List<string>();
        if (kind == "unknown")
        {
            warnings.Add(string.Format(
                "Project is {0}x{1}; expected 1080x1920 (highlight) or 1920x1080 (full_gameplay). " +
                "Kind set to \"unknown\".",
                projWidth, projHeight));
        }

        List<TrackCapture> videoCaps = CaptureVideoTracks(proj, warnings);
        List<AudioCapture> audioCaps = CaptureAudioTracks(proj, warnings);

        if (videoCaps.Count == 0 && audioCaps.Count == 0)
        {
            throw new Exception(
                "No video or audio tracks found. Build a project first " +
                "(HighlightBuilder, or drag a .mp4 in), then run TuneFrame.");
        }

        string audioDiagnostic = AUDIO_DIAGNOSTIC
            ? BuildAudioDiagnostic()
            : "";

        string videoDiagnostic = VIDEO_DIAGNOSTIC
            ? BuildVideoDiagnostic(proj)
            : "";

        string summary = BuildHumanReadableSummary(
            kind, projWidth, projHeight, projFrameRate,
            videoCaps, audioCaps, warnings,
            audioDiagnostic + videoDiagnostic + GetDouble2DDiagnostic());

        // Always write a full log to a fixed path — before the SaveFileDialog.
        // This way the diagnostic is on disk even if the user cancels, and
        // the text is easy to paste when the MessageBox isn't selectable.
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LAST_RUN_LOG));
            File.WriteAllText(LAST_RUN_LOG,
                summary + "\r\n\r\n── JSON (would-be preset contents) ──\r\n\r\n"
                + BuildPresetJson("PREVIEW", kind, projWidth, projHeight,
                                  projFrameRate, videoCaps, audioCaps));
        }
        catch (Exception ex)
        {
            warnings.Add("Could not write diagnostic log: " + ex.Message);
        }

        // Suggest a default filename based on kind.
        string defaultName = (kind == "highlight") ? "vertical_tiktok"
                           : (kind == "full_gameplay") ? "horizontal_full"
                           : "untitled";

        string savePath = AskWhereToSave(defaultName);
        if (savePath == null)
        {
            // User cancelled. Don't write anything.
            return;
        }

        // Derive preset name from filename (without extension).
        string presetName = Path.GetFileNameWithoutExtension(savePath);
        // Strip a trailing ".tune" if SaveFileDialog appended .tune.json
        if (presetName.EndsWith(".tune", StringComparison.OrdinalIgnoreCase))
            presetName = presetName.Substring(0, presetName.Length - ".tune".Length);

        string json = BuildPresetJson(
            presetName, kind, projWidth, projHeight, projFrameRate,
            videoCaps, audioCaps);

        // Make sure the preset directory exists.
        Directory.CreateDirectory(Path.GetDirectoryName(savePath));
        File.WriteAllText(savePath, json);

        // Archive: timestamped .txt in the ARCHIVE_OUTPUT_DIR for diffs.
        string timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        string archiveFile = string.Format("TuneFrame_preset_{0}_{1}.txt",
            SafeFileName(presetName), timestamp);
        Directory.CreateDirectory(ARCHIVE_OUTPUT_DIR);
        string archivePath = Path.Combine(ARCHIVE_OUTPUT_DIR, archiveFile);
        File.WriteAllText(archivePath, summary + "\r\n── JSON ──\r\n\r\n" + json);

        MessageBox.Show(
            summary + "\r\n\r\n" +
            "Preset written to:\r\n  " + savePath + "\r\n\r\n" +
            "Archive (diff history):\r\n  " + archivePath + "\r\n\r\n" +
            "Full log (selectable text):\r\n  " + LAST_RUN_LOG,
            "TuneFrame — captured preset");
    }


    // ────────────────────────────────────────────────────────────────────────
    //   VIDEO CAPTURE
    // ────────────────────────────────────────────────────────────────────────

    static List<TrackCapture> CaptureVideoTracks(Project proj, List<string> warnings)
    {
        List<VideoTrack> videoTracks = new List<VideoTrack>();
        foreach (Track t in proj.Tracks)
        {
            VideoTrack vt = t as VideoTrack;
            if (vt != null) videoTracks.Add(vt);
        }

        List<TrackCapture> captures = new List<TrackCapture>();

        for (int i = 0; i < videoTracks.Count; i++)
        {
            VideoTrack vt = videoTracks[i];

            VideoEvent firstEvent = null;
            foreach (TrackEvent te in vt.Events)
            {
                VideoEvent ve = te as VideoEvent;
                if (ve != null) { firstEvent = ve; break; }
            }

            if (firstEvent == null)
            {
                warnings.Add(string.Format(
                    "Video track {0} (\"{1}\") has no video events — skipped.",
                    i, vt.Name));
                continue;
            }

            VideoMotion motion = firstEvent.VideoMotion;
            if (motion == null || motion.Keyframes.Count == 0)
            {
                warnings.Add(string.Format(
                    "Video track {0} (\"{1}\") has no Pan/Crop keyframes — skipped.",
                    i, vt.Name));
                continue;
            }
            if (motion.Keyframes.Count > 1)
            {
                warnings.Add(string.Format(
                    "Video track {0} (\"{1}\") has {2} Pan/Crop keyframes; " +
                    "only the first was captured (animation not preserved).",
                    i, vt.Name, motion.Keyframes.Count));
            }

            VideoMotionKeyframe kf = motion.Keyframes[0];

            TrackCapture cap = new TrackCapture();
            cap.Index           = i;
            cap.Name            = vt.Name ?? "";
            cap.Composite       = vt.CompositeMode;
            cap.TopLeftX        = kf.TopLeft.X;
            cap.TopLeftY        = kf.TopLeft.Y;
            cap.TopRightX       = kf.TopRight.X;
            cap.TopRightY       = kf.TopRight.Y;
            cap.BottomRightX    = kf.BottomRight.X;
            cap.BottomRightY    = kf.BottomRight.Y;
            cap.BottomLeftX     = kf.BottomLeft.X;
            cap.BottomLeftY     = kf.BottomLeft.Y;
            cap.CenterX         = kf.Center.X;
            cap.CenterY         = kf.Center.Y;
            cap.Rotation        = kf.Rotation;
            cap.Smoothness      = kf.Smoothness;
            cap.KeyframeType    = kf.Type;
            cap.ScaleToFill     = motion.ScaleToFill;
            cap.Effects         = CaptureEffects(firstEvent, warnings);
            captures.Add(cap);
        }
        return captures;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   AUDIO CAPTURE
    // ────────────────────────────────────────────────────────────────────────

    static List<AudioCapture> CaptureAudioTracks(Project proj, List<string> warnings)
    {
        List<AudioTrack> audioTracks = new List<AudioTrack>();
        foreach (Track t in proj.Tracks)
        {
            AudioTrack at = t as AudioTrack;
            if (at != null) audioTracks.Add(at);
        }

        List<AudioCapture> captures = new List<AudioCapture>();

        for (int i = 0; i < audioTracks.Count; i++)
        {
            AudioTrack at = audioTracks[i];

            // Vegas stores audio volume as a linear scalar on AudioTrack.Volume.
            // 1.0 == unity gain (0 dB). We convert to dB for readability.
            double linearVol = at.Volume;
            double db;
            if (linearVol <= 0.0)
            {
                db = MUTED_SENTINEL_DB;
            }
            else
            {
                db = 20.0 * Math.Log10(linearVol);
                if (db > MAX_VOLUME_DB) db = MAX_VOLUME_DB;
                if (db < MUTED_SENTINEL_DB) db = MUTED_SENTINEL_DB;
            }

            // Mute state — read via reflection so this script works whether
            // or not Vegas 21's AudioTrack exposes .Mute.
            bool muted;
            if (MUTE_PROP != null)
            {
                try
                {
                    muted = (bool)MUTE_PROP.GetValue(at, null);
                }
                catch
                {
                    muted = (db <= MUTED_SENTINEL_DB + 1.0);
                }
            }
            else
            {
                muted = (db <= MUTED_SENTINEL_DB + 1.0);
            }

            AudioCapture cap = new AudioCapture();
            cap.Index  = i;
            cap.Name   = at.Name ?? "";
            cap.Volume_dB = db;
            cap.Muted  = muted;
            captures.Add(cap);
        }
        return captures;
    }


    // ────────────────────────────────────────────────────────────────────────
    //   OFX EFFECT CAPTURE
    // ────────────────────────────────────────────────────────────────────────

    static List<EffectCapture> CaptureEffects(VideoEvent ve, List<string> warnings)
    {
        List<EffectCapture> result = new List<EffectCapture>();
        try
        {
            foreach (Effect e in ve.Effects)
            {
                if (!e.IsOFX)
                {
                    warnings.Add("Skipping non-OFX effect on track event: "
                        + (e.Description ?? "(unknown)"));
                    continue;
                }

                EffectCapture ec = new EffectCapture();
                ec.PlugInId   = (e.PlugIn != null) ? e.PlugIn.UniqueID : "";
                ec.PlugInName = (e.PlugIn != null) ? e.PlugIn.Name     : "";
                ec.Bypass     = e.Bypass;

                OFXEffect ofx = e.OFXEffect;
                if (ofx == null || ofx.Parameters == null)
                {
                    warnings.Add("OFX effect '" + ec.PlugInName
                        + "' exposed no Parameters — captured empty.");
                }
                else
                {
                    foreach (OFXParameter p in ofx.Parameters)
                    {
                        ParamCapture pc = CaptureParam(p);
                        if (pc != null) ec.Parameters.Add(pc);
                    }
                }
                result.Add(ec);
            }
        }
        catch (Exception ex)
        {
            warnings.Add("Effect capture failed: " + ex.Message);
        }
        return result;
    }

    // Capture one OFX parameter. Returns null for Group / PushButton types
    // (no state to save) and for types we don't know how to serialize.
    static ParamCapture CaptureParam(OFXParameter p)
    {
        if (p == null) return null;
        string ptype = p.ParameterType.ToString();
        ParamCapture pc = new ParamCapture();
        pc.Name = p.Name;
        pc.Type = ptype;

        // Use reflection for the Value property since concrete types differ.
        PropertyInfo valProp = p.GetType().GetProperty("Value",
            BindingFlags.Public | BindingFlags.Instance);
        if (valProp == null) return null;  // Group, PushButton, etc.

        object v;
        try { v = valProp.GetValue(p, null); }
        catch { return null; }
        if (v == null) return null;

        switch (ptype)
        {
            case "Boolean":
                pc.BoolValue = (bool)v;
                return pc;
            case "Double":
                pc.DoubleValue = (double)v;
                return pc;
            case "Integer":
                pc.IntValue = (int)v;
                return pc;
            case "Choice":
                // OFXChoice has a Name property (the string shown in the UI).
                PropertyInfo np = v.GetType().GetProperty("Name",
                    BindingFlags.Public | BindingFlags.Instance);
                pc.StringValue = (np != null)
                    ? (string)np.GetValue(v, null)
                    : v.ToString();
                return pc;
            case "Double2D":
                double dx, dy;
                if (TryGetXY(v, out dx, out dy))
                {
                    pc.XValue = dx;
                    pc.YValue = dy;
                    return pc;
                }
                // Couldn't extract — log once for diagnosis.
                LogDouble2DShape(v);
                return null;
            case "String":
            case "Custom":
                pc.StringValue = v.ToString();
                return pc;
            default:
                // Unhandled: Double3D, Integer2D, Integer3D, RGB, RGBA.
                // Fall back to ToString so the data isn't lost; apply-side
                // will log a warning if it can't use it.
                pc.StringValue = v.ToString();
                return pc;
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   DIAGNOSTICS (kept around but gated by VIDEO_DIAGNOSTIC / AUDIO_DIAGNOSTIC)
    // ────────────────────────────────────────────────────────────────────────

    // Reflection dump of VideoEvent / VideoMotion / VideoMotionKeyframe
    // public properties + a probe into VideoEvent.Effects + a search of
    // the whole ScriptPortal.Vegas assembly for any type/property with
    // "mask" in its name. Goal: find where Vegas exposes Pan/Crop Mask.
    static string BuildVideoDiagnostic(Project proj)
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("── Video diagnostic (looking for Mask property) ──");
        sb.AppendLine();

        DumpTypeProps(sb, "VideoEvent",          typeof(VideoEvent));
        DumpTypeProps(sb, "VideoMotion",         typeof(VideoMotion));
        DumpTypeProps(sb, "VideoMotionKeyframe", typeof(VideoMotionKeyframe));

        // Probe the first video event's actual current values for any
        // property containing "mask" in its name. Also dump its Effects
        // list in full — Pan/Crop's Mask feature may live inside the
        // Effects collection rather than on VideoMotion.
        foreach (Track t in proj.Tracks)
        {
            VideoTrack vt = t as VideoTrack;
            if (vt == null) continue;
            foreach (TrackEvent te in vt.Events)
            {
                VideoEvent ve = te as VideoEvent;
                if (ve == null) continue;

                sb.AppendLine("── First VideoEvent — Mask-related current values ──");
                ProbeMask(sb, "VideoEvent",          ve);
                ProbeMask(sb, "VideoMotion",         ve.VideoMotion);
                if (ve.VideoMotion != null && ve.VideoMotion.Keyframes.Count > 0)
                    ProbeMask(sb, "VideoMotionKeyframe[0]", ve.VideoMotion.Keyframes[0]);
                sb.AppendLine();

                sb.AppendLine("── First VideoEvent — Effects collection ──");
                try
                {
                    Effects fx = ve.Effects;
                    sb.AppendLine(string.Format("Effects count: {0}", fx.Count));
                    for (int i = 0; i < fx.Count; i++)
                    {
                        Effect e = fx[i];
                        DumpEffect(sb, i, e);
                    }
                }
                catch (Exception ex)
                {
                    sb.AppendLine("Error walking Effects: " + ex.Message);
                }
                goto done;
            }
        }
        done:
        sb.AppendLine();

        // Broad search: iterate every type in the Vegas SDK assembly and
        // flag anything whose name contains mask-related keywords. Mask
        // might be exposed under a non-obvious name.
        string[] keywords = new[] {
            "mask", "matte", "alpha", "clip", "path", "region", "shape",
            "cutout", "stencil", "cookie"
        };
        sb.AppendLine("── Vegas assembly: types/properties matching mask-like keywords ──");
        try
        {
            Assembly sdk = typeof(VideoEvent).Assembly;
            foreach (Type t in sdk.GetTypes())
            {
                string typeHit = null;
                foreach (string kw in keywords)
                {
                    if (t.Name.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        typeHit = kw;
                        break;
                    }
                }
                List<string> propHits = new List<string>();
                foreach (PropertyInfo p in t.GetProperties(
                             BindingFlags.Public | BindingFlags.Instance))
                {
                    foreach (string kw in keywords)
                    {
                        if (p.Name.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            propHits.Add(p.PropertyType.Name + " " + p.Name
                                + "  (matched: " + kw + ")");
                            break;
                        }
                    }
                }
                if (typeHit != null || propHits.Count > 0)
                {
                    sb.AppendLine(string.Format("  {0}{1}", t.FullName,
                        typeHit != null ? "  [TYPE MATCHED: " + typeHit + "]" : ""));
                    foreach (string ph in propHits)
                        sb.AppendLine("      ." + ph);
                }
            }
        }
        catch (Exception ex)
        {
            sb.AppendLine("Error scanning assembly: " + ex.Message);
        }
        sb.AppendLine();

        // Full type dump — every public type in the Vegas SDK assembly.
        // Big list but one-time; lets us eyeball anything we missed.
        sb.AppendLine("── Vegas assembly: full type list (every public type) ──");
        try
        {
            Assembly sdk = typeof(VideoEvent).Assembly;
            sb.AppendLine("Assembly: " + sdk.FullName);
            List<string> typeNames = new List<string>();
            foreach (Type t in sdk.GetTypes())
            {
                if (!t.IsPublic && !t.IsNestedPublic) continue;
                typeNames.Add((t.IsInterface ? "I " : "C ") + t.FullName);
            }
            typeNames.Sort();
            foreach (string tn in typeNames) sb.AppendLine("  " + tn);
        }
        catch (Exception ex)
        {
            sb.AppendLine("Error listing types: " + ex.Message);
        }
        sb.AppendLine();

        // Referenced assemblies — maybe a separate assembly exposes Mask.
        sb.AppendLine("── Referenced assemblies from ScriptPortal.Vegas ──");
        try
        {
            foreach (AssemblyName an in typeof(VideoEvent).Assembly.GetReferencedAssemblies())
                sb.AppendLine("  " + an.FullName);
        }
        catch (Exception ex)
        {
            sb.AppendLine("Error listing refs: " + ex.Message);
        }
        sb.AppendLine();

        // Also dump Track / VideoTrack / TrackMotion-like properties —
        // maybe Mask is track-level rather than event-level.
        sb.AppendLine("── Track / VideoTrack properties ──");
        DumpTypeProps(sb, "Track",      typeof(Track));
        DumpTypeProps(sb, "VideoTrack", typeof(VideoTrack));

        return sb.ToString();
    }

    static void DumpEffect(StringBuilder sb, int idx, Effect e)
    {
        sb.AppendLine(string.Format("Effect[{0}]:", idx));
        if (e == null) { sb.AppendLine("  (null)"); return; }
        try { sb.AppendLine("  Description: " + e.Description); } catch { }
        try { sb.AppendLine("  IsOFX:       " + e.IsOFX); } catch { }
        try { sb.AppendLine("  Bypass:      " + e.Bypass); } catch { }
        try
        {
            if (e.PlugIn != null)
            {
                sb.AppendLine("  PlugIn.Name:     " + e.PlugIn.Name);
                sb.AppendLine("  PlugIn.UniqueID: " + e.PlugIn.UniqueID);
            }
        }
        catch { }

        // Deep-dump the OFXEffect's parameters — names, labels, types,
        // and current values. This is the data we need to script Bezier
        // Masking capture + apply.
        try
        {
            OFXEffect ofx = e.OFXEffect;
            if (ofx == null)
            {
                sb.AppendLine("  OFXEffect: (null — not an OFX effect)");
                return;
            }
            sb.AppendLine("  OFXEffect parameters:");
            OFXParameters ps = ofx.Parameters;
            sb.AppendLine(string.Format("    parameter count: {0}", ps.Count));
            for (int i = 0; i < ps.Count; i++)
            {
                OFXParameter p = ps[i];
                DumpOFXParameter(sb, i, p);
            }
        }
        catch (Exception ex)
        {
            sb.AppendLine("  (OFXEffect dump failed: " + ex.Message + ")");
        }
    }

    // Dump one OFX parameter — name, label, type, current value. For value,
    // we reflect over the concrete parameter subtype (OFXDoubleParameter,
    // OFXDouble2DParameter, etc.) and try to pull the Value property.
    static void DumpOFXParameter(StringBuilder sb, int idx, OFXParameter p)
    {
        if (p == null) { sb.AppendLine(string.Format("    [{0}] (null)", idx)); return; }
        Type pt = p.GetType();
        sb.AppendLine(string.Format("    [{0}] {1} (type: {2})",
            idx, SafeGet(p, "Name"), pt.Name));
        sb.AppendLine(string.Format("         Label:       {0}", SafeGet(p, "Label")));
        sb.AppendLine(string.Format("         ParamType:   {0}", SafeGet(p, "ParameterType")));
        sb.AppendLine(string.Format("         IsAnimating: {0}", SafeGet(p, "IsAnimating")));
        // Try reading a "Value" property (defined on concrete subtypes).
        PropertyInfo valProp = pt.GetProperty("Value",
            BindingFlags.Public | BindingFlags.Instance);
        if (valProp != null)
        {
            object v;
            try { v = valProp.GetValue(p, null); }
            catch (Exception ex) { v = "(getter threw: " + ex.Message + ")"; }
            sb.AppendLine(string.Format("         Value:       {0} ({1})",
                v == null ? "null" : v.ToString(),
                valProp.PropertyType.Name));
        }
        // For OFXChoice etc., also dump Choices if present.
        PropertyInfo choicesProp = pt.GetProperty("Choices",
            BindingFlags.Public | BindingFlags.Instance);
        if (choicesProp != null)
        {
            try
            {
                object choices = choicesProp.GetValue(p, null);
                sb.AppendLine("         Choices:     " +
                    (choices == null ? "null" : choices.ToString()));
            }
            catch { }
        }
        // Dump property names of this parameter's type for reference
        // (so we can see what else is available per parameter kind).
        List<string> extraProps = new List<string>();
        foreach (PropertyInfo ep in pt.GetProperties(
                     BindingFlags.Public | BindingFlags.Instance))
        {
            if (ep.Name == "Name" || ep.Name == "Label"
             || ep.Name == "ParameterType" || ep.Name == "IsAnimating"
             || ep.Name == "Value" || ep.Name == "Choices") continue;
            extraProps.Add(ep.PropertyType.Name + " " + ep.Name
                + (ep.CanWrite ? "" : " (ro)"));
        }
        if (extraProps.Count > 0)
        {
            sb.AppendLine("         Other props: " +
                string.Join(", ", extraProps.ToArray()));
        }
    }

    // Extract the X, Y scalars from an OFX Double2D. Verified 2026-04-24:
    // ScriptPortal.Vegas.OFXDouble2D exposes X and Y as public FIELDS
    // (not properties). Older Vegas versions may differ — fall back to
    // property lookup if fields miss.
    static bool TryGetXY(object v, out double x, out double y)
    {
        x = 0; y = 0;
        if (v == null) return false;
        Type t = v.GetType();

        // Fields first (OFXDouble2D on Vegas 21).
        FieldInfo xf = t.GetField("X", BindingFlags.Public | BindingFlags.Instance);
        FieldInfo yf = t.GetField("Y", BindingFlags.Public | BindingFlags.Instance);
        if (xf != null && yf != null)
        {
            try
            {
                x = Convert.ToDouble(xf.GetValue(v));
                y = Convert.ToDouble(yf.GetValue(v));
                return true;
            }
            catch { /* fall through to property attempts */ }
        }

        // Properties fallback (defensive for alternate SDKs).
        string[] xNames = { "X", "x", "U", "u" };
        string[] yNames = { "Y", "y", "V", "v" };
        for (int i = 0; i < xNames.Length; i++)
        {
            PropertyInfo xp = t.GetProperty(xNames[i],
                BindingFlags.Public | BindingFlags.Instance);
            PropertyInfo yp = t.GetProperty(yNames[i],
                BindingFlags.Public | BindingFlags.Instance);
            if (xp == null || yp == null) continue;
            try
            {
                x = Convert.ToDouble(xp.GetValue(v, null));
                y = Convert.ToDouble(yp.GetValue(v, null));
                return true;
            }
            catch { continue; }
        }
        return false;
    }

    // One-shot reflection dump collected into a static buffer when Double2D
    // extraction fails — included in the summary (which is what actually
    // gets written to LAST_RUN_LOG). Fixed-path File.AppendAllText here
    // would be wiped by the final File.WriteAllText in Run().
    static StringBuilder _double2DDiagnostic = new StringBuilder();
    static bool _double2DShapeLogged = false;
    static void LogDouble2DShape(object v)
    {
        if (_double2DShapeLogged || v == null) return;
        _double2DShapeLogged = true;
        try
        {
            _double2DDiagnostic.AppendLine();
            _double2DDiagnostic.AppendLine("── OFXDouble2D shape diagnostic ──");
            _double2DDiagnostic.AppendLine("  Concrete type: " + v.GetType().FullName);
            _double2DDiagnostic.AppendLine("  Public properties:");
            foreach (PropertyInfo p in v.GetType().GetProperties(
                         BindingFlags.Public | BindingFlags.Instance))
            {
                _double2DDiagnostic.AppendLine(string.Format("    {0} {1} {2}",
                    p.PropertyType.Name, p.Name,
                    p.CanWrite ? "(read/write)" : "(read only)"));
            }
            _double2DDiagnostic.AppendLine("  Public fields:");
            foreach (FieldInfo f in v.GetType().GetFields(
                         BindingFlags.Public | BindingFlags.Instance))
            {
                _double2DDiagnostic.AppendLine(string.Format("    {0} {1}",
                    f.FieldType.Name, f.Name));
            }
        }
        catch { /* ignore */ }
    }

    public static string GetDouble2DDiagnostic()
    {
        return _double2DDiagnostic.Length > 0 ? _double2DDiagnostic.ToString() : "";
    }

    static string SafeGet(object obj, string propName)
    {
        PropertyInfo p = obj.GetType().GetProperty(propName,
            BindingFlags.Public | BindingFlags.Instance);
        if (p == null) return "(no " + propName + " property)";
        try
        {
            object v = p.GetValue(obj, null);
            return v == null ? "null" : v.ToString();
        }
        catch (Exception ex) { return "(threw: " + ex.Message + ")"; }
    }

    static void DumpTypeProps(StringBuilder sb, string label, Type t)
    {
        sb.AppendLine(string.Format("Public properties of {0}:", label));
        PropertyInfo[] props = t.GetProperties(
            BindingFlags.Public | BindingFlags.Instance);
        Array.Sort(props, (a, b) => string.Compare(a.Name, b.Name, StringComparison.Ordinal));
        foreach (PropertyInfo p in props)
        {
            sb.AppendLine(string.Format("    {0} {1} {2}",
                p.PropertyType.Name,
                p.Name,
                p.CanWrite ? "(read/write)" : "(read only)"));
        }
        sb.AppendLine();
    }

    static void ProbeMask(StringBuilder sb, string label, object obj)
    {
        if (obj == null)
        {
            sb.AppendLine(string.Format("  {0}: (null)", label));
            return;
        }
        PropertyInfo[] props = obj.GetType().GetProperties(
            BindingFlags.Public | BindingFlags.Instance);
        foreach (PropertyInfo p in props)
        {
            if (p.Name.IndexOf("mask", StringComparison.OrdinalIgnoreCase) < 0) continue;
            object val;
            try { val = p.GetValue(obj, null); }
            catch (Exception ex) { val = "(getter threw: " + ex.Message + ")"; }
            sb.AppendLine(string.Format("  {0}.{1} = {2}  ({3})",
                label, p.Name, val == null ? "null" : val.ToString(),
                p.PropertyType.Name));
        }
    }

    // Reflection dump of AudioTrack's public properties — shown in the
    // summary dialog on first run so we can verify .Mute exists.
    static string BuildAudioDiagnostic()
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("── Audio diagnostic (AudioTrack public properties) ──");
        sb.AppendLine();
        sb.AppendLine(string.Format(
            "Mute property via reflection: {0}",
            MUTE_PROP != null
                ? "FOUND — " + MUTE_PROP.PropertyType.Name + " Mute"
                : "NOT FOUND — using volume <= -99 dB sentinel"));
        sb.AppendLine();
        sb.AppendLine("Public properties exposed by ScriptPortal.Vegas.AudioTrack:");
        PropertyInfo[] props = typeof(AudioTrack).GetProperties(
            BindingFlags.Public | BindingFlags.Instance);
        Array.Sort(props, (a, b) => string.Compare(a.Name, b.Name, StringComparison.Ordinal));
        foreach (PropertyInfo p in props)
        {
            sb.AppendLine(string.Format("    {0} {1} {2}",
                p.PropertyType.Name,
                p.Name,
                p.CanWrite ? "(read/write)" : "(read only)"));
        }
        sb.AppendLine();
        return sb.ToString();
    }


    // ────────────────────────────────────────────────────────────────────────
    //   JSON OUTPUT
    // ────────────────────────────────────────────────────────────────────────

    static string BuildPresetJson(
        string presetName, string kind,
        int projWidth, int projHeight, double projFrameRate,
        List<TrackCapture> videoCaps, List<AudioCapture> audioCaps)
    {
        // Hand-rolled JSON writer — keeps the Vegas script free of a JSON
        // library dependency. Output is formatted 2-space indent for diffability.
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("{");
        sb.AppendLine(string.Format("  \"schema_version\": {0},", SCHEMA_VERSION));
        sb.AppendLine(string.Format("  \"name\": {0},", JsonString(presetName)));
        sb.AppendLine(string.Format("  \"kind\": {0},", JsonString(kind)));
        sb.AppendLine(string.Format("  \"captured_at\": {0},",
            JsonString(DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss",
                CultureInfo.InvariantCulture))));

        sb.AppendLine("  \"project\": {");
        sb.AppendLine(string.Format("    \"width\": {0},", projWidth));
        sb.AppendLine(string.Format("    \"height\": {0},", projHeight));
        sb.AppendLine(string.Format("    \"framerate\": {0}", Fmt(projFrameRate)));
        sb.AppendLine("  },");

        // Video tracks
        sb.AppendLine("  \"video_tracks\": [");
        for (int i = 0; i < videoCaps.Count; i++)
        {
            TrackCapture c = videoCaps[i];
            sb.AppendLine("    {");
            sb.AppendLine(string.Format("      \"name\": {0},", JsonString(c.Name)));
            sb.AppendLine(string.Format("      \"composite\": {0},",
                JsonString(c.Composite.ToString())));
            sb.AppendLine(string.Format(
                "      \"top_left\":     {{\"x\": {0}, \"y\": {1}}},",
                Fmt(c.TopLeftX), Fmt(c.TopLeftY)));
            sb.AppendLine(string.Format(
                "      \"top_right\":    {{\"x\": {0}, \"y\": {1}}},",
                Fmt(c.TopRightX), Fmt(c.TopRightY)));
            sb.AppendLine(string.Format(
                "      \"bottom_right\": {{\"x\": {0}, \"y\": {1}}},",
                Fmt(c.BottomRightX), Fmt(c.BottomRightY)));
            sb.AppendLine(string.Format(
                "      \"bottom_left\":  {{\"x\": {0}, \"y\": {1}}},",
                Fmt(c.BottomLeftX), Fmt(c.BottomLeftY)));
            sb.AppendLine(string.Format(
                "      \"center\":       {{\"x\": {0}, \"y\": {1}}},",
                Fmt(c.CenterX), Fmt(c.CenterY)));
            sb.AppendLine(string.Format("      \"rotation_rad\": {0},", Fmt(c.Rotation)));
            sb.AppendLine(string.Format("      \"smoothness\":   {0},", Fmt(c.Smoothness)));
            sb.AppendLine(string.Format("      \"keyframe_type\": {0},",
                JsonString(c.KeyframeType.ToString())));
            sb.AppendLine(string.Format("      \"scale_to_fill\": {0},",
                c.ScaleToFill ? "true" : "false"));
            // effects array (may be empty)
            sb.AppendLine("      \"effects\": [");
            for (int ei = 0; ei < c.Effects.Count; ei++)
            {
                EffectCapture ec = c.Effects[ei];
                sb.AppendLine("        {");
                sb.AppendLine(string.Format("          \"plugin_id\":   {0},", JsonString(ec.PlugInId)));
                sb.AppendLine(string.Format("          \"plugin_name\": {0},", JsonString(ec.PlugInName)));
                sb.AppendLine(string.Format("          \"bypass\":      {0},", ec.Bypass ? "true" : "false"));
                sb.AppendLine("          \"parameters\": [");
                for (int pi = 0; pi < ec.Parameters.Count; pi++)
                {
                    ParamCapture pc = ec.Parameters[pi];
                    string valStr;
                    switch (pc.Type)
                    {
                        case "Boolean":
                            valStr = pc.BoolValue ? "true" : "false";
                            break;
                        case "Double":
                            valStr = Fmt(pc.DoubleValue);
                            break;
                        case "Integer":
                            valStr = pc.IntValue.ToString(CultureInfo.InvariantCulture);
                            break;
                        case "Double2D":
                            valStr = "{\"x\": " + Fmt(pc.XValue) + ", \"y\": " + Fmt(pc.YValue) + "}";
                            break;
                        default:
                            // Choice, String, Custom, and any unhandled type fall through
                            // as a JSON string — safest lossless representation.
                            valStr = JsonString(pc.StringValue ?? "");
                            break;
                    }
                    sb.Append(string.Format(
                        "            {{\"name\": {0}, \"type\": {1}, \"value\": {2}}}",
                        JsonString(pc.Name), JsonString(pc.Type), valStr));
                    sb.AppendLine(pi < ec.Parameters.Count - 1 ? "," : "");
                }
                sb.AppendLine("          ]");
                sb.Append("        }");
                sb.AppendLine(ei < c.Effects.Count - 1 ? "," : "");
            }
            sb.AppendLine("      ]");
            sb.Append("    }");
            sb.AppendLine(i < videoCaps.Count - 1 ? "," : "");
        }
        sb.AppendLine("  ],");

        // Audio tracks
        sb.AppendLine("  \"audio_tracks\": [");
        for (int i = 0; i < audioCaps.Count; i++)
        {
            AudioCapture a = audioCaps[i];
            sb.AppendLine("    {");
            sb.AppendLine(string.Format("      \"name\": {0},", JsonString(a.Name)));
            sb.AppendLine(string.Format("      \"volume_db\": {0},", Fmt(a.Volume_dB)));
            sb.AppendLine(string.Format("      \"muted\": {0}",
                a.Muted ? "true" : "false"));
            sb.Append("    }");
            sb.AppendLine(i < audioCaps.Count - 1 ? "," : "");
        }
        sb.AppendLine("  ]");

        sb.AppendLine("}");
        return sb.ToString();
    }


    // ────────────────────────────────────────────────────────────────────────
    //   HUMAN-READABLE SUMMARY
    // ────────────────────────────────────────────────────────────────────────

    static string BuildHumanReadableSummary(
        string kind, int projWidth, int projHeight, double projFrameRate,
        List<TrackCapture> videoCaps, List<AudioCapture> audioCaps,
        List<string> warnings, string audioDiagnostic)
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("TuneFrame — captured preset summary");
        sb.AppendLine("───────────────────────────────────");
        sb.AppendLine(string.Format("Kind        : {0}", kind));
        sb.AppendLine(string.Format("Project     : {0} x {1} @ {2} fps",
            projWidth, projHeight, Fmt(projFrameRate)));
        sb.AppendLine(string.Format(
            "Captured    : {0} video track(s), {1} audio track(s)",
            videoCaps.Count, audioCaps.Count));
        sb.AppendLine();

        if (videoCaps.Count > 0) sb.AppendLine("Video tracks:");
        for (int i = 0; i < videoCaps.Count; i++)
        {
            TrackCapture c = videoCaps[i];
            sb.AppendLine(string.Format(
                "  [{0}] {1}  (composite: {2})",
                i, string.IsNullOrEmpty(c.Name) ? "(unnamed)" : c.Name, c.Composite));
            sb.AppendLine(string.Format(
                "      crop: TL({0},{1})  TR({2},{3})  BR({4},{5})  BL({6},{7})",
                Fmt(c.TopLeftX),     Fmt(c.TopLeftY),
                Fmt(c.TopRightX),    Fmt(c.TopRightY),
                Fmt(c.BottomRightX), Fmt(c.BottomRightY),
                Fmt(c.BottomLeftX),  Fmt(c.BottomLeftY)));
            double rotationDeg = c.Rotation * (180.0 / Math.PI);
            sb.AppendLine(string.Format(
                "      center: ({0},{1})  rotation: {2} rad ({3} deg)  scale_to_fill: {4}",
                Fmt(c.CenterX), Fmt(c.CenterY),
                Fmt(c.Rotation), Fmt(rotationDeg),
                c.ScaleToFill ? "true" : "false"));
            if (c.Effects.Count > 0)
            {
                int totalParams = 0;
                foreach (EffectCapture ec in c.Effects) totalParams += ec.Parameters.Count;
                sb.AppendLine(string.Format(
                    "      effects: {0} ({1} params total)",
                    c.Effects.Count, totalParams));
                foreach (EffectCapture ec in c.Effects)
                    sb.AppendLine(string.Format(
                        "        - {0}  [{1} params]{2}",
                        ec.PlugInName, ec.Parameters.Count,
                        ec.Bypass ? "  [BYPASSED]" : ""));
            }
        }

        if (audioCaps.Count > 0)
        {
            sb.AppendLine();
            sb.AppendLine("Audio tracks:");
            for (int i = 0; i < audioCaps.Count; i++)
            {
                AudioCapture a = audioCaps[i];
                sb.AppendLine(string.Format(
                    "  [{0}] {1}  volume: {2} dB{3}",
                    i,
                    string.IsNullOrEmpty(a.Name) ? "(unnamed)" : a.Name,
                    Fmt(a.Volume_dB),
                    a.Muted ? "  [MUTED]" : ""));
            }
        }

        if (warnings.Count > 0)
        {
            sb.AppendLine();
            sb.AppendLine("Warnings:");
            foreach (string w in warnings) sb.AppendLine("  ! " + w);
        }

        if (!string.IsNullOrEmpty(audioDiagnostic))
        {
            sb.AppendLine();
            sb.Append(audioDiagnostic);
        }

        return sb.ToString();
    }


    // ────────────────────────────────────────────────────────────────────────
    //   SAVE-AS DIALOG
    // ────────────────────────────────────────────────────────────────────────

    static string AskWhereToSave(string defaultName)
    {
        // Make sure the preset dir exists so the dialog opens there.
        Directory.CreateDirectory(PRESET_OUTPUT_DIR);

        // Pre-select the newest existing .tune.json if one exists.
        string preselect = null;
        try
        {
            string[] existing = Directory.GetFiles(PRESET_OUTPUT_DIR, "*.tune.json");
            if (existing.Length > 0)
            {
                Array.Sort(existing, (a, b) =>
                    File.GetLastWriteTime(b).CompareTo(File.GetLastWriteTime(a)));
                preselect = Path.GetFileName(existing[0]);
            }
        }
        catch { /* ignore */ }

        using (SaveFileDialog dlg = new SaveFileDialog())
        {
            dlg.Title            = "TuneFrame — save preset";
            dlg.InitialDirectory = PRESET_OUTPUT_DIR;
            dlg.Filter           = "Tune presets (*.tune.json)|*.tune.json|All files (*.*)|*.*";
            dlg.DefaultExt       = "tune.json";
            dlg.AddExtension     = true;
            dlg.OverwritePrompt  = true;
            dlg.FileName         = preselect != null ? preselect : (defaultName + ".tune.json");

            DialogResult result = dlg.ShowDialog();
            if (result != DialogResult.OK) return null;

            string chosen = dlg.FileName;
            // AddExtension adds ".tune.json" if user typed without extension,
            // but may just add ".json" if the filter's default extension is
            // interpreted as one token. Normalize: if it doesn't end with
            // .tune.json, append it (avoid double-appending).
            if (!chosen.EndsWith(".tune.json", StringComparison.OrdinalIgnoreCase))
            {
                // Strip a lone .json if the user ended up with name.json
                if (chosen.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                    chosen = chosen.Substring(0, chosen.Length - ".json".Length);
                chosen += ".tune.json";
            }
            return chosen;
        }
    }


    // ────────────────────────────────────────────────────────────────────────
    //   HELPERS
    // ────────────────────────────────────────────────────────────────────────

    class TrackCapture
    {
        public int    Index;
        public string Name;
        public CompositeMode Composite;
        public double TopLeftX,     TopLeftY;
        public double TopRightX,    TopRightY;
        public double BottomRightX, BottomRightY;
        public double BottomLeftX,  BottomLeftY;
        public double CenterX,      CenterY;
        public double Rotation;
        public double Smoothness;
        public VideoKeyframeType KeyframeType;
        public bool   ScaleToFill;        // VideoMotion.ScaleToFill — per-event, not per-keyframe
        public List<EffectCapture> Effects = new List<EffectCapture>();
    }

    class EffectCapture
    {
        public string PlugInId;           // e.g. "{Svfx:com.vegascreativesoftware:bzmasking}"
        public string PlugInName;
        public bool   Bypass;
        public List<ParamCapture> Parameters = new List<ParamCapture>();
    }

    class ParamCapture
    {
        public string Name;
        public string Type;               // "Boolean" | "Double" | "Integer" | "Choice" | "Double2D" | "String" | "Custom"
        // One of the following is populated based on Type:
        public bool   BoolValue;
        public double DoubleValue;
        public int    IntValue;
        public string StringValue;        // also used for Choice (stores choice name) and Custom
        public double XValue, YValue;     // for Double2D
    }

    class AudioCapture
    {
        public int    Index;
        public string Name;
        public double Volume_dB;
        public bool   Muted;
    }

    static string InferKind(int w, int h)
    {
        if (w == 1080 && h == 1920) return "highlight";
        if (w == 1920 && h == 1080) return "full_gameplay";
        return "unknown";
    }

    static string Fmt(double d)
    {
        return d.ToString("0.####", CultureInfo.InvariantCulture);
    }

    // Escape a string as a JSON string literal (double-quoted, backslash + quote escaped).
    static string JsonString(string raw)
    {
        if (raw == null) return "\"\"";
        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        foreach (char ch in raw)
        {
            switch (ch)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"':  sb.Append("\\\""); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (ch < 0x20)
                        sb.AppendFormat(CultureInfo.InvariantCulture,
                            "\\u{0:x4}", (int)ch);
                    else
                        sb.Append(ch);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    // Strip path-illegal characters from a preset name used in a filename.
    static string SafeFileName(string raw)
    {
        StringBuilder sb = new StringBuilder();
        foreach (char ch in raw)
        {
            if (char.IsLetterOrDigit(ch) || ch == '_' || ch == '-') sb.Append(ch);
            else sb.Append('_');
        }
        return sb.Length == 0 ? "untitled" : sb.ToString();
    }
}
