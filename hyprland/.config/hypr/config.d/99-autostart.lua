-- Autostart Applications

local services = {
  "hyprpolkitagent.service",
  "hyprpaper.service",
  "hypridle.service",
  "swaync.service",
  "swayosd.service",
  "kanshi.service",
  "clipse.service",
  "wl-gammarelay-rs.service",
  "playerctl-waybar.service",
  "poweralertd.service",
  "input-remapper-autoload.service",
  "ydotool.service",
  "wayland-pipewire-idle-inhibit.service",
}

hl.on("hyprland.start", function()
  hl.exec_cmd("uwsm finalize; systemctl --user start --no-block " .. table.concat(services, " "))
end)
