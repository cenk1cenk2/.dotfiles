-- Application-specific key bindings: each acts in one app's window and hands
-- the same shortcut back to every other window.

local d = require("definitions")

-- Amp's own dictation goes through speech.py, so the room is quieted for the
-- take and handed back when Amp stops recording, however it stops.
-- send_shortcut reaches the window directly, past the binds, so neither
-- speech.py's Ctrl+Shift+D to Amp nor the replay lands back here.
hl.bind("CTRL + SHIFT + D", function()
  local active = hl.get_active_window()
  if not active then
    return
  end
  if active.class == "amp-desktop" then
    hl.exec_cmd(("%s stt toggle --target amp"):format(d.speech))
    return
  end
  hl.dispatch(hl.dsp.send_shortcut({ mods = "CTRL SHIFT", key = "d", window = "address:" .. active.address }))
end)
