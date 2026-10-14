// Forwards the moments worth looking up for to the shared agent notify hook,
// wayland/scripts/notify.py, in codex's hook shape: it chimes, rings the bell,
// pops a popup that focuses this tmux pane, and lists the session for the
// Stream Deck's agent keys. opencode has no hooks of its own, so a plugin is
// the only route.
//
// A client (TUI) plugin, not a server one: it runs in the terminal process, so
// notify.py inherits the TMUX_PANE and kitty environment its focus check reads,
// where the server may be a detached process with neither. The client
// discovers <config dir>/plugins/<name>/tui.* on its own, and hyprpilot points
// OPENCODE_CONFIG_DIR at this profile, so nothing lists it in cli.json.

import { spawn } from "node:child_process"

const PROFILE = "kilic"
const NOTIFY = `${process.env.HOME}/.config/wayland/scripts/notify.py`
const ASK_GRACE_MS = 1500

export default {
  id: "dotfiles.notify",
  setup(context) {
    const permissions = new Set()
    const pending = new Map()
    const forms = new Set()
    const ended = new Set()

    const notify = (sessionID, event, location, fields) => {
      const session = sessionID ? context.data.session.get(sessionID) : undefined
      const cwd = session?.location?.directory ?? location?.directory ?? context.location?.directory ?? process.cwd()
      const payload = JSON.stringify({ cwd, hook_event_name: event, session_id: sessionID, ...fields })

      // Detached and swallowed: a missing daemon or a broken hook must never
      // take down the session that triggered the notification.
      try {
        const child = spawn(NOTIFY, ["opencode", PROFILE], { detached: true, stdio: ["pipe", "ignore", "ignore"] })
        child.on("error", () => {})
        child.stdin.on("error", () => {})
        child.stdin.end(payload)
        child.unref()
      } catch {}
    }

    // The newest assistant text since the prompt that started this execution,
    // read from the client's store; undefined when it holds none.
    const answer = (sessionID) => {
      try {
        const messages = context.data.session.message.list(sessionID) ?? []
        for (let index = messages.length - 1; index >= 0; index--) {
          const message = messages[index]
          if (message.type === "user") return
          if (message.type !== "assistant") continue
          const text = message.content
            .filter((part) => part.type === "text")
            .map((part) => part.text)
            .join("")
            .trim()
          if (text) return text
        }
      } catch {}
    }

    // A subagent finishing is not the end of the turn; its parent still runs.
    // Its permission prompts and forms do block the turn, so those still go out.
    // A shutdown or a superseding prompt ends the execution without the user
    // having anything to come back to.
    const stop = (event) => {
      if (event.data.reason === "shutdown" || event.data.reason === "superseded") return
      const sessionID = event.data.sessionID
      if (ended.has(sessionID)) return
      ended.add(sessionID)
      if (context.data.session.get(sessionID)?.parentID !== undefined) return
      notify(sessionID, "Stop", event.location, { last_assistant_message: answer(sessionID) })
    }

    const dispose = [
      // Under --auto the server still asks and this client replies "once" right
      // after, so an ask only alarms once it has stayed unanswered for a moment.
      context.data.on("permission.asked", (event) => {
        const id = event.data.id
        if (permissions.has(id)) return
        permissions.add(id)
        pending.set(
          id,
          setTimeout(() => {
            pending.delete(id)
            notify(event.data.sessionID, "PermissionRequest", event.location, { tool_name: event.data.action })
          }, ASK_GRACE_MS),
        )
      }),
      context.data.on("permission.replied", (event) => {
        permissions.delete(event.data.requestID)
        clearTimeout(pending.get(event.data.requestID))
        pending.delete(event.data.requestID)
      }),
      // A question waiting on an answer goes out as its own Question event,
      // titled by the form. --auto never answers one, so it alarms at once.
      context.data.on("form.created", (event) => {
        if (forms.has(event.data.form.id)) return
        forms.add(event.data.form.id)
        notify(event.data.form.sessionID, "Question", event.location, { tool_name: event.data.form.title })
      }),
      context.data.on("form.replied", (event) => forms.delete(event.data.id)),
      context.data.on("form.cancelled", (event) => forms.delete(event.data.id)),
      context.data.on("session.execution.started", (event) => ended.delete(event.data.sessionID)),
      context.data.on("session.execution.succeeded", stop),
      context.data.on("session.execution.failed", stop),
      context.data.on("session.execution.interrupted", stop),
    ]

    return () => {
      pending.forEach((timer) => clearTimeout(timer))
      pending.clear()
      dispose.reverse().forEach((cleanup) => cleanup())
    }
  },
}
