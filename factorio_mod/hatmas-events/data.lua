-- data.lua - prototype definitions for hatmas-events
-- Boss biter: scaled, tinted, high-HP behemoth clone.
-- Pet biters: tougher biter clones that live on the player's force.

local BOSS_SCALE = 1.6
local BOSS_TINT = {r = 1.0, g = 0.45, b = 0.45}

-- Recursively scale (and optionally tint) every sprite layer in an
-- animation table. Only touches tables that look like sprite
-- definitions (have filename/filenames/stripes). Shadow layers keep
-- their natural tint so shadows do not turn red.
local function adjust_animation(t, factor, tint, seen)
  if type(t) ~= "table" then return end
  seen = seen or {}
  if seen[t] then return end
  seen[t] = true
  if t.filename or t.filenames or t.stripes then
    t.scale = (t.scale or 1) * factor
    if tint and not t.draw_as_shadow then
      t.tint = tint
    end
  end
  for _, v in pairs(t) do
    if type(v) == "table" then
      adjust_animation(v, factor, tint, seen)
    end
  end
end

local function scale_box(box, factor)
  if not box then return end
  box[1][1] = box[1][1] * factor
  box[1][2] = box[1][2] * factor
  box[2][1] = box[2][1] * factor
  box[2][2] = box[2][2] * factor
end

-- Boss biter ----------------------------------------------------------------

local boss = util.table.deepcopy(data.raw["unit"]["behemoth-biter"])
boss.name = "hatmas-boss-biter"
boss.max_health = 40000
boss.movement_speed = boss.movement_speed * 0.8
adjust_animation(boss.run_animation, BOSS_SCALE, BOSS_TINT)
if boss.attack_parameters then
  boss.attack_parameters.damage_modifier = (boss.attack_parameters.damage_modifier or 1) * 2
  if boss.attack_parameters.animation then
    adjust_animation(boss.attack_parameters.animation, BOSS_SCALE, BOSS_TINT)
  end
end
scale_box(boss.collision_box, 1.4)
scale_box(boss.selection_box, 1.4)
boss.loot = {{item = "raw-fish", count_min = 5, count_max = 10, probability = 1}}

-- Pet biters ----------------------------------------------------------------
-- High HP relative to their look so a stray friendly shot cannot
-- one-shot them (the friendly-fire heal-back in control stage handles
-- sustained damage; the HP pool handles burst).

local PET_SOURCES = {
  small = "small-biter",
  medium = "medium-biter",
  big = "big-biter",
  behemoth = "behemoth-biter",
}
local PET_HEALTH = {small = 750, medium = 1500, big = 3000, behemoth = 6000}

local protos = {boss}
for size, source in pairs(PET_SOURCES) do
  local pet = util.table.deepcopy(data.raw["unit"][source])
  pet.name = "hatmas-pet-" .. size
  pet.max_health = PET_HEALTH[size]
  pet.movement_speed = 0.3
  protos[#protos + 1] = pet
end

data:extend(protos)
