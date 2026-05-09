-- The directory name `config.d` contains a `.`, which collides with
-- Lua's module-path separator — `require("config.d.X")` would look
-- for `config/d/X.lua`. Side-step with explicit dofile().

local dir = os.getenv("HOME") .. "/.config/hypr/config.d"

dofile(dir .. "/50-systemd-user.lua")
dofile(dir .. "/90-theming.lua")
dofile(dir .. "/97-layer-rules.lua")
dofile(dir .. "/98-window-rules.lua")
dofile(dir .. "/99-autostart.lua")
