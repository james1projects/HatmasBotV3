-- boss.lua - viewer-named boss biter with floating name tag and HP bar.
local outbox = require("scripts.outbox")
local common = require("scripts.common")

local boss = {}

boss.PROTO = "hatmas-boss-biter"

local BAR_HALF = 2.5          -- HP bar is 2*BAR_HALF tiles wide
local BAR_TOP = -4.2
local BAR_BOT = -3.8
local TAG_OFFSET = {0, -5.0}
local DEFAULT_DISTANCE = 150
local ENRAGE_FRACTION = 0.25

local DIRECTIONS = {
  north = {0, -1},
  south = {0, 1},
  east = {1, 0},
  west = {-1, 0},
}
local DIRECTION_KEYS = {"north", "south", "east", "west"}

function boss.spawn(viewer, direction, distance)
  viewer = tostring(viewer or "chat")
  distance = tonumber(distance) or DEFAULT_DISTANCE
  if not DIRECTIONS[direction] then
    direction = DIRECTION_KEYS[math.random(#DIRECTION_KEYS)]
  end
  local dirvec = DIRECTIONS[direction]
  local anchor = common.get_anchor()
  if not anchor then return "no player character available" end
  local surface = anchor.surface
  local target = {
    x = anchor.position.x + dirvec[1] * distance,
    y = anchor.position.y + dirvec[2] * distance,
  }
  -- The spawn point may be in ungenerated map; generate it first.
  surface.request_to_generate_chunks(target, 2)
  surface.force_generate_chunk_requests()
  local pos = surface.find_non_colliding_position(boss.PROTO, target, 50, 1) or target
  local ent = surface.create_entity{name = boss.PROTO, position = pos, force = "enemy"}
  if not ent then return "spawn failed" end
  local b = {
    entity = ent,
    viewer = viewer,
    spawned_tick = game.tick,
    max_health = ent.health,   -- spawns at full health
    enraged = false,
  }
  b.tag = rendering.draw_text{
    text = viewer .. "'s Boss",
    surface = surface,
    target = {entity = ent, offset = TAG_OFFSET},
    color = {r = 1, g = 0.35, b = 0.35},
    scale = 1.6,
    alignment = "center",
  }
  b.bar_bg = rendering.draw_rectangle{
    color = {r = 0.1, g = 0.1, b = 0.1, a = 0.8},
    filled = true,
    left_top = {entity = ent, offset = {-BAR_HALF, BAR_TOP}},
    right_bottom = {entity = ent, offset = {BAR_HALF, BAR_BOT}},
    surface = surface,
  }
  b.bar_fill = rendering.draw_rectangle{
    color = {r = 0.2, g = 0.9, b = 0.2, a = 0.9},
    filled = true,
    left_top = {entity = ent, offset = {-BAR_HALF, BAR_TOP}},
    right_bottom = {entity = ent, offset = {BAR_HALF, BAR_BOT}},
    surface = surface,
  }
  local c = ent.commandable
  if c then
    c.set_command{
      type = defines.command.attack_area,
      destination = anchor.position,
      radius = 30,
      distraction = defines.distraction.by_anything,
    }
  end
  storage.bosses[ent.unit_number] = b
  game.print("[Boss] " .. viewer .. " sent a boss biter from the " .. direction .. ".")
  outbox.emit("boss_spawned", {viewer = viewer, direction = direction, distance = distance})
  return "ok"
end

function boss.on_damaged(event)
  local b = storage.bosses[event.entity.unit_number]
  if not b then return end
  local frac = math.max(event.final_health, 0) / b.max_health
  if b.bar_fill and b.bar_fill.valid then
    b.bar_fill.right_bottom = {
      entity = event.entity,
      offset = {-BAR_HALF + 2 * BAR_HALF * frac, BAR_BOT},
    }
    if frac < ENRAGE_FRACTION then
      b.bar_fill.color = {r = 0.95, g = 0.25, b = 0.15, a = 0.9}
    end
  end
  if not b.enraged and frac <= ENRAGE_FRACTION and frac > 0 and event.entity.valid then
    b.enraged = true
    event.entity.speed = (event.entity.speed or 0.2) * 1.5
    rendering.draw_text{
      text = "ENRAGED",
      surface = event.entity.surface,
      target = {entity = event.entity, offset = {0, -6}},
      color = {r = 1, g = 0.2, b = 0.1},
      scale = 2.0,
      alignment = "center",
      time_to_live = 180,
    }
    outbox.emit("boss_enraged", {viewer = b.viewer})
  end
end

function boss.on_died(event)
  local b = storage.bosses[event.entity.unit_number]
  if not b then return end
  storage.bosses[event.entity.unit_number] = nil
  local killer = "unknown"
  if event.cause and event.cause.valid then killer = event.cause.name end
  local secs = math.floor((game.tick - b.spawned_tick) / 60)
  game.print("[Boss] " .. b.viewer .. "'s boss is down after " .. secs .. "s. Final blow: " .. killer)
  outbox.emit("boss_died", {viewer = b.viewer, seconds_alive = secs, killed_by = killer})
end

return boss
