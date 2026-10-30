#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Take a screenshot of a window, an output, a selection or every screen.

The screen is frozen with hyprpicker for the length of the shot, so a
selection is drawn over a still image and the capture matches it."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

import click
from dotlib.cli import create_logger
from dotlib.notify import Notification, OsdIcon

from lib import Hyprctl


class Subject(StrEnum):
    ACTIVE = "active"
    SCREEN = "screen"
    OUTPUT = "output"
    AREA = "area"
    WINDOW = "window"


class Screenshot:
    REQUIRES = ("grim", "slurp", "wl-copy", "hyprpicker")
    # The screenshot submap's hints are a layer the overlay daemon unmaps once
    # the submap resets. Shooting before then captures them too.
    HINTS_NAMESPACE = "overlay-hints"
    FREEZE_NAMESPACE = "hyprpicker"
    LAYER_WAIT = 1.0
    LAYER_POLL = 0.025
    CAPTURE_TIMEOUT = 10.0
    # slurp waits on the user, so this only bounds a selection left hanging.
    SELECT_TIMEOUT = 300.0

    log = logging.getLogger("screenshot")

    def __init__(self, hypr: Hyprctl, notify: bool, cursor: bool):
        self._hypr = hypr
        self._notify = notify
        self._cursor = cursor
        self._notification = Notification("Screenshot", osd_icon=OsdIcon.DONE)

    def _namespaces(self) -> list[tuple[str, int]]:
        found = []
        for monitor in (self._hypr.query("layers") or {}).values():
            for layers in monitor.get("levels", {}).values():
                found.extend((layer["namespace"], layer["pid"]) for layer in layers)

        return found

    def _wait_hints_gone(self) -> None:
        deadline = time.monotonic() + self.LAYER_WAIT
        while time.monotonic() < deadline:
            if not any(ns == self.HINTS_NAMESPACE for ns, _ in self._namespaces()):
                return
            time.sleep(self.LAYER_POLL)
        self.log.debug("hints still mapped after %gs", self.LAYER_WAIT)

    @contextmanager
    def freeze(self) -> Iterator[None]:
        """Hold every output still until the block exits.

        Waits for hyprpicker to map a layer on every monitor rather than for a
        fixed delay, so a slow start never lets the live screen into the shot.
        A click that reaches hyprpicker instead of slurp picks a colour and
        prints it, hence the discarded stdout."""
        cmd = ["hyprpicker", "--render-inactive", "--no-zoom", "--quiet"]
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=sys.stderr)
        # A signal would otherwise end the process without unwinding, leaving
        # the screen frozen with nothing left to thaw it.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda signum, _: sys.exit(128 + signum))
        try:
            monitors = len(self._hypr.monitors())
            deadline = time.monotonic() + self.LAYER_WAIT
            while time.monotonic() < deadline:
                mapped = sum(
                    1
                    for ns, pid in self._namespaces()
                    if ns == self.FREEZE_NAMESPACE and pid == proc.pid
                )
                if mapped >= monitors:
                    break
                time.sleep(self.LAYER_POLL)
            else:
                self.log.warning("freeze not mapped after %gs", self.LAYER_WAIT)
            yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _select(self, cmd: list[str], candidates: str | None = None) -> str:
        self.log.debug("spawn: %s", " ".join(cmd))
        # subprocess.run kills slurp on any exception, including the SystemExit
        # a signal raises, so an interrupted shot leaves no selection overlay.
        try:
            result = subprocess.run(
                cmd,
                input=candidates,
                capture_output=True,
                text=True,
                timeout=self.SELECT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.log.info("selection timed out")
            sys.exit(1)
        if result.stderr:
            self.log.debug("slurp stderr: %s", result.stderr.strip())
        geometry = result.stdout.strip()
        if result.returncode != 0 or not geometry:
            self.log.info("selection cancelled")
            sys.exit(1)

        return geometry

    def _visible_windows(self) -> str:
        """slurp candidates for the windows on screen: every monitor's active
        workspace and any special workspace shown over it."""
        shown = set()
        for monitor in self._hypr.monitors():
            shown.add(monitor.get("activeWorkspace", {}).get("id"))
            shown.add(monitor.get("specialWorkspace", {}).get("id"))
        shown.discard(0)

        return "".join(
            f"{c['at'][0]},{c['at'][1]} {c['size'][0]}x{c['size'][1]}\n"
            for c in self._hypr.clients()
            if c.get("mapped")
            and not c.get("hidden")
            and c.get("workspace", {}).get("id") in shown
        )

    def target(self, subject: Subject) -> tuple[list[str], str]:
        """The grim arguments that frame the subject, and how to name it."""
        match subject:
            case Subject.SCREEN:
                return [], "Screen"
            case Subject.OUTPUT:
                monitor = self._hypr.focused_monitor()
                if not monitor:
                    self.fail("No focused output")

                return ["-o", monitor["name"]], monitor["name"]
            case Subject.ACTIVE:
                window = self._hypr.active_window()
                if not window:
                    self.fail("No active window")
                (x, y), (w, h) = window["at"], window["size"]

                return ["-g", f"{x},{y} {w}x{h}"], f"{window['class']} window"
            case Subject.AREA:
                return ["-g", self._select(["slurp", "-d"])], "Area"
            case Subject.WINDOW:
                return [
                    "-g",
                    self._select(["slurp"], self._visible_windows()),
                ], "Window"

    def capture(self, args: list[str]) -> bytes:
        cmd = ["grim", *(["-c"] if self._cursor else []), *args, "-"]
        self.log.debug("spawn: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.CAPTURE_TIMEOUT, check=False
            )
        except subprocess.TimeoutExpired:
            self.fail("grim timed out")
        if proc.stderr:
            self.log.debug("grim stderr: %s", proc.stderr.decode().strip())
        if proc.returncode != 0:
            self.fail("Unable to invoke grim")

        return proc.stdout

    def shoot(self, subject: Subject) -> tuple[bytes, str]:
        self._wait_hints_gone()
        with self.freeze():
            args, what = self.target(subject)

            return self.capture(args), what

    def copy(self, subject: Subject) -> None:
        png, what = self.shoot(subject)
        cmd = ["wl-copy", "--type", "image/png"]
        self.log.debug("spawn: %s", " ".join(cmd))
        # wl-copy forks a server that outlives it and keeps whatever pipes it
        # inherited, so capturing its output would block until the clipboard
        # is next replaced.
        try:
            proc = subprocess.run(
                cmd,
                input=png,
                stdout=sys.stderr,
                stderr=sys.stderr,
                timeout=self.CAPTURE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("wl-copy timed out")
        if proc.returncode != 0:
            self.fail("Clipboard error")
        self.ok(f"{what} copied to clipboard")

    def save(self, subject: Subject, path: str | None) -> None:
        png, what = self.shoot(subject)
        if path == "-":
            sys.stdout.buffer.write(png)
            sys.stdout.flush()
            self.ok(f"{what} sent to stdout")
            return

        path = path or os.path.join(
            self.directory(), f"{datetime.now().astimezone().isoformat()}.png"
        )
        with open(path, "wb") as f:
            f.write(png)
        self.log.info("saved: %s", path)
        sys.stdout.write(path + "\n")
        self.ok(f"{what} saved to {os.path.basename(path)}")

    @staticmethod
    def directory() -> str:
        """XDG_SCREENSHOTS_DIR, else XDG_PICTURES_DIR, from user-dirs.dirs."""
        dirs = {}
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        try:
            with open(os.path.join(config, "user-dirs.dirs")) as f:
                for match in re.finditer(
                    r'^(XDG_\w+_DIR)="(.*)"$', f.read(), re.MULTILINE
                ):
                    dirs[match[1]] = os.path.expandvars(match[2])
        except FileNotFoundError:
            pass

        return (
            dirs.get("XDG_SCREENSHOTS_DIR")
            or dirs.get("XDG_PICTURES_DIR")
            or os.path.expanduser("~")
        )

    def ok(self, message: str) -> None:
        if self._notify:
            self._notification.send(message)

    def fail(self, message: str) -> NoReturn:
        self.log.error(message)
        if self._notify:
            self._notification.send(message, icon=OsdIcon.ERROR)
        sys.exit(2)

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    @click.option("-n", "--notify", is_flag=True, help="Notify on completion.")
    @click.option("-c", "--cursor", is_flag=True, help="Include the cursor.")
    @click.pass_context
    def cli(ctx: click.Context, verbose: bool, notify: bool, cursor: bool):
        """Take screenshots under Hyprland."""
        create_logger(verbose, log_file="screenshot.log", quiet={"check"})
        ctx.obj = (notify, cursor)

    @cli.command("copy")
    @click.argument(
        "subject",
        type=click.Choice([s.value for s in Subject]),
        default=Subject.SCREEN.value,
    )
    @click.pass_obj
    def cmd_copy(obj: tuple[bool, bool], subject: str):
        """Copy a screenshot to the clipboard."""
        Screenshot(Hyprctl(), *obj).copy(Subject(subject))

    @cli.command("save")
    @click.argument(
        "subject",
        type=click.Choice([s.value for s in Subject]),
        default=Subject.SCREEN.value,
    )
    @click.argument("path", required=False)
    @click.pass_obj
    def cmd_save(obj: tuple[bool, bool], subject: str, path: str | None):
        """Save a screenshot to PATH, or '-' for stdout."""
        Screenshot(Hyprctl(), *obj).save(Subject(subject), path)

    @cli.command("check")
    def cmd_check():
        """Check that the required tools are installed."""
        missing = [tool for tool in Screenshot.REQUIRES if not shutil.which(tool)]
        for tool in Screenshot.REQUIRES:
            sys.stdout.write(f"{tool}: {'NOT FOUND' if tool in missing else 'OK'}\n")
        sys.exit(1 if missing else 0)


if __name__ == "__main__":
    Screenshot.cli()
