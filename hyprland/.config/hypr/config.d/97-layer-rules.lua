local screen_share = require("screen-share")

screen_share.layer("swaync-control-center", { namespace = "swaync-control-center" })
screen_share.layer("swaync-notification-window", { namespace = "swaync-notification-window" })

-- Hints go the moment their submap ends, not over a fade, so a screenshot
-- taken from the submap never catches them on the way out.
hl.layer_rule({
  name = "overlay-hints",
  match = { namespace = "overlay-hints" },
  no_anim = true,
})
