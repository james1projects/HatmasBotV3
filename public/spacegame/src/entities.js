/* =======================================================================
   Entities — the player's enemies. Loaded after config.js.
     Enemy (base)  ->  RedShip, GreenShip, YellowShip, OrangeShip
     Missile (yellow's homing projectile)
   Each ship's AI lives in its behave() method, called once per frame by
   the scene. spawnEnemy() in scene.js is the single place these are made.
   ======================================================================= */

class Enemy extends Phaser.Physics.Arcade.Sprite {
  constructor(scene, x, y, texture, type, name) {
    super(scene, x, y, texture);
    scene.add.existing(this);
    scene.physics.add.existing(this);
    this.scene = scene;
    this.type = type;
    this.hp = CONFIG[type].hp;
    this.bornAt = scene.time.now;
    this.phase = 'enter';
    this.setDepth(5);
    // floating username label (the viewer who deployed it)
    this.labelOffset = CONFIG.display.enemy * 0.62 + 10;  // sit just above the ship
    this.label = scene.add.text(x, y - this.labelOffset, name || '', {
      fontFamily: 'ui-monospace, monospace', fontSize: '20px', color: '#cfe3ff',
    }).setOrigin(0.5).setDepth(6);
    this.label.setVisible(scene.showNames);
    // normalize whatever art resolution this texture is to a consistent size
    this.baseScale = CONFIG.display.enemy / Math.max(this.width, this.height);
    // brief warp-in pop (previews the future "warp deploy" feel)
    this.setScale(this.baseScale * 0.2); this.setAlpha(0.4);
    scene.tweens.add({ targets: this, scale: this.baseScale, alpha: 1, duration: 220, ease: 'Back.Out' });
  }
  hurt(dmg) {
    this.hp -= dmg;
    if (this.hp <= 0) { this.scene.onEnemyKilled(this); return true; }
    // brief hit flash — only while still alive (never tween a destroyed sprite)
    this.setTintFill(0xffffff);
    this.scene.time.delayedCall(60, () => { if (this.active) this.clearTint(); });
    return false;
  }
  syncLabel() { if (this.label) this.label.setPosition(this.x, this.y - this.labelOffset); }
  preDestroy() {} // hook
  destroy(fromScene) {
    // Cancel any in-flight tween (e.g. the warp-in pop) so it never runs on a
    // destroyed sprite — that was the freeze when a ship died mid-spawn.
    if (this.scene) this.scene.tweens.killTweensOf(this);
    if (this.label) { this.label.destroy(); this.label = null; }
    this.preDestroy();
    super.destroy(fromScene);
  }
  // overridden per type
  behave(_t, _dt) {}
}

class RedShip extends Enemy {
  constructor(s, x, y, name) { super(s, x, y, 'red', 'red', name);
    this.setVelocityY(CONFIG.red.speed); }
  behave() {
    if (this.y > CONFIG.height + 30) this.scene.registerLeak(this);
    this.syncLabel();
  }
}

class GreenShip extends Enemy {
  constructor(s, x, y, name) { super(s, x, y, 'green', 'green', name);
    this.targetY = rnd(CONFIG.green.backY[0], CONFIG.green.backY[1]);
    this.dir = pick([-1, 1]); this.lastShot = s.time.now + rnd(300, 900); }
  behave(t) {
    // Enter to the back line, then hold and snipe slowly.
    if (this.phase === 'enter') {
      this.setVelocity(0, 120);
      if (this.y >= this.targetY) { this.phase = 'attack'; this.setVelocityY(0); }
    } else {
      this.setVelocityX(CONFIG.green.strafe * this.dir);
      if (this.x < 40) this.dir = 1; else if (this.x > CONFIG.width - 40) this.dir = -1;
      if (t - this.lastShot > CONFIG.green.fireRate) { this.lastShot = t; this.snipe(); }
    }
    this.syncLabel();
  }
  snipe() {
    const p = this.scene.player; if (!p || !p.active) return;
    // lead the player's current motion a touch
    const aimX = p.x + p.body.velocity.x * CONFIG.green.lead;
    const aimY = p.y + p.body.velocity.y * CONFIG.green.lead;
    const ang = Phaser.Math.Angle.Between(this.x, this.y, aimX, aimY);
    this.scene.fireEnemyBullet(this.x, this.y + 14, ang, CONFIG.green.bulletSpeed);
  }
}

