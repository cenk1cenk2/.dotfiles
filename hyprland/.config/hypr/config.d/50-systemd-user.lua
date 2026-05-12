-- Systemd User Environment

-- Import environment variables for systemd user session
hl.on("hyprland.start", function()
  hl.exec_cmd(
    "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP QT_QPA_PLATFORM QT_QPA_PLATFORMTHEME QT_WAYLAND_DISABLE_WINDOWDECORATION"
  )
end)

hl.on("hyprland.start", function()
  hl.exec_cmd(
    "dbus-update-activation-environment --systemd DISPLAY WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP QT_QPA_PLATFORM QT_QPA_PLATFORMTHEME QT_WAYLAND_DISABLE_WINDOWDECORATION"
  )
end)
