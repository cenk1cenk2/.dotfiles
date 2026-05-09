-- Window Rules

hl.window_rule({
  name = "tiled-noborder-workspace",
  match = { float = false, workspace = "w[tv1]" },
  border_size = 0,
})

hl.window_rule({
  name = "floating-max-size",
  match = { float = true },
  max_size = "(monitor_w) (monitor_h)",
})

-- hl.window_rule({
--   name = "context-menu",
--   match = { class = "^()$", title = "^()$" },
--   opaque = "on",
--   float = "off",
-- })

-- hl.window_rule({
--   name = "default-float-size",
--   match = { float = true },
--   center = "on",
-- })

hl.window_rule({
  name = "save-file-dialog",
  match = { title = "^(Save File)$" },
  float = "on",
  center = "on",
})

hl.window_rule({
  name = "open-file-dialog",
  match = { title = "^(Open File|Select File|Choose File)$" },
  float = "on",
  center = "on",
})

hl.window_rule({
  name = "screen-share-dialog",
  match = { title = ".* is sharing your screen\\.$" },
  float = "on",
  center = "off",
  border_size = 0,
  opaque = "on",
})

-- hl.window_rule({
--   name = "meet",
--   match = { title = "Meet - .*$" },
--   float = "on",
--   center = "off",
--   border_size = 0,
--   opaque = "on",
-- })

hl.window_rule({
  name = "picture-in-picture",
  match = { title = "^(Picture[- ]in[- ][Pp]icture)$" },
  pin = "on",
  float = "on",
  center = "off",
  border_size = 0,
  opaque = "on",
})

-- Fullscreen inhibit idle - prevent screen lock during fullscreen
hl.window_rule({
  name = "fullscreen-idle-inhibit",
  match = { fullscreen = true },
  idle_inhibit = "fullscreen",
})

-- shell class rules

hl.window_rule({
  name = "floating-shell",
  match = { class = "floating_shell" },
  float = "on",
  border_size = 1,
  size = "(monitor_w*0.65) (monitor_h*0.65)",
})

hl.window_rule({
  name = "floating-shell-lg",
  match = { class = "floating_shell_lg" },
  float = "on",
  border_size = 1,
  size = "(monitor_w*0.75) (monitor_h*0.75)",
})

hl.window_rule({
  name = "floating-shell-portrait",
  match = { class = "floating_shell_portrait" },
  float = "on",
  border_size = 1,
  size = "(monitor_w*0.65) (monitor_h*0.75)",
})

hl.window_rule({
  name = "floating-shell-portrait-lg",
  match = { class = "floating_shell_portrait_lg" },
  float = "on",
  border_size = 1,
  size = "(monitor_w*0.75) (monitor_h*0.85)",
})

hl.window_rule({
  name = "clipse",
  match = { class = "clipse" },
  float = "on",
  border_size = 1,
  size = "(monitor_w*0.65) (monitor_h*0.65)",
  no_screen_share = "on",
})

hl.window_rule({
  name = "lxappearance",
  match = { class = "^(lxappearance)$" },
  float = "on",
})

hl.window_rule({
  name = "pamac",
  match = { class = "org.manjaro.pamac.manager" },
  float = "on",
  center = "on",
  size = "(monitor_w*0.85) (monitor_h*0.85)",
})

hl.window_rule({
  name = "yazi",
  match = { class = "yazi" },
  float = "on",
  size = "(monitor_w*0.65) (monitor_h*0.65)",
})

hl.window_rule({
  name = "numbat",
  match = { class = "numbat" },
  float = "on",
  size = "(monitor_w*0.4) (monitor_h*0.75)",
})

hl.window_rule({
  name = "file-chooser",
  match = { title = "FileChooser" },
  float = "on",
  size = "(monitor_w*0.65) (monitor_h*0.65)",
})

hl.window_rule({
  name = "brave",
  match = { class = "brave-browser" },
  opaque = "on",
})

hl.window_rule({
  name = "bitwarden",
  match = { class = "Bitwarden" },
  float = "on",
  size = "(monitor_w*0.75) (monitor_h*0.70)",
  no_screen_share = "on",
})

hl.window_rule({
  name = "brave-bitwarden",
  match = { class = "^(brave-)(.*)$", title = "Bitwarden" },
  float = "on",
  no_screen_share = "on",
})

hl.window_rule({
  name = "brave-whatsapp",
  match = { class = "^(brave-)(.*)$", title = "WhatsApp Web" },
  no_screen_share = "on",
})

hl.window_rule({
  name = "1password",
  match = { class = "1Password" },
  float = "on",
  size = "(monitor_w*0.75) (monitor_h*0.70)",
  no_screen_share = "on",
})

hl.window_rule({
  name = "obsidian",
  match = { class = "obsidian" },
  float = "on",
  size = "(monitor_w*0.95) (monitor_h*0.95)",
  opaque = "on",
})

hl.window_rule({
  name = "zathura",
  match = { class = "org.pwmt.zathura" },
  float = "on",
  border_size = 1,
  opaque = "on",
  size = "(monitor_w*0.50) (monitor_h*0.95)",
})

hl.window_rule({
  name = "swayimg",
  match = { class = "swayimg" },
  float = "on",
  border_size = 0,
  opaque = "on",
  size = "(monitor_w*0.95) (monitor_h*0.95)",
})

hl.window_rule({
  name = "mpv",
  match = { class = "mpv" },
  opaque = "on",
})

hl.window_rule({
  name = "virt-manager",
  match = { class = "virt-manager" },
  opaque = "on",
})

hl.window_rule({
  name = "zenity",
  match = { class = "^(zenity)$" },
  float = "on",
  size = "(monitor_w*0.75) (monitor_h*0.95)",
})

hl.window_rule({
  name = "hyprpwcenter",
  match = { class = "^(hyprpwcenter)$" },
  float = "on",
  size = "(monitor_w*0.75) (monitor_h*0.95)",
})

hl.window_rule({
  name = "hyprland-share-picker",
  match = { class = "^(hyprland-share-picker)$" },
  float = "on",
  size = "(monitor_w*0.50) (monitor_h*0.50)",
})

hl.window_rule({
  name = "steam",
  match = { class = "steam" },
  opaque = "on",
})
