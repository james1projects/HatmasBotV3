-- outbox.lua - game -> bot event channel.
-- Appends one JSON object per line to script-output/hatmas/events.jsonl.
-- The HatmasBot factorio plugin tails this file.
local outbox = {}

function outbox.emit(event_type, payload)
  payload = payload or {}
  payload.event = event_type
  payload.tick = game.tick
  helpers.write_file("hatmas/events.jsonl", helpers.table_to_json(payload) .. "\n", true)
end

return outbox
