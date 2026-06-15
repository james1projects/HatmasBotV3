-- control.lua - event wiring, remote interface, console test commands.
-- The remote interface ("hatmas") is the surface the HatmasBot RCON
-- plugin will call. The /hatmas-* console commands exist so everything
-- can be tested solo before any bot wiring.
local pets = require("scripts.pets")
local boss = require("scripts.boss")

local TRACKED_PROTOS = {
  boss.PROTO,
  "hatmas-pet-small",
  "hatmas-pet-medium",
  "hatmas-pet-big",
  "hatmas-pet-behemoth",
}

local function init_storage()
  storage.pets = storage.pets or {}
  storage.pet_owner_index = storage.pet_owner_index or {}
  storage.bosses = storage.bosses or {}
end

script.on_init(init_storage)
script.on_configuration_changed(init_storage)

-- A mod gets ONE handler per event, so control.lua owns registration
-- and dispatches by prototype name.
local function tracked_filters()
  local filters = {}
  for _, name in pairs(TRACKED_PROTOS) do
    filters[#filters + 1] = {filter = "name", name = name}
  end
  return filters
end

script.on_event(defines.events.on_entity_damaged, function(event)
  if event.entity.name == boss.PROTO then
    boss.on_damaged(event)
  else
    pets.on_damaged(event)
  end
end, tracked_filters())

script.on_event(defines.events.on_entity_died, function(event)
  if event.entity.name == boss.PROTO then
    boss.on_died(event)
  else
    pets.on_died(event)
  end
end, tracked_filters())

script.on_nth_tick(30, pets.tick)

-- Remote interface (future RCON surface) -------------------------------------
-- e.g. /sc remote.call("hatmas", "spawn_boss", "viewerName", "north", 150)

remote.add_interface("hatmas", {
  ping = function() return "hatmas-events 0.1.0" end,
  spawn_pet = function(owner, pet_name, size) return pets.spawn(owner, pet_name, size) end,
  upgrade_pet = function(owner) return pets.upgrade(owner) end,
  remove_pet = function(owner) return pets.remove(owner) end,
  pet_say = function(owner, message) return pets.say(owner, message) end,
  list_pets = function() return pets.list() end,
  spawn_boss = function(viewer, direction, distance) return boss.spawn(viewer, direction, distance) end,
})

-- Console test commands -------------------------------------------------------

local function split_words(s)
  local words = {}
  for w in (s or ""):gmatch("%S+") do words[#words + 1] = w end
  return words
end

commands.add_command("hatmas-pet",
  "Spawn a viewer pet: /hatmas-pet <owner> [pet name ...] [small|medium|big|behemoth]",
  function(cmd)
    local args = split_words(cmd.parameter)
    if #args == 0 then
      game.print("[Hatmas] usage: /hatmas-pet <owner> [pet name ...] [small|medium|big|behemoth]")
      return
    end
    local owner = table.remove(args, 1)
    local size = nil
    if #args > 0 and pets.SIZE_INDEX[args[#args]] then
      size = table.remove(args)
    end
    local pet_name = (#args > 0) and table.concat(args, " ") or nil
    game.print("[Hatmas] " .. tostring(pets.spawn(owner, pet_name, size)))
  end)

commands.add_command("hatmas-pet-grow",
  "Grow a pet one size: /hatmas-pet-grow <owner>",
  function(cmd)
    local args = split_words(cmd.parameter)
    game.print("[Hatmas] " .. tostring(pets.upgrade(args[1])))
  end)

commands.add_command("hatmas-pet-remove",
  "Remove a pet: /hatmas-pet-remove <owner>",
  function(cmd)
    local args = split_words(cmd.parameter)
    game.print("[Hatmas] " .. tostring(pets.remove(args[1])))
  end)

commands.add_command("hatmas-pet-say",
  "Pet speech: /hatmas-pet-say <owner> <message>",
  function(cmd)
    local args = split_words(cmd.parameter)
    local owner = table.remove(args, 1)
    if not owner or #args == 0 then
      game.print("[Hatmas] usage: /hatmas-pet-say <owner> <message>")
      return
    end
    game.print("[Hatmas] " .. tostring(pets.say(owner, table.concat(args, " "))))
  end)

commands.add_command("hatmas-boss",
  "Spawn a boss biter: /hatmas-boss <viewer> [north|south|east|west] [distance]",
  function(cmd)
    local args = split_words(cmd.parameter)
    local viewer = args[1] or "tester"
    game.print("[Hatmas] " .. tostring(boss.spawn(viewer, args[2], args[3])))
  end)
