-- Output configuration

-- Default monitor configuration
-- kanshi will handle dynamic monitor configuration
hl.monitor({
  output = "",
  mode = "preferred",
  position = "auto",
  scale = "1",
})

-- HDR-capable monitors: 10-bit + on-demand HDR via cm_auto_hdr
-- kanshi still manages mode/position/scale

hl.monitor({
  output = "desc:LG Electronics 38GN950",
  bitdepth = 10,
  supports_hdr = 1,
  supports_wide_color = 1,
})

hl.monitor({
  output = "desc:ASUSTek COMPUTER INC VG27A",
  bitdepth = 10,
  supports_hdr = 1,
  supports_wide_color = 1,
})

hl.monitor({
  output = "desc:Sony SONY TV  *30",
  disabled = true,
  bitdepth = 10,
  supports_hdr = 1,
  supports_wide_color = 1,
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
