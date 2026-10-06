#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import base64
import fcntl
import html
import json
import logging
import os
import re
import subprocess
import time
from collections import deque
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

from deck import Key, Plugin, command


class Action(StrEnum):
    NOTIFY = "dev.kilic.system.notify"
    GAUGE = "dev.kilic.system.gauge"
    UPTIME = "dev.kilic.system.uptime"
    TIMER = "dev.kilic.system.timer"
    AGENT = "dev.kilic.system.agent"


class Metric(StrEnum):
    CPU = "cpu"
    MEM = "mem"
    TEMP = "temp"
    GPU = "gpu"


class NotifyLook(IntEnum):
    IDLE = 0
    DND = 1


class SystemKey(Key):
    @property
    def metric(self) -> Metric:
        return Metric(self.settings["metric"])

    @property
    def overlay(self) -> Metric | None:
        """A second metric, drawn as a line over the first one's area."""
        return Metric(self.settings["overlay"]) if "overlay" in self.settings else None

    @property
    def icon(self) -> str:
        return self.settings["icon"]

    @property
    def launch(self) -> str:
        return self.settings["launch"]


class SystemPlugin(Plugin):
    """Stream Deck plugin for the System page: notifications and live charts.

    Charts keep a minute of samples and draw them as an area over the key's
    own icon; presses open the same apps as before through `launch-app.py`."""

    LAUNCH = Path.home() / ".config/hypr/scripts/launch-app.py"
    ICONS = Path.home() / ".config/opendeck/plugins/dev.kilic.system.sdPlugin/icons"
    SAMPLE_SECONDS = 1.0
    SAMPLES = 60
    LINE = "#e5c07b"
    TILE = re.compile(r'<rect y="36" width="144" height="108" fill="(#[0-9a-f]{6})"/>')
    # The package sensor of each CPU vendor's hwmon driver.
    CPU_SENSORS = (("k10temp", "Tctl"), ("coretemp", "Package id 0"))
    # Persisted so a redeploy or an OpenDeck restart keeps the count.
    TIMER_STATE = (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / "opendeck/timer.json"
    )
    # Written by the agents' notify hook, one entry per tmux pane.
    WAITING = (
        Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        / "agents-waiting.json"
    )
    NOTIFY = Path.home() / ".config/wayland/scripts/notify.py"
    # An entry with no tmux pane can be neither checked nor focused.
    PANELESS_SECONDS = 30 * 60
    URGENT = "#e06c75"
    # Simple Icons marks (CC0) in the plugin's icons/agents/, by hook vendor.
    VENDORS: ClassVar[dict[str, str]] = {
        "Claude Code": "claude",
        "Codex": "openai",
        "OpenCode": "opencode",
    }
    WAITING_TILE = "#d19a66"
    TERMINAL = Path(
        "/usr/share/icons/Tela-yellow-dark/scalable/apps/utilities-terminal.svg"
    )
    RUNNING = "#98c379"
    PAUSED = "#e5c07b"
    KEY = SystemKey

    log = logging.getLogger("system-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.history: dict[Metric, deque[float]] = {
            metric: deque(maxlen=self.SAMPLES) for metric in Metric
        }
        self.readout: dict[Metric, str] = {}
        self.notifications: dict | None = None
        self.next_sample = 0.0
        self.cpu_times: tuple[int, int] | None = None
        self.cpu_sensor = self.find_cpu_sensor()
        self.gpu = self.find_gpu()
        # Wall-clock start of the running stretch, and the seconds banked
        # before it.
        self.waiting: list[dict] = []
        self.timer_started: float | None = None
        self.timer_banked = 0.0
        try:
            saved = json.loads(self.TIMER_STATE.read_text())
            self.timer_started, self.timer_banked = saved["started"], saved["banked"]
        except OSError, ValueError, KeyError:
            pass

    def query(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """A status read, traced at DEBUG since it runs every second."""
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2, check=False
        )
        if proc.stderr:
            self.log.debug("%s stderr: %s", cmd[0], proc.stderr.strip())

        return proc

    def find_cpu_sensor(self) -> Path | None:
        for hwmon in Path("/sys/class/hwmon").iterdir():
            name = (hwmon / "name").read_text().strip()
            for driver, label in self.CPU_SENSORS:
                if name != driver:
                    continue
                for path in hwmon.glob("temp*_label"):
                    if path.read_text().strip() == label:
                        return path.with_name(path.name.replace("_label", "_input"))

        return None

    @staticmethod
    def find_gpu() -> Path | None:
        """An NVIDIA card driving a connected display, or None.

        Every NVML query restarts the driver's runtime-PM idle timer, so a GPU
        that could otherwise sleep, a hybrid laptop's dGPU, is never asked."""
        for card in Path("/sys/class/drm").glob("card[0-9]"):
            driver = card / "device/driver"
            if not driver.exists() or driver.resolve().name != "nvidia":
                continue
            if any(
                (connector / "status").read_text().strip() == "connected"
                for connector in Path("/sys/class/drm").glob(f"{card.name}-*")
            ):
                return card

        return None

    def sample_cpu(self) -> None:
        fields = [int(x) for x in Path("/proc/stat").read_text().split()[1:8]]
        idle, total = fields[3] + fields[4], sum(fields)
        if self.cpu_times is not None:
            busy = 1 - (idle - self.cpu_times[0]) / max(1, total - self.cpu_times[1])
            self.history[Metric.CPU].append(busy)
            self.readout[Metric.CPU] = f"{busy * 100:.0f}%"
        self.cpu_times = (idle, total)

    def sample_mem(self) -> None:
        info = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        used = 1 - int(info["MemAvailable"].split()[0]) / int(
            info["MemTotal"].split()[0]
        )
        self.history[Metric.MEM].append(used)
        self.readout[Metric.MEM] = f"{used * 100:.0f}%"

    def sample_temp(self) -> None:
        if self.cpu_sensor is None:
            return
        celsius = int(self.cpu_sensor.read_text()) / 1000
        self.history[Metric.TEMP].append(min(1.0, max(0.0, (celsius - 30) / 70)))
        self.readout[Metric.TEMP] = f"{celsius:.0f}°"

    def sample_gpu(self) -> None:
        if self.gpu is None:
            return
        if (self.gpu / "device/power/runtime_status").read_text().strip() != "active":
            self.readout[Metric.GPU] = "asleep"
            return
        try:
            proc = self.query(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ]
            )
        except subprocess.TimeoutExpired:
            return
        if proc.returncode != 0:
            return

        busy, celsius = (int(x) for x in proc.stdout.split(",")[:2])
        self.history[Metric.GPU].append(busy / 100)
        self.readout[Metric.GPU] = f"{busy}% {celsius}°"

    def sample_notifications(self) -> None:
        try:
            count = self.query(["swaync-client", "--count", "--skip-wait"])
            dnd = self.query(["swaync-client", "--get-dnd", "--skip-wait"])
        except subprocess.TimeoutExpired:
            return
        if count.returncode != 0 or dnd.returncode != 0:
            self.notifications = None
            return

        self.notifications = {
            "count": int(count.stdout.strip() or 0),
            "dnd": dnd.stdout.strip() == "true",
        }

    def poll(self) -> None:
        if time.monotonic() < self.next_sample:
            return
        self.next_sample = time.monotonic() + self.SAMPLE_SECONDS

        metrics = {
            metric
            for key in self.keys.values()
            if key.action == Action.GAUGE
            for metric in (key.metric, key.overlay)
        }
        if Metric.CPU in metrics:
            self.sample_cpu()
        if Metric.MEM in metrics:
            self.sample_mem()
        if Metric.TEMP in metrics:
            self.sample_temp()
        if Metric.GPU in metrics:
            self.sample_gpu()
        if any(key.action == Action.NOTIFY for key in self.keys.values()):
            self.sample_notifications()
        if any(key.action == Action.AGENT for key in self.keys.values()):
            self.sample_waiting()

    def panes_seen(self) -> dict[str, bool] | None:
        """Every tmux pane, mapped to whether it is on screen; None without tmux.

        On screen means the active pane of its session's active window, in a
        session some client is attached to. Kitty's own focus is not asked,
        which would cost a `kitty @ ls` every second."""
        try:
            proc = self.query(
                [
                    "tmux",
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_id} #{pane_active}#{window_active}#{session_attached}",
                ]
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        panes = {}
        for line in proc.stdout.splitlines():
            pane, _, flags = line.partition(" ")
            panes[pane] = flags[:2] == "11" and flags[2:] not in ("", "0")

        return panes

    def edit_waiting(self, keep) -> None:
        """Rewrite the waiting list under the hook's lock, keeping what `keep` says."""
        with open(self.WAITING.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                waiting = json.loads(self.WAITING.read_text())
            except OSError, ValueError:
                waiting = []
            kept = [w for w in waiting if keep(w)]
            if kept != waiting:
                tmp = self.WAITING.with_suffix(".tmp")
                tmp.write_text(json.dumps(kept))
                tmp.replace(self.WAITING)
        self.waiting = sorted(kept, key=lambda w: (not w.get("urgent"), w.get("at", 0)))

    def sample_waiting(self) -> None:
        if not self.WAITING.exists():
            self.waiting = []
            return

        panes = self.panes_seen()

        def keep(entry: dict) -> bool:
            if not entry.get("pane"):
                return time.time() - entry.get("at", 0) < self.PANELESS_SECONDS
            if panes is None:
                return True

            return panes.get(entry["pane"]) is False

        self.edit_waiting(keep)

    def agent(self, key: SystemKey) -> dict | None:
        index = int(key.settings["slot"])
        return self.waiting[index] if index < len(self.waiting) else None

    def agent_image(self, key: SystemKey) -> str:
        """The waiting agent's mark over its project name, in big type.

        A dark key with a faint terminal while nothing waits in this slot."""
        entry = self.agent(key)
        if entry is None:
            icon = base64.b64encode(self.TERMINAL.read_bytes()).decode()
            return self.uri(
                '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
                '<rect width="144" height="144" fill="#17191e"/>'
                f'<image href="data:image/svg+xml;base64,{icon}" x="40" y="52" width="64" height="64"'
                ' opacity="0.3"/></svg>'
            )

        colour = self.URGENT if entry.get("urgent") else self.WAITING_TILE
        vendor = self.VENDORS.get(entry.get("vendor", ""), "claude")
        mark = re.search(
            r' d="([^"]+)"', (self.ICONS / f"agents/{vendor}.svg").read_text()
        )
        project = entry.get("directory") or "?"
        if len(project) > 12:
            project = project[:11] + "…"
        size = min(26, round(230 / max(len(project), 1)))
        extra = len(self.waiting) - 2 if key.settings["slot"] == 1 else 0
        profile = html.escape(entry.get("profile") or "")

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            '<rect width="144" height="144" fill="#17191e"/>'
            f'<rect y="36" width="144" height="108" fill="{colour}"/>'
            f'<text x="{60 if extra > 0 else 72}" y="28" font-family="Liberation Sans" font-size="24"'
            f' font-weight="bold" fill="#e5e5e5" text-anchor="middle">{profile}</text>'
            + (
                f'<text x="138" y="28" font-family="Liberation Sans" font-size="24" font-weight="bold"'
                f' fill="{colour}" text-anchor="end">+{extra}</text>'
                if extra > 0
                else ""
            )
            + (
                f'<g transform="translate(50 44) scale(1.8333)" fill="#17191e"><path d="{mark[1]}"/></g>'
                if mark
                else ""
            )
            + f'<text x="72" y="130" font-family="Liberation Sans" font-size="{size}" font-weight="bold"'
            f' fill="#17191e" text-anchor="middle">{html.escape(project)}</text></svg>'
        )

    def timer_elapsed(self) -> float:
        running = time.time() - self.timer_started if self.timer_started else 0.0

        return self.timer_banked + running

    def timer_save(self) -> None:
        self.TIMER_STATE.parent.mkdir(parents=True, exist_ok=True)
        self.TIMER_STATE.write_text(
            json.dumps({"started": self.timer_started, "banked": self.timer_banked})
        )

    def timer_toggle(self) -> None:
        if self.timer_started:
            self.timer_banked, self.timer_started = self.timer_elapsed(), None
        else:
            self.timer_started = time.time()
        self.timer_save()
        self.render()

    def timer_reset(self) -> None:
        self.timer_started, self.timer_banked = None, 0.0
        self.timer_save()
        self.render()

    def timer(self) -> str:
        """Elapsed time in large digits, ringed by a sweep that laps each minute."""
        elapsed = self.timer_elapsed()
        if not elapsed and not self.timer_started:
            return self.uri((self.ICONS / "uptime.svg").read_text())

        whole = int(elapsed)
        hours, rest = divmod(whole, 3600)
        digits = (
            f"{hours}:{rest // 60:02d}:{rest % 60:02d}"
            if hours
            else f"{rest // 60}:{rest % 60:02d}"
        )
        colour = self.RUNNING if self.timer_started else self.PAUSED
        circumference = 2 * 3.14159 * 48
        sweep = circumference * (elapsed % 60) / 60

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            '<rect width="144" height="144" fill="#17191e"/>'
            '<circle cx="72" cy="88" r="48" fill="none" stroke="#3e4451" stroke-width="8"/>'
            f'<circle cx="72" cy="88" r="48" fill="none" stroke="{colour}" stroke-width="8"'
            f' stroke-dasharray="{sweep:.1f} {circumference:.1f}" transform="rotate(-90 72 88)"/>'
            f'<text x="72" y="99" font-family="Liberation Sans" font-weight="bold"'
            f' font-size="{22 if hours else 30}" fill="{colour}" text-anchor="middle">{digits}</text>'
            "</svg>"
        )

    @staticmethod
    def uri(svg: str) -> str:
        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

    def chart(self, key: SystemKey) -> str:
        """The key's icon over a faded tile, with the history as a filled area."""
        svg = (self.ICONS / f"{key.icon}.svg").read_text()
        step = 144 / (self.SAMPLES - 1)

        def points(metric: Metric) -> tuple[float, str] | None:
            samples = list(self.history[metric])
            if len(samples) < 2:
                return None
            offset = 144 - step * (len(samples) - 1)

            return offset, " ".join(
                f"{offset + i * step:.1f},{144 - 108 * value:.1f}"
                for i, value in enumerate(samples)
            )

        area = ""
        if series := points(key.metric):
            area = rf'<polygon points="{series[0]:.1f},144 {series[1]} 144,144" fill="\1"/>'
        if key.overlay and (series := points(key.overlay)):
            area += (
                f'<polyline points="{series[1]}" fill="none" stroke="{self.LINE}"'
                ' stroke-width="5" stroke-linejoin="round" stroke-linecap="round"/>'
            )

        return self.uri(
            self.TILE.sub(
                rf'<rect y="36" width="144" height="108" fill="\1" fill-opacity="0.35"/>{area}',
                svg,
            )
        )

    def image(self, context: str, key: SystemKey) -> str | None:
        if key.action == Action.TIMER:
            return self.timer()
        if key.action == Action.AGENT:
            return self.agent_image(key)
        if key.action != Action.GAUGE:
            return None

        return self.chart(key)

    def look(self, context: str, key: SystemKey) -> tuple[int, str]:
        match key.action:
            case Action.NOTIFY:
                state = self.notifications
                if state is None:
                    return NotifyLook.IDLE, ""
                if state.get("dnd"):
                    return NotifyLook.DND, f"dnd {state['count']}"

                return NotifyLook.IDLE, str(state["count"]) if state["count"] else ""
            case Action.GAUGE if key.overlay:
                return 0, " ".join(
                    f"{metric.value[0]}{self.readout[metric]}"
                    for metric in (key.metric, key.overlay)
                    if metric in self.readout
                )
            case Action.GAUGE:
                return 0, self.readout.get(key.metric, "")
            case Action.TIMER:
                return 0, ""
            case Action.AGENT:
                return 0, ""
            case Action.UPTIME:
                minutes = int(float(Path("/proc/uptime").read_text().split()[0])) // 60
                days, hours = divmod(minutes // 60, 24)
                return 0, f"{days}d {hours}h" if days else f"{hours}h {minutes % 60}m"

        raise ValueError(f"unknown action {key.action}")

    def press(self, context: str, key: SystemKey) -> None:
        match key.action:
            case Action.NOTIFY:
                self.spawn(["swaync-client", "--toggle-panel", "--skip-wait"])
            case Action.GAUGE:
                self.spawn([str(self.LAUNCH), key.launch])
            case Action.TIMER:
                self.timer_toggle()
            case Action.AGENT if (entry := self.agent(key)) is not None:
                if entry.get("pane"):
                    self.spawn([str(self.NOTIFY), "focus", "--pane", entry["pane"]])
                self.dismiss(entry)

    def dismiss(self, entry: dict) -> None:
        self.edit_waiting(lambda w: w != entry)
        self.render()

    def holds(self, key: SystemKey) -> bool:
        return key.action in (Action.NOTIFY, Action.TIMER, Action.AGENT)

    def hold_status(self, key: SystemKey) -> str | None:
        match key.action:
            case Action.TIMER:
                return "reset"
            case Action.AGENT:
                return "dismiss"

        return None

    def hold(self, context: str, key: SystemKey) -> None:
        if key.action == Action.TIMER:
            return self.timer_reset()
        if key.action == Action.AGENT:
            if (entry := self.agent(key)) is not None:
                self.dismiss(entry)
            return

        self.spawn(["swaync-client", "--toggle-dnd", "--skip-wait"])
        self.next_sample = 0.0


if __name__ == "__main__":
    command(SystemPlugin)()
