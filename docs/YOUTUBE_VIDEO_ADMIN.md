# YouTube video admin (`/admin/videos`)

Broadcaster-only page for deciding which god a YouTube upload pays out.
Every commenter on a video gets `YOUTUBE_FREE_SHARE_COUNT` shares of that
video's god (plugins/youtube_rewards.py). Until now the only way to tag a
video the title parser couldn't read ("Full Gameplay: X vs Y") was the
`tools/mark_youtube_video.py` CLI. This page replaces that for day-to-day
use and adds the retroactive grant: the moment a video is tagged, its
existing comments are walked and shares are paid out.

## What the page shows

Three tabs, one table:

| Tab | Rows | Row actions |
|---|---|---|
| **Uncategorized** (default) | uploads with no god and not skipped | god picker (pre-filled with a suggestion when a god name appears anywhere in the title), **Save & grant**, **Skip** |
| **Categorized** | uploads with a god | god + who set it (`auto` / `manual` / `web:<login>`), grant count, **Rescan**, **Change** (only while the video has zero grants) |
| **Skipped** | uploads marked "no share" (shorts, tier lists, montages) | **Unskip** (back to Uncategorized) |

Every row: thumbnail (`i.ytimg.com`), title linking to the video, publish
date. Toolbar: **Refresh from YouTube** (walks the whole uploads playlist,
not just the scanner's 250-video window), a title search box, and the
count per tab.

## Decisions

- **Same gate as `/admin/events`**: broadcaster's Twitch login only,
  everyone else gets the styled 404. Origin + IP rate-limit guards on
  every write, audit log at `data/youtube_videos_audit.log`.
- **Tagging pays out immediately.** Save calls the plugin's comment scan
  for that one video and reports "granted N". Nothing is different about
  those grants from the ones the hourly scanner makes; the same
  `youtube_processed_comments` dedup applies, so a rescan never
  double-pays.
- **Skipped is a real state.** Without it every non-gameplay upload would
  sit in Uncategorized forever. A skipped video is never auto-tagged and
  never scanned. Unskipping puts it back in the queue; it is not
  auto-tagged again unless the scanner sees it later.
- **Changing an already-paid video is blocked for now.** James said wrong
  tags are rare and reassignment is a later problem. The button stays
  disabled with a tooltip once a video has grants. Untouched videos (zero
  grants, typically fresh auto-tags) can be changed freely.
- **Uploads are persisted.** New table `youtube_videos` (id, title,
  published_at, first/last seen, skipped). The scanner upserts every
  upload it walks, so the page works from the DB and costs zero quota to
  open. "Refresh from YouTube" is the only on-demand API walk.
- **God list = live roster** (`core/god_roster.names()`) unioned with the
  icon library the scanner already uses, so a brand-new god can be picked
  the day it ships. Picker is a searchable datalist, not a 130-entry
  `<select>`.
- **Plugin off = read-only page.** If `youtube_rewards` is disabled or
  has no API key, the list still renders but Save/Rescan/Refresh return a
  clear error instead of silently doing nothing.

## Pieces

| File | Role |
|---|---|
| `core/youtube_schema.py` | `youtube_videos` table |
| `core/youtube_videos.py` | DB store: upsert/list/set god/skip/grant counts, suggestion helper. No I/O beyond SQLite, fully testable |
| `plugins/youtube_rewards.py` | upserts uploads during scans, honours skipped, exposes `refresh_videos()` and `scan_video()` for the page |
| `core/youtube_admin_web.py` | routes, registered from `PublicWebServer` like `BingoWeb` |
| `public/videos_admin.html` | the page |
| `tests/test_youtube_videos.py` | store + route tests with a fake plugin |

## API

All JSON, `Cache-Control: no-store`, 404 unless the session is the broadcaster.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/admin/videos` | | `{ok, user, plugin_ready, counts:{uncategorized,categorized,skipped}, videos:[…]}` |
| GET | `/api/admin/videos/gods` | | `{ok, gods:[{name, icon}]}` |
| POST | `/api/admin/videos/{id}/god` | `{god}` | `{ok, god, granted, action:"tagged"\|"changed"}` — 409 if the video already has grants |
| POST | `/api/admin/videos/{id}/skip` | | `{ok}` |
| POST | `/api/admin/videos/{id}/unskip` | | `{ok}` |
| POST | `/api/admin/videos/{id}/rescan` | | `{ok, granted}` |
| POST | `/api/admin/videos/refresh` | | `{ok, seen, added, auto_tagged}` |

Video object: `{video_id, title, published_at, thumb, url, status, god,
god_icon, set_by, set_at, grants, suggestion}` where `status` is
`uncategorized` / `categorized` / `skipped`.

## Not in this pass

- Reassigning a god on a video that already paid out (needs a clawback or
  a "top-up the right god" policy — James's call).
- Per-video share count overrides (everyone gets one share today).
- Bulk actions (select many rows, one god).
