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
---@field SAMSUNG_ATNA60 string
---@field SONY_BRAVIA7 string

---@class ProfilesModule
---@field monitors Monitors
---@field profiles table<string, Profile>
---@field _connected table<string, boolean>       Descriptions of connected KNOWN monitors, including ones a profile disabled. Maintained from monitor.added/removed. Matching reads this, never `hl.get_monitor` — Hyprland's Lua queries see only *enabled* outputs, so a profile that disables a monitor it requires (docked → GPD) would otherwise self-invalidate on the next event.
---@field _active string|nil                       Name of the last-applied profile. `auto_apply` no-ops when the match is unchanged, so `exec` side effects fire only on real transitions.
---@field _disabled_by_active table<string, boolean> Descriptions the active profile disabled — what `rescue` re-enables when nothing matches.
---@field _pending_removed table<string, boolean>   One-shot markers for rule-driven disables in flight. A disable emits exactly one `monitor.removed`; the handler consumes the marker and keeps the monitor in `_connected`. A later removed for the same description (no marker left) is a genuine unplug and drops it — a lifetime shield here would mask real unplugs of profile-disabled monitors forever.
---@field _debounce HL.Timer|nil                   Re-arming oneshot that coalesces a burst of monitor events into one `auto_apply` after quiescence. Replaces the old fixed settle window — it decides *when* matching runs, never *whether* `_connected` is trusted.
---@field match fun(): string|nil
---@field apply fun(name: string): boolean
---@field auto_apply fun(): boolean
---@field rescue fun()
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
    SAMSUNG_ATNA60 = "desc:Samsung Display Corp. ATNA60KA04-0",
    SONY_BRAVIA7 = "desc:Sony SONY TV  *30",
  },
  profiles = {},
  _connected = {},
  _active = nil,
  _disabled_by_active = {},
  _pending_removed = {},
  _debounce = nil,
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
      -- `transform = 0` is explicit: 0.56 merges rules per selector, so
      -- omitting it would inherit `main`/`main-bottom`'s XG17A rotation.
      {
        output = M.monitors.ASUS_XG17A,
        mode = "1920x1080@239.964",
        position = "0x0",
        scale = "1",
        transform = 0,
        disabled = false,
      },
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

  -- ── laptop (Yoga OLED) layouts ─────────────────────────────────────

  ["lenovo"] = {
    order = 1,
    required = { M.monitors.SAMSUNG_ATNA60 },
    monitors = {
      {
        output = M.monitors.SAMSUNG_ATNA60,
        mode = "3200x2000@120",
        position = "0x0",
        -- 1.66667 (200/120), not 1.67: Hyprland only accepts fractional
        -- scales that are multiples of 1/120, and 1.67×120=200.4 is
        -- rejected. 200/120 gives a clean 1920×1200 logical.
        scale = "1.66667",
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
  },

  ["portable-mb16qhg-lenovo"] = {
    order = 3,
    required = { M.monitors.SAMSUNG_ATNA60, M.monitors.ASUS_MB16 },
    monitors = {
      {
        output = M.monitors.SAMSUNG_ATNA60,
        mode = "3200x2000@120",
        position = "0x0",
        scale = "1.66667",
        bitdepth = 10,
        supports_hdr = 1,
        supports_wide_color = 1,
        vrr = true,
        disabled = false,
      },
      {
        output = M.monitors.ASUS_MB16,
        mode = "2560x1600@119.963",
        position = "1920x0",
        scale = "1.333",
        disabled = false,
      },
      {
        output = M.monitors.ANY,
        disabled = true,
      },
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

-- Matching runs over `M._connected`, NOT `hl.get_monitor` — the Lua
-- query resolver sees only enabled outputs, so it can't observe a
-- monitor a profile has disabled (docked disables the GPD it requires).
-- We compare `desc:` selectors ourselves against the tracked set.

-- Strip a selector's `desc:` prefix to the raw description text.
---@param sel string
---@return string
local function needle_of(sel)
  return sel:sub(1, 5) == "desc:" and sel:sub(6) or sel
end

-- A description is "known" when it prefix-matches one of the selectors
-- in `M.monitors`. Only known monitors enter `_connected` — a fallback /
-- headless output or an unexpected display never gets swept into an ANY
-- disable rule.
---@param desc string
---@return boolean
local function is_known(desc)
  for _, sel in pairs(M.monitors) do
    if sel ~= M.monitors.ANY then
      local n = needle_of(sel)
      if desc:sub(1, #n) == n then
        return true
      end
    end
  end

  return false
end

-- True when a connected description prefix-matches the selector. Plain
-- (non-pattern) comparison so the literal `*`, `.`, `-` in descriptions
-- match verbatim — same shape as Hyprland's `desc:` resolver, but over
-- `_connected` (which includes monitors we disabled).
---@param sel string
---@return boolean
local function selector_present(sel)
  local n = needle_of(sel)
  for desc in pairs(M._connected) do
    if desc:sub(1, #n) == n then
      return true
    end
  end

  return false
end

---@param profile Profile
---@return boolean
local function is_profile_satisfied(profile)
  for _, req in ipairs(profile.required) do
    if not selector_present(req) then
      return false
    end
  end

  return true
end

-- Pick the most-specific (= largest required set) auto-eligible profile.
-- Ties broken by `order` (lower wins), then by name — `pairs()` order is
-- undefined, and two profiles equal on (count, order) would otherwise
-- oscillate between applies.
---@return string|nil
function M.match()
  local best
  ---@type integer, integer, string
  local best_count, best_order, best_name = -1, math.huge, ""
  for name, profile in pairs(M.profiles) do
    if not profile.on_demand and is_profile_satisfied(profile) then
      local count = #profile.required
      local order = profile.order or math.huge
      if
        count > best_count
        or (count == best_count and order < best_order)
        or (count == best_count and order == best_order and name < best_name)
      then
        best, best_count, best_order, best_name = name, count, order, name
      end
    end
  end

  return best
end

-- ── state persistence ────────────────────────────────────────────────

-- `hyprctl reload` recreates the Lua VM, wiping module state, and
-- hyprland.start does not re-fire on a running compositor. Monitors a
-- profile disabled are invisible to hl.get_monitors(), so a reload
-- would lose them for good — persist the registry in the instance
-- runtime dir instead. Scoping the file to the instance signature
-- means a fresh compositor never reads a previous session's state.
-- Format is a Lua chunk (`return { ... }`): %q handles all escaping on
-- write, loadfile is the parser, and external tools can reparse it
-- with any Lua interpreter.
local function state_path()
  local runtime = os.getenv("XDG_RUNTIME_DIR")
  local instance = os.getenv("HYPRLAND_INSTANCE_SIGNATURE")
  if not (runtime and instance) then
    return nil
  end

  return ("%s/hypr/%s/profiles.state.lua"):format(runtime, instance)
end

local function save_state()
  local path = state_path()
  if not path then
    return
  end
  local f = io.open(path, "w")
  if not f then
    return
  end
  f:write("return {\n")
  if M._active then
    f:write(("  active = %q,\n"):format(M._active))
  end
  f:write("  connected = {\n")
  for desc in pairs(M._connected) do
    f:write(("    [%q] = true,\n"):format(desc))
  end
  f:write("  },\n  disabled = {\n")
  for desc in pairs(M._disabled_by_active) do
    f:write(("    [%q] = true,\n"):format(desc))
  end
  f:write("  },\n}\n")
  f:close()
end

local function load_state()
  local path = state_path()
  if not path then
    return
  end
  local chunk = loadfile(path)
  if not chunk then
    return
  end
  local ok, state = pcall(chunk)
  if not ok or type(state) ~= "table" then
    return
  end
  M._active = state.active
  for desc in pairs(state.connected or {}) do
    M._connected[desc] = true
  end
  for desc in pairs(state.disabled or {}) do
    M._disabled_by_active[desc] = true
  end
end

-- ── apply ────────────────────────────────────────────────────────────

-- Record every connected description a `disabled` selector covers, so the
-- removed handler can tell our own disable from a physical unplug.
---@param sel string
---@param into table<string, boolean>
local function mark_disabled(sel, into)
  local n = needle_of(sel)
  for desc in pairs(M._connected) do
    if desc:sub(1, #n) == n then
      into[desc] = true
    end
  end
end

---@param name string
---@return boolean
function M.apply(name)
  local profile = M.profiles[name]
  if not profile then
    hl.exec_cmd(("notify-send -u critical display 'Unknown profile %s.'"):format(name))

    return false
  end

  ---@type table<string, boolean>
  local disabled = {}

  -- Selectors this profile targets explicitly (everything but ANY).
  ---@type table<string, boolean>
  local targeted = {}
  for _, spec in ipairs(profile.monitors) do
    if spec.output and spec.output ~= "" and spec.output ~= M.monitors.ANY then
      targeted[spec.output] = true
    end
  end

  for _, spec in ipairs(profile.monitors) do
    if spec.output == M.monitors.ANY then
      -- Expand ANY over connected known monitors not claimed by a
      -- targeted selector — one `desc:` rule each. Drawn from
      -- `_connected` so monitors a previous profile disabled are still
      -- covered (the enabled-only query would miss them).
      local claimed = {}
      for tgt in pairs(targeted) do
        local n = needle_of(tgt)
        for desc in pairs(M._connected) do
          if desc:sub(1, #n) == n then
            claimed[desc] = true
          end
        end
      end
      for desc in pairs(M._connected) do
        if not claimed[desc] then
          local expanded = { output = "desc:" .. desc }
          for k, v in pairs(spec) do
            if k ~= "output" then
              expanded[k] = v
            end
          end
          hl.monitor(expanded)
          if spec.disabled then
            disabled[desc] = true
          end
        end
      end
    else
      hl.monitor(spec)
      if spec.disabled then
        mark_disabled(spec.output, disabled)
      end
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

  -- Only disables that actually flip a monitor emit `monitor.removed`;
  -- ones the previous profile already disabled fire nothing, so they
  -- get no marker — an unconsumed marker would shield a later real
  -- unplug.
  ---@type table<string, boolean>
  local pending = {}
  for desc in pairs(disabled) do
    if not M._disabled_by_active[desc] then
      pending[desc] = true
    end
  end

  M._active = name
  M._disabled_by_active = disabled
  M._pending_removed = pending
  save_state()

  return true
end

-- Nothing matched: fall back to enabling every known connected monitor
-- at preferred/auto so no topology dead-ends dark. Also covers the
-- undock-to-disabled-panel case — the enabled-only query can't see a
-- disabled monitor, and re-enabling fires real monitor.added events
-- that re-drive matching.
function M.rescue()
  for desc in pairs(M._connected) do
    hl.monitor({ output = "desc:" .. desc, disabled = false, mode = "preferred", position = "auto" })
  end
  M._disabled_by_active = {}
  M._pending_removed = {}
  M._active = nil
  save_state()
end

---@return boolean
function M.auto_apply()
  -- On-demand stickiness: an on_demand profile (tv) is never chosen by
  -- `match`, so a manual apply must survive its own layout churn as long
  -- as it stays satisfiable.
  if M._active then
    local active = M.profiles[M._active]
    if active and active.on_demand and is_profile_satisfied(active) then
      return true
    end
  end

  local name = M.match()
  if not name then
    M.rescue()

    return false
  end
  -- Idempotent: same profile → no re-apply, no re-exec. In 0.56 a
  -- same-profile re-application is a compositor-level no-op that emits no
  -- events anyway, so even a stray call converges instead of clobbering.
  if name == M._active then
    return true
  end

  return M.apply(name)
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

-- ── event wiring ─────────────────────────────────────────────────────

-- Coalesce a burst of monitor events into one `auto_apply` after a
-- quiescence gap. Only decides *when* matching runs — never *whether*
-- `_connected` is trusted — so a slow hotplug tail (MST, EDID, CEC) costs
-- one extra converging pass, not a clobber.
local function schedule()
  if M._debounce then
    M._debounce:set_enabled(false)
  end
  M._debounce = hl.timer(function()
    M._debounce = nil
    M.auto_apply()
  end, { timeout = 300, type = "oneshot" })
end

hl.on("hyprland.start", function()
  for _, mon in ipairs(hl.get_monitors()) do
    if is_known(mon.description) then
      M._connected[mon.description] = true
    end
  end
  -- Debounced, not direct: boot-time monitors enumerate hundreds of ms
  -- apart (DP/MST), and a direct apply here would run 2-3 transitional
  -- profiles (with notify + audio execs) before converging.
  schedule()
end)
hl.on("monitor.added", function(mon)
  if is_known(mon.description) then
    M._connected[mon.description] = true
    save_state()
  end
  schedule()
end)
hl.on("monitor.removed", function(mon)
  local desc = mon.description
  if M._pending_removed[desc] then
    -- Our own rule-disable; consume the one-shot marker and keep the
    -- monitor in the registry so a profile requiring it still matches.
    M._pending_removed[desc] = nil
  else
    -- Genuine unplug — including of a monitor the profile disabled.
    M._connected[desc] = nil
    M._disabled_by_active[desc] = nil
  end
  save_state()
  schedule()
end)
-- End-of-batch signal (0.56): fired once after Hyprland finishes an
-- arrange pass. The honest "settled" edge the old fixed timer faked.
hl.on("monitor.layout_changed", function()
  schedule()
end)

-- Top level runs on cold boot (before monitors exist — the start hook
-- and added events cover those) and again on every `hyprctl reload`,
-- where it restores the registry the fresh VM lost: persisted state
-- brings back profile-disabled monitors the live query can't see, the
-- live query brings back everything else.
load_state()
for _, mon in ipairs(hl.get_monitors()) do
  if is_known(mon.description) then
    M._connected[mon.description] = true
  end
end
schedule()

return M
