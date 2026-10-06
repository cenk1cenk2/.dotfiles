#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import base64
import logging
import re
import subprocess
import time
from collections import deque
from enum import IntEnum, StrEnum
from pathlib import Path

from deck import Key, Plugin, command


class Action(StrEnum):
    NOTIFY = "dev.kilic.system.notify"
    GAUGE = "dev.kilic.system.gauge"


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

        raise ValueError(f"unknown action {key.action}")

    def press(self, context: str, key: SystemKey) -> None:
        match key.action:
            case Action.NOTIFY:
                self.spawn(["swaync-client", "--toggle-panel", "--skip-wait"])
            case Action.GAUGE:
                self.spawn([str(self.LAUNCH), key.launch])

    def holds(self, key: SystemKey) -> bool:
        return key.action == Action.NOTIFY

    def hold(self, context: str, key: SystemKey) -> None:
        self.spawn(["swaync-client", "--toggle-dnd", "--skip-wait"])
        self.next_sample = 0.0


if __name__ == "__main__":
    command(SystemPlugin)()
