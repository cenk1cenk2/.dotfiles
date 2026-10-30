-- Swayimg configuration file.
-- Migrated from old INI format to Lua (v5.2).

--------------------------------------------------------------------------------
-- General configuration
--------------------------------------------------------------------------------
swayimg.mode = "viewer"
swayimg.antialiasing = true

--------------------------------------------------------------------------------
-- Image list configuration
--------------------------------------------------------------------------------
swayimg.imagelist.order = "mtime"
swayimg.imagelist.recursive = false
swayimg.imagelist.adjacent = true

--------------------------------------------------------------------------------
-- Text overlay / font configuration
--------------------------------------------------------------------------------
swayimg.text.font = "Segoe UI"
swayimg.text.size = 16
swayimg.text.color = 0xffefefef
swayimg.text.shadow = 0xa0000000
swayimg.text.timeout = 5
swayimg.text.status_timeout = 3

--------------------------------------------------------------------------------
-- Viewer mode configuration
--------------------------------------------------------------------------------
swayimg.viewer.set_window_background(0x00000000)
swayimg.viewer.default_scale = "fit"
swayimg.viewer.autopos = "center"
swayimg.viewer.loop = true
swayimg.viewer.history = 1
swayimg.viewer.preload = 1

swayimg.viewer.text = {
  topleft = {
    "File:\t{name}",
    "Format:\t{format}",
    "File size:\t{sizehr}",
    "Image size:\t{frame.width}x{frame.height}",
    "EXIF date:\t{meta.Exif.Photo.DateTimeOriginal}",
    "EXIF camera:\t{meta.Exif.Image.Model}",
  },
  topright = {
    "{list.index} of {list.total}",
  },
  bottomleft = {
    "Scale: {scale}",
    "Frame: {frame.index}/{frame.total}",
  },
}

--------------------------------------------------------------------------------
-- Slideshow configuration
--------------------------------------------------------------------------------
swayimg.slideshow.timeout = 3

--------------------------------------------------------------------------------
-- Gallery mode configuration
--------------------------------------------------------------------------------
swayimg.gallery.thumb_size = 480
swayimg.gallery.cache = 100
swayimg.gallery.aspect = "fill"
swayimg.gallery.window_color = 0x00000000
swayimg.gallery.unselected_color = 0xff282c34
swayimg.gallery.selected_color = 0xff4b5263
swayimg.gallery.border_color = 0xffe5c07b

swayimg.gallery.text = {
  bottomright = {
    "{name}",
  },
}

--------------------------------------------------------------------------------
-- Helpers
--------------------------------------------------------------------------------
local function step(dx, dy)
  local wnd = swayimg.get_window_size()
  local pos = swayimg.viewer.get_position()
  swayimg.viewer.set_abs_position(math.floor(pos.x + wnd.width * dx / 100), math.floor(pos.y + wnd.height * dy / 100))
end

local function zoom(pct)
  local scale = swayimg.viewer.scale
  swayimg.viewer.set_abs_scale(scale + scale * pct / 100)
end

local function toggle_info()
  swayimg.text.visible = not swayimg.text.visible
end

local function prev_frame()
  local frame = swayimg.viewer.frame
  if frame > 0 then
    swayimg.viewer.frame = frame - 1
  end
end

local function next_frame()
  local frame = swayimg.viewer.frame
  if frame + 1 < swayimg.viewer.get_image().frames then
    swayimg.viewer.frame = frame + 1
  end
end

local function skip_file()
  local image = swayimg.viewer.get_image()
  swayimg.imagelist.remove(image.path)
end

--------------------------------------------------------------------------------
-- Signal handlers
--------------------------------------------------------------------------------
swayimg.viewer.on_signal("USR1", function()
  swayimg.viewer.reload()
end)
swayimg.viewer.on_signal("USR2", function()
  swayimg.viewer.open("next")
end)

