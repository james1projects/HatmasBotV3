# Thumbnail Commands — Quick Copy/Paste

Drop-in command reference for `tools/build_thumbnail.py`. Each section
shows the preset, what it's for, and a few ready-to-modify examples.
Replace the god names, skin names, results, and KDAs with whatever
matches the video you just recorded, then paste into a terminal at the
repo root (`C:\Users\james\HatmasBot`).

If you'd rather be prompted for inputs interactively (Stream Deck-friendly),
just double-click `build_thumbnail.bat` instead — it asks for each field
one at a time.

---

## 1v1 — Standard matchup (one of my gods vs one opponent)

```
python tools\build_thumbnail.py --god Ymir --vs Loki --preset 1v1
python tools\build_thumbnail.py --god Ymir --vs Loki --preset 1v1 --result win
python tools\build_thumbnail.py --god Ymir --vs Loki --preset 1v1 --result win --kda 12/3/8
python tools\build_thumbnail.py --god "Hou Yi" --vs "Baron Samedi" --preset 1v1 --text "PENTAKILL" --result win --kda 18/2/5
```

With a custom skin for my god (drop the art at `Custom God Cards\<God>-<Skin>.png` first):

```
python tools\build_thumbnail.py --god Sylvanus --vs Loki --preset 1v1 --skin "High Noon" --result win
```

---

## 1v2 — One of my gods vs two opponents (e.g. recorded two matches against different opponents on the same god)

```
python tools\build_thumbnail.py --god Awilix --vs Eset --vs2 Chiron --preset 1v2
python tools\build_thumbnail.py --god Awilix --vs Eset --vs2 Chiron --preset 1v2 --result win --result2 loss
python tools\build_thumbnail.py --god Awilix --vs Eset --vs2 Chiron --preset 1v2 --result win --result2 win --text "DOUBLE DOWN"
```

`--vs` is the top opponent, `--vs2` is the bottom opponent. `--result` /
`--result2` apply per-matchup.

---

## 2matches — Two separate 1v1 matchups in one video (different gods AND different opponents per match)

```
python tools\build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches
python tools\build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches --result win --result2 loss
```

Top row is Match 1 (`--god` vs `--vs`), bottom row is Match 2 (`--god2` vs `--vs2`).
Per-match results via `--result` / `--result2`.

---

## 2gods — One video covering two of my gods, no opponents shown

```
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --text "STREAM HIGHLIGHTS"
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --result win
```

With a skin variant for one or both gods:

```
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "High Noon"
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "High Noon" --skin2 "Tundra"
```

---

## 3gods — One video covering three of my gods, no opponents shown

Three vertical panels left-to-right, each god card zoomed in on the face,
with an icon badge and the god's name at the bottom of each panel.

```
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --god3 Ymir --preset 3gods
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --god3 Ymir --preset 3gods --text "TRIPLE FEATURE"
```

With skin variants and flips (each flip toggles that god's card only):

```
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --god3 Ymir --preset 3gods --skin "High Noon" --skin3 "Tundra"
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --god3 Ymir --preset 3gods --flip-god --flip-god3
```

---

## single — Single hero god (no opponent shown)

```
python tools\build_thumbnail.py --god "Hou Yi" --preset single
python tools\build_thumbnail.py --god "Hou Yi" --preset single --text "SOLO LANE DOMINATION"
python tools\build_thumbnail.py --god "Hou Yi" --preset single --text "SOLO LANE DOMINATION" --result win --kda 14/0/4
```

---

## Skin art (optional)

Drop manually-downloaded skin art into `Custom God Cards\` at the repo
root. Filename can be either form — both work and the lookup is
case-insensitive on Windows:

```
Custom God Cards\Sylvanus-High Noon.png      <- hyphenated
Custom God Cards\SylvanusHighNoon.webp       <- concatenated (what you usually get)
```

Supported extensions: `.png`, `.webp`, `.jpg`, `.jpeg`.

Use `--skin "<name>"` for `--god`, `--skin2 "<name>"` for `--god2`, and
`--skin3 "<name>"` for `--god3`.
The lookup chain is:

1. `Custom God Cards\<God>-<Skin>.<ext>` (or concat form)
2. `Custom God Cards\<God>.<ext>` — default override for that god, no flag needed
3. `data\god_cards\<slug>.png` — auto-downloaded base art

---

## Common flag tweaks (work with any preset)

```
--text "CUSTOM HEADLINE"     # override the auto-filled headline above VS
--no-text                    # disable the headline entirely
--subtext "CUSTOM SUBTEXT"   # override the auto-filled subtext below VS
--no-subtext                 # disable the subtext entirely
--result win                 # WIN badge; use loss for LOSS, blank for none
--kda 12/3/8                 # K/D/A badge
--no-open                    # don't auto-launch Paint.NET on the result
--no-random-icons            # use the primary <God>.png icon (skip variant pool)
--seed 42                    # reproducible random icon picks
--out thumbnails\custom.png  # custom output filename
```

---

## Helpers

```
python tools\build_thumbnail.py --list                  # list available presets
python tools\download_god_cards.py --add "Atlas"        # fetch base card art for a new god
python tools\download_god_cards.py --add "Hou Yi"       # multi-word god names need quotes
```

If you ever see `[warn] No card found for '<God>'`, run the second
command above for that god. Card art lives at `data\god_cards\<slug>.png`
and only needs to be downloaded once per god.

---

## Examples for today's recordings (edit these in place)

These are templates — replace the gods/skins/results/kdas with what
actually happened in your recordings before pasting:

```
REM Sylvanus & Atlas video (with Sylvanus High Noon skin)
python tools\build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "High Noon" --text "SYLVANUS & ATLAS"

REM Two matches, different gods
python tools\build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches --result win --result2 loss

REM Standard 1v1 with KDA
python tools\build_thumbnail.py --god Ymir --vs Loki --preset 1v1 --result win --kda 12/3/8
```
