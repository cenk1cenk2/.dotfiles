# CLAUDE.md

Repository knowledge base for agent sessions. Scope today: Python
script conventions, waybar and kitty configuration, and NVIDIA dGPU
runtime power. Everything below is an established rule — apply it to
every new script (and every touch of an old one) without re-discussion.

Linux (Arch, Wayland) is the only deployment target. `Taskfile.yml` has
no darwin path, and a config directive that reads as macOS-specific is
not automatically dead — see the kitty section.

## UWSM environment files

- `uwsm/env-*` files are selected and sourced by UWSM's environment
  preloader for the active compositor/session profile. UWSM exports the
  resulting environment delta to systemd and D-Bus activation
  environments, marks those variables for cleanup, and unsets them
  during session shutdown.

- Do **not** add defensive `unset` lines just because another
  `uwsm/env-*` profile exports a variable. Profiles do not need to clean
  up each other's exports. Only unset a variable when it can be
  inherited from outside the selected UWSM profile and the selected
  profile must actively neutralize that inherited value.

## NVIDIA dGPU runtime power (hybrid profile, RTX 5070 / nvidia-open)

Goal state: the dGPU reaches D3cold whenever idle. Verified against driver
source at `/usr/src/nvidia-<version>/` — read the source, not the docs, when
these need re-checking.

### What blocks sleep (all three must hold, independently)

- **Open fds on `/dev/nvidia0`** hold a power ref for as long as any fd is
  open: the first open takes a COARSE ref, the last close releases it
  (`nv.c`, `nv_start_device`/`nv_stop_device`). `/dev/nvidiactl` never pins
  (control opens bypass `nv_start_device`). Bare `/dev/dri/renderD*` opens
  take no ref (`nv_drm_open` assigns an id and returns).
- **`NVreg_DynamicPowerManagementVideoMemoryThreshold` is a cap in MB, not a
  toggle**: GCOFF (full power-off) requires used vidmem <= threshold. RM keeps
  ~2MiB allocated, so a threshold of 0 forbids power-off permanently. Leave it
  at the default (200).
- **Any `nvidia-smi` invocation resets the runtime-PM idle timer** as well as
  waking a sleeping GPU — polling it keeps an active GPU active forever.
  `tdp nvidia show` is passive (sysfs only) precisely for this; smi runs only
  with `--smi`.

### Standing constraints

- `nvidia-persistenced` is **enabled, and required for Proton**. Without it
  the kernel tears device state down whenever no client holds the GPU, and
  vkd3d-proton then fails swapchain creation with `VK_ERROR_UNKNOWN` (-13)
  in `dxgi_vk_swap_chain_recreate_swapchain_in_present_task`, followed by a
  `vkGetPastPresentationTimingEXT` fault — a crash seconds in, or an endless
  retry loop showing as a black screen. It does pin the GPU awake (the daemon
  opens `/dev/nvidia0` before enabling SW persistence, so its own fds hold a
  COARSE ref for its lifetime), which costs nothing while the driver bug
  below makes sleep unreachable anyway. Never toggle the daemon or
  `nvidia-smi -pm` live: it flips the persistence flag against in-flight
  refs, the refcount desyncs silently (asserts are compiled out of release
  builds), and the GPU stops attempting suspend until reboot. The signature
  of that state: zero rpm trace events with zero holders.
- `NVreg_DynamicPowerManagement=0x02` (FINE) forces fine-grained RTD3 and
  pairs with `80-nvidia-pm.rules` writing `power/control=auto`: the driver's
  own `nv_allow_runtime_suspend()` call (`dynamic-power.c`, gated on the
  regkey being exactly `0x03`/DEFAULT) is just `pm_runtime_allow`, which the
  udev rule does instead. Change one, change both.
- `nvidia-power.conf` may only carry parameters the open kernel module
  actually declares (`kernel-open/nvidia/nv-reg.h`); an unknown one is
  logged as `unknown parameter ... ignored` at module load and does nothing.
