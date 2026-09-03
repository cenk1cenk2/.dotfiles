-- Compositor-level event handlers.

-- Tablet input follows the focused monitor. Replaces the old
-- hyprland-listener.py + systemd service which polled the .socket2
-- IPC just to keep `input:tablet:output` in sync.
hl.on("monitor.focused", function(monitor)
  hl.config({ input = { tablet = { output = monitor.name } } })
end)

-- Lid open turns the panels back on. A lid-open is a switch event, not
-- input, so nothing else does it: hypridle's dpms-on and brightness
-- restore both hang off on-resume, which waits for input.
--
-- switch:off is the OPENING edge. locked = true because hyprlock is up.
-- dpms takes a table: the string form discards the arg and toggles, so
-- `dpms("on")` blanks an already-lit panel.
hl.bind("switch:off:Lid Switch", function()
  hl.dispatch(hl.dsp.dpms({ action = "on" }))
  hl.exec_cmd("brightnessctl -r")
end, { locked = true })
