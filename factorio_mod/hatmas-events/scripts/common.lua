-- common.lua - tiny shared helpers
local common = {}

-- The streamer's character: first connected player that has a body.
function common.get_anchor()
  for _, p in pairs(game.connected_players) do
    if p.character then return p.character end
  end
  return nil
end

function common.distance(a, b)
  local dx, dy = a.x - b.x, a.y - b.y
  return math.sqrt(dx * dx + dy * dy)
end

return common
