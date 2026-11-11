"""Which programs are recording from a microphone right now.

A program that records opens a capture stream (a PipeWire source output) for
as long as it listens and closes it after, so the set of capture streams is
the recording state of every program on the machine, however it was started
or stopped. `Capturing` follows it from PipeWire's own events rather than a
poll."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import pulsectl


class Capturing:
    """The process binaries holding a capture stream open, kept current on a
    background thread from source-output events."""

    RETRY_SECONDS = 5.0

    log = logging.getLogger(__name__)

    def __init__(self) -> None:
        self.binaries: frozenset[str] = frozenset()
        self.listed = False
        self.changed = threading.Condition()
        threading.Thread(target=self.follow, daemon=True).start()

    def __contains__(self, binary: str) -> bool:
        return binary in self.binaries

    def wait(
        self, predicate: Callable[[frozenset[str]], bool], timeout: float | None
    ) -> bool:
        """Block until `predicate` holds for a listing, or `timeout` passes.

        Only a real listing counts, so the empty set held before the first one
        never satisfies a wait for a program to stop."""
        with self.changed:
            return self.changed.wait_for(
                lambda: self.listed and predicate(self.binaries), timeout
            )

    def publish(self, binaries: frozenset[str], listed: bool) -> None:
        with self.changed:
            self.binaries = binaries
            self.listed = listed
            self.changed.notify_all()

    def follow(self) -> None:
        def on_event(_event: pulsectl.PulseEventInfo) -> None:
            # Queries are not allowed inside the callback; leaving the loop
            # hands control back to re-list the streams.
            raise pulsectl.PulseLoopStop

        while True:
            try:
                with pulsectl.Pulse("dotlib-capturing") as pulse:
                    pulse.event_mask_set("source_output")
                    pulse.event_callback_set(on_event)
                    while True:
                        self.publish(
                            frozenset(
                                output.proplist.get("application.process.binary", "")
                                for output in pulse.source_output_list()
                            ),
                            True,
                        )
                        self.log.debug("capturing: %s", sorted(self.binaries))
                        pulse.event_listen()
            except pulsectl.PulseError as e:
                self.log.warning("pulse connection lost: %s", e)
            self.publish(frozenset(), False)
            time.sleep(self.RETRY_SECONDS)