--------------------------------------------------------------------------------
-- Viewer mode key bindings
--------------------------------------------------------------------------------
swayimg.viewer.on_key("F1", function()
  toggle_info()
end)
swayimg.viewer.on_key({ "Home", "g" }, function()
  swayimg.viewer.open("first")
end)
swayimg.viewer.on_key({ "End", "Shift-g" }, function()
  swayimg.viewer.open("last")
end)
swayimg.viewer.on_key("p", function()
  swayimg.viewer.open("prev")
end)
swayimg.viewer.on_key("n", function()
  swayimg.viewer.open("next")
end)
swayimg.viewer.on_key("Shift-n", function()
  swayimg.viewer.open("random")
end)
swayimg.viewer.on_key("Shift-d", function()
  swayimg.viewer.open("prev_dir")
end)
swayimg.viewer.on_key("d", function()
  swayimg.viewer.open("next_dir")
end)
swayimg.viewer.on_key("Shift-o", function()
  prev_frame()
end)
swayimg.viewer.on_key("o", function()
  next_frame()
end)
swayimg.viewer.on_key("c", function()
  skip_file()
end)
swayimg.viewer.on_key("Shift-s", function()
  swayimg.mode = "slideshow"
end)
swayimg.viewer.on_key("s", function()
  swayimg.viewer.animation = not swayimg.viewer.animation
end)
swayimg.viewer.on_key("f", function()
  swayimg.fullscreen = not swayimg.fullscreen
end)
swayimg.viewer.on_key("Return", function()
  swayimg.mode = "gallery"
end)

-- Arrow keys / hjkl: pan image
swayimg.viewer.on_key({ "Left", "h" }, function()
  step(12, 0)
end)
swayimg.viewer.on_key({ "Right", "l" }, function()
  step(-12, 0)
end)
swayimg.viewer.on_key({ "Up", "k" }, function()
  step(0, 12)
end)
swayimg.viewer.on_key({ "Down", "j" }, function()
  step(0, -12)
end)

-- Shift+arrows / Shift+hjkl: file navigation / zoom
swayimg.viewer.on_key({ "Shift-Left", "Shift-h" }, function()
  swayimg.viewer.open("prev")
end)
swayimg.viewer.on_key({ "Shift-Right", "Shift-l" }, function()
  swayimg.viewer.open("next")
end)
swayimg.viewer.on_key({ "Shift-Up", "Shift-k" }, function()
  zoom(12)
end)
swayimg.viewer.on_key({ "Shift-Down", "Shift-j" }, function()
  zoom(-12)
end)

-- Zoom keys
swayimg.viewer.on_key("equal", function()
  zoom(12)
end)
swayimg.viewer.on_key("plus", function()
  zoom(12)
end)
swayimg.viewer.on_key("minus", function()
  zoom(-12)
end)
swayimg.viewer.on_key("w", function()
  swayimg.viewer.set_fix_scale("width")
end)
swayimg.viewer.on_key("Shift-w", function()
  swayimg.viewer.set_fix_scale("height")
end)
swayimg.viewer.on_key("z", function()
  swayimg.viewer.set_fix_scale("fit")
end)
swayimg.viewer.on_key("Shift-z", function()
  swayimg.viewer.set_fix_scale("fill")
end)
swayimg.viewer.on_key("0", function()
  swayimg.viewer.set_fix_scale("real")
end)
swayimg.viewer.on_key("BackSpace", function()
  swayimg.viewer.set_fix_scale("optimal")
end)

-- Transform
swayimg.viewer.on_key("bracketleft", function()
  swayimg.viewer.rotate(270)
end)
swayimg.viewer.on_key("bracketright", function()
  swayimg.viewer.rotate(90)
end)
swayimg.viewer.on_key("m", function()
  swayimg.viewer.flip_vertical()
end)
swayimg.viewer.on_key("Shift-m", function()
  swayimg.viewer.flip_horizontal()
end)

