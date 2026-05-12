-- Autostart Applications
--
-- Note: hl.on("hyprland.start", ...) handlers registered during the
-- initial config load do not fire in 0.55 (the event is dispatched
-- before the config chunk finishes). Module-top hl.exec_cmd is the
-- only reliable way to autostart commands, even ones we'd ideally
-- run exec-once. systemctl start/restart is idempotent so reload
-- re-execution is safe.

-- Session target
hl.exec_cmd("systemctl --user start hyprland-session.service")
hl.exec_cmd("systemctl --user start hyprpolkitagent.service")

-- Daemons that should restart with each Hyprland load
hl.exec_cmd("systemctl --user restart hyprpaper.service")
hl.exec_cmd("systemctl --user restart hypridle.service")
hl.exec_cmd("systemctl --user restart swaync.service")
hl.exec_cmd("systemctl --user restart swayosd.service")
hl.exec_cmd("systemctl --user restart hyprland-autoname-workspaces.service")
hl.exec_cmd("systemctl --user restart kanshi.service")
hl.exec_cmd("systemctl --user restart waybar@hyprland.service")

-- One-shot services (idempotent under `start`)
-- hl.exec_cmd("systemctl --user start dex.service")
hl.exec_cmd("systemctl --user start clipse.service")
hl.exec_cmd("systemctl --user start wl-gammarelay-rs.service")
hl.exec_cmd("systemctl --user start playerctl-waybar.service")
hl.exec_cmd("systemctl --user start poweralertd.service")
hl.exec_cmd("systemctl --user start input-remapper-autoload.service")
hl.exec_cmd("systemctl --user start ydotool.service")
hl.exec_cmd("systemctl --user start hyprland-listener.service")
