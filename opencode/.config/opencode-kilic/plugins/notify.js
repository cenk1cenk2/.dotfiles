// Forwards the two moments worth looking up for to the shared agent notify
// hook, wayland/scripts/notify.py, in codex's hook shape: it chimes, rings the
// bell, pops a popup that focuses this tmux pane, and lists the session for
// the Stream Deck's agent keys. opencode has no hooks of its own, so a plugin
// is the only route.
//
// Loaded because hyprpilot points OPENCODE_CONFIG_DIR at this directory, which
// is searched for plugins just like a project-local .opencode/.

const PROFILE = "kilic"
const NOTIFY = `${process.env.HOME}/.config/wayland/scripts/notify.py`

export const NotifyPlugin = async ({ $, directory }) => {
  const notify = async (event) => {
    const payload = JSON.stringify({ cwd: directory, hook_event_name: event })

    // nothrow: a missing daemon or a broken hook must never take down the
    // session that triggered the notification.
    await $`${NOTIFY} opencode ${PROFILE} < ${new Response(payload)}`.nothrow().quiet()
  }

  return {
    "session.idle": async () => {
      await notify("Stop")
    },
    "permission.asked": async () => {
      await notify("PermissionRequest")
    },
  }
}
