# CLAUDE.md

Repository knowledge base for agent sessions. Scope today: Python
script conventions. Everything below is a rule established in the
wayland-scripts refactor — apply it to every new script (and every
touch of an old one) without re-discussion.

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

### Logging: rich, stderr, never stdout

- `lib.cli.create_logger(verbose)` is the only way scripts set up
  logging. Installs a `RichHandler` on the root logger bound to
  `sys.stderr`. `--verbose` / `-v` flips level to DEBUG; default
  INFO. Call once in the click group callback.

- **Stdout is pipe-only.** Waybar reads JSON off stdout; the user
  pipes scripts together (`speech | pilot`). No `print("…")` for
  status or info — use `log.info`. Only pipe-intended payloads
  (waybar JSON, transcripts, structured output) land on stdout via
  explicit `sys.stdout.write(...)`.

- **Don't guard `log.debug(...)` with `isEnabledFor`.** The logger
  gates it automatically. Guarding is dead code.

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

### Adapter pattern

Where a script talks to multiple providers (enrichment backends,
input sources, output sinks, AI agents), each provider is an
adapter class with a `Protocol` interface. The CLI flag picks the
adapter; the rest of the code never knows which one.

- Protocols live next to the adapters (`lib/enrich.py` has
  `EnrichAdapter` + `EnrichProvider` enum + three concrete
  classes). New adapters are pluggable by adding an enum value +
  a class; no core changes.

- CLI wiring is a `match` on the Provider enum value inside the
  click command callback. No factory functions, no indirection
  layers — the match lives with the flag it reads from.

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

### GTK widgets (pilot-specific)

- **Nerd Font** (Material Design Icons) glyphs for every in-app
  label. No emojis — the user's GTK font stack renders Nerd Font
  reliably; emojis depend on the desktop font fallback chain.

- Pills / buttons / CSS variants live in `lib/overlay.py` and
  `lib/overlay.css`. Adding a new tint = one entry in `PillVariant`
  + one CSS block.

- `lib/overlay.py` imports `gi` at module-top. Headless scripts
  (waybar-status polls, MCP subprocesses) must NOT go through
  `from lib import X` for overlay symbols — import directly from
  `lib.overlay` so the non-GUI scripts don't pay the GTK load cost
  on every invocation.
