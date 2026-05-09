hl.layer_rule({
  name = "swaync-control-center",
  match = { namespace = "swaync-control-center" },
  no_screen_share = "on",
})

hl.layer_rule({
  name = "swaync-notification-window",
  match = { namespace = "swaync-notification-window" },
  no_screen_share = "on",
})
