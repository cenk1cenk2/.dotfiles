-- Compositor-level event handlers.

-- Tablet input follows the focused monitor. Replaces the old
-- hyprland-listener.py + systemd service which polled the .socket2
-- IPC just to keep `input:tablet:output` in sync.
hl.on("monitor.focused", function(monitor)
  hl.config({ input = { tablet = { output = monitor.name } } })
end)
