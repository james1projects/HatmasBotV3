-- pets.lua - viewer pet biters.
-- A pet follows the streamer, never fights, is immune to friendly fire
-- (heal-back), and can die heroically to enemies. One pet per owner.
local outbox = require("scripts.outbox")
local common = require("scripts.common")

local pets = {}

pets.SIZES = {"small", "medium", "big", "behemoth"}
pets.SIZE_INDEX = {small = 1, medium = 2, big = 3, behemoth = 4}

local PET_SPEED = 0.32        -- max speed (tiles/tick); keeps up with exo legs
local FOLLOW_DIST = 6         -- start walking toward the player beyond this
local TELEPORT_DIST = 60      -- catch-up teleport beyond this
local COMMAND_REISSUE_TICKS = 120
local MAX_SAY_LENGTH = 80
local TAG_COLOR = {r = 1, g = 0.85, b = 0.3}

local function proto_for(size) return "hatmas-pet-" .. size end

local function attach_tag(pet)
  pet.tag = rendering.draw_text{
    text = pet.owner,
    surface = pet.entity.surface,
    target = {entity = pet.entity, offset = {0, -1.8}},
    color = TAG_COLOR,
    scale = 1.0,
    alignment = "center",
  }
end

local function find_pet(owner)
  local key = tostring(owner or ""):lower()
  local unit_number = storage.pet_owner_index[key]
  return unit_number and storage.pets[unit_number] or nil, key
end

-- Destroy + recreate the pet entity (used for size upgrades and
-- cross-surface moves, since units cannot teleport across surfaces).
-- entity.destroy() does not fire on_entity_died, so no false RIP.
local function rebuild_entity(pet, surface, position)
  local old = pet.entity
  if old and old.valid then
    storage.pets[old.unit_number] = nil
    old.destroy()
  end
  if pet.tag and pet.tag.valid then pet.tag.destroy() end
  local pos = surface.find_non_colliding_position(proto_for(pet.size), position, 20, 0.5) or position
  local ent = surface.create_entity{name = proto_for(pet.size), position = pos, force = "player"}
  if not ent then
    storage.pet_owner_index[pet.owner:lower()] = nil
    return nil
  end
  ent.speed = PET_SPEED
  pet.entity = ent
  pet.last_cmd_tick = 0
  attach_tag(pet)
  storage.pets[ent.unit_number] = pet
  storage.pet_owner_index[pet.owner:lower()] = ent.unit_number
  return ent
end

function pets.spawn(owner, pet_name, size)
  owner = tostring(owner or "viewer")
  pet_name = tostring(pet_name or (owner .. "'s biter"))
  if not pets.SIZE_INDEX[size] then size = "small" end
  local anchor = common.get_anchor()
  if not anchor then return "no player character available" end
  local existing = find_pet(owner)
  if existing then pets.remove(owner) end
  local surface = anchor.surface
  local pos = surface.find_non_colliding_position(proto_for(size), anchor.position, 20, 0.5) or anchor.position
  local ent = surface.create_entity{name = proto_for(size), position = pos, force = anchor.force}
  if not ent then return "spawn failed" end
  ent.speed = PET_SPEED
  local pet = {
    entity = ent,
    owner = owner,
    pet_name = pet_name,
    size = size,
    created_tick = game.tick,
    last_cmd_tick = 0,
  }
  attach_tag(pet)
  storage.pets[ent.unit_number] = pet
  storage.pet_owner_index[owner:lower()] = ent.unit_number
  game.print("[Pets] " .. pet_name .. " joined the stream. Owner: " .. owner)
  outbox.emit("pet_spawned", {owner = owner, pet_name = pet_name, size = size})
  return "ok"
end

function pets.upgrade(owner)
  local pet = find_pet(owner)
  if not pet then return "no pet for " .. tostring(owner) end
  local idx = pets.SIZE_INDEX[pet.size]
  if idx >= #pets.SIZES then return pet.pet_name .. " is already max size" end
  if not (pet.entity and pet.entity.valid) then return "pet entity is gone" end
  pet.size = pets.SIZES[idx + 1]
  local pos = pet.entity.position
  local ent = rebuild_entity(pet, pet.entity.surface, pos)
  if not ent then return "upgrade failed" end
  game.print("[Pets] " .. pet.pet_name .. " grew to " .. pet.size .. ".")
  outbox.emit("pet_upgraded", {owner = pet.owner, pet_name = pet.pet_name, size = pet.size})
  return "ok"
