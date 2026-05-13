---@class Profile
---@field required string[]
---@field monitors HL.MonitorSpec[]
---@field order? integer  Lower wins on ties (same `required` count). Missing = lowest priority.
---@field exec? string[]
---@field on_demand? boolean

---@class Monitors
---@field ANY string
---@field LG_38GN950 string
---@field ASUS_VG27A string
---@field ASUS_XG17A string
---@field ASUS_MB16 string
---@field GPD string
---@field SONY_BRAVIA7 string

---@class ProfilesModule
---@field monitors Monitors
---@field profiles table<string, Profile>
---@field _applying boolean             Re-entry guard. `auto_apply` returns false while `apply` is in flight. Held true through a settle window after each `apply` because monitor.added/removed events fired by our own `hl.monitor` calls land async — without the window, `auto_apply` runs against intermediate state and matches a different profile that overwrites our rules.
---@field _settle_timer HL.Timer|nil    Oneshot that clears `_applying` after the settle window. Cancelled and re-armed on each `apply`.
---@field match fun(): string|nil
---@field apply fun(name: string): boolean
---@field auto_apply fun(): boolean
---@field list fun(): string[]

---@type ProfilesModule
local M = {
  monitors = {
    -- Wildcard sentinel. `M.apply` expands `output = M.monitors.ANY`
    -- into one rule per currently-connected monitor that isn't
    -- explicitly targeted by the profile. This is *not* a real
    -- Hyprland selector — the empty-string catch-all and a literal
    -- "*" both fail to override leftover `desc:` rules from a
    -- previous profile, so we generate per-monitor `desc:` rules
    -- ourselves.
    ANY = "*",
    LG_38GN950 = "desc:LG Electronics 38GN950",
    ASUS_VG27A = "desc:ASUSTek COMPUTER INC VG27A",
    ASUS_XG17A = "desc:ASUSTek COMPUTER INC ASUS XG17A",
    ASUS_MB16 = "desc:ASUSTek COMPUTER INC MB16QHG",
    GPD = "desc:Japan Display Inc. GPD1001H",
    SONY_BRAVIA7 = "desc:Sony SONY TV  *30",
  },
  profiles = {},
  _applying = false,
  _settle_timer = nil,
}

-- Audio routing helper.
---@param sink string
---@param source string
---@return string[]
local function audio(sink, source)
  return {
    ("jumpy sound 'Audio/Sink' '%s'"):format(sink),
    ("jumpy sound 'Audio/Source' '%s'"):format(source),
  }
end

