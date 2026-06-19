local build_dir = yay.opt.build_dir
  or ((os.getenv("XDG_CACHE_HOME") or (os.getenv("HOME") .. "/.cache")) .. "/yay")
local cache_file = build_dir .. "/maintainer_cache"

local function load_cache()
  local cache = {}
  local f = io.open(cache_file, "r")
  if not f then
    return cache
  end

  for line in f:lines() do
    local name, maintainer = line:match("^([^=]+)=(.*)$")
    if name then
      cache[name] = maintainer
    end
  end
  f:close()

  return cache
end

local function save_cache(cache)
  local names = {}
  for name, _ in pairs(cache) do
    names[#names + 1] = name
  end
  table.sort(names)

  local f = assert(io.open(cache_file, "w"))
  for _, name in ipairs(names) do
    f:write(name .. "=" .. cache[name] .. "\n")
  end
  f:close()
end

yay.create_autocmd("UpgradeSelect", {
  desc = "warn on AUR maintainer changes",
  callback = function(event)
    yay.log.info("checking AUR maintainer changes")

    local cache = load_cache()
    local dirty = false

    for _, pkg in ipairs(event.data.upgrades) do
      if pkg.repository == "aur" and pkg.maintainer ~= "" then
        local cached = cache[pkg.name]
        if cached == nil then
          cache[pkg.name] = pkg.maintainer
          dirty = true
        elseif cached == pkg.maintainer then
          yay.log.debug("maintainer unchanged:", pkg.name, pkg.maintainer)
        else
          yay.log.error("new maintainer, double check build files:", pkg.name, "(was: " .. cached .. ", now: " .. pkg.maintainer .. ")")
          cache[pkg.name] = pkg.maintainer
          dirty = true
        end
      end
    end

    if dirty then
      yay.log.info("saving maintainer cache:", cache_file)
      save_cache(cache)
    end

    return { exclude = {}, skip_menu = false }
  end,
})
