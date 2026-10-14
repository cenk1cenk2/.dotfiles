hl.layer_rule({
  name = "swaync-control-center",
  match = { namespace = "swaync-control-center" },
  no_screen_share = true,
})

hl.layer_rule({
  name = "swaync-notification-window",
  match = { namespace = "swaync-notification-window" },
  no_screen_share = true,
})

-- Hints go the moment their submap ends, not over a fade, so a screenshot
-- taken from the submap never catches them on the way out.
hl.layer_rule({
  name = "overlay-hints",
  match = { namespace = "overlay-hints" },
  no_anim = true,
})
