#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Bring a remote machine's microphone in as a local source over RTP.

`start` runs an RTP receiver here that shows up as a source, then reaches the
peer over ssh and starts an RTP sender there that captures its default
source. Both are transient user units, so `stop` is a unit stop on each side.
The peer falls back to Sunshine's `SUNSHINE_CLIENT_NAME`, which it exports to
an app's prep commands, so a stream carries the client's microphone back to
the host the way it carries the host's audio to the client.

`start` makes the new source the default and `stop` puts the previous one
back."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import click
from dotlib.cli import create_logger, run


class VirtualAudio:
    DEFAULT_NAME = "VIRTUAL"
    DEFAULT_PORT = 46000
    SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
    # Both ends must agree on the format: the receiver takes no SDP, so it
    # cannot learn it from the stream.
    FORMAT = 'audio.format = "S16BE" audio.rate = 48000 audio.channels = 1 audio.position = [ MONO ]'
    CONTEXT = """context.spa-libs = {
  audio.convert.* = audioconvert/libspa-audioconvert
  support.* = support/libspa-support
}
context.modules = [
  { name = libpipewire-module-rt flags = [ ifexists nofail ] }
  { name = libpipewire-module-protocol-native }
  { name = libpipewire-module-client-node }
  { name = libpipewire-module-adapter }
  %s
]
"""

    log = logging.getLogger("virtual-audio")

    def __init__(self, name: str):
        self.name = name
        self.unit = f"virtual-audio-{name}"
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        self._config = runtime / f"{self.unit}.conf"
        self._state = runtime / f"{self.unit}.json"

    def receiver(self, peer: str, port: int) -> str:
        return self.CONTEXT % (
            f"{{ name = libpipewire-module-rtp-source args = {{ "
            f'source.ip = "0.0.0.0" source.port = {port} {self.FORMAT} '
            f'stream.props = {{ media.class = "Audio/Source" node.name = "{self.unit}" '
            f'node.description = "{peer} microphone" }} }} }}'
        )

    def sender(self, destination: str, port: int) -> str:
        return self.CONTEXT % (
            f"{{ name = libpipewire-module-rtp-sink args = {{ "
            f'destination.ip = "{destination}" destination.port = {port} {self.FORMAT} '
            f'stream.props = {{ media.class = "Stream/Input/Audio" node.name = "{self.unit}" }} }} }}'
        )

    def ssh(self, peer: str, command: str, input: str | None = None) -> str:
        return run(
            [*self.SSH, peer, command], log=self.log, input=input, check=True
        ).stdout

    def default_source(self) -> str:
        return run(
            ["pactl", "get-default-source"], log=self.log, check=True
        ).stdout.strip()

    def wait_for_source(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sources = run(["pactl", "list", "short", "sources"], log=self.log).stdout
            if any(line.split("\t")[1] == self.unit for line in sources.splitlines()):
                return
            time.sleep(0.2)
        raise click.ClickException(f"source {self.unit} did not appear in {timeout:g}s")

    def start(self, peer: str, port: int) -> None:
        # The address the peer sees this machine on is the one it can send to,
        # whichever interface or VPN the ssh connection took.
        destination = self.ssh(peer, 'echo "$SSH_CONNECTION"').split()[0]
        self.log.info("receiving %s microphone on port %d", peer, port)

        self.stop_unit()
        self._config.write_text(self.receiver(peer, port))
        run(
            [
                "systemd-run",
                "--user",
                f"--unit={self.unit}",
                "--collect",
                "pipewire",
                "-c",
                str(self._config),
            ],
            log=self.log,
            check=True,
        )

        self.log.info("sending from %s to %s:%d", peer, destination, port)
        remote = f'"$XDG_RUNTIME_DIR"/{self.unit}.conf'
        self.ssh(
            peer,
            f"cat > {remote} && "
            f"{{ systemctl --user stop {self.unit} 2>/dev/null; "
            f"systemd-run --user --unit={self.unit} --collect pipewire -c {remote}; }}",
            input=self.sender(destination, port),
        )

        if not self._state.exists():
            self._state.write_text(
                json.dumps({"peer": peer, "default_source": self.default_source()})
            )
        self.wait_for_source()
        run(["pactl", "set-default-source", self.unit], log=self.log, check=True)

    def stop_unit(self) -> None:
        run(["systemctl", "--user", "stop", self.unit], log=self.log)

    def stop(self) -> None:
        if not self._state.exists():
            self.log.info("%s not running", self.unit)
            self.stop_unit()
            return
        state = json.loads(self._state.read_text())

        self.log.info("restoring default source %s", state["default_source"])
        run(["pactl", "set-default-source", state["default_source"]], log=self.log)
        self.stop_unit()
        self._config.unlink(missing_ok=True)

        # A peer that already went away takes its sender with it; nothing to
        # stop there is not a failure.
        run(
            [
                *self.SSH,
                state["peer"],
                f'systemctl --user stop {self.unit}; rm -f "$XDG_RUNTIME_DIR"/{self.unit}.conf',
            ],
            log=self.log,
        )
        self._state.unlink()

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """Bring a remote microphone in as a local source."""
        create_logger(verbose, log_file="virtual-audio.log")

    @cli.command("start")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    @click.option(
        "--peer",
        envvar="SUNSHINE_CLIENT_NAME",
        required=True,
        help="Ssh host to take the microphone from. Defaults to SUNSHINE_CLIENT_NAME.",
    )
    @click.option("--port", type=int, default=DEFAULT_PORT, show_default=True)
    def cmd_start(name: str, peer: str, port: int):
        """Start the link and make it the default source."""
        VirtualAudio(name).start(peer, port)

    @cli.command("stop")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    def cmd_stop(name: str):
        """Stop the link and restore the previous default source."""
        VirtualAudio(name).stop()


if __name__ == "__main__":
    sys.exit(VirtualAudio.cli())
