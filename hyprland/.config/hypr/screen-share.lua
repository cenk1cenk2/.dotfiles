-- Rules that keep windows out of screen captures, kept as handles so a
-- remote desktop stream (`virtual-display.py start --reveal`) can lift them
-- for its duration: that stream is the user looking at their own desktop.
-- Each rule carries only `no_screen_share`, so lifting it leaves the
-- window's other rules (float, size) in place.

local M = { rules = {} }

---@param name string
---@param match table<string, string|number|boolean>
function M.window(name, match)
  M.rules[#M.rules + 1] = hl.window_rule({
    name = name .. "-no-screen-share",
    match = match,
    no_screen_share = true,
  })
end

---@param name string
---@param match table<string, string|number|boolean>
function M.layer(name, match)
  M.rules[#M.rules + 1] = hl.layer_rule({
    name = name .. "-no-screen-share",
    match = match,
    no_screen_share = true,
  })
end

---@param hidden boolean
function M.set(hidden)
  for _, rule in ipairs(M.rules) do
    rule:set_enabled(hidden)
  end
end

return M
