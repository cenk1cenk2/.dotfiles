-- Swayimg configuration file.
-- Migrated from old INI format to Lua (v5.2).

--------------------------------------------------------------------------------
-- General configuration
--------------------------------------------------------------------------------
swayimg.set_mode("viewer")
swayimg.enable_antialiasing(true)

--------------------------------------------------------------------------------
-- Image list configuration
--------------------------------------------------------------------------------
swayimg.imagelist.set_order("mtime")
swayimg.imagelist.enable_recursive(false)
swayimg.imagelist.enable_adjacent(true)

--------------------------------------------------------------------------------
-- Text overlay / font configuration
--------------------------------------------------------------------------------
swayimg.text.set_font("Segoe UI")
swayimg.text.set_size(14)
swayimg.text.set_foreground(0xffefefef)
swayimg.text.set_shadow(0xa0000000)
swayimg.text.set_timeout(5)
swayimg.text.set_status_timeout(3)

--------------------------------------------------------------------------------
-- Viewer mode configuration
--------------------------------------------------------------------------------
swayimg.viewer.set_window_background(0x00000000)
swayimg.viewer.set_default_scale("fit")
swayimg.viewer.enable_centering(true)
swayimg.viewer.enable_loop(true)
swayimg.viewer.limit_history(1)
swayimg.viewer.limit_preload(1)

swayimg.viewer.set_text("topleft", {
  "File:\t{name}",
  "Format:\t{format}",
  "File size:\t{sizehr}",
  "Image size:\t{frame.width}x{frame.height}",
  "EXIF date:\t{meta.Exif.Photo.DateTimeOriginal}",
  "EXIF camera:\t{meta.Exif.Image.Model}",
})
swayimg.viewer.set_text("topright", {
  "{list.index} of {list.total}",
})
swayimg.viewer.set_text("bottomleft", {
  "Scale: {scale}",
  "Frame: {frame.index}/{frame.total}",
})

--------------------------------------------------------------------------------
-- Slideshow configuration
--------------------------------------------------------------------------------
swayimg.slideshow.set_timeout(3)

--------------------------------------------------------------------------------
-- Gallery mode configuration
--------------------------------------------------------------------------------
swayimg.gallery.set_thumb_size(480)
swayimg.gallery.limit_cache(100)
swayimg.gallery.set_aspect("fill")
swayimg.gallery.set_window_color(0x00000000)
swayimg.gallery.set_unselected_color(0xff282c34)
swayimg.gallery.set_selected_color(0xff4b5263)
swayimg.gallery.set_border_color(0xffe5c07b)

swayimg.gallery.set_text("bottomright", {
  "{name}",
})

--------------------------------------------------------------------------------
-- Helpers
--------------------------------------------------------------------------------
local aa_enabled = true

local function step(dx, dy)
  local wnd = swayimg.get_window_size()
  local pos = swayimg.viewer.get_position()
  swayimg.viewer.set_abs_position(
    math.floor(pos.x + wnd.width * dx / 100),
    math.floor(pos.y + wnd.height * dy / 100)
  )
end

local function zoom(pct)
  local scale = swayimg.viewer.get_scale()
  swayimg.viewer.set_abs_scale(scale + scale * pct / 100)
end

local function toggle_info()
  if swayimg.text.visible() then
    swayimg.text.hide()
  else
    swayimg.text.show()
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
  swayimg.viewer.switch_image("next")
end)

--------------------------------------------------------------------------------
-- Viewer mode key bindings
--------------------------------------------------------------------------------
swayimg.viewer.on_key("F1", function() toggle_info() end)
swayimg.viewer.on_key("Home", function() swayimg.viewer.switch_image("first") end)
swayimg.viewer.on_key("End", function() swayimg.viewer.switch_image("last") end)
swayimg.viewer.on_key("p", function() swayimg.viewer.switch_image("prev") end)
swayimg.viewer.on_key("n", function() swayimg.viewer.switch_image("next") end)
swayimg.viewer.on_key("Shift-n", function() swayimg.viewer.switch_image("random") end)
swayimg.viewer.on_key("Shift-d", function() swayimg.viewer.switch_image("prev_dir") end)
swayimg.viewer.on_key("d", function() swayimg.viewer.switch_image("next_dir") end)
swayimg.viewer.on_key("Shift-o", function() swayimg.viewer.prev_frame() end)
swayimg.viewer.on_key("o", function() swayimg.viewer.next_frame() end)
swayimg.viewer.on_key("c", function() skip_file() end)
swayimg.viewer.on_key("Shift-s", function() swayimg.set_mode("slideshow") end)
swayimg.viewer.on_key("s", function() swayimg.viewer.set_animation() end)
swayimg.viewer.on_key("f", function() swayimg.set_fullscreen() end)
swayimg.viewer.on_key("Return", function() swayimg.set_mode("gallery") end)

-- Arrow keys: pan image
swayimg.viewer.on_key("Left", function() step(12, 0) end)
swayimg.viewer.on_key("Right", function() step(-12, 0) end)
swayimg.viewer.on_key("Up", function() step(0, 12) end)
swayimg.viewer.on_key("Down", function() step(0, -12) end)

-- Shift+arrows: file navigation / zoom
swayimg.viewer.on_key("Shift-Left", function() swayimg.viewer.switch_image("prev") end)
swayimg.viewer.on_key("Shift-Right", function() swayimg.viewer.switch_image("next") end)
swayimg.viewer.on_key("Shift-Up", function() zoom(12) end)
swayimg.viewer.on_key("Shift-Down", function() zoom(-12) end)