M.profiles = {

  -- ── desktop layouts ────────────────────────────────────────────────

  ["main"] = {
    order = 1,
    required = { M.monitors.LG_38GN950, M.monitors.ASUS_VG27A, M.monitors.ASUS_XG17A },
    monitors = {
      {
        output = M.monitors.LG_38GN950,
        mode = "3840x1600@160",
        position = "0x1440",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ASUS_VG27A,
        mode = "2560x1440@164.999",
        position = "700x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        disabled = false,
      },
      {
        output = M.monitors.ASUS_XG17A,
        mode = "1920x1080@239.964",
        position = "960x3040",
        scale = "1",
        transform = 2,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("Scarlett 8i6 USB", "Scarlett 8i6 USB"),
  },

  ["main-bottom"] = {
    order = 2,
    required = { M.monitors.LG_38GN950, M.monitors.ASUS_XG17A },
    monitors = {
      {
        output = M.monitors.LG_38GN950,
        mode = "3840x1600@160",
        position = "0x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ASUS_XG17A,
        mode = "1920x1080@239.964",
        position = "960x1600",
        scale = "1",
        transform = 2,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("Scarlett 8i6 USB", "Scarlett 8i6 USB"),
  },

  ["main-top"] = {
    order = 3,
    required = { M.monitors.LG_38GN950, M.monitors.ASUS_VG27A },
    monitors = {
      {
        output = M.monitors.LG_38GN950,
        mode = "3840x1600@160",
        position = "0x1440",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ASUS_VG27A,
        mode = "2560x1440@164.999",
        position = "700x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("Scarlett 8i6 USB", "Scarlett 8i6 USB"),
  },

  ["main-solo"] = {
    order = 4,
    required = { M.monitors.LG_38GN950 },
    monitors = {
      {
        output = M.monitors.LG_38GN950,
        mode = "3840x1600@160",
        position = "0x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("Scarlett 8i6 USB", "Scarlett 8i6 USB"),
  },

  -- ── GPD-only / portable layouts ────────────────────────────────────

  ["gpd"] = {
    order = 1,
    required = { M.monitors.GPD },
    monitors = {
      { output = M.monitors.GPD, mode = "2560x1600@60.009", position = "0x0", scale = "2", disabled = false },
    },
  },

  ["docked"] = {
    -- Only wins when GPD+LG are the *only* connected pair. As soon as
    -- VG27A or XG17A joins the desk, `main-top` / `main-bottom` (both
    -- count=2 too) tie-break in front of docked.
    order = 5,
    required = { M.monitors.GPD, M.monitors.LG_38GN950 },
    monitors = {
      {
        output = M.monitors.LG_38GN950,
        mode = "3840x1600@119.982",
        position = "0x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      { output = M.monitors.GPD, disabled = true },
    },
  },

  ["portable-xg17a"] = {
    order = 3,
    required = { M.monitors.GPD, M.monitors.ASUS_XG17A },
    monitors = {
      { output = M.monitors.ASUS_XG17A, mode = "1920x1080@239.964", position = "0x0", scale = "1", disabled = false },
      { output = M.monitors.GPD, mode = "2560x1600@60.009", position = "350x1080", scale = "2", disabled = false },
    },
  },

  ["portable-mb16qhg"] = {
    order = 3,
    required = { M.monitors.GPD, M.monitors.ASUS_MB16 },
    monitors = {
      {
        output = M.monitors.ASUS_MB16,
        mode = "2560x1600@119.963",
        position = "0x0",
        scale = "1.333",
        disabled = false,
      },
      { output = M.monitors.GPD, mode = "2560x1600@60.009", position = "300x1200", scale = "2", disabled = false },
    },
  },

  -- ── TV ─────────────────────────────────────────────────────────────

  ["tv"] = {
    order = 10,
    required = { M.monitors.SONY_BRAVIA7 },
    on_demand = true,
    monitors = {
      {
        output = M.monitors.SONY_BRAVIA7,
        mode = "3840x2160@119.880",
        position = "0x0",
        scale = "2",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("HDA NVidia", "Scarlett 8i6 USB"),
  },

  ["tv-4k"] = {
    order = 10,
    required = { M.monitors.SONY_BRAVIA7 },
    on_demand = true,
    monitors = {
      {
        output = M.monitors.SONY_BRAVIA7,
        mode = "3840x2160@119.880",
        position = "0x0",
        scale = "1",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
    },
    exec = audio("HDA NVidia", "Scarlett 8i6 USB"),
  },
}

-- ── matching ─────────────────────────────────────────────────────────

-- All `monitor_present` checks go through `hl.get_monitor(selector)` —
-- Hyprland's own resolver, the same one that binds `hl.monitor(...)`
-- rules to outputs. Inlined at each call site below.

---@param profile Profile
---@return boolean
local function is_profile_satisfied(profile)
  for _, req in ipairs(profile.required) do
    if not hl.get_monitor(req) then
      return false
    end
  end

  return true
end

-- Pick the most-specific (= largest required set) auto-eligible profile.
-- Ties broken by per-profile `order` — lower wins. Profiles without
-- `order` sink to the bottom.
---@return string|nil
function M.match()
  local best
  ---@type integer|nil, integer
  local best_count, best_order = -1, math.huge
  for name, profile in pairs(M.profiles) do
    if not profile.on_demand and is_profile_satisfied(profile) then
      local count = #profile.required
      local order = profile.order or math.huge
      if count > best_count or (count == best_count and order < best_order) then
        best, best_count, best_order = name, count, order
      end
    end
  end

  return best
end

-- ── apply ────────────────────────────────────────────────────────────

---@param name string
---@return boolean
function M.apply(name)
  local profile = M.profiles[name]
  if not profile then
    hl.exec_cmd(("notify-send -u critical display 'Unknown profile %s.'"):format(name))

    return false
  end

  -- Cancel any in-flight settle timer from a prior apply; we'll arm
  -- a fresh one at the bottom of this call.
  if M._settle_timer then
    M._settle_timer:set_enabled(false)
    M._settle_timer = nil
  end
  M._applying = true

  -- Build the set of selectors this profile explicitly targets.
  ---@type table<string, boolean>
  local targeted = {}
  for _, spec in ipairs(profile.monitors) do
    if spec.output and spec.output ~= "" and spec.output ~= "*" then
      targeted[spec.output] = true
    end
  end

  for _, spec in ipairs(profile.monitors) do
    local sel = spec.output
    if sel == "*" then
      -- Wildcard: emit one rule per connected monitor not claimed by
      -- any targeted selector. Hyprland's own resolver decides which
      -- monitor each targeted selector claims.
      local claimed = {}
      for tgt in pairs(targeted) do
        local mon = hl.get_monitor(tgt)
        if mon then
          claimed[mon.description] = true
        end
      end
      for _, mon in ipairs(hl.get_monitors()) do
        if not claimed[mon.description] then
          local expanded = { output = "desc:" .. mon.description }
          for k, v in pairs(spec) do
            if k ~= "output" then
              expanded[k] = v
            end
          end
          hl.monitor(expanded)
        end
      end
    else
      hl.monitor(spec)
    end
  end
  for _, cmd in ipairs(profile.exec or {}) do
    hl.exec_cmd(cmd)
  end

  hl.exec_cmd(
    ("notify-send display 'Applied profile %s.' " .. "-i /usr/share/icons/Adwaita/scalable/devices/video-display.svg"):format(
      name
    )
  )

  -- Hold the guard for a settle window. monitor.added / monitor.removed
  -- events triggered by the rules we just registered land async (after
  -- this function returns); without the window, the next auto_apply
  -- runs against partially-applied state and re-matches a different
  -- profile that clobbers our work.
  M._settle_timer = hl.timer(function()
    M._applying = false
    M._settle_timer = nil
  end, { timeout = 3000, type = "oneshot" })

  return true
end

---@return boolean
function M.auto_apply()
  -- Suppress re-entry while a manual `apply` is in flight: our own
  -- `hl.monitor` calls fire `monitor.added` / `monitor.removed` events
  -- that would otherwise re-trigger auto_apply mid-stream.
  if M._applying then
    return false
  end
  local name = M.match()
  if name then
    return M.apply(name)
  end

  return false
end

-- ── introspection ────────────────────────────────────────────────────

---@return string[]
function M.list()
  ---@type string[]
  local names = {}
  for n in pairs(M.profiles) do
    names[#names + 1] = n
  end
  table.sort(names)

  return names
end

hl.on("hyprland.start", function()
  M.auto_apply()
end)
hl.on("monitor.added", function()
  M.auto_apply()
end)
hl.on("monitor.removed", function()
  M.auto_apply()
end)

return M
