-- Output configuration

-- Default monitor configuration
-- kanshi will handle dynamic monitor configuration
hl.monitor({
  output = "",
  mode = "preferred",
  position = "auto",
  scale = "1",
})

hl.config({
  render = {
    -- cm_fs_passthrough removed in 0.55; behavior is now automatic
    -- via cm_auto_hdr.
    cm_auto_hdr = 2,
  },
  misc = {
    vrr = 2,
  },
  debug = {
    -- moved from `misc:vfr` to `debug:vfr` in 0.55.
    vfr = true,
  },
})

local function apply_monitor_extras()
  hl.monitor({
    output = "desc:Samsung Display Corp. ATNA60KA04-0",
    bitdepth = 10,
    supports_hdr = 1,
    supports_wide_color = 1,
    vrr = true,
  })
  hl.monitor({
    output = "desc:Sony SONY TV  *30",
    bitdepth = 10,
    supports_hdr = 1,
    supports_wide_color = 1,
    vrr = true,
  })
  hl.monitor({
    output = "desc:LG Electronics 38GN950",
    bitdepth = 10,
    supports_hdr = 1,
    supports_wide_color = 1,
    vrr = true,
  })
  hl.monitor({
    output = "desc:ASUSTek COMPUTER INC VG27A",
    bitdepth = 10,
    supports_hdr = 1,
    supports_wide_color = 1,
  })
end

apply_monitor_extras()
hl.on("hyprland.start", function()
  apply_monitor_extras()
end)
hl.on("monitor.added", function()
  apply_monitor_extras()
end)
hl.on("monitor.removed", function()
  apply_monitor_extras()
end)
