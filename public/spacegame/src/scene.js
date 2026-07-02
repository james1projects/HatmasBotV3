/* =======================================================================
   GameScene — the whole game loop: load, build, spawn, collide, score.
   Loaded after config.js + entities.js. Booted by boot.js.
   ======================================================================= */

class GameScene extends Phaser.Scene {
  constructor() { super('game'); }

  preload() {
    this._loadedKeys = new Set();
    this.load.on('filecomplete', (key) => this._loadedKeys.add(key));
    this.load.on('loaderror', (file) =>
      console.warn('[spacegame] asset not found, will use a placeholder for "' + file.key + '" (' + file.src + ')'));
    if (CONFIG.useImages) {
      for (const [key, file] of Object.entries(ART_MANIFEST)) this.load.image(key, 'assets/' + file);
      this.load.image('bg', 'assets/background.png');
    }
  }

  create() {
    this.showNames = true;
    this.paused = false;
    this.isOver = false;
    this.leaks = 0;
    this.score = 0;

    this.makePlaceholders();   // fills in only the textures your real art didn't provide
    this.buildBackground();
    this.reportArt();

    this.cameras.main.setBackgroundColor('#05060f');

    // groups
    this.enemies = this.add.group();
    this.missiles = this.add.group();
    this.playerBullets = this.physics.add.group();
    this.enemyBullets = this.physics.add.group();

    // player
    this.player = this.physics.add.sprite(CONFIG.width / 2, CONFIG.height - 90, 'player');
    this.player.setDepth(8).setCollideWorldBounds(true);
    this.player.setScale(CONFIG.display.player / Math.max(this.player.width, this.player.height));
    this.player.body.setSize(this.player.width * 0.6, this.player.height * 0.6, true);
    this.player.hp = CONFIG.player.maxHp;
    this.lastShot = 0;

    // input
    this.cursors = this.input.keyboard.createCursorKeys();
    this.keys = this.input.keyboard.addKeys('W,A,S,D,P,R,ONE,TWO,THREE,FOUR');
    this.input.keyboard.on('keydown-P', () => this.pauseToggle());
    this.input.keyboard.on('keydown-R', () => this.scene.restart());
    this.input.keyboard.on('keydown-ONE',   () => this.spawnEnemy('red'));
    this.input.keyboard.on('keydown-TWO',   () => this.spawnEnemy('green'));
    this.input.keyboard.on('keydown-THREE', () => this.spawnEnemy('yellow'));
    this.input.keyboard.on('keydown-FOUR',  () => this.spawnEnemy('orange'));

    // collisions
    this.physics.add.overlap(this.playerBullets, this.enemies, this.hitEnemy, null, this);
    this.physics.add.overlap(this.playerBullets, this.missiles, this.hitMissile, null, this);
    this.physics.add.overlap(this.player, this.enemyBullets, this.playerHitByBullet, null, this);
    this.physics.add.overlap(this.player, this.missiles, this.playerHitByMissile, null, this);
    this.physics.add.overlap(this.player, this.enemies, this.playerTouchEnemy, null, this);

    this.buildHud();

    // auto-wave timer
    this.autoOn = CONFIG.autoWave.enabled;
    this.time.addEvent({ delay: CONFIG.autoWave.interval, loop: true, callback: () => {
      if (this.autoOn && !this.paused && !this.isOver) this.spawnEnemy(this.weightedType());
    }});

    // expose API for the HTML buttons + the chat WebSocket bridge
    window.SG = {
      spawn: (t, name) => this.spawnEnemy(t, name ? { name } : {}),
      toggleAuto: () => { this.autoOn = !this.autoOn;
        document.getElementById('autoBtn').textContent = 'Auto-wave: ' + (this.autoOn ? 'ON' : 'OFF'); },
      toggleNames: () => { this.showNames = !this.showNames;
        this.enemies.getChildren().forEach(e => e.label && e.label.setVisible(this.showNames));
        document.getElementById('nameBtn').textContent = 'Name tags: ' + (this.showNames ? 'ON' : 'OFF'); },
      pause: () => this.pauseToggle(),
      restart: () => this.scene.restart(),
    };
  }