- `env-hybrid` keeps `__EGL_VENDOR_LIBRARY_FILENAMES` and `MANGOHUD=0`
  commented out. Both do remove `/dev/nvidia0` holders (GLVND EGL vendor
  enumeration for the compositor, the MangoHud implicit Vulkan layer's NVML
  load for Chromium/Electron), but that buys nothing while the driver bug
  below stands, and the Mesa EGL pin actively hides the dGPU from Proton:
  `winewayland.drv` presents through EGL, so without NVIDIA EGL, Wine offers
  a one-device list and every game lands on the iGPU. Hyprland and hyprpaper
  holding `nvidia0` is the expected, currently free consequence.
- Proton titles reach the dGPU via `DXVK_FILTER_DEVICE_NAME=NVIDIA` and
  `VKD3D_FILTER_DEVICE_NAME=NVIDIA` in `env-hybrid` — read by DXVK/vkd3d
  inside the pressure-vessel container. `prime-run` cannot do this job for
  Steam: the steam-runtime launcher resets `PATH`, so `prime-run %command%`
  resolves to `/usr/bin/prime-run`, and its `__VK_LAYER_NV_optimus` is inert
  because pressure-vessel does not import `nvidia_layers.json` into the
  container. Use `MANGOHUD=1 %command%` for the overlay.
- `rootfs/etc/modprobe.d/*` is **copied** to `/etc` by `install.py` — edits
  need `task deploy:linux:system` (or `./install.py system`) to take effect.
  Bare `task` only lists tasks and copies nothing. `rootfs/etc/udev/rules.d/*`
  is stow-symlinked and live on save.

### Known blocker (610.43.03 and 610.57.04, parked)

