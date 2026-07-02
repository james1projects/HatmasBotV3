/* =======================================================================
   Boot — create the Phaser game once everything else is defined.
   Loaded after config.js, entities.js, scene.js.
   ======================================================================= */

new Phaser.Game({
  type: Phaser.AUTO,
  pixelArt: true,   // keep small pixel-art ships crisp when scaled up
  parent: 'game',
  width: CONFIG.width,
  height: CONFIG.height,
  backgroundColor: '#05060f',
  physics: { default: 'arcade', arcade: { gravity: { y: 0 }, debug: false } },
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
  scene: GameScene,
});