end

function pets.remove(owner)
  local pet, key = find_pet(owner)
  if not pet then return "no pet for " .. tostring(owner) end
  if pet.entity and pet.entity.valid then
    storage.pets[pet.entity.unit_number] = nil
    pet.entity.destroy()
  end
  if pet.tag and pet.tag.valid then pet.tag.destroy() end
  storage.pet_owner_index[key] = nil
  outbox.emit("pet_removed", {owner = pet.owner, pet_name = pet.pet_name})
  return "ok"
end

function pets.say(owner, message)
  local pet = find_pet(owner)
  if not pet then return "no pet for " .. tostring(owner) end
  if not (pet.entity and pet.entity.valid) then return "pet entity is gone" end
  message = tostring(message or ""):gsub("%s+", " ")
  if #message == 0 then return "empty message" end
  if #message > MAX_SAY_LENGTH then message = message:sub(1, MAX_SAY_LENGTH) end
  rendering.draw_text{
    text = '"' .. message .. '"',
    surface = pet.entity.surface,
    target = {entity = pet.entity, offset = {0, -2.8}},
    color = {r = 1, g = 1, b = 1},
    scale = 1.1,
    alignment = "center",
    time_to_live = 300,
  }
  return "ok"
end

function pets.list()
  local out = {}
  for _, pet in pairs(storage.pets) do
    if pet.entity and pet.entity.valid then
      out[#out + 1] = {
        owner = pet.owner,
        pet_name = pet.pet_name,
        size = pet.size,
        position = pet.entity.position,
      }
    end
  end
  return out
end

-- Follow loop, runs every 30 ticks (0.5s). Iterates over a snapshot
-- because rebuild_entity mutates storage.pets mid-loop.
function pets.tick()
  local anchor = common.get_anchor()
  if not anchor then return end
  local snapshot = {}
  for _, pet in pairs(storage.pets) do snapshot[#snapshot + 1] = pet end
  for _, pet in pairs(snapshot) do
    local ent = pet.entity
    if ent and ent.valid then
      if ent.surface ~= anchor.surface then
        rebuild_entity(pet, anchor.surface, anchor.position)
      else
        local d = common.distance(ent.position, anchor.position)
        if d > TELEPORT_DIST then
          local pos = anchor.surface.find_non_colliding_position(ent.name, anchor.position, 15, 0.5)
          if pos then
            ent.teleport(pos)
            pet.last_cmd_tick = 0
          end
        elseif d > FOLLOW_DIST and game.tick - pet.last_cmd_tick > COMMAND_REISSUE_TICKS then
          local c = ent.commandable
          if c then
            c.set_command{
              type = defines.command.go_to_location,
              destination_entity = anchor,
              radius = 3,
              distraction = defines.distraction.none,
            }
            pet.last_cmd_tick = game.tick
          end
        end
      end
    end
  end
end

-- Friendly fire: if the damage came from the pet's own force, give the
-- health straight back. Burst damage that would kill outright is
-- buffered by the pets' large HP pools (data.lua); whether a true
-- one-shot can still kill is an edge to verify in-game.
function pets.on_damaged(event)
  local pet = storage.pets[event.entity.unit_number]
  if not pet then return end
  if event.force and event.entity.valid and event.force.name == event.entity.force.name then
    event.entity.health = event.entity.health + event.final_damage_amount
  end
end

function pets.on_died(event)
  local pet = storage.pets[event.entity.unit_number]
  if not pet then return end
  storage.pets[event.entity.unit_number] = nil
  storage.pet_owner_index[pet.owner:lower()] = nil
  local killer = "unknown causes"
  if event.cause and event.cause.valid then killer = event.cause.name end
  local lifetime_s = math.floor((game.tick - pet.created_tick) / 60)
  game.print("[Pets] " .. pet.pet_name .. " (" .. pet.owner .. ") died after " .. lifetime_s .. "s. Killed by: " .. killer)
  outbox.emit("pet_died", {
    owner = pet.owner,
    pet_name = pet.pet_name,
    size = pet.size,
    lifetime_seconds = lifetime_s,
    killed_by = killer,
  })
end

return pets
