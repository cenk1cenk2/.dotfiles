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
