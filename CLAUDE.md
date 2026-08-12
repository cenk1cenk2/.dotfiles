# CLAUDE.md

Repository knowledge base for agent sessions. Scope today: Python
script conventions and NVIDIA dGPU runtime power. Everything below is
an established rule — apply it to every new script (and every touch of
an old one) without re-discussion.

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

- `nvidia-persistenced` stays **disabled**. The daemon opens `/dev/nvidia0`
  before it enables SW persistence, so its own fds hold the COARSE ref for
  the daemon's lifetime and the GPU never sleeps while it runs. The FINE-ref
  open path (`nv_start_device` with `NV_FLAG_PERSISTENT_SW_STATE` set) only
  helps clients whose first open happens after the flag is set — the daemon
  itself defeats it. Never toggle the daemon or `nvidia-smi -pm` live: it
  flips the flag against in-flight refs, the refcount desyncs silently
  (asserts are compiled out of release builds), and the GPU stops attempting
  suspend until reboot. The signature of that state: zero rpm trace events
  with zero holders.
- `NVreg_DynamicPowerManagement=0x03` stays pinned although it equals the
  built-in default: the `nv_allow_runtime_suspend()` path is gated on the
  regkey being exactly DEFAULT — an explicit `0x02` (FINE) skips it.
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
- `rootfs/etc/modprobe.d/*` is **copied** to `/etc` by the Taskfile — edits
  need a re-install (`task` or `sudo install`). `rootfs/etc/udev/rules.d/*`
  is stow-symlinked and live on save.

### Known blocker (610.43.03 and 610.57.04, unresolved)

- With every userspace pin fixed, the GPU sleeps only until the desktop
  session's first wake of the device, then never again: RM never re-indicates
  idle, so the driver's runtime-PM usage count sticks at 1 (visible as
  `cnt-1` in `rpm:*` trace events after toggling `power/control` on→auto).
  The decision lives in RM core / GSP firmware (`nv-kernel.o` prebuilt, GCx
  prerequisite evaluated GSP-side) — not debuggable from the OS layer.
  `nvidia-drm.fbdev=0` does not help. Suspected driver bug on GB206M;
  re-test on driver updates before re-opening the userspace investigation.

### Verifying without waking the GPU

- `tdp nvidia show` — passive; marks pinning holders vs harmless
  (`nvidiactl`-only) ones. Unprivileged `fuser` cannot see other users'
  processes, so root daemons (persistenced) are invisible in its holder list.
- `cat /sys/class/drm/card1/device/power_state` / `power/runtime_status` —
  sysfs reads never wake it. `nvidia-smi`, `btop`, `lspci` all do.
- Kernel-side truth: `sudo /sys/kernel/tracing` rpm events show whether the
  driver even attempts suspend; zero events with zero holders means the
  driver-internal refcount is desynced (reboot).

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

- Parenthesise multi-type `except` tuples:
  `except (FileNotFoundError, ConnectionRefusedError):`. Some
  linters in the toolchain strip the parens to the Py2
  `except A, B:` form, which Py3 rejects as a `SyntaxError`. Keep
  an eye on this after auto-format runs.
