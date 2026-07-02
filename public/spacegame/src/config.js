/* =======================================================================
   Streaming Space Game — configuration, art manifest, and shared helpers.
   Loaded FIRST. Everything else (entities, scene, net) reads these globals.
   ======================================================================= */

const CONFIG = {
  // The play field matches the background art (1920x1080) and the background
  // is drawn once, static — it does not scroll.
  width: 1920,
  height: 1080,

  // Flip to false to force the generated placeholder shapes instead of art.
  useImages: true,

  player: { speed: 700, fireRate: 180, bulletSpeed: 850, maxHp: 100,
            lowerBand: 0.45 /* can roam the bottom 55% of the screen */ },

  leakMax: 10,            // lose after this many enemies pass the bottom
  maxEnemies: 40,         // hard cap on concurrent ships (anti-flood / perf)
  bulletDamage: 1,        // player shot damage
  enemyBulletDamage: 8,   // green sniper shot
  touchDamage: 12,        // non-orange ship colliding with the player

  red:    { hp: 1, speed: 150, score: 10 },
  green:  { hp: 1, score: 20, backY: [90, 280], strafe: 110,
            fireRate: 1900, bulletSpeed: 360, lead: 0.55 },   // slow back-line sniper, leads the player
  yellow: { hp: 1, score: 30, holdY: [210, 420], strafe: 150,
            fireRate: 2600, missile: { speed: 110, accel: 110, maxSpeed: 470, turn: 2.0, life: 9000 } },
  orange: { hp: 3, score: 40, accel: 440, maxSpeed: 580, fuse: 4500,
            blastRadius: 175, blastDamage: 30 },

  autoWave: { enabled: true, interval: 1400, weights: { red:5, green:3, yellow:2, orange:2 } },

  // Normalized on-screen size (longest side, px) so mixed-resolution art
  // (your 16px ships vs 250px ships) all render at a consistent game scale.
  display: { player: 120, enemy: 100, missile: 46 },

  colors: { player:0x33e1ff, red:0xff5a5a, green:0x56e06a, yellow:0xffd24a,
            orange:0xff9a3c, pBullet:0x9bf0ff, eBullet:0xff7a7a, missile:0xffe07a },
};

// Texture key -> file under assets/. Anything missing falls back to a
// generated placeholder, so you can add real art incrementally.
const ART_MANIFEST = {
  player:  'ships/player.png',
  red:     'ships/ship_red.png',
  green:   'ships/ship_green.png',
  yellow:  'ships/ship_yellow.png',
  orange:  'ships/ship_orange.png',
  pBullet: 'fx/bullet_player.png',
  eBullet: 'fx/bullet_enemy.png',
  missile: 'fx/missile.png',
  // background is loaded separately in scene.js (assets/background.png).
};

/* ---------------------------- helpers ---------------------------------- */
const rnd = Phaser.Math.Between;
const pick = (arr) => arr[rnd(0, arr.length - 1)];
const SAMPLE_NAMES = ['nova_kid','warpFox','bitlord','hatfan42','zapqueen','dr_meteor',
  'orbital','glitchy','starseed','vortex','pewpew','comet99'];
