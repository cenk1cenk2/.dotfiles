-- Input configuration - Touchpad and Mouse

hl.config({
  input = {
    accel_profile = "flat",
    sensitivity = 0.15,
    scroll_factor = 1.5,

    touchpad = {
      disable_while_typing = false,
      tap_to_click = true,
      drag_lock = true,
      natural_scroll = false,
    },
  },
})

-- Device-specific configurations

-- input-remapper forwards the grabbed mouse through a copy with the same name;
-- it takes "-2" or "-3" depending on whether its keyboard copy came up first
for _, name in ipairs({ "--mouse-for-windows-2", "--mouse-for-windows-3" }) do
  hl.device({
    name = name,
    accel_profile = "custom 1 0 2",
    scroll_factor = 1.5,
  })
end

hl.device({
  name = "orbit-bt5.0-mouse",
  middle_button_emulation = true,
  sensitivity = 0.0,
})

-- emanet touchpad
hl.device({
  name = "pnp0c50:00-093a:0255-touchpad",
  accel_profile = "flat",
  sensitivity = 0.5,
  scroll_factor = 1.0,
})

-- lenovo touchpad
hl.device({
  name = "wacf2205:00-04f3:3355-touchpad",
  accel_profile = "flat",
  sensitivity = 0.4,
  scroll_factor = 0.5,
})

hl.device({
  name = "kensington-orbit-wireless-tb-mouse",
  middle_button_emulation = true,
  sensitivity = 0.0,
})

hl.device({
  name = "kensington-slimblade-pro(2.4ghz-receiver)-kensington-slimblade-pro-trackball(2.4ghz-receiver)",
  middle_button_emulation = true,
  sensitivity = 0.0,
})