  /* ----- placeholder textures (skipped for any key real art provided) ---- */
  makePlaceholders() {
    // Each helper bails if a real texture already loaded for that key, so your
    // art wins and only the gaps get a generated stand-in.
    const ship = (key, color, up) => {
      if (this.textures.exists(key)) return;
      const g = this.make.graphics({ add: false }); const w = 38, h = 38;
      g.fillStyle(color, 1);
      if (up) g.fillTriangle(w / 2, 0, 2, h, w - 2, h);
      else    g.fillTriangle(2, 2, w - 2, 2, w / 2, h);
      g.fillStyle(0xffffff, 0.9); g.fillCircle(w / 2, up ? h * 0.62 : h * 0.42, 4);
      g.generateTexture(key, w, h); g.destroy();
    };
    ship('player', CONFIG.colors.player, true);
    ship('red', CONFIG.colors.red, false);
    ship('green', CONFIG.colors.green, false);
    ship('yellow', CONFIG.colors.yellow, false);
    ship('orange', CONFIG.colors.orange, false);

    const dot = (key, color, r) => {
      if (this.textures.exists(key)) return;
      const g = this.make.graphics({ add: false });
      g.fillStyle(color, 1); g.fillCircle(r, r, r); g.generateTexture(key, r * 2, r * 2); g.destroy(); };
    dot('eBullet', CONFIG.colors.eBullet, 10);

    const rect = (key, color, w, h) => {
      if (this.textures.exists(key)) return;
      const g = this.make.graphics({ add: false });
      g.fillStyle(color, 1); g.fillRoundedRect(0, 0, w, h, Math.min(w, h) / 2);
      g.generateTexture(key, w, h); g.destroy(); };
    rect('pBullet', CONFIG.colors.pBullet, 12, 34);
    rect('missile', CONFIG.colors.missile, 16, 38);
  }

  // Console summary so you can see which filenames matched your art.
  reportArt() {
    if (!CONFIG.useImages) { console.info('[spacegame] useImages is false — all placeholders.'); return; }
    const keys = [...Object.keys(ART_MANIFEST), 'bg'];
    const real = keys.filter(k => this._loadedKeys && this._loadedKeys.has(k));
    const ph = keys.filter(k => !real.includes(k));
    console.info('[spacegame] REAL art loaded for: ' + (real.join(', ') || '(none)'));
    console.info('[spacegame] placeholders used for: ' + (ph.join(', ') || '(none)'));
  }

  buildBackground() {
    // One static image stretched to fill the field — no scrolling.
    if (CONFIG.useImages && this.textures.exists('bg')) {
      this.add.image(0, 0, 'bg').setOrigin(0).setDisplaySize(CONFIG.width, CONFIG.height).setDepth(-10);
      return;
    }
    // Placeholder: a static starfield drawn once across the whole field.
    const g = this.make.graphics({ add: false });
    const stars = Math.round((CONFIG.width * CONFIG.height) / 7000); // density
    for (let i = 0; i < stars; i++) {
      g.fillStyle(0xffffff, Phaser.Math.FloatBetween(0.2, 1));
      const s = Math.random() < 0.2 ? 2 : 1;
      g.fillRect(rnd(0, CONFIG.width), rnd(0, CONFIG.height), s, s);
    }
    g.generateTexture('stars', CONFIG.width, CONFIG.height); g.destroy();
    this.add.image(0, 0, 'stars').setOrigin(0).setDepth(-10).setAlpha(0.9);
  }

