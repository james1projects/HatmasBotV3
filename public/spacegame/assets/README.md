# Streaming Space Game — art assets

Art is organized into subfolders. The game loads files by the paths in
`src/config.js` (`ART_MANIFEST`); anything missing falls back to a generated
placeholder, so you can add/replace art freely.

```
assets/
  background.png        # static play-field backdrop (1920x1080), drawn once
  ships/                # the player + the four enemy ships (used now)
    player.png
    ship_red.png        # (not present yet → red uses a placeholder)
    ship_green.png
    ship_yellow.png
    ship_orange.png
    player_alt.png      # alternate/secondary player art (purpose TBD)
  bosses/               # upgraded boss art for Phase 4 (not wired up yet)
    green_boss.png
    green_boss_firing.png
    orange_boss.png
    yellow_boss.png
    yellow_boss_upgraded.png
  fx/                   # projectiles + effects (not wired up yet)
    bullet_player.png   # (not present yet → placeholder)
    bullet_enemy.png    # (not present yet → placeholder)
    missile.png         # (not present yet → placeholder)
    green_shot.png      # was green-firing.png
    explosion.png       # was shooting_explosiont.png (typo fixed)
    upgrade_shot.png    # was Upgrade_Shooting.png
```

## What the game uses right now

`ships/player.png`, `ships/ship_green.png`, `ships/ship_yellow.png`,
`ships/ship_orange.png`, and `background.png`. The rest is staged for later
phases (bosses, real projectiles).

## Renames applied during the cleanup

| Old name | New path |
|---|---|
| `player_other.png` | `ships/player_alt.png` |
| `ship_orange_boss.png` | `bosses/orange_boss.png` |
| `ship_yellow_boss.png` | `bosses/yellow_boss.png` |
| `green-firing.png` | `fx/green_shot.png` |
| `shooting_explosiont.png` | `fx/explosion.png` (typo fixed) |
| `Upgrade_Shooting.png` | `fx/upgrade_shot.png` |

If any of those guessed names/locations are wrong, tell me what each file
actually is and I'll re-map it (and wire it into the game where it belongs).

## Adding more art

- Drop a PNG at the path listed in `ART_MANIFEST` (e.g. `ships/ship_red.png`)
  and reload — it replaces the placeholder automatically.
- Ships are auto-scaled to a consistent in-game size, so source resolution
  doesn't need to match; transparent backgrounds (PNG alpha) look best.
- To wire a projectile/effect into the game, point me at the file and I'll
  hook it up.
