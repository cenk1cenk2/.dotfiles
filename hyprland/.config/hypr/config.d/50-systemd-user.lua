-- Systemd User Environment
--
-- Import environment variables for systemd user session. Has to be
-- module-top — hl.on("hyprland.start", ...) handlers registered
-- during the initial config load do not fire in 0.55.

hl.exec_cmd(
  "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP QT_QPA_PLATFORM QT_QPA_PLATFORMTHEME QT_WAYLAND_DISABLE_WINDOWDECORATION"
)

hl.exec_cmd(
  "dbus-update-activation-environment --systemd DISPLAY WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP QT_QPA_PLATFORM QT_QPA_PLATFORMTHEME QT_WAYLAND_DISABLE_WINDOWDECORATION"
)