  buildHud() {
    this.hud = this.add.container(0, 0).setDepth(20);
    this.hpBack = this.add.rectangle(28, 28, 360, 26, 0x222a52).setOrigin(0);
    this.hpBar = this.add.rectangle(28, 28, 360, 26, 0x33e1ff).setOrigin(0);
    this.hpText = this.add.text(30, 60, 'HULL', { fontFamily: 'monospace', fontSize: '20px', color: '#8ea2cf' });
    this.leakText = this.add.text(CONFIG.width - 28, 24, '', { fontFamily: 'monospace', fontSize: '28px', color: '#ff8a8a' }).setOrigin(1, 0);
    this.scoreText = this.add.text(CONFIG.width - 28, 64, '', { fontFamily: 'monospace', fontSize: '24px', color: '#cfe3ff' }).setOrigin(1, 0);
    this.hud.add([this.hpBack, this.hpBar, this.hpText, this.leakText, this.scoreText]);
    this.refreshHud();
  }
  refreshHud() {
    const frac = Phaser.Math.Clamp(this.player ? this.player.hp / CONFIG.player.maxHp : 0, 0, 1);
    this.hpBar.width = 360 * frac;
    this.hpBar.fillColor = frac > 0.5 ? 0x33e1ff : frac > 0.25 ? 0xffd24a : 0xff5a5a;
    this.leakText.setText('LEAKS  ' + this.leaks + ' / ' + CONFIG.leakMax);
    this.scoreText.setText('SCORE  ' + this.score);
  }

  /* ------------------------------ spawning ------------------------------ */
  weightedType() {
    const w = CONFIG.autoWave.weights; const bag = [];
    for (const k in w) for (let i = 0; i < w[k]; i++) bag.push(k);
    return pick(bag);
  }
  // The ONE spawn entry point. Chat/web deploys call this with opts.name.
  spawnEnemy(type, opts = {}) {
    if (this.isOver) return;
    if (this.enemies.getChildren().length >= CONFIG.maxEnemies) return; // flood/perf cap
    const name = opts.name || pick(SAMPLE_NAMES);
    const x = opts.x != null ? opts.x : rnd(40, CONFIG.width - 40);
    const y = opts.y != null ? opts.y : -20;
    let e;
    if (type === 'red') e = new RedShip(this, x, y, name);
    else if (type === 'green') e = new GreenShip(this, x, y, name);
    else if (type === 'yellow') e = new YellowShip(this, x, y, name);
    else if (type === 'orange') e = new OrangeShip(this, x, y, name);
    else return;
    e.body.setSize(e.width * 0.8, e.height * 0.8, true);
    this.enemies.add(e);
    return e;
  }

  fireEnemyBullet(x, y, ang, speed) {
    const b = this.enemyBullets.create(x, y, 'eBullet').setDepth(4);
    b.setVelocity(Math.cos(ang) * speed, Math.sin(ang) * speed);
  }
  fireMissile(x, y) { this.missiles.add(new Missile(this, x, y)); }

  /* ----------------------------- combat --------------------------------- */
  // Identify objects by what they are, not by argument position — Phaser's
  // overlap can hand these back in either order.
  hitEnemy(a, b) {
    const enemy = (a && typeof a.hurt === 'function') ? a : b;
    const bullet = (enemy === a) ? b : a;
    if (bullet && bullet.destroy) bullet.destroy();
    if (enemy && enemy.active && typeof enemy.hurt === 'function') enemy.hurt(CONFIG.bulletDamage);
  }
  hitMissile(a, b) {
    const missile = (a instanceof Missile) ? a : b;
    const bullet = (missile === a) ? b : a;
    if (bullet && bullet.destroy) bullet.destroy();
    if (missile && missile.active) { this.boom(missile.x, missile.y, 26, 0xffe07a); missile.destroy(); }
  }

  onEnemyKilled(enemy, silent) {
    if (!silent) { this.score += CONFIG[enemy.type].score; this.boom(enemy.x, enemy.y, 30, CONFIG.colors[enemy.type]); }
    enemy.destroy();
    this.refreshHud();
  }

  playerHitByBullet(a, b) {
    const bullet = (a === this.player) ? b : a;
    if (bullet && bullet.destroy) bullet.destroy();
    this.damagePlayer(CONFIG.enemyBulletDamage);
  }
  playerHitByMissile(a, b) {
    const missile = (a instanceof Missile) ? a : b;
    if (missile && missile.active) { this.boom(missile.x, missile.y, 30, 0xffe07a); missile.destroy(); }
    this.damagePlayer(20);
  }
  playerTouchEnemy(a, b) {
    const enemy = (a === this.player) ? b : a;
    if (!enemy || !enemy.active || typeof enemy.type === 'undefined') return;
    if (enemy.type === 'orange') { enemy.explode(); return; }
    this.boom(enemy.x, enemy.y, 26, CONFIG.colors[enemy.type]);
    enemy.destroy(); this.damagePlayer(CONFIG.touchDamage);
  }

