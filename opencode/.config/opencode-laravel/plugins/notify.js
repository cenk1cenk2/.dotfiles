// Desktop notification plus terminal bell on the two moments worth looking up
// for. opencode's config schema carries no notification keys at all, so unlike
// codex ([tui] notification_method) and claude ([hooks] + preferredNotifChannel)
// a plugin is the only route here — the bell included, which is why it is
// written to /dev/tty by hand rather than configured.
//
// Loaded because hyprpilot points OPENCODE_CONFIG_DIR at this directory, which
// is searched for plugins just like a project-local .opencode/.

const PROFILE = "laravel"

export const NotifyPlugin = async ({ $, directory }) => {
  const notify = async (body) => {
    const dir = directory.split("/").filter(Boolean).pop() ?? directory
    const title = `opencode (${PROFILE}) — ${dir}`

    // nothrow throughout: a missing daemon or a detached tty must never take
    // down the session that triggered the notification.
    await $`notify-send -a opencode -i utilities-terminal -u normal ${title} ${body}`.nothrow().quiet()
    await $`printf '\a' > /dev/tty`.nothrow().quiet()
  }

  return {
    "session.idle": async () => {
      await notify("Turn finished")
    },
    "permission.asked": async () => {
      await notify("Waiting for your approval")
    },
  }
}
