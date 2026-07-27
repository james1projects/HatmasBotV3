# Sony Vegas Automation Pipeline — TODO

Plan for extending the existing VOD → `.events.json` → Vegas pipeline into a
two-phase workflow: **full-gameplay render** (horizontal YouTube) and
**highlight render** (vertical TikTok), driven by a Python orchestrator, with
presets captured once in Vegas and replayed on every future video.

Partner: James. Target: Sony Vegas Pro **21.0**. Date planned: 2026-04-24.
**Status (last updated 2026-04-24):** Steps 1–7 complete. Preset capture +
JSON-driven HighlightBuilder + Bézier Masking OFX capture/apply all working.
Up next: Step 8 (capture horizontal preset), Step 9 (ProcessVideo.cs mega-script),
Step 10 (Python orchestrator).

---

## 1. Goal

1. Drag `.mp4` files into `inbox\`.
2. Run `python tools\process_vods.py`. For each video it:
   - Generates `<name>.events.json` (existing `extract_events.py` already does this).
   - Opens Vegas with the **full gameplay** timeline pre-built from a saved preset.
     James tweaks the start/end, renders to `rendered\`, closes Vegas.
   - Opens Vegas again with the **highlight** timeline pre-built from the original
     `.mp4` + `.events.json` + vertical preset. James tweaks, renders to
     `highlight\`, closes Vegas.
   - Moves to the next file.
3. Presets (pan/crop + composite + audio track volumes) are captured by
   `TuneFrame.cs` as JSON files — no more pasting C# blocks into source.

**No auto-render for now.** James renders manually and closes Vegas when done;
Python resumes on process exit. Render templates (`"Youtube HD"`,
`"TikTok YouTube Short HD"`) are wired into config so flipping auto-render on
later is a one-line change.

---

## 2. Current state

- `tools/extract_events.py` — writes `<name>.events.json` alongside each `.mp4`. Working well.
- `HighlightBuilder.cs` — builds a vertical TikTok timeline from `.events.json` + hardcoded `PRESET_DEFAULT`. Canonical copy: `C:\Users\james\Tools (1)\HighlightBuilder.cs` (has file picker, intro/outro title cards, title-generator OFX lookup). A drifted older copy lives in `HatmasBot\` with a newer preset paste but missing later features — will be deleted.
- `TuneFrame.cs` — captures pan/crop + composite mode from an open Vegas project, emits a pasteable C# `PRESET_*` block + a `.txt` archive. Both copies are byte-identical. **Video tracks only — does NOT capture audio.**

---

## 3. Target file layout

```
C:\Users\james\HatmasBot\
  tools\
    process_vods.py                 NEW — orchestrator
    extract_events.py               existing
    vod_detector.py                 existing
  vegas_scripts\
    HighlightBuilder.cs             MOVED + edited (JSON preset loader, no hardcoded block)
    FullGameplayBuilder.cs          NEW
    TuneFrame.cs                    MOVED + edited (audio volumes, JSON output, save-as picker)
    _preset_io.cs                   NEW shared JSON parser (see Risk #R-7 about sharing)
  vegas_presets\
    vertical_tiktok.tune.json       captured from a tuned vertical project
    horizontal_full.tune.json       captured from a tuned horizontal project
  config\
    vegas_pipeline.json             paths, render template names, vegas.exe location
  jobs\
    current.json                    Python writes, Vegas reads (per-step handoff)
  inbox\                            drop .mp4 here
  rendered\                         full-gameplay outputs
  highlight\                        shorts outputs
```

`C:\Users\james\Tools (1)\` — delete the two .cs files once the moved copies
are verified working.

---

## 4. Config shapes

### 4.1 `config/vegas_pipeline.json`

```json
{
  "vegas_exe": "C:\\Program Files\\VEGAS\\VEGAS Pro 21.0\\vegas210.exe",
  "inbox_dir":      "C:\\Users\\james\\HatmasBot\\inbox",
  "rendered_dir":   "C:\\Users\\james\\HatmasBot\\rendered",
  "highlight_dir":  "C:\\Users\\james\\HatmasBot\\highlight",
  "preset_dir":     "C:\\Users\\james\\HatmasBot\\vegas_presets",
  "jobs_dir":       "C:\\Users\\james\\HatmasBot\\jobs",
  "scripts_dir":    "C:\\Users\\james\\HatmasBot\\vegas_scripts",
  "render_templates": {
    "full_gameplay": "Youtube HD",
    "highlight":     "TikTok YouTube Short HD"
  },
  "presets": {
    "full_gameplay": "horizontal_full",
    "highlight":     "vertical_tiktok"
  },
  "auto_render": false
}
```

### 4.2 `vegas_presets/<name>.tune.json`

```json
{
  "schema_version": 1,
  "name": "vertical_tiktok",
  "kind": "highlight",
  "captured_at": "2026-04-24T18:00:00",
  "project": { "width": 1080, "height": 1920, "framerate": 60.0 },
  "video_tracks": [
    {
      "name": "Items Overlay",
      "composite": "SrcAlpha",
      "top_left":     {"x": 11.1021,  "y": 965.1342},
      "top_right":    {"x": 418.7823, "y": 965.1342},
      "bottom_right": {"x": 418.7823, "y": 1689.8989},
      "bottom_left":  {"x": 11.1021,  "y": 1689.8989},
      "center":       {"x": 214.9422, "y": 1327.5166},
      "rotation_rad": 0.0,
      "smoothness":   0.0,
      "keyframe_type": "Linear"
    }
  ],
  "audio_tracks": [
    { "name": "Gameplay",      "volume_db": 0.0 },
    { "name": "Voice",         "volume_db": -3.0 },
    { "name": "Music / Other", "volume_db": -9.0 },
    { "name": "Combined Mix",  "volume_db": -100.0 }
  ]
}
```

Notes:
- `schema_version` at the top lets future readers detect upgrades.
- Audio volumes stored as dB for readability. Vegas's `AudioTrack.Volume` is a linear scalar where 1.0 = 0dB; conversion: `linear = 10^(db/20)`, `db = 20*log10(linear)`. Vegas's UI caps at +12 dB (linear ≈ 3.98); preset clamps there.
- `-100.0` dB is our "muted" sentinel (linear 1e-5), since Vegas doesn't have a proper mute flag exposed on `AudioTrack` — muted tracks are just set to a very low gain. **Verify this during implementation; there may be `AudioTrack.Mute` I haven't looked up yet** (see R-4).

### 4.3 `jobs/current.json`

```json
{
  "kind": "full_gameplay",
  "source_video":   "C:\\...\\inbox\\game1.mp4",
  "events_json":    null,
  "preset_name":    "horizontal_full",
  "output_dir":     "C:\\...\\rendered",
  "render_template":"Youtube HD",
  "auto_render":    false
}
```

Highlight jobs set `kind: "highlight"`, `events_json` populated, preset = vertical, output_dir = highlight folder.

---

## 5. Work items

### 5.1 Vegas script changes

**TuneFrame.cs — edits (DONE)**
- [x] Walk `proj.Tracks` for `AudioTrack`s; capture `Volume` (dB) and `Mute` (reflection-verified as Boolean). Used instead of the `-100 dB` sentinel.
- [x] SaveFileDialog defaulting to `vegas_presets\`, `*.tune.json` filter, pre-selects newest existing preset, `.tune.json` extension auto-appended, Cancel aborts cleanly.
- [x] JSON output per schema §4.2 (`schema_version`, `name`, `kind`, `project`, `video_tracks`, `audio_tracks`).
- [x] `.txt` archive kept at `C:\Users\james\Videos\` with timestamped filename (diff history). Full run log also dumped to `vegas_presets\_tuneframe_last_run.txt` so dialog text can be copied from disk.
- [x] `kind` inferred from project dims: 1080x1920 → `highlight`, 1920x1080 → `full_gameplay`, else `unknown`.
- [x] **Added `scale_to_fill` capture** (per VideoMotion) — not in original plan but essential: controls whether cropped region stretches to fill output frame.
- [x] **Added per-event OFX effect capture** — not in original plan but required after discovering Pan/Crop Mask isn't in the Vegas SDK. Captures every OFX effect on the event: `plugin_id` (UniqueID), `plugin_name`, `bypass`, and a typed parameter list (Boolean / Double / Integer / Choice / Double2D / String / Custom). Used for Bézier Masking; generic enough for any OFX effect.

**HighlightBuilder.cs — edits (DONE)**
- [x] Deleted hardcoded `PRESET_DEFAULT` block + `ACTIVE_PRESET` constant. `ACTIVE_PRESET` is now a runtime-loaded static field.
- [x] `LoadPreset(string path) → FramePreset` reads `.tune.json` (using the existing hand-rolled regex-based JSON parser). `ParseVideoTracks` / `ParseAudioTracks` / `ParseEffects` / `ParseParameters` handle the nested structures. Composite mode and keyframe type parsed via `Enum.Parse<T>(...ignoreCase:true)`.
- [x] `PRESET_FILE` const at top of file points at the default preset; ProcessVideo.cs will override via `jobs/current.json` (Step 9).
- [x] After audio tracks are created, applies `volume_db` → linear gain and sets `AudioTrack.Mute` from the preset.
- [x] `ScaleToFill` applied to each `VideoMotion` before setting keyframe bounds.
- [x] **OFX effects applied** per video track: `ApplyEffects` resolves plugin via `FindPluginByUniqueID(vegas.VideoFX, …)`, calls `vEvent.Effects.AddEffect(plugin)`, sets `Bypass`, then re-applies each captured parameter by name. Supports Boolean/Double/Integer/Choice/Double2D/String/Custom OFX parameter types.
- [x] Skips preset tracks named "Titles" (HighlightBuilder synthesizes its own Titles track for intro/outro cards — preset would create a duplicate).
- [x] Build warnings surfaced in the summary dialog (missing plugins, unmatched choices, audio-track count mismatches, etc.).
- [x] Intro/outro title cards unchanged.

**ProcessVideo.cs — NEW (Step 9, pending)** — replaces the two-script FullGameplay+Highlight design because CLI `-SCRIPT:` always spawns a new Vegas window. See revised architecture below.

**Shared code between scripts** — R-7 resolved. The JSON parser + preset-loading code is copy-pasted from HighlightBuilder.cs into ProcessVideo.cs (option 3).

### 5.2 Python orchestrator (`tools/process_vods.py`)

- [ ] Load `config/vegas_pipeline.json`.
- [ ] Scan `inbox_dir` for `*.mp4`. For each:
  1. If no `<name>.events.json`, call `extract_events.py` (import as a module or `subprocess`).
  2. Write `jobs/current.json` for **full gameplay**: source path, preset name (`config.presets.full_gameplay`), output dir (`rendered_dir`), render template (`config.render_templates.full_gameplay`), `auto_render: false`.
  3. `subprocess.Popen([vegas_exe, f"-SCRIPT:{scripts_dir}\\FullGameplayBuilder.cs"])` and `.wait()`.
  4. Write `jobs/current.json` for **highlight**: source path, events_json path, preset (`config.presets.highlight`), output dir (`highlight_dir`), template, `auto_render: false`.
  5. `subprocess.Popen([vegas_exe, f"-SCRIPT:{scripts_dir}\\HighlightBuilder.cs"])` and `.wait()`.
  6. Move the processed `.mp4` out of `inbox\` (to a `processed\` folder) so re-runs skip it.
- [ ] Flags: `--dry-run`, `--include <glob>`, `--skip-full`, `--skip-highlight` (run just one phase), `--keep` (don't move processed files).
- [ ] Stdout progress: `[1/3] game1.mp4 → events.json ... done (12 events)`, then `[1/3] game1.mp4 → full gameplay: opening Vegas ...`, `[1/3] game1.mp4 → full gameplay: Vegas closed (8m 41s). Continuing ...`, etc.
- [ ] Ctrl+C cleanup: kill Vegas child process if still alive.

---

## 6. Known risks / open problems

**R-1. Waiting on Vegas exit (RESOLVED with revised architecture).**
Verified 2026-04-24: `-SCRIPT:` **always spawns a new Vegas window** — even when an instance is already open. The old two-script-per-phase design would have left orphaned Vegas windows. **Fix:** Use one mega-script (`ProcessVideo.cs`) per video that handles both phases in a single Vegas instance via a poll loop on `jobs/go.flag`. Orchestrator preflight still refuses to start if Vegas is already running, to avoid multi-instance confusion.

**R-2. `-SCRIPT:` command-line behaviour in Vegas 21 (RESOLVED).**
Verified 2026-04-24 via `HelloScript.cs`: `"vegas210.exe" -SCRIPT:"absolute_path.cs"` works as documented. No flag syntax changes in Vegas 21.

**R-3. No argument passing to Vegas scripts (STILL OPEN — mitigate in Step 9).**
Mitigation in plan: ProcessVideo.cs reads `jobs/current.json`, requires mtime within last 60 seconds, falls back to a dialog if missing.

**R-4. Audio mute semantics (RESOLVED).**
Verified 2026-04-24 via reflection: `AudioTrack.Mute` exists on Vegas 21 as a `Boolean` read/write property. Captured and applied cleanly — no linear-gain sentinel needed.

**R-5. TuneFrame needs audio tracks to exist before it captures them.**
Decision locked in: James drags a raw `.mp4` into an empty 1920x1080 Vegas project the first time for the horizontal preset. No bootstrap script. This happens in Step 8.

**R-6. `events.json` timestamps are against the original `.mp4`.**
Unchanged — James confirmed the highlight pass consumes the original source. Document in process_vods.py help text.

**R-7. Sharing code between Vegas scripts (RESOLVED).**
Picked option 3 (copy-paste). HighlightBuilder.cs has the complete preset parser + apply code; ProcessVideo.cs will copy it over when written in Step 9.

**R-8. Preset schema drift (RESOLVED).**
Implemented: `Enum.Parse<CompositeMode>(...)` and `Enum.Parse<VideoKeyframeType>(...)` with `ignoreCase: true`. Unknown values throw a descriptive exception from the outer try/catch in `FromVegas()`.

**R-9. TuneFrame overwrite UX (RESOLVED).**
Implemented: `DefaultExt = "tune.json"`, `AddExtension = true`, plus a post-dialog normalization that appends `.tune.json` if the user typed a name without extension or with a lone `.json`.

**R-10. Project moving between machines.**
Unchanged — preset uses track names, falls back to index via the empty-name check.

**R-11. Full-gameplay project might not need multiple video tracks.**
Unchanged — ProcessVideo.cs's Phase A handles N tracks with single events spanning source duration.

**R-12. Audio track count mismatch between preset capture and target file (IMPLEMENTED).**
HighlightBuilder applies `min(preset_count, source_count)` and warns in the final dialog on mismatch. ProcessVideo.cs will do the same.

**R-13. OBS HEVC source files.** Unchanged — Vegas Pro 21 reads HEVC natively.

**R-14. `inbox\processed\` cleanup.** Unchanged — plan has `--keep` flag, manual drag-back option.

**R-15. Intro/outro text is hardcoded in HighlightBuilder.** Still deferred (Step 13).

**R-16. Pan/Crop Mask is NOT in Vegas 21's scripting SDK (NEW, WORKED AROUND).**
Major discovery during Step 7. Reflection over `VideoEvent`, `VideoMotion`, `VideoMotionKeyframe`, and the entire `ScriptPortal.Vegas.dll` assembly found zero type or property with "mask" in its name. No `ScriptPortal.VegasUI` side-assembly either. The Mask checkbox in the Pan/Crop dialog is a UI-only feature — data lives in the `.veg` XML but there's no scripting API for it. **Workaround:** Use **Bézier Masking OFX** instead. It's a real OFX Effect (accessed via `VideoEvent.Effects`), so it's fully scriptable via `Effect.OFXEffect.Parameters`. TuneFrame captures and HighlightBuilder re-applies all 151 parameters (across up to 8 mask slots). Verified working with rectangular mask on Items Overlay.

**R-17. OFXDouble2D exposes X/Y as public FIELDS, not properties (NEW, RESOLVED).**
Discovered during OFX parameter capture. `OFXDouble2D` has no `X`/`Y` properties — just two public `Double` fields named `X` and `Y`. Generic reflection using `GetProperty` missed them and returned null. **Fix:** both TuneFrame capture (`TryGetXY`) and HighlightBuilder apply (`TrySetXY`) check fields first via `FieldInfo.SetValue`. The apply side also uses boxed-object reflection on the `Value` property (via `Activator.CreateInstance`) so it works whether `OFXDouble2D` turns out to be a class or a struct.

**R-18. Vegas `const bool` causes "unreachable expression code" compile error (NEW, RESOLVED).**
Vegas's strict C# compiler folds `const bool` values used in ternaries and flags the other branch as unreachable (error 0x80131600). Use `static readonly bool` instead — same "set once" semantics but not compile-time-folded. Affects any diagnostic/feature toggles.

**R-19. TuneFrame's `File.AppendAllText` to the log file gets wiped (NEW, RESOLVED).**
Writing diagnostic info from deep inside capture via `File.AppendAllText(LAST_RUN_LOG, …)` was overwritten by the final `File.WriteAllText` in `Run()`. **Fix:** collect diagnostic text into a static `StringBuilder` and include it in the summary that gets passed to the final `WriteAllText`.

**R-20. OFX effect parameter counts can be large.**
Bézier Masking has 151 parameters across 8 mask slots (many for unused slots at defaults). Current behaviour: capture every value-bearing parameter (skip Group / PushButton), apply all on load. JSON grows but correctness > size. Optimisation (skip defaults / disabled masks) deferred unless size becomes a problem.

---

## 7. Order of work

1. [x] **Sanity-check `-SCRIPT:` on Vegas 21** via `HelloScript.cs`. CLI invocation works; always spawns a new Vegas window.
2. [x] **Create folders:** `vegas_scripts\`, `vegas_presets\`, `config\`, `jobs\`, `inbox\`, `rendered\`, `highlight\`, `inbox\processed\`.
3. [x] **Move scripts** — `Tools (1)\HighlightBuilder.cs` (advanced version) copied into `vegas_scripts\`, `TuneFrame.cs` moved from HatmasBot root. Drifted `HatmasBot\HighlightBuilder.cs` deleted.
4. [x] **Write `config/vegas_pipeline.json`** with paths, render templates, preset names, `auto_render: true`.
5. [x] **Extend TuneFrame.cs** — audio capture + Mute (verified via reflection), SaveFileDialog with `.tune.json` filter, JSON output, `scale_to_fill` capture, per-event OFX effect capture (all parameter types).
6. [x] **Capture `vertical_tiktok.tune.json`** from tuned vertical project. Includes 2 video tracks (Items Overlay + Gameplay Video), 4 audio tracks, and Bézier Masking OFX on Items Overlay (151 params captured including Location_N as `{x, y}`).
7. [x] **Refactor HighlightBuilder.cs** — `PRESET_FILE` constant points at the default `.tune.json`, `LoadPreset` parses the JSON, audio volumes + mute applied, `scale_to_fill` applied, OFX effects re-added via `vegas.VideoFX` plugin lookup, Titles track filtered out of preset (synthesized separately). Tested end-to-end with Bézier Masking location preserved.
8. [ ] **Capture `horizontal_full.tune.json`** — James drags a raw `.mp4` into an empty Vegas project (1920x1080), adjusts audio faders + optional Pan/Crop, runs TuneFrame. (R-5)
9. [ ] **Write ProcessVideo.cs** (mega-script replacing the original two-builder design; see Architecture section below for the poll-loop flow). Handles both phases in one Vegas instance. Copy-pastes the preset loader from HighlightBuilder (R-7 decision).
10. [ ] **Write `tools/process_vods.py`** — preflight (refuse to start if Vegas is running), iterate `inbox/*.mp4`, write `jobs/current.json`, launch ProcessVideo.cs, coordinate keypress-to-render flow.
11. [ ] **End-to-end dry run** with one `.mp4`.
12. [ ] **Batch test** with 3 `.mp4`s.
13. [ ] (Deferred) Lift intro/outro text to config (R-15).
14. [ ] (Deferred) Audio normalization via ffmpeg `volumedetect` (Step 12b — only if post-dry-run shows level drift between recordings).

---

## 7b. Architecture change — ProcessVideo.cs mega-script (replaces FullGameplayBuilder + HighlightBuilder for orchestrated runs)

Originally the plan was four scripts per video (full-gameplay build + render + highlight build + render), each launched separately. Step 1 verified that CLI `-SCRIPT:` always spawns a **new** Vegas window, so separate launches would leave the built timeline behind and the second launch would render a fresh empty project. Replaced with a single script per video:

**ProcessVideo.cs flow:**
1. Read `jobs/current.json`: source video, events.json path, both preset names, both render templates, both output dirs.
2. **Phase A — full gameplay:** `NewProject`, dims from horizontal preset, one video event per preset track spanning full source, audio events per source stream, apply preset volumes + mute + scale_to_fill + OFX effects. No intro/outro.
3. Poll for `jobs/go.flag` (with `Application.DoEvents()` + `Thread.Sleep(200)`). Orchestrator writes the flag when James presses Enter in CMD. On detection, consume the flag.
4. Look up render template by name from `myVegas.Renderers`, render to `rendered/<basename>.mp4`.
5. **Phase B — highlight:** `NewProject`, dims from vertical preset, events laid down per `.events.json`, Pan/Crop per preset, audio per stream, intro/outro title cards (reuse FindTitleGenerator/AddTitleEvent helpers).
6. Poll again. Render to `highlight/<basename>_highlight.mp4` using `highlight_render_template`.
7. Write `jobs/phase_done.flag`; exit. Orchestrator detects process exit or the flag and moves to the next video.

**Error handling:** any exception → write `jobs/error.flag` with message+stack, pop MessageBox, exit. Python picks up the error on next iteration.

---

## 8. Definition of done for v1

- [ ] Drop three `.mp4`s into `inbox\`.
- [ ] Run `python tools\process_vods.py`.
- [ ] For each video: Vegas opens once with ProcessVideo.cs, builds Phase A, waits for keypress, renders, builds Phase B, waits for keypress, renders, exits.
- [ ] No manual editing of C# files or preset JSONs between runs.
- [ ] Presets captured via TuneFrame's save-as dialog, zero code edits needed when tuning is re-done.
- [ ] Bézier Masking OFX on Items Overlay preserved across every highlight output.