  damagePlayer(amount) {
    if (this.isOver) return;
    this.player.hp -= amount;
    this.cameras.main.shake(120, 0.006);
    this.tweens.add({ targets: this.player, duration: 60, yoyo: true,
      onStart: () => this.player.setTintFill(0xffffff), onComplete: () => this.player.clearTint() });
    this.refreshHud();
    if (this.player.hp <= 0) this.gameOver('HULL BREACH');
  }

  registerLeak(enemy) {
    enemy.destroy();
    this.leaks += 1;
    this.leakText && this.tweens.add({ targets: this.leakText, scale: 1.4, duration: 90, yoyo: true });
    this.refreshHud();
    if (this.leaks >= CONFIG.leakMax) this.gameOver('TOO MANY LEAKS');
  }

  boom(x, y, radius, color) {
    const c = this.add.circle(x, y, radius * 0.4, color, 0.6).setDepth(7);
    this.tweens.add({ targets: c, radius: radius, alpha: 0, scale: 1.6, duration: 300,
      onComplete: () => c.destroy() });
  }

  /* ------------------------------ flow ---------------------------------- */
  pauseToggle() {
    if (this.isOver) return;
    this.paused = !this.paused;
    if (this.paused) { this.physics.world.pause(); this.showBanner('PAUSED'); }
    else { this.physics.world.resume(); if (this.banner) { this.banner.destroy(); this.banner = null; } }
    document.getElementById('pauseBtn').textContent = this.paused ? 'Resume (P)' : 'Pause (P)';
  }
  showBanner(text, sub) {
    if (this.banner) this.banner.destroy();
    this.banner = this.add.container(CONFIG.width / 2, CONFIG.height / 2).setDepth(30);
    const t = this.add.text(0, 0, text, { fontFamily: 'monospace', fontSize: '72px', color: '#ffffff' }).setOrigin(0.5);
    this.banner.add(t);
    if (sub) this.banner.add(this.add.text(0, 70, sub, { fontFamily: 'monospace', fontSize: '28px', color: '#8ea2cf' }).setOrigin(0.5));
  }
  gameOver(reason) {
    this.isOver = true;
    this.physics.world.pause();
    this.showBanner('GAME OVER', reason + '   ·   SCORE ' + this.score + '   ·   press R to restart');
  }

  update(time, delta) {
    if (this.paused || this.isOver) return;
    const dt = delta / 1000;

    // player movement
    const p = this.player, s = CONFIG.player.speed; let vx = 0, vy = 0;
    if (this.cursors.left.isDown || this.keys.A.isDown) vx = -s;
    else if (this.cursors.right.isDown || this.keys.D.isDown) vx = s;
    if (this.cursors.up.isDown || this.keys.W.isDown) vy = -s;
    else if (this.cursors.down.isDown || this.keys.S.isDown) vy = s;
    p.setVelocity(vx, vy);
    const minY = CONFIG.height * CONFIG.player.lowerBand;
    if (p.y < minY) { p.y = minY; if (p.body.velocity.y < 0) p.setVelocityY(0); }

    // auto-fire
    if (time - this.lastShot > CONFIG.player.fireRate) {
      this.lastShot = time;
      const b = this.playerBullets.create(p.x, p.y - CONFIG.display.player * 0.5, 'pBullet').setDepth(4);
      b.setVelocityY(-CONFIG.player.bulletSpeed);
    }

    // behaviors (iterate over a copy — behave() may destroy the entity)
    [...this.enemies.getChildren()].forEach(e => e.active && e.behave(time, dt));
    [...this.missiles.getChildren()].forEach(m => m.active && m.behave(time, dt));

    // cull stray bullets
    this.playerBullets.getChildren().forEach(b => { if (b.y < -30) b.destroy(); });
    this.enemyBullets.getChildren().forEach(b => {
      if (b.y < -30 || b.y > CONFIG.height + 30 || b.x < -30 || b.x > CONFIG.width + 30) b.destroy(); });
  }
}