class YellowShip extends Enemy {
  constructor(s, x, y, name) { super(s, x, y, 'yellow', 'yellow', name);
    this.targetY = rnd(CONFIG.yellow.holdY[0], CONFIG.yellow.holdY[1]);
    this.dir = pick([-1, 1]); this.lastShot = s.time.now + rnd(600, 1400); }
  behave(t) {
    if (this.phase === 'enter') {
      this.setVelocity(0, 130);
      if (this.y >= this.targetY) { this.phase = 'attack'; this.setVelocityY(0); }
    } else {
      this.setVelocityX(CONFIG.yellow.strafe * this.dir);
      if (this.x < 50) this.dir = 1; else if (this.x > CONFIG.width - 50) this.dir = -1;
      if (t - this.lastShot > CONFIG.yellow.fireRate) { this.lastShot = t; this.scene.fireMissile(this.x, this.y + 14); }
    }
    if (this.y > CONFIG.height + 30) this.scene.registerLeak(this);
    this.syncLabel();
  }
}

class OrangeShip extends Enemy {
  constructor(s, x, y, name) { super(s, x, y, 'orange', 'orange', name);
    this.body.setMaxVelocity(CONFIG.orange.maxSpeed, CONFIG.orange.maxSpeed); }
  behave(t) {
    const p = this.scene.player;
    if (p && p.active) {
      const ang = Phaser.Math.Angle.Between(this.x, this.y, p.x, p.y);
      this.body.setAcceleration(Math.cos(ang) * CONFIG.orange.accel, Math.sin(ang) * CONFIG.orange.accel);
      this.setRotation(ang - Math.PI / 2); // texture points up
    }
    if (t - this.bornAt > CONFIG.orange.fuse) { this.explode(); return; }
    if (this.y > CONFIG.height + 30) this.scene.registerLeak(this);
    this.syncLabel();
  }
  explode() {
    this.scene.boom(this.x, this.y, CONFIG.orange.blastRadius, CONFIG.orange.colorBoom || 0xff9a3c);
    const p = this.scene.player;
    if (p && p.active && Phaser.Math.Distance.Between(this.x, this.y, p.x, p.y) < CONFIG.orange.blastRadius) {
      this.scene.damagePlayer(CONFIG.orange.blastDamage);
    }
    this.scene.onEnemyKilled(this, true); // no score for a self-detonation
  }
}

class Missile extends Phaser.Physics.Arcade.Sprite {
  constructor(scene, x, y) {
    super(scene, x, y, 'missile');
    scene.add.existing(this); scene.physics.add.existing(this);
    this.scene = scene; this.bornAt = scene.time.now; this.setDepth(4);
    this.setDisplaySize(CONFIG.display.missile * (this.width / this.height), CONFIG.display.missile);
    const m = CONFIG.yellow.missile;
    this.spd = m.speed; this.maxSpeed = m.maxSpeed; this.accel = m.accel;
    this.turn = m.turn; this.life = m.life;
    this.heading = Math.PI / 2; // downward
  }
  behave(t, dt) {
    const p = this.scene.player;
    if (p && p.active) {
      const want = Phaser.Math.Angle.Between(this.x, this.y, p.x, p.y);
      this.heading = Phaser.Math.Angle.RotateTo(this.heading, want, this.turn * dt);
    }
    this.spd = Math.min(this.maxSpeed, this.spd + this.accel * dt);
    this.setVelocity(Math.cos(this.heading) * this.spd, Math.sin(this.heading) * this.spd);
    this.setRotation(this.heading - Math.PI / 2);
    const off = this.x < -30 || this.x > CONFIG.width + 30 || this.y < -30 || this.y > CONFIG.height + 30;
    if (off || t - this.bornAt > this.life) this.destroy();
  }
}
