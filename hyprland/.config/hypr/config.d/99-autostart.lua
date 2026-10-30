-- Autostart Applications

local services = {
  "hyprpolkitagent.service",
  "hyprpaper.service",
  "hypridle.service",
  "swaync.service",
  "swayosd.service",
  "overlay.service",
  "kanshi.service",
  "clipse.service",
  "poweralertd.service",
  "wayland-pipewire-idle-inhibit.service",
  "pimsync.service",
  "tailscale-systray.service",
}

hl.on("hyprland.start", function()
  -- one start per unit: a single missing unit fails the whole batch enqueue
  hl.exec_cmd(
    ("uwsm finalize; for unit in %s; do systemctl --user start --no-block \"$unit\"; done"):format(
      table.concat(services, " ")
    )
  )
end)
