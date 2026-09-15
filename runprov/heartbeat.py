# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Proof of life during a silent stretch of a long run.

WHY SILENCE AND NOT A METRONOME. A line every thirty seconds regardless is noise during
active work and tells you nothing you did not already see. This fires only when nothing has
happened for the interval, and every registered read, write or step resets it — so during
work you see real events, and during a twenty-minute computation you see that the run is
still there and what it last did.

WHAT A THREAD COSTS HERE, and why each of these is handled rather than hoped about:

* **Signals reach the main thread only.** CPython delivers them there, so this thread never
  sees SIGTERM. When `scancel` arrives the main thread raises `Terminated` and unwinds while
  this one would keep printing — a "still running" line landing after "record written" is the
  package lying about itself. `Run.__exit__` stops it before assembling the record.
* **Daemon, and stopped anyway.** A non-daemon thread keeps a finished process alive; a
  daemon thread is killed abruptly at shutdown and can raise from inside a module being torn
  down. So: daemon for safety, explicit stop for correctness, `atexit` as the backstop.
* **Two writers to one stderr**, which `_report._WRITE_LOCK` serialises.
* **It must never kill the run it is describing.** The tick swallows everything, the same
  rule `_report._write` follows, and for the same reason.

TESTABLE WITHOUT A THREAD. `tick()` is the whole decision and takes the clock as an argument,
so the behaviour is tested directly and the thread is tested only for starting and stopping.
A heartbeat whose logic can only be exercised by sleeping is a heartbeat nobody tests.
"""

from __future__ import annotations

__all__: list[str] = []

import threading
import time
import typing

#: Silence, in seconds, before the run says it is still there. Long enough that ordinary work
#: never reaches it, short enough that a stalled run is noticed within a coffee.
DEFAULT_INTERVAL = 30.0


class Heartbeat:
    """Says the run is alive after `interval` seconds with no event.

    `beat(text)` is called with the line to emit; `clock` returns monotonic seconds. Both are
    arguments so the decision can be tested without a thread and without sleeping.
    """

    def __init__(
        self,
        interval: float,
        beat: typing.Callable[[str], None],
        *,
        clock: typing.Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = interval
        self._beat = beat
        self._clock = clock
        self._started = clock()
        self._last_event = self._started
        self._last_note = "starting"
        self._beats = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the decision
    def saw(self, note: str) -> None:
        """Record that something happened. Resets the silence, and names what it was."""
        with self._lock:
            self._last_event = self._clock()
            self._last_note = note

    def tick(self) -> bool:
        """One decision. True when a beat was emitted.

        Silent unless the run has been quiet for the whole interval — so a run producing
        events steadily never beats at all, which is the point.
        """
        with self._lock:
            now = self._clock()
            if now - self._last_event < self.interval:
                return False
            self._last_event = now
            self._beats += 1
            elapsed = int(now - self._started)
            note = self._last_note
        try:
            self._beat(f"  [{elapsed // 60:02d}:{elapsed % 60:02d}] still running — last: {note}")
        except Exception:  # a message is worth less than the run it is about
            return False
        return True

    # ------------------------------------------------------------------ the thread
    def start(self) -> None:
        """Begin beating. Idempotent; a zero or negative interval never starts."""
        if self._thread is not None or self.interval <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name="runprov-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:  # pragma: no cover - the body is `tick`, tested directly
        # `Event.wait` rather than `sleep`: a stop is acted on at once rather than after the
        # rest of an interval, so exiting a run does not hang for up to thirty seconds.
        while not self._stop.wait(min(self.interval, 1.0)):
            self.tick()

    def stop(self) -> None:
        """Stop beating, and wait briefly for the thread to notice. Idempotent.

        Called from `Run.__exit__` BEFORE the record is assembled, so no beat can be printed
        after the run has reported itself finished.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def beats(self) -> int:
        """How many times it reported. Zero for a run that never fell silent."""
        return self._beats