-- Misc
swayimg.viewer.on_key("a", function()
  swayimg.antialiasing = not swayimg.antialiasing
end)
swayimg.viewer.on_key("r", function()
  swayimg.viewer.reload()
end)
swayimg.viewer.on_key("i", function()
  toggle_info()
end)
swayimg.viewer.on_key("Shift-Delete", function()
  local image = swayimg.viewer.get_image()
  os.remove(image.path)
  swayimg.imagelist.remove(image.path)
  swayimg.text.status = "Deleted: " .. image.path
end)
swayimg.viewer.on_key("Escape", function()
  swayimg.exit()
end)
swayimg.viewer.on_key("q", function()
  swayimg.exit()
end)

--------------------------------------------------------------------------------
-- Viewer mode scroll bindings
--------------------------------------------------------------------------------
-- Negative vertical is scroll up, negative horizontal is scroll left.
swayimg.viewer.on_scroll(function(kmods, horizontal, vertical)
  if horizontal and horizontal ~= 0 then
    step(horizontal < 0 and -12 or 12, 0)
  end
  if not vertical or vertical == 0 then
    return
  end
  local up = vertical < 0
  if kmods == "Ctrl" then
    step(0, up and 12 or -12)
  elseif kmods == "Shift" then
    swayimg.viewer.open(up and "prev" or "next")
  elseif kmods == "Alt" then
    if up then
      prev_frame()
    else
      next_frame()
    end
  else
    zoom(up and 12 or -12)
  end
end)

--------------------------------------------------------------------------------
-- Gallery mode key bindings
--------------------------------------------------------------------------------
swayimg.gallery.on_key("F1", function()
  toggle_info()
end)
swayimg.gallery.on_key({ "Home", "g" }, function()
  swayimg.gallery.select("first")
end)
swayimg.gallery.on_key({ "End", "Shift-g" }, function()
  swayimg.gallery.select("last")
end)
swayimg.gallery.on_key({ "Left", "h" }, function()
  swayimg.gallery.select("left")
end)
swayimg.gallery.on_key({ "Right", "l" }, function()
  swayimg.gallery.select("right")
end)
swayimg.gallery.on_key({ "Up", "k" }, function()
  swayimg.gallery.select("up")
end)
swayimg.gallery.on_key({ "Down", "j" }, function()
  swayimg.gallery.select("down")
end)
swayimg.gallery.on_key({ "Prior", "Ctrl-u" }, function()
  swayimg.gallery.select("pgup")
end)
swayimg.gallery.on_key({ "Next", "Ctrl-d" }, function()
  swayimg.gallery.select("pgdown")
end)
swayimg.gallery.on_key("c", function()
  local image = swayimg.gallery.get_image()
  swayimg.imagelist.remove(image.path)
end)
swayimg.gallery.on_key("f", function()
  swayimg.fullscreen = not swayimg.fullscreen
end)
swayimg.gallery.on_key("Return", function()
  swayimg.mode = "viewer"
end)
swayimg.gallery.on_key("a", function()
  swayimg.antialiasing = not swayimg.antialiasing
end)
swayimg.gallery.on_key("r", function()
  swayimg.gallery.reload()
end)
swayimg.gallery.on_key("i", function()
  toggle_info()
end)
swayimg.gallery.on_key("Shift-Delete", function()
  local image = swayimg.gallery.get_image()
  os.remove(image.path)
  swayimg.imagelist.remove(image.path)
  swayimg.text.status = "Deleted: " .. image.path
end)
swayimg.gallery.on_key("Escape", function()
  swayimg.exit()
end)
swayimg.gallery.on_key("q", function()
  swayimg.exit()
end)

--------------------------------------------------------------------------------
-- Gallery mode scroll bindings
--------------------------------------------------------------------------------
swayimg.gallery.on_scroll(function(_, horizontal, vertical)
  if horizontal and horizontal ~= 0 then
    swayimg.gallery.select(horizontal < 0 and "right" or "left")
  end
  if vertical and vertical ~= 0 then
    swayimg.gallery.select(vertical < 0 and "up" or "down")
  end
end)