-- Zoom keys
swayimg.viewer.on_key("equal", function() zoom(12) end)
swayimg.viewer.on_key("plus", function() zoom(12) end)
swayimg.viewer.on_key("minus", function() zoom(-12) end)
swayimg.viewer.on_key("w", function() swayimg.viewer.set_fix_scale("width") end)
swayimg.viewer.on_key("Shift-w", function() swayimg.viewer.set_fix_scale("height") end)
swayimg.viewer.on_key("z", function() swayimg.viewer.set_fix_scale("fit") end)
swayimg.viewer.on_key("Shift-z", function() swayimg.viewer.set_fix_scale("fill") end)
swayimg.viewer.on_key("0", function() swayimg.viewer.set_fix_scale("real") end)
swayimg.viewer.on_key("BackSpace", function() swayimg.viewer.set_fix_scale("optimal") end)

-- Transform
swayimg.viewer.on_key("bracketleft", function() swayimg.viewer.rotate(270) end)
swayimg.viewer.on_key("bracketright", function() swayimg.viewer.rotate(90) end)
swayimg.viewer.on_key("m", function() swayimg.viewer.flip_vertical() end)
swayimg.viewer.on_key("Shift-m", function() swayimg.viewer.flip_horizontal() end)

-- Misc
swayimg.viewer.on_key("a", function()
  aa_enabled = not aa_enabled
  swayimg.enable_antialiasing(aa_enabled)
end)
swayimg.viewer.on_key("r", function() swayimg.viewer.reload() end)
swayimg.viewer.on_key("i", function() toggle_info() end)
swayimg.viewer.on_key("Shift-Delete", function()
  local image = swayimg.viewer.get_image()
  os.remove(image.path)
  swayimg.imagelist.remove(image.path)
  swayimg.text.set_status("Deleted: " .. image.path)
end)
swayimg.viewer.on_key("Escape", function() swayimg.exit() end)
swayimg.viewer.on_key("q", function() swayimg.exit() end)

--------------------------------------------------------------------------------
-- Viewer mode mouse bindings
--------------------------------------------------------------------------------
swayimg.viewer.on_mouse("ScrollUp", function() zoom(12) end)
swayimg.viewer.on_mouse("ScrollDown", function() zoom(-12) end)
swayimg.viewer.on_mouse("ScrollLeft", function() step(-12, 0) end)
swayimg.viewer.on_mouse("ScrollRight", function() step(12, 0) end)
swayimg.viewer.on_mouse("Ctrl-ScrollUp", function() step(0, 12) end)
swayimg.viewer.on_mouse("Ctrl-ScrollDown", function() step(0, -12) end)
swayimg.viewer.on_mouse("Shift-ScrollUp", function() swayimg.viewer.switch_image("prev") end)
swayimg.viewer.on_mouse("Shift-ScrollDown", function() swayimg.viewer.switch_image("next") end)
swayimg.viewer.on_mouse("Alt-ScrollUp", function() swayimg.viewer.prev_frame() end)
swayimg.viewer.on_mouse("Alt-ScrollDown", function() swayimg.viewer.next_frame() end)

--------------------------------------------------------------------------------
-- Gallery mode key bindings
--------------------------------------------------------------------------------
swayimg.gallery.on_key("F1", function() toggle_info() end)
swayimg.gallery.on_key("Home", function() swayimg.gallery.switch_image("first") end)
swayimg.gallery.on_key("End", function() swayimg.gallery.switch_image("last") end)
swayimg.gallery.on_key("Left", function() swayimg.gallery.switch_image("left") end)
swayimg.gallery.on_key("Right", function() swayimg.gallery.switch_image("right") end)
swayimg.gallery.on_key("Up", function() swayimg.gallery.switch_image("up") end)
swayimg.gallery.on_key("Down", function() swayimg.gallery.switch_image("down") end)
swayimg.gallery.on_key("Prior", function() swayimg.gallery.switch_image("pgup") end)
swayimg.gallery.on_key("Next", function() swayimg.gallery.switch_image("pgdown") end)
swayimg.gallery.on_key("c", function()
  local image = swayimg.gallery.get_image()
  swayimg.imagelist.remove(image.path)
end)
swayimg.gallery.on_key("f", function() swayimg.set_fullscreen() end)
swayimg.gallery.on_key("Return", function() swayimg.set_mode("viewer") end)
swayimg.gallery.on_key("a", function()
  aa_enabled = not aa_enabled
  swayimg.enable_antialiasing(aa_enabled)
end)
swayimg.gallery.on_key("r", function()
  swayimg.viewer.reload()
end)
swayimg.gallery.on_key("i", function() toggle_info() end)
swayimg.gallery.on_key("Shift-Delete", function()
  local image = swayimg.gallery.get_image()
  os.remove(image.path)
  swayimg.imagelist.remove(image.path)
  swayimg.text.set_status("Deleted: " .. image.path)
end)
swayimg.gallery.on_key("Escape", function() swayimg.exit() end)
swayimg.gallery.on_key("q", function() swayimg.exit() end)

--------------------------------------------------------------------------------
-- Gallery mode mouse bindings
--------------------------------------------------------------------------------
swayimg.gallery.on_mouse("ScrollLeft", function() swayimg.gallery.switch_image("right") end)
swayimg.gallery.on_mouse("ScrollRight", function() swayimg.gallery.switch_image("left") end)
swayimg.gallery.on_mouse("ScrollUp", function() swayimg.gallery.switch_image("up") end)
swayimg.gallery.on_mouse("ScrollDown", function() swayimg.gallery.switch_image("down") end)
