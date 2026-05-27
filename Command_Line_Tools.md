# HatmasBot Command-Line Tools Reference

Comprehensive reference for every CLI tool, script, and `.bat` wrapper in the HatmasBot project. For high-level architecture and Twitch chat commands, see `HatmasBot.md`.

**Conventions:**

- All commands assume your working directory is the repo root: `C:\Users\james\HatmasBot`.
- `python` means whichever Python you've configured for the project (3.14 per HatmasBot.md). Substitute `py -3.14` if you have multiple installs.
- Square brackets `[--flag]` denote optional arguments; angle brackets `<value>` denote required values.
- Defaults shown in parentheses are the actual code defaults; nothing is invented.

---

## Table of Contents

1. [Entry Points](#entry-points)
2. [Authentication](#authentication)
3. [Asset Downloaders](#asset-downloaders)
4. [Video Processing Pipeline](#video-processing-pipeline)
5. [Thumbnail Tools](#thumbnail-tools)
6. [Economy Management](#economy-management)
7. [YouTube Integration](#youtube-integration)
8. [Diagnostics & Calibration](#diagnostics--calibration)
9. [Stream Deck Wrappers (.bat)](#stream-deck-wrappers-bat)
10. [Quick-Reference Summary](#quick-reference-summary)

---

## Entry Points

### `main.py`

Main entry point. Initializes the TokenManager, WebServer, Bot, and registers all plugins (Basic, Smite, SongRequest, OBS, GodRequest, ClaudeChat, Gamble, KillDeathDetector, VoiceLine, DeathCounter, Economy). Starts the bot and the public webserver.

**Run:**

```
python main.py
```

**Flags:** None — no argparse.

**Shutdown:** Type `quit`, `exit`, `stop`, or `close` in the console, or press `Ctrl+C`.

---

## Authentication

### `python -m core.auth`

Browser OAuth flow that generates and persists Twitch tokens. Run this once at install time; the bot auto-refreshes tokens after that.

**Run:**

```
python -m core.auth                  # Bot token (log in as HatmasBot)
python -m core.auth --broadcaster    # Broadcaster token (log in as Hatmaster)
```

**Flags:**

| Flag | Description |
|---|---|
| `--broadcaster` | Generate the broadcaster token instead of the bot token. Required when adding new broadcaster scopes (e.g. `channel:manage:redemptions`, `moderator:read:chatters`). |

**Output:** Tokens land in `data/twitch_token.json` and `data/twitch_broadcaster_token.json`. Both are auto-refreshed by `TokenManager` after they expire.

---

### `spotify_auth.py` *(archived)*

> **Archived to `archive/superseded/spotify_auth.py`.** The live bot's
> Spotify auth flow is auto-handled on first launch by code in
> `plugins/songrequest.py` (which talks directly to the Spotify API
> via `aiohttp`). The standalone tool is no longer used. Kept in the
> archive for reference if you ever need to manually re-do the flow
> outside the bot.

---

## Asset Downloaders

### `download_god_icons.py`

Downloads every Smite 2 god's small portrait icon (~256×256) for use by the in-game god portrait matcher (`core/god_matcher.py`). Source-of-truth is the saved `Gods - SMITE 2 Wiki.html` page in the repo root.

**Run:**

```
python download_god_icons.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--force` | off | Re-download even if icons already exist locally. |
| `--check` | off | List which gods are missing, don't download. |
| `--add <name>` | — | Download a single god by display name (e.g. `--add "Hou Yi"`). Useful for newly-released gods not yet in the saved wiki HTML. |

**Examples:**

```
python download_god_icons.py --check
python download_god_icons.py --add "Atlas"
python download_god_icons.py --force
```

**Output:** `data/god_icons/<slug>.png` (e.g. `ymir.png`, `hou-yi.png`).

---

### `tools/download_god_cards.py`

Downloads 400×600 god card / splash art from `wiki.smite2.com` for use by the thumbnail builder. Uses `curl_cffi` (Chrome TLS fingerprint) to bypass Cloudflare. Tries every observed wiki naming convention (`T_<Name>S2_Default.png`, `T_<Name>(S2)_Default.png`, `SkinArt_<Name>S2_Default.png`, `GodCard_<Name>.png`, `.jpg` variants, etc.).

**Run:**

```
python tools/download_god_cards.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--force` | off | Re-download everything. |
| `--check` | off | List missing cards without downloading. |
| `--add <name>` | — | Download a single god by display name. |
| `--only <names>` | — | Comma-separated subset (e.g. `"Ymir,Loki,Hou Yi"`). |
| `--throttle <sec>` | 0.4 | Seconds to sleep between HTTP requests. |
| `--use-og-scrape` | off | Enable HTML `og:image` fallback if direct URL patterns fail. |
| `-v`, `--verbose` | off | Print every URL tried with its HTTP status. |

**Examples:**

```
python tools/download_god_cards.py --only "Ra,Ymir,Hou Yi" -v
python tools/download_god_cards.py --add "Atlas"
python tools/download_god_cards.py --check
```

**Output:** `data/god_cards/<slug>.png` (JPEG responses are transcoded to PNG).

**Dependency:** `pip install curl_cffi` — required for Cloudflare bypass.

---

### `tools/download_voicelines.py`

Downloads all god voice lines (jokes, taunts, laughs, abilities, kills, deaths, etc.) from the Smite fandom wiki. Used by the VoiceLine plugin. `curl_cffi` again for Cloudflare bypass.

**Run:**

```
python tools/download_voicelines.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<gods>` (positional) | all | Specific god names to download (e.g. `achilles zeus`). |
| `--list` | off | List all available gods on the fandom wiki and exit. |
| `--resume` | off | Skip gods that have already been fully downloaded. |
| `--output <dir>` | `data/smite_voicelines` | Override output directory. |

**Examples:**

```
python tools/download_voicelines.py --list
python tools/download_voicelines.py achilles zeus
python tools/download_voicelines.py --resume
```

**Output:** `data/smite_voicelines/<god>/<category>/*.ogg` (e.g. `achilles/jokes/joke1.ogg`).

---

## Video Processing Pipeline

### `tools/extract_events.py`

Offline VOD scanner. Walks a folder of OBS recordings (1920×1080 60fps HEVC `.mp4`s) and emits sibling `<name>.events.json` files describing every kill / death / assist timestamp. Output is consumed by `vegas_scripts/HighlightBuilder.cs` (Sony Vegas script) to auto-cut highlight reels. Runs offline with no dependency on the live bot.

**Run:**

```
python tools/extract_events.py "C:\Users\james\Videos"
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<folder>` (positional, required) | — | Folder of `.mp4` recordings to scan. |
| `--include <types>` | kills only | Comma-separated extra event types: `deaths`, `assists`, or `all`. |
| `--overwrite` | off | Re-scan even if `.events.json` already exists. |
| `--dry-run` | off | List what would be scanned without writing anything. |
| `--enroll-templates` | off | Save confirmed digit crops back into `data/digit_templates/` to grow the matcher library. |
| `--coarse <sec>` | 5.0 | Coarse-scan interval. |
| `--precision <sec>` | 0.2 | Binary-search refinement precision for event timestamps. |
| `--no-refine` | off | Skip refinement (faster, wider clip windows). |
| `--no-merge-overlaps` | off | Disable kill+death event merging (keeps every event as a separate clip). |
| `--no-lobby-skip` | off | Disable pre-match lobby sparse-sampling optimization. |
| `--no-ffmpeg-crop` | off | Disable ffmpeg-side HUD-strip crop (debugging only). |
| `--hwaccel <value>` | software | NVIDIA / d3d11va / dxva2 / auto / none. CUDA gives ~2.6× speedup on HEVC. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg binary. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe binary. |
| `--tesseract <path>` | auto-detect | Path to `tesseract.exe` (optional fallback OCR). |
| `--data-dir <path>` | `data/` | HatmasBot data directory (holds `digit_templates/`). |
| `--workers <int>` | 1 | Concurrent video scans (3-4 is a good sweet spot; not compatible with `--enroll-templates`). |
| `--debug-misreads [<dir>]` | `data/vod_debug` | Save offending frames on KDA misreads. |
| `-v`, `--verbose` | off | Print per-sample detection progress. |

**Examples:**

```
python tools/extract_events.py "C:\Videos\stream_2026-04-30"
python tools/extract_events.py "C:\Videos" --include deaths,assists --hwaccel cuda
python tools/extract_events.py "C:\Videos" --workers 4 --no-refine
python tools/extract_events.py "C:\Videos" --overwrite --enroll-templates
```

**Output:** Sibling `<recording>.events.json` next to each `.mp4`. Top-level fields: `source_video`, `events[]`, `gods_seen[]`. Consumed by HighlightBuilder.cs.

---

### `tools/process_recordings.py`

End-of-stream orchestrator. Scans `recordings/` for unprocessed `.mp4`s, runs the detector, writes each `<name>.events.json`, and sorts the `.mp4` + JSON pair into `recordings/<God Name>/`, `recordings/mixed/`, or `recordings/unknown/` depending on which gods appeared. Renames to `<stem>-N.<ext>` using lowest-unused-integer per folder. Defaults are tuned for the daily Stream-Deck flow.

**Run:**

```
python tools/process_recordings.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--source <folder>` | `recordings/` | Folder of new recordings to process. |
| `--include <types>` | `deaths` | Event types to extract (kills always included). |
| `--hwaccel <value>` | `cuda` | ffmpeg hardware acceleration. Pass `none` to force software decode. |
| `--dry-run` | off | Scan and print routing decisions but don't move files. |
| `--enroll-templates` | off | Save confirmed digit crops during scan. |
| `--coarse <sec>` | 5.0 | Coarse-scan interval. |
| `--precision <sec>` | 0.2 | Refinement precision. |
| `--no-refine` | off | Skip refinement. |
| `--no-merge-overlaps` | off | Disable event merging. |
| `--no-lobby-skip` | off | Disable lobby sparse-sampling. |
| `--no-ffmpeg-crop` | off | Disable ffmpeg HUD-strip crop. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg binary. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe binary. |
| `--tesseract <path>` | auto-detect | Path to tesseract. |
| `--data-dir <path>` | `data/` | HatmasBot data directory. |
| `--debug-misreads [<dir>]` | — | Save offending frames on misreads. |
| `-v`, `--verbose` | off | Print progress. |

**Examples:**

```
python tools/process_recordings.py
python tools/process_recordings.py --dry-run
python tools/process_recordings.py --hwaccel none --include kills
```

**Output:** `recordings/<God>/<God>-N.mp4` + matching `.events.json`. Multi-god sessions land in `recordings/mixed/`. Recordings with no confirmed god land in `recordings/unknown/` for manual triage via `sort_unknowns.py`.

---

### `tools/sort_unknowns.py`

Interactive triage for `recordings/unknown/`. Suggests the most likely god from sample frames, captures a portrait reference, and moves the recording to its proper per-god subfolder. Use this when `process_recordings.py` couldn't confidently identify the god (short clips, demos, custom games).

**Run:**

```
python tools/sort_unknowns.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--source <folder>` | `recordings/unknown` | Folder of unknown recordings. |
| `--target-root <folder>` | `recordings` | Root where confirmed recordings move. |
| `--reference-dir <path>` | `Portrait_Source` | Where to save portrait reference crops. |
| `--samples <int>` | 6 | Frames to sample per recording. |
| `--hwaccel <value>` | `cuda` | ffmpeg hardware acceleration. |
| `--no-open` | off | Don't auto-open the preview folder. |
| `--data-dir <path>` | `data/` | HatmasBot data directory. |
| `--overlay-icons-dir <path>` | `Custom God Icons` | Custom OBS overlay icons. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe. |

**Interactive prompts** per recording: `[Y]es` accept suggestion, `[n]o`, `[O]ther god`, `[s]kip`, `[?]` show details, `[r]eopen folder`, `[q]uit`.

---

## Thumbnail Tools

### `tools/import_god_icons.py`

Auto-imports candidate images into `Custom God Icons/`. Drop images into `Custom_Icons_Inbox/` (any combination of `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`); the tool detects the god from the filename, smart-crops to 1:1 with a configurable vertical bias, resizes to 512×512, and saves as PNG with the naming convention used by `build_thumbnail.py` (primary `<God>.png`, then `<God>-1.png`, `<God>-2.png` for variants).

**Run:**

```
python tools/import_god_icons.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--inbox <dir>` | `Custom_Icons_Inbox/` | Folder of candidate images to process. |
| `--output <dir>` | `Custom God Icons/` | Where finished icons land. |
| `--size <px>` | `512` | Output square dimension. |
| `--crop-bias <top\|center\|bottom>` | `top` | Vertical anchor for the 1:1 crop on portrait inputs (heads-up by default). |
| `--variants` | off | If `<God>.png` already exists, save as the next free `<God>-N.png` instead of skipping. |
| `--force` | off | Overwrite the primary `<God>.png` if it exists. |
| `--keep-source` | off | Don't move processed files into `_processed/` subfolder. |
| `--dry-run` | off | Show what would happen without saving anything. |
| `--list-missing` | off | List gods without a primary icon in the output folder, then exit. |

**Filename matching is fuzzy.** All of these resolve to god `Hou Yi`:

```
Hou Yi.png    Hou_Yi.jpg    HouYi.gif    Hou Yi-2.png    HouYi-3.webp    hou_yi-skin.png
```

**Default behavior:** if `<God>.png` already exists in `Custom God Icons/`, the input file is **skipped** entirely (the "leave them alone" rule). Use `--variants` to opt into adding numbered variants for gods that already have a primary; use `--force` to replace the primary outright.

**Examples:**

```
python tools/import_god_icons.py --list-missing                # show which gods need icons
python tools/import_god_icons.py --dry-run                     # preview what would happen
python tools/import_god_icons.py                               # default: only fill missing primaries
python tools/import_god_icons.py --variants                    # also add variants to gods that have primaries
python tools/import_god_icons.py --crop-bias center            # center the crop (for portraits where head isn't at top)
python tools/import_god_icons.py --inbox staging/ --keep-source
```

**Output:** `Custom God Icons/<God>.png` (primary) or `<God>-N.png` (variants). Successfully processed source files move to `<inbox>/_processed/` so the inbox shows what's left to do at a glance. Files with names that can't be matched to a known god are left in place with `[??]` in the report.

---

### `tools/build_thumbnail.py`

Preset-driven Pillow compositor. Builds a 1280×720 YouTube thumbnail from a JSON preset + CLI inputs, then auto-launches Paint.NET on the result. Outputs flat composite PNG, per-layer transparent PNGs (for layered Paint.NET editing via drag-import), and optionally a layered PSD.

**Run:**

```
python tools/build_thumbnail.py --god <name> [options]
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--god <name>` | (required) | My god display name. Drives `{my_god}`, card art lookup, icon lookup. |
| `--vs <name>` | "" | Opposing god display name (1v1 preset). |
| `--preset <name>` | `1v1` | Preset name from `thumbnail_presets/`. |
| `--text <string>` | (auto: my god name) | Headline text above VS. Defaults to `{my_god}` when omitted. |
| `--no-text` | off | Disable the headline (overrides auto-fill default). |
| `--subtext <string>` | (auto: vs god name) | Sub-headline text below VS. Defaults to `{vs_god}` when omitted. |
| `--no-subtext` | off | Disable the subtext (overrides auto-fill default). |
| `--result <win\|loss>` | "" | Fills `{result}` as WIN/LOSS, else blank (badge layer skips). |
| `--kda <K/D/A>` | "" | KDA string for `{kda}` placeholder (e.g. `12/3/8`). Only used by `single` preset. |
| `--size <WxH>` | preset size | Override canvas size (e.g. `1920x1080`). |
| `--out <path>` | `thumbnails/<auto>.png` | Output PNG path. |
| `--no-open` | off | Skip auto-launch into Paint.NET. |
| `--no-random-icons` | off | Always use the primary `<God>.png` icon, even when Custom God Icons variants exist. |
| `--seed <int>` | — | Random seed for the icon variant picker (reproducible renders). |
| `--list` | off | List available presets and exit. |

**Auto-fill behavior:** if you don't pass `--text`, the headline auto-fills with the my-god name (e.g. `GANESHA`). If you don't pass `--subtext`, the subtext auto-fills with the opposing god name (e.g. `BACCHUS`). Pass `--no-text` / `--no-subtext` to disable a label entirely.

**Random icon variants:** when a god has multiple **numbered** icon files in `Custom God Icons/` — e.g. `Achilles.png` plus `Achilles-1.png`, `Achilles-2.png`, `Achilles-3.png` — the tool randomly picks one per render so each thumbnail looks fresh. Only files with a purely numeric suffix after the final `-` count as variants; legacy skin-named files like `<God>-Battleworn.png` are intentionally ignored. The console output shows how many variants are in the pool. Pass `--no-random-icons` to always use the primary `<God>.png`, or `--seed <int>` to make the random choice reproducible.

**Examples:**

```
# Defaults: god names auto-fill above and below VS
python tools/build_thumbnail.py --god Ganesha --vs Bacchus

# Custom headline, default subtext (= "Bacchus")
python tools/build_thumbnail.py --god Ganesha --vs Bacchus --text "Fear the Drunk Man"

# Both labels custom
python tools/build_thumbnail.py --god Ganesha --vs Bacchus --text "Pentakill" --subtext "Comeback"

# No labels at all
python tools/build_thumbnail.py --god Ymir --vs Loki --no-text --no-subtext

# Single-hero preset with KDA + result
python tools/build_thumbnail.py --god "Hou Yi" --preset single --text "Solo Lane Domination" --kda 18/2/4 --result win

# Misc
python tools/build_thumbnail.py --list
python tools/build_thumbnail.py --god Ymir --vs Loki --no-open
```

**Output:** `thumbnails/<stem>.png` (flat) + `thumbnails/<stem>_layers/` (per-layer PNGs) + optionally `thumbnails/<stem>.psd` (if `psd-tools` is installed).

**Optional dependency:** `pip install psd-tools` for single-file layered PSD output.

---

## Economy Management

### `tools/seed_economy.py`

Seeds the economy database with realistic price history simulated from tracker.gg profile aggregates. Run this once after the first install (or after `--force` reset). The bot auto-fills prices going forward via `settle_match()` after each game.

**Run:**

```
python tools/seed_economy.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<gods>` (positional) | all from profile | Specific gods to seed (e.g. `Ymir Geb Sylvanus`). |
| `--force` | off | Re-seed even if a god already has price history. |

**Examples:**

```
python tools/seed_economy.py
python tools/seed_economy.py Ymir Geb Sylvanus
python tools/seed_economy.py --force
```

**Output:** Rows in `data/economy.db` (`god_prices`, `price_history` tables).

---

### `tools/replay_economy.py`

Wipes `god_prices` + `price_history` + `processed_matches` and re-fetches tracker.gg profile aggregates across every gamemode in `SMITE2_GAMEMODES_TO_TRACK`, then recomputes every god's price via the fair-value formula. Backs up `economy.db` first. Portfolios are preserved.

**Run:**

```
python tools/replay_economy.py --dry-run
python tools/replay_economy.py --yes
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--yes` | off | Skip the "type reset to confirm" prompt. |
| `--dry-run` | off | List what would be replayed; don't write. |
| `--max <N>` | 2000 | Maximum number of matches to replay. |

**Examples:**

```
python tools/replay_economy.py --dry-run
python tools/replay_economy.py --yes
python tools/replay_economy.py --max 500
```

---

### `tools/purge_excluded.py`

Permanently deletes rows from `portfolios` and `transactions` for any account in `ECONOMY_EXCLUDED_USERNAMES` (StreamElements, Nightbot, Moobot, Fossabot, Pretzelrocks, Soundalerts, Wizebot, plus the bot's own `TWITCH_BOT_USERNAME`). Backs up `economy.db` first.

**Run:**

```
python tools/purge_excluded.py --dry-run
python tools/purge_excluded.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Preview only; don't delete. |
| `--yes` | off | Skip the "type purge to confirm" prompt. |

---

## YouTube Integration

### `tools/mark_youtube_video.py`

Manage the `youtube_video_gods` table. Maps each YouTube video ID to a Smite god so the YouTube share-rewards plugin knows which god to grant when someone comments. Supports manual tagging, auto-scan from "Full Gameplay: X vs Y" titles, listing untagged videos, and triggering one comment-scan pass.

**Run:**

```
python tools/mark_youtube_video.py <command>
```

**Subcommand: `set <video_id> <god>`** — manually tag a specific video.

**Top-level flags:**

| Flag | Description |
|---|---|
| `set <video_id> <god>` | Manually tag a video (e.g. `set abc123 Ymir`). |
| `--auto-scan` | Fetch your latest YouTube uploads and auto-fill the table from titles. |
| `--overwrite` | (with `--auto-scan`) Overwrite existing entries. Manual tags (`set_by='manual'`) are protected. |
| `--list-untagged` | Show videos awaiting a god mapping. |
| `--list-tagged` | Dump everything currently tagged. |
| `--scan-comments` | Run one YouTube comment scan + share-grant pass. |
| `--stats` | Diagnostic stats dump. |

**Examples:**

```
python tools/mark_youtube_video.py set abc123 Ymir
python tools/mark_youtube_video.py --auto-scan
python tools/mark_youtube_video.py --auto-scan --overwrite
python tools/mark_youtube_video.py --list-untagged
python tools/mark_youtube_video.py --scan-comments
python tools/mark_youtube_video.py --stats
```

---

## Diagnostics & Calibration

### `capture_frames.py` *(archived)*

> **Archived to `archive/superseded/capture_frames.py`.** Superseded by
> `tools/obs_screenshot.py` (single-frame) and the kill detector's own
> `--debug` flag (which saves frames during real gameplay to
> `data/killdetect_debug/`). Kept in the archive in case you ever
> want a continuous capture loop again.

---

### `tools/obs_screenshot.py`

Captures a single OBS screenshot from the "Smite 2" source for KDA region calibration. Saves the full frame + a KDA crop so you can visually verify the crop coordinates in `core/kda_reader.py`.

**Run:**

```
python tools/obs_screenshot.py
```

**Flags:** None.

**Output:** `data/obs_screenshot.png` and `data/obs_screenshot_kda_crop.png`.

---

### `tools/check_kda_region.py`

Pull one frame from a recording and draw the detector's region boxes (KDA = red, HUD variance = yellow, gameplay check = green) so you can confirm alignment without re-running a full scan.

**Run:**

```
python tools/check_kda_region.py <video.mp4>
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<video>` (positional, required) | — | Path to `.mp4` file. |
| `--timestamp <sec>` | 30% of duration | Frame to extract. |
| `--region-offset <dx,dy>` | `0,0` | Shift HUD/gameplay regions (not KDA) by this offset. |
| `--kda-region <x1,y1,x2,y2>` | from config | Override KDA crop region. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe. |

**Output:** `<video>_kda_check.png` next to the input file with region boxes drawn.

---

### `tools/capture_god_reference.py`

Pull a clean god-portrait crop from a recording to create a custom reference icon for the portrait matcher. Useful when a god's default wiki icon doesn't match well in-game (e.g. heavy palette differences).

**Run:**

```
python tools/capture_god_reference.py <video.mp4>
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<video>` (positional, required) | — | Path to `.mp4`. |
| `--god <name>` | auto-detect | Force a specific god identification (skip auto-detect). |
| `--samples <int>` | 3 | Number of sample frames to analyze. |
| `--start-pct <float>` | 0.15 | First sample at this fraction of duration. |
| `--end-pct <float>` | 0.85 | Last sample at this fraction of duration. |
| `--output-dir <path>` | `Portrait_Source/` | Where to save the captured crop. |
| `--overwrite` | off | Replace an existing `Custom God Icons/<God>.png`. |
| `--hwaccel <value>` | software | ffmpeg hardware acceleration. |
| `--data-dir <path>` | `data/` | HatmasBot data directory. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe. |

**Examples:**

```
python tools/capture_god_reference.py "recordings/Vulcan/Vulcan-1.mp4"
python tools/capture_god_reference.py "recording.mp4" --god Vulcan --hwaccel cuda
```

---

### `tools/diagnose_god_detection.py`

Sample frames from a recording and print a per-frame report: gameplay check pass/fail, overlay check pass/fail, and the top-3 god candidates with their HSV-correlation scores. The fastest way to debug "why isn't this god being detected?"

**Run:**

```
python tools/diagnose_god_detection.py <video.mp4>
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `<video>` (positional, required) | — | Path to `.mp4`. |
| `--samples <int>` | 6 | Number of frames to sample. |
| `--start-pct <float>` | 0.15 | First sample fraction. |
| `--end-pct <float>` | 0.85 | Last sample fraction. |
| `--output-dir <path>` | `<video>_diag/` | Where to dump portrait crops + frames. |
| `--data-dir <path>` | `data/` | HatmasBot data directory. |
| `--overlay-icons-dir <path>` | `Custom God Icons` | Custom OBS overlay icons. |
| `--reference-icons-dir <path>` | `Portrait_Source` | Capture reference icons. |
| `--ffmpeg <path>` | `ffmpeg` | Path to ffmpeg. |
| `--ffprobe <path>` | `ffprobe` | Path to ffprobe. |
| `--tesseract <path>` | auto-detect | Path to tesseract. |
| `--hwaccel <value>` | software | ffmpeg hardware acceleration. |

**Examples:**

```
python tools/diagnose_god_detection.py "path/to/video.mp4"
python tools/diagnose_god_detection.py "path/to/video.mp4" --samples 8 --hwaccel cuda
```

---

## Diagnostics & Calibration (continued)

### `tools/check_stream_ready.py`

Pre-stream readiness check. Runs ~12 concurrent end-to-end checks against everything HatmasBot needs in order to stream cleanly: bot dashboard, both Twitch tokens (validated against Twitch + scope-checked), OBS WebSocket + game source, MixItUp API, tracker.gg, public website (localhost:8070 + hatmaster.tv via Cloudflare), cloudflared service state, disk space, asset library integrity, Spotify token, and the SMITE 2 process. Each check exercises the real interface, not just process existence — so a green light is real-world meaningful.

**Run:**

```
python tools/check_stream_ready.py
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--json` | off | Machine-readable JSON output (no colors, no formatting). Useful for piping into other tools or Stream Deck companion buttons. |
| `--quick` | off | Skip slower checks (external HTTP to tracker.gg, hatmaster.tv, Spotify). Cuts runtime from ~3s to ~150ms. |

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | All checks OK or only WARNs — safe to stream |
| 1 | At least one FAIL — fix the listed hints before going live |
| 2 | Script itself errored (config missing, etc.) |

**What each check actually exercises** (transitively validates several dependencies per check):

| Check | What gets validated |
|---|---|
| Bot dashboard | Bot process + dashboard webserver + plugin manager |
| Twitch tokens (×2) | Token freshness + required scopes + network to Twitch |
| OBS WebSocket + Smite 2 source | OBS running + plugin enabled + correct port + game source named correctly |
| MixItUp API | MixItUp open + Developer API enabled on 8911 |
| tracker.gg | Cloudflare bypass working + your profile reachable |
| Public webserver (local) | Bot's PublicWebServer is up on 8070 |
| hatmaster.tv | Internet + DNS + Cloudflare + cloudflared + public webserver (one HTTP request validates all five) |
| cloudflared service | Windows service installed and running |
| Disk space | At least 30 GB free in `recordings/` (warn) / 10 GB (fail) |
| God asset library | All wiki gods have icons + cards on disk |
| Spotify token | Token still valid, Spotify reachable |
| SMITE 2 process | Game launched (warning only, not blocking) |

**Examples:**

```
python tools/check_stream_ready.py             # full check, ~400ms
python tools/check_stream_ready.py --quick     # skip external HTTP, ~150ms
python tools/check_stream_ready.py --json      # JSON output for automation
```

For a one-press Stream Deck workflow, point a button at `check_stream.bat` instead.

---

## YouTube Integration (continued)

### `tools/youtube_live_badge.py`

Slap a "🔴 LIVE NOW" badge onto your last 8 YouTube video thumbnails when you go live, and revert them when you go offline. Pure passive viewer-funnel — every old-video visitor who lands on your channel during a stream sees red badges and has a one-click jump to live.

**Architecture:** download-on-first-encounter. The first time we badge a video, we download its current YouTube thumbnail and cache it locally at `data/youtube_thumbnails/<video_id>.png` (the canonical original we revert to later). Subsequent stream cycles never re-download — they only paint the badge and upload. Survives manual edits in YouTube Studio (we always pull the *current* thumbnail when caching).

**Subcommands:**

| Command | Purpose |
|---|---|
| `apply` | Stream-start. Badge the last N videos. |
| `revert` | Stream-end. Restore originals via cached PNGs. |
| `status` | Show which videos are currently badged. |
| `auth` | Run the one-time OAuth browser flow. |

**Run:**

```
python tools/youtube_live_badge.py apply
python tools/youtube_live_badge.py revert
python tools/youtube_live_badge.py status
python tools/youtube_live_badge.py auth        # first-time only
```

**Apply flags:**

| Flag | Default | Description |
|---|---|---|
| `--count <N>` | 8 | How many recent videos to badge. |
| `--text <string>` | `LIVE NOW` | Badge text. |
| `--corner <pos>` | `top_right` | `top_right`, `top_left`, `bottom_right`, or `bottom_left`. |

**Quota cost per stream:**

```
Apply (start):  100 (search.list) + 8 × 50 (thumbnails.set) = 500 units
Revert (end):                       8 × 50                   = 400 units
                                                              ─────────
                                                              900 units / day = ~9% of 10K daily budget
```

The cached thumbnail downloads from the YouTube CDN cost zero quota — they're plain HTTP. So the cost above is the steady-state per stream, regardless of whether videos are being badged for the first time or the hundredth.

**First-time setup:**

1. Google Cloud Console → enable **YouTube Data API v3** → create OAuth 2.0 Client ID (Desktop app type) → download the JSON.
2. Save it to `data/youtube_client_secrets.json`.
3. Install dependencies: `pip install google-auth-oauthlib google-api-python-client`.
4. Run `python tools/youtube_live_badge.py auth` — opens browser, grant "manage YouTube account" permission, refresh token saves to `data/youtube_oauth.json`.
5. From now on, `apply` and `revert` are headless.

**Examples:**

```
python tools/youtube_live_badge.py apply                              # default 8 videos
python tools/youtube_live_badge.py apply --count 5                    # smaller scope
python tools/youtube_live_badge.py apply --text "LIVE!" --corner top_left
python tools/youtube_live_badge.py revert                             # restore everything
python tools/youtube_live_badge.py status                             # what's currently badged?
```

**State tracking:** `data/live_badge_state.json` records which video IDs got badged during the current stream so revert knows exactly which videos to touch. If the bot crashes mid-stream and the state file is intact, `revert` still works perfectly.

**Failure recovery:** if upload fails for any video during `revert`, the state file is NOT cleared — re-run `revert` and only the failed videos are retried.

---

## Stream Deck Wrappers (.bat)

These are at the repo root, not in `tools/`. Designed to be one-click via Stream Deck "System: Open" buttons.

### `go_live.bat` / `go_offline.bat`

Stream Deck-friendly wrappers around `tools/youtube_live_badge.py`. One-press apply / revert.

**Pair them on your Stream Deck:**

- `go_live.bat` — press when starting your stream. Badges the last 8 videos.
- `go_offline.bat` — press when ending your stream. Restores originals.

Both pause briefly after running so any errors stay readable. If `apply` succeeded, `revert` later restores cleanly. If `apply` errored mid-flight (e.g. network hiccup partway through 8 videos), the state file still records what got through, so `revert` correctly handles partial state.

---

### `process_recordings.bat`

Wraps `tools/process_recordings.py`. `pushd`s to the repo root, runs the orchestrator, tees stdout + stderr to `data/process_recordings.log`, and pauses 5 seconds at the end so the summary stays visible.

**Run:** Drag-and-drop into a Stream Deck "System: Open" button. Press once at end of stream for hands-free clip sorting + JSON emission.

---

### `build_thumbnail.bat`

Wraps `tools/build_thumbnail.py` interactively. Prompts for preset / god / vs / headline / subtext / result / KDA via `set /p`, builds the python command, runs it, and pauses at the end. Blank inputs skip optional flags so you can leave anything off.

**Run:** Drag-and-drop into a Stream Deck "System: Open" button. Press at end of stream and answer the prompts; thumbnail auto-opens in Paint.NET.

**Defaults:** preset=1v1, --vs only asked if preset=1v1, blank inputs skip optional flags.

---

## Quick-Reference Summary

| Tool | Purpose | One-liner |
|---|---|---|
| `main.py` | Start the bot | `python main.py` |
| `python -m core.auth` | Twitch bot OAuth | `python -m core.auth` |
| `python -m core.auth --broadcaster` | Twitch broadcaster OAuth | `python -m core.auth --broadcaster` |
| `download_god_icons.py` | Download god portrait icons | `python download_god_icons.py` |
| `tools/download_god_cards.py` | Download god card art (thumbnails) | `python tools/download_god_cards.py` |
| `tools/download_voicelines.py` | Download god voice lines | `python tools/download_voicelines.py` |
| `tools/extract_events.py` | Scan VODs → events.json | `python tools/extract_events.py "C:\Videos"` |
| `tools/process_recordings.py` | End-of-stream sort + scan | `python tools/process_recordings.py` |
| `tools/sort_unknowns.py` | Interactive triage of unknown clips | `python tools/sort_unknowns.py` |
| `tools/import_god_icons.py` | Auto-crop/resize candidate images into Custom God Icons/ | `python tools/import_god_icons.py` |
| `tools/build_thumbnail.py` | Build YouTube thumbnail | `python tools/build_thumbnail.py --god Ymir --vs Loki --text "Pentakill"` |
| `tools/seed_economy.py` | Seed economy DB | `python tools/seed_economy.py` |
| `tools/replay_economy.py` | Rebuild prices from tracker.gg | `python tools/replay_economy.py --dry-run` |
| `tools/purge_excluded.py` | Wipe bot accounts from economy | `python tools/purge_excluded.py --dry-run` |
| `tools/mark_youtube_video.py` | YouTube video ↔ god mapping | `python tools/mark_youtube_video.py --auto-scan` |
| `tools/youtube_live_badge.py` | Apply/revert LIVE NOW badge on recent thumbnails | `python tools/youtube_live_badge.py apply` |
| `tools/obs_screenshot.py` | Single OBS screenshot for calibration | `python tools/obs_screenshot.py` |
| `tools/check_kda_region.py` | Verify KDA crop on a recording | `python tools/check_kda_region.py video.mp4` |
| `tools/capture_god_reference.py` | Capture custom portrait reference | `python tools/capture_god_reference.py video.mp4` |
| `tools/diagnose_god_detection.py` | Debug portrait matcher | `python tools/diagnose_god_detection.py video.mp4` |
| `tools/check_stream_ready.py` | Pre-stream readiness check | `python tools/check_stream_ready.py` |
| `process_recordings.bat` | Stream Deck: process recordings | (drag onto Stream Deck button) |
| `build_thumbnail.bat` | Stream Deck: build thumbnail | (drag onto Stream Deck button) |
| `check_stream.bat` | Stream Deck: pre-stream readiness check | (drag onto Stream Deck button) |
| `go_live.bat` | Stream Deck: apply LIVE badges | (drag onto Stream Deck button) |
| `go_offline.bat` | Stream Deck: revert LIVE badges | (drag onto Stream Deck button) |

---

## See Also

- **`HatmasBot.md`** — full architecture, plugin docs, Twitch chat commands, web server endpoints, EventSub subscriptions, and config reference.
- **`SonyVegasTODO.md`** — Sony Vegas Pro automation pipeline (`vegas_scripts/HighlightBuilder.cs`, `TuneFrame.cs`, `ProcessVideo.cs`).
- **`README.md`** — project overview and first-time setup.