- With every userspace pin fixed, the GPU sleeps only until the desktop
  session's first wake of the device, then never again: the driver's
  runtime-PM usage count sticks at 1 (`cnt-1` in `rpm:*` trace events after
  toggling `power/control` on→auto). Upstream, unfixed, not GB206-specific:
  [open-gpu-kernel-modules#905](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/905)
  (sleeps once per boot; system suspend reportedly resets it),
  [#1121](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1121)
  (`usage_count=1` with zero openers),
  [#1071](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1071)
  (mechanism: `RmRemoveIdleHoldoff` reschedules forever, so
  `nv_indicate_idle` never drops the ref; candidate patch
  [PR #1074](https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1074)).
  A near-twin (5070 Ti GB205, Panther Lake, Arch, 610.57.04) sleeps
  repeatedly in
  [#1300](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1300)
  with `DynamicPowerManagement=3`, no `EnableS0ixPowerManagement`, and
  persistenced off — its only residual is NVPCF ACPI wakes, patched by
  [PR #1181](https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1181).
  This laptop carries the `NPCF` device in `SSDT25`.
- Driver mechanics (verified in `/usr/src/nvidia-<version>/`): exactly three
  sites touch `usage_count` in `kernel-open/nvidia/nv.c` — `nv_indicate_idle`
  (-1), `nv_indicate_not_idle` (+1), `nv_idle_holdoff` (+1, taken on every
  resume in `dynamic-power.c` `RmTransitionDynamicPower`). On a notebook
  `RmConfigureUpstreamPortForRTD3` short-circuits on `b_mobile_config_enabled`,
  so the GC6 path is used and the GCOFF checks (`VideoMemoryThreshold`,
  `clients_gcoff_disallow_refcount`) are never evaluated; every idle gate
  reduces to the GSP query `RmCheckForGcxSupportOnCurrentState`. No
  nvkms/fbdev/HDA ref exists on that path (`fbdev=0` is inert;
  [#759](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/759) was
  fixed in 595.45.04).
- RM core is **not** prebuilt: DKMS compiles `nv-kernel.o` from `src/nvidia/`
  (no `nv-kernel.o_binary` in the tree), so cherry-picking PR #1181 / #1074
  or adding `-DDEBUG` to `src/nvidia/Makefile` for the state-transition
  trace is a local rebuild. `NVreg_RmMsg` is dead in release builds
  (`NV_PRINTF_STRINGS_ALLOWED 0`); `NVreg_EnableGpuFirmwareLogs=1` is the
  only lever that exposes the GSP-side verdict. `kernel-open` symbols are
  ftrace-able (`nv_indicate_idle`, `nv_idle_holdoff`, `nv_pmops_runtime_*`);
  only `nv-kernel.o` refuses kprobes, because linux-zen lacks
  `CONFIG_KPROBE_EVENTS_ON_NOTRACE`.
- Parked, not open: persistenced is mandatory for Proton and pins the GPU by
  design, so even a fixed driver yields sleep only with persistenced stopped.
  Do not reopen the userspace investigation; re-test only on a driver newer
  than 610.57.04, or if the persistenced requirement goes away.

### Verifying without waking the GPU

- `tdp nvidia show` — passive; marks pinning holders vs harmless
  (`nvidiactl`-only) ones. Unprivileged `fuser` cannot see other users'
  processes, so root daemons (persistenced) are invisible in its holder list.
- `cat /sys/class/drm/card1/device/power_state` / `power/runtime_status` —
  sysfs reads never wake it. `nvidia-smi`, `btop`, `lspci` all do.
- Kernel-side truth: `sudo /sys/kernel/tracing` rpm events show whether the
  driver even attempts suspend; zero events with zero holders means the
  driver-internal refcount is desynced (reboot).
- Naming the stuck ref: kprobes on `nvidia:nv_indicate_idle`,
  `nv_indicate_not_idle`, `nv_idle_holdoff` plus the `rpm_usage` tracepoint
  with `options/stacktrace`, filtered on the dGPU PCI address, across one
  sleep/wake cycle in a zero-holder session. `power/runtime_usage` is absent
  (`CONFIG_PM_ADVANCED_DEBUG` off), so ftrace is the only usage-count readout.

## Python scripts

### Dependencies & entry points

- `pyproject.toml` + uv shebang trampoline:

  ```
  #!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
  ```

  Lets `uv run` resolve the project regardless of the shell's cwd
  (compositor keybinds hand us whatever working dir they have). The
  `sh -c` indirection is the supported pattern — PEP 723 inline
  metadata explicitly ignores project deps, so we stay with
  project-mode.

- Single-script tools get their own `pyproject.toml` next to the
  script when they deserve pinned deps. Shared helpers live under a
  `lib/` package that imports normally.

- Re-export public names through `lib/__init__.py` using `from X
  import Y as Y`. Ruff treats that as an explicit public re-export
  (silences F401); LSP rename still works because those are real
  imported names. **No `__all__`** — the string entries in it don't
  refactor with symbol renames.

### CLI: click, class-based

- Each script has a class that owns its CLI. `cli = click.Group(...)`
  is a class attribute; subcommands hang off `@cli.command("name")`.
  `if __name__ == "__main__": MyClass.cli()` is the entrypoint.

- **Command callbacks are named `cmd_<verb>`, never the bare verb.**
  Decorating `def start():` inside a class where a method `start`
  also exists silently replaces the method with a `click.Command`
  object — `self.start()` then invokes the CLI runner instead of
  the method. We hit this with `start` / `stop` / `kill` / `run`.
  Always prefix the callback with `cmd_` and pin the subcommand
  name via `@cli.command("<verb>")` so the CLI-facing name stays
  clean.

- Class constants (`WAYBAR_MODULE`, `ICON`, `SYSTEM_PROMPT`, `log`)
  live on the class, not at module top. Module level only for
  things used across multiple classes or before a class definition.

- Flag help strings are **short**, end with a period, lean on click's
  defaults. `help="Text source."` — not a paragraph. click already
  prints the default, the choices, the type.

### Logging: rich, stderr for traces

- `lib.cli.create_logger(verbose)` is the only way scripts set up
  logging. Installs a `RichHandler` on the root logger bound to
  `sys.stderr`. `--verbose` / `-v` flips level to DEBUG; default
  INFO. Call once in the click group callback.

- **Don't guard `log.debug(...)` with `isEnabledFor`.** The logger
  gates it automatically. Guarding is dead code.

### Stdout policy: per-script, not global

Two buckets. Pick the one that matches how the script is actually
used; don't assume one pattern fits all:

- **Pipe-involved scripts** (waybar modules, stdin/stdout sinks,
  anything whose output is parsed by another tool, e.g.
  `speech.py | hyprpilot ctl prompts send`): **stdout is for the
  payload only**. No `print("…")` for prose. Status / progress goes
  through `log.*` which lands on stderr. Use explicit
  `sys.stdout.write(...)` for the pipe-intended bytes.
  `copywriter`, `recorder`, `speech` all fall here.

- **Interactive scripts** (operator runs the command, reads the
  output on-screen, never pipes): **stdout is allowed for pretty
  output** — `rich.Console()` writing rules, tables, panels
  directly to stdout is fine. Keep the logger on stderr for
  timestamped traces / subprocess spawns so `-v` stays scannable.
  `remsi` fits here. The convention is two consoles:
  ```py
  console = Console()                        # stdout — prose
  _log_console = Console(file=sys.stderr,    # stderr — traces
                         stderr=True)
  ```

Whichever bucket: **nothing gets printed to stdout accidentally.**
If in doubt, ask; defaulting to pipe-only is the safe call.

### Rich output when scripts can use stdout

For the interactive bucket, use rich directly instead of stuffing
markup into `log.info` strings. It composes better and reads
cleaner on output:

- `console.rule("[bold yellow]Section[/]")` — horizontal divider
  with a title. Better than `log.info("── Section ──")`.
- `console.print(f"key: [bold]{value}[/]")` — key-value prose,
  no timestamp prefix.
- `rich.table.Table(show_header=True, box=None)` — tabular output
  (timelines, before/after comparisons). Add columns, add rows,
  `console.print(table)`.
- `rich.syntax.Syntax(text, "json")` — highlighted code / config.

Log messages still take rich markup (`markup=True` on the
handler) — keep `log.info("spawn: %s", ...)` plain; use markup
for per-item results (`log.info("  filler: [red]%s[/]", …)`).

### Subprocess discipline

- Wire `stdout` / `stderr` manually at each spawn site. We had a
  helper once; pulled it out because call shapes vary too much
  (capture / inherit / redirect) to abstract without awkwardness.

- **Always** call `log.debug("spawn: %s", " ".join(cmd))` before the
  subprocess call so `--verbose` traces every process we fork.

- Capturing callers (`subprocess.run(..., capture_output=True)`)
  dump `proc.stderr` through `log.debug("<tool> stderr: %s", ...)`
  after the call so verbose mode still surfaces the tool's chatter
  even when stdout is programmatic.

- Non-capture callers set both `stdout=sys.stderr` and
  `stderr=sys.stderr` explicitly. Inherit-stdout leaks into the
  user's pipe.

- Level policy: INFO for one-off user-facing spawns (enrichment
  CLIs, agent processes). DEBUG for waybar-polling / status spawns.

- **Every spawn that can block gets a `timeout`.** `lib.cli.run`
  starts the child in its own session and a timeout kills the whole
  process group, then `wait()`s it. Both matter: the AI CLIs fork
  children of their own (claude runs node, opencode spawns a local
  server), so killing the direct child orphans the rest, and an
  unreaped child stays a zombie for the life of the parent — which
  for a forked `copywriter` worker is unbounded. Callers catch
  `subprocess.TimeoutExpired`; `run` re-raises it rather than
  folding it into a returncode.

### Adapter pattern

Where a script talks to multiple providers (enrichment backends,
input sources, output sinks, AI agents), each provider is an
adapter class with a `Protocol` interface. The CLI flag picks the
adapter; the rest of the code never knows which one.

- Protocols live next to the adapters (`lib/enrich.py` has
  `EnrichAdapter` + `EnrichProvider` enum + two concrete classes).
  New adapters are pluggable by adding an enum value + a class; no
  core changes.

- **One call site: `match` inline.** CLI wiring is a `match` on the
  Provider enum value inside the click command callback — no factory,
  no indirection, the match lives with the flag it reads from.

- **Three or more call sites: one spec dataclass + one factory.**
  `lib/enrich.py` carries `EnrichSpec` (every knob every backend
  takes) and `build_enricher(spec, …)`; callers fill the spec from
  their flags and never name an adapter class. This replaced three
  duplicated `match` blocks — `copywriter.py` plus two in
  `speech.py`, where the socket override path had silently drifted
  into supporting fewer options than the CLI path. Don't reach for
  this until the duplication is real.

- **Delegate the vendor matrix rather than modelling it.** The
  claude / opencode / codex adapters collapsed into one that shells
  out to `hyprpilot <profile> --file`: the profile already carries
  model, permission mode, MCP set and the vendor's config-dir env,
  so a new agent backend is a hyprpilot config entry, not a class.
  `spec.model` names a model id for http and a profile id for
  hyprpilot — one flag, resolved per adapter.

- hyprpilot only ever passes `--append-system-prompt`, so an agent
  profile keeps its vendor's own agent identity. Enrichment
  profiles therefore need `mcp.enabled: false` and a
  `system_prompt` reset, and those must be set in a **patch**, not
  on the profile body — patches fold over profiles, so a profile
  field silently loses to the root patch.

- Secrets cross process boundaries as the *name* of an env var, not
  the value — `EnrichSpec.api_key_env` is resolved at call time, so
  the key never enters speech's socket JSON.

### Typing & style

- **Don't quote forward-ref return types.** `def foo() -> Foo:`,
  not `def foo() -> "Foo":`. Use `from __future__ import
  annotations` at the module top so all annotations are strings at
  runtime regardless.

- **No backwards-compat wrappers.** Renames delete the old name in
  the same commit; this is a private codebase with one caller. A
  function that just forwards to the new name is dangling code.

- **Inline single-use helpers.** A three-line function called from
  exactly one site adds a search hop for readers. Inline it. If it
  grows to something that deserves its own name, extract later.

- **Brief docstrings.** First line says what. Follow with *why* or
  *gotchas* only when non-obvious. Two-line getters skip the
  docstring entirely.

- **No redundant comments.** `# increment counter` above `counter
  += 1` is noise. Comments explain *why* the code exists, not what
  it does.

### Exception handling

- Multi-type `except` clauses go **unparenthesised**:
  `except FileNotFoundError, ConnectionRefusedError:`. PEP 758 made
  that form legal in 3.14, `requires-python` pins `>=3.14`, and ruff's
  formatter strips the parens once it infers that target. Adding them
  back starts an edit war with the next format run.

## kitty

`kitty/.config/kitty/kitty.conf` is a vendored upstream sample with our
edits threaded through it, so most `#:` blocks document settings that are
not set. Read the active line, never the comment above it.

- **`cmd` is `super` on every platform, not just macOS.** kitty aliases
  `⌘` / `COMMAND` / `CMD` to `SUPER` in `options/utils.py`, so a
  `map cmd+...` line is a live binding here: `map cmd+shift+v
  paste_from_buffer a1` is Super+Shift+V. A macOS-looking `map cmd+*` is
  never safe to delete as dead weight — confirm with the probe below
  first. Only the `macos_*`-prefixed settings are genuinely inert.

- **`hide_window_decorations titlebar-only` is a Wayland value.** It hides
  the titlebar while keeping the window shadow for resizing, and parses to
  a distinct bitmask — `titlebar-only` is `0b10`, `yes` is `0b01`. The
  `#: On macOS, titlebar-only...` comment beneath it is stale upstream
  text from an older kitty.

- **The platform hook is `globinclude ${KITTY_OS}.conf`, deliberately not
  `include`.** `KITTY_OS` expands for both forms and resolves to `linux`.
  With no file behind it, `include` logs `Could not find included config
  file: ..., ignoring` on every start whereas `globinclude` is silent, so
  the hook survives with no placeholder file to maintain.

- **`kitty --debug-config` does not exist.** Read the effective config
  with the Python entry point instead:

  ```sh
  kitty +runpy "from kitty.config import load_config
  o = load_config('$HOME/.config/kitty/kitty.conf')
  print(o.font_size, o.hide_window_decorations)"
  ```

  `load_config()` with no path silently returns defaults, so passing the
  path is mandatory or the probe proves nothing.

## Waybar modules

- Waybar reads its config once at startup and never re-reads it —
  `reload_style_on_change` covers `style.css` only. Renaming a
  script's CLI, or any `exec` / `exec-if` string, needs
  `systemctl --user restart waybar@<compositor>.service`. Without
  it the bar keeps invoking the old command and the module silently
  vanishes: a failing `exec-if` hides a module rather than reporting
  anything, and the command's own error surfaces only in the waybar
  journal (`journalctl --user -u 'waybar*'`).

- `hyprland.jsonc` and `sway.jsonc` are separate bars, so a module
  each compositor shares has to be edited in both. Only that
  compositor's own modules belong in its file — a `sway/*` module
  listed in the Hyprland bar (or the reverse) is instantiated and
  fails, since the backing IPC is absent.

- Custom module `signal` numbers come from `waybar-signal.sh`, which
  is the whole map: a module whose number is missing there is never
  poked and only refreshes on its `interval`.
