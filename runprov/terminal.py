# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Capture what the run printed, without taking the output away from the terminal.

The field this replaces
-----------------------
`transformation_log.yml` carried `terminal_log_file: logs/<step>_v1_<stamp>.log` on most
entries, and it is the one field of that log with no `runprov` equivalent. It is also the
field this project has most often needed: RESUME.md §7 is a list of incidents whose only
evidence was what a run printed — a job that looked frozen while alive, a `$?` read from
the wrong end of a pipeline, a log showing stale metadata.

TEE, NEVER REDIRECT
-------------------
`_report.py` exists because the library once wrote to the caller's **stdout** and corrupted
a redirected artifact. Capturing output walks straight back toward that defect, so the rule
here is absolute: **everything written still reaches the original stream, unchanged and in
order.** This module copies; it never diverts. If the capture fails, the stream is restored
and the run continues — a provenance module that swallows a script's output is worse than
one that records nothing.

Two mechanisms, and why the difference is not a detail
------------------------------------------------------
An ordinary Python write reaches file descriptor 1 *through* `sys.stdout`. A **subprocess**
writes to file descriptor 1 **directly** and never touches `sys.stdout`. So:

* **fd** — `dup2` a pipe over fds 1 and 2 and mirror from a reader thread. Sees everything,
  including subprocesses.
* **python** — swap `sys.stdout`/`sys.stderr`. Sees only what this interpreter prints.
  A wrapped script's entire output is invisible.

The second is not a lesser version of the first; for a harness that wraps other programs it
records an **empty file that looks like a log**. That is why the mechanism is written into
the record as `capture: "fd" | "python"` rather than left to be inferred: a reader has to be
able to tell "the run printed nothing" from "this capture could never have seen it".

fd is attempted first and `python` is the fallback, announced when it happens — the same
shape as the `flock` downgrade in `sinks.py`, and for the same reason. The fallback is not
hypothetical: fds 1 and 2 can be closed outright (a daemonised job), and `dup2` then raises.

TWO CAPTURES AT ONCE
--------------------
fds 1 and 2 are process-global, so a second capture does not get its own copy of the
terminal: it `dup2`s over the first one's pipe, and the "original" it saves IS that pipe.
Captures therefore form a CHAIN — the inner one mirrors into the outer one, which mirrors
to the terminal — and it can only be taken apart from the inside out.

Nothing enforces that on the caller, so `_LIVE` tracks it here. A capture stopped while an
inner one is still running does not touch the descriptors; it closes its log, records
`out_of_order`, and is unwound when the inner one ends. Restoring out of order reinstalls a
pipe whose reader has already gone, and every subsequent write in the process disappears
into it — the failure this file's third paragraph calls worse than recording nothing.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
import typing

from ._report import diagnostic

#: Read size for the mirror thread. Large enough that a chatty run does not spin, small
#: enough that output reaches the terminal promptly — a capture that batches a progress bar
#: into invisibility has changed the thing it was meant to observe.
_CHUNK = 65536

#: Live fd captures, outermost first. **fds 1 and 2 are process-global**, so a second
#: capture started while a first is running does not get its own copy of the terminal — it
#: dup2s over the first one's pipe, and its saved "original" IS that pipe. Unwinding
#: therefore has to happen in reverse order of starting, and `stop()` cannot assume it is
#: the innermost. See `_stop_fd`; measured behaviour before this existed is in its comment.
_LIVE: list[Capture] = []
_LIVE_LOCK = threading.Lock()


class Capture:
    """A running capture. Started by `start()`, always ended by `stop()`.

    Not a context manager on purpose. `Run.__exit__` has to stop this **before** it hashes,
    and it must stop it on the failure path too — expressing that as a nested `with` inside
    `Run` would make the ordering implicit at exactly the point where the ordering is the
    whole correctness argument.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.mode: str = "none"
        self.error: str | None = None
        self.out_of_order = False
        self.prebound_handlers = 0
        self._fh: typing.IO[bytes] | None = None
        self._saved: dict[int, int] = {}
        self._thread: threading.Thread | None = None
        self._py_saved: tuple[typing.IO[str], typing.IO[str]] | None = None
        self._finished = False
        self._seen = 0

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        """Begin capturing. NEVER raises — a failure downgrades, then gives up quietly."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "wb")
        except OSError as exc:
            self.error = f"could not open {self.path}: {exc}"
            diagnostic(f"  WARNING: terminal capture off — {self.error}")
            return
        if self._start_fd():
            self.mode = "fd"
        elif self._start_python():
            self.mode = "python"
            diagnostic(
                "  PROVENANCE NOTICE: terminal capture fell back to PYTHON level "
                "(fd capture unavailable). Output from SUBPROCESSES will NOT appear in "
                f'{self.path.name} — recorded as capture: "python".'
            )
        else:
            self.error = self.error or "no capture mechanism available"
            diagnostic(f"  WARNING: terminal capture off — {self.error}")
            self._close_file()

    def _start_fd(self) -> bool:
        """`dup2` a pipe over fds 1 and 2, mirroring from a thread."""
        try:
            # Python's own buffers must reach the OLD fd before it moves, or their contents
            # land in the capture out of order with anything already written.
            _flush_std()
            rfd, wfd = os.pipe()
            self._saved = {1: os.dup(1), 2: os.dup(2)}
            os.dup2(wfd, 1)
            os.dup2(wfd, 2)
            os.close(wfd)
        except Exception as exc:  # guards-ok: `start()` promises NEVER to raise, and a
            # narrower tuple made that promise false — (OSError, ValueError, AttributeError)
            # let anything else through, out of start(), into the caller's __init__. Found
            # by a test that made `_flush_std` raise RuntimeError. The whole point of this
            # path is to degrade, so the catch has to be as wide as the promise.
            self.error = f"fd capture unavailable: {exc}"
            # No pump thread exists yet on this path, so the saved fds can go immediately.
            self._restore_fds()
            self._close_saved()
            return False
        # The mirror target is the SAVED fd — the real terminal — not fd 1, which is now the
        # pipe. Writing the mirror to fd 1 would feed the capture its own output forever.
        self._thread = threading.Thread(
            target=self._pump, args=(rfd, self._saved[1]), daemon=True, name="runprov-tee"
        )
        self._thread.start()
        with _LIVE_LOCK:
            _LIVE.append(self)
        return True

    def _pump(self, rfd: int, mirror: int) -> None:
        """Read the pipe until it closes; write every byte to the file AND the terminal."""
        try:
            while True:
                chunk = os.read(rfd, _CHUNK)
                if not chunk:
                    return
                fh = self._fh  # bound ONCE: an out-of-order stop closes it from another
                # thread, and re-reading the attribute between the check and the write is
                # exactly the window that produces a write to a closed file.
                if fh is not None:
                    try:
                        fh.write(chunk)
                        fh.flush()
                        self._seen += len(chunk)
                    except (OSError, ValueError):  # guards-ok: a full disk must not eat the
                        # output — the mirror below still runs, so the terminal keeps
                        # working and only the RECORD is short. The reverse would lose the
                        # run's output. ValueError is the closed-handle race above.
                        pass
                _write_all(mirror, chunk)
        except OSError:  # guards-ok: the pipe went away (process teardown). Nothing left
            # to mirror and nothing to report to — the run is already ending.
            return
        finally:
            try:
                os.close(rfd)
            except OSError:  # guards-ok: closing an already-closed descriptor raised
                # STRAIGHT OUT of the `finally`, past the handler that had just dealt with
                # the same condition — so the guard above protected nothing in the one case
                # it was written for. This ran on a thread, where an escaping exception is
                # printed by the interpreter and lost.
                pass

    def _start_python(self) -> bool:
        """Swap `sys.stdout`/`sys.stderr` for tees. Sees this interpreter only."""
        try:
            _flush_std()
            # Counted BEFORE the swap, because after it these handlers hold the only
            # remaining references to the streams being replaced. See `_prebound_handlers`.
            self.prebound_handlers = _prebound_handlers(sys.stdout, sys.stderr)
            self._py_saved = (sys.stdout, sys.stderr)
            # No `type: ignore` needed: `_StreamTee.__getattr__` returns Any, so mypy
            # accepts it as an IO[str] stand-in. An ignore here was unused, and an unused
            # ignore is a silenced check that silences nothing — `--strict` flags it.
            sys.stdout = _StreamTee(sys.stdout, self._fh)
            sys.stderr = _StreamTee(sys.stderr, self._fh)
        except Exception as exc:  # guards-ok: whatever went wrong, the streams must be
            # put back before this returns, or the caller loses its output entirely
            self.error = f"python capture unavailable: {exc}"
            self._restore_python()
            return False
        return True

    # ------------------------------------------------------------------- stop
    def stop(self) -> dict[str, typing.Any] | None:
        """End the capture and describe what was recorded. NEVER raises."""
        if self.mode == "none":
            return {"path": str(self.path), "capture": "none", "error": self.error}
        try:
            _flush_std()
            if self.mode == "fd":
                self._stop_fd()
            else:
                self._restore_python()
                self._close_file()
        except Exception as exc:  # guards-ok: this runs while an exception may already be
            # in flight; it must never become the failure the caller sees
            self.error = f"stopping capture failed: {exc}"
            diagnostic(f"  WARNING: {self.error}")
        finally:
            if self.mode == "fd" and not self.out_of_order:
                # A capture whose teardown never reached `_stop_fd` — this method can raise
                # in `_flush_std`, before the stack is touched at all — would otherwise stay
                # on `_LIVE` for the life of the process, and every capture started
                # afterwards would find itself "not innermost" and defer forever. One
                # failed stop would silently disable capture teardown for good.
                self._forget()
        return self.describe()

    def _stop_fd(self) -> None:
        """End an fd capture, respecting the fact that fds 1 and 2 are process-global.

        THE DEFECT THIS CLOSES. fds are one resource shared by every capture in the
        process. Capture B, started inside A, dup2s its pipe over A's — so B's "saved
        original" is *A's pipe*, not the terminal. Unwinding B then A restores the chain
        correctly. Unwinding **A then B** does not: B's restore reinstalls A's pipe over
        fd 1 after A has already stopped and closed its mirror. Measured before this
        existed: every subsequent `print` in the process went into a pipe nobody was
        reading, and stdout was gone for the rest of the run — the exact failure this
        module's docstring calls worse than recording nothing.

        So a capture that is not the innermost does NOT touch the descriptors. It closes
        its file (its log is complete and its record accurate) and leaves the pipe
        installed as the inner capture's mirror target. The innermost unwinds itself and
        then everything below it that is already finished, restoring the real terminal
        exactly once, at the bottom.
        """
        with _LIVE_LOCK:
            self._finished = True
            unwind: list[Capture] = []
            if self not in _LIVE:
                pass  # already unwound, or a second `stop()` — do not restore twice
            elif _LIVE[-1] is not self:
                self.out_of_order = True
            else:
                while _LIVE and _LIVE[-1]._finished:
                    unwind.append(_LIVE.pop())
        if self.out_of_order:
            # Finalise the LOG but not the descriptors, so `describe()` reports a file that
            # has stopped growing while the pump keeps mirroring to the terminal. Closing
            # it here is only safe because the pump tolerates the handle vanishing
            # mid-write — it races this call by construction.
            self._quiesce()
            self._close_file()
            diagnostic(
                "  PROVENANCE NOTICE: terminal capture stopped while an inner capture was "
                f"still running ({self.path.name}). Its log is closed here and does not "
                "include later output; the output descriptors stay installed until the "
                "inner capture ends."
            )
            return
        for cap in unwind:
            cap._teardown_fds()

    def _forget(self) -> None:
        """Drop this capture from the live stack. Idempotent; safe to call twice."""
        with _LIVE_LOCK:
            if self in _LIVE:
                _LIVE.remove(self)

    def _quiesce(self, limit: float = 2.0, idle: float = 0.05) -> None:
        """Wait, bounded, for the pump to finish what is already in the pipe.

        Only used by the out-of-order path, where the descriptors cannot be closed to
        force an EOF and there is therefore no *exact* end-of-log. Everything written
        while this capture was live is somewhere in a chain of pipes and pump threads that
        may not have been scheduled yet; closing the file the instant `stop()` is called
        loses all of it. Measured without this: an outer capture stopped immediately after
        a write recorded **none** of it.

        This is a HEURISTIC and is labelled as one — it waits for the byte count to stop
        moving, which cannot distinguish "drained" from "briefly idle". It narrows the
        window; it does not close it, which is why `out_of_order` goes into the record.
        """
        deadline = time.monotonic() + limit
        last = -1
        while time.monotonic() < deadline:
            seen = self._seen
            if seen == last:
                return
            last = seen
            time.sleep(idle)

    def _teardown_fds(self) -> None:
        """Put the descriptors back and stop the pump. ORDER IS LOAD-BEARING.

        `_restore_fds` puts fds 1 and 2 back, which drops the last write ends of the pipe
        so the pump's `os.read` returns empty and the thread finishes. The SAVED fds must
        stay open across that join: they are the mirror target, and closing them first
        makes every remaining `_write_all` fail with EBADF -- into a guard that returns
        quietly. Measured before this was split: the tail of the run reached the log file
        and never the terminal.
        """
        self._restore_fds()
        if self._thread is not None:
            # Bounded: the pipe's write ends are closed above, so the read returns empty
            # and the thread exits. The timeout is a backstop against a subprocess that
            # inherited the fd and is still holding it open — a provenance module must not
            # hang the run it is describing.
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self.error = "mirror thread did not finish within 5 s"
                diagnostic(
                    "  WARNING: terminal capture: a process still holds the "
                    "captured output open; the log may be short."
                )
        self._close_saved()
        self._close_file()

    def describe(self) -> dict[str, typing.Any]:
        """The record entry: where, how, how big, and whether it is trustworthy."""
        from .hashing import sha256

        rec: dict[str, typing.Any] = {"path": str(self.path), "capture": self.mode}
        if self.path.is_file():
            rec["sha256"] = sha256(self.path)
            rec["bytes"] = self.path.stat().st_size
        else:
            # Registered and absent is a FINDING, exactly as for any other output.
            rec["kind"] = "MISSING"
        if self.mode == "python":
            # Stated in the record, not only in a warning nobody kept. This is the field a
            # reader needs to know an empty log may mean "could not see" rather than
            # "printed nothing".
            rec["note"] = "python-level capture: output from subprocesses is NOT included"
            if self.prebound_handlers:
                # The subtler half, and the one that bites a well-behaved script hardest.
                # `logging.StreamHandler(sys.stdout)` stores the STREAM OBJECT it was given,
                # so a handler installed before the capture keeps writing to the original
                # stream and its lines never reach the log -- while a bare `print()`, which
                # resolves `sys.stdout` at call time, does. Measured: the log held
                # `PRINTED-DIRECTLY` and not `LOGGED-VIA-HANDLER`, from the same block.
                #
                # A script that routes everything through `logging` therefore gets a log
                # that looks complete and contains almost nothing. Reported rather than
                # repaired: rebinding another library's handlers from inside a provenance
                # module is the overreach `_report.py` exists to prevent.
                rec["prebound_stream_handlers"] = self.prebound_handlers
                rec["note"] += (
                    f"; {self.prebound_handlers} logging handler(s) were bound to the "
                    "original streams before capture started and are NOT included"
                )
        if self.out_of_order:
            # A reader comparing this log to the run's own start/finish times would
            # otherwise find output from AFTER the run ended and have no way to explain it.
            rec["out_of_order"] = True
            rec["note"] = (
                "this capture was stopped while an inner capture was still running, so the "
                "log also contains output written after this run finished"
            )
        if self.error:
            rec["error"] = self.error
        return rec

    # ---------------------------------------------------------------- helpers
    def _restore_fds(self) -> None:
        """Put fds 1 and 2 back. Leaves the saved copies OPEN — see `stop`."""
        for fd, saved in self._saved.items():
            try:
                os.dup2(saved, fd)
            except OSError:  # guards-ok: nothing useful remains to be done about a failed
                # restore, and raising here would replace the run's own outcome
                pass

    def _close_saved(self) -> None:
        """Release the mirror fds. Only after the pump thread has stopped using them."""
        for saved in self._saved.values():
            try:
                os.close(saved)
            except OSError:  # guards-ok: as above
                pass
        self._saved = {}

    def _restore_python(self) -> None:
        if self._py_saved is not None:
            sys.stdout, sys.stderr = self._py_saved
            self._py_saved = None

    def _close_file(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:  # guards-ok: the bytes are already flushed per write
                pass
            self._fh = None


class _StreamTee:
    """Text stream that writes through to `stream` and copies bytes to `fh`.

    Deliberately not a `TextIOBase` subclass. It has to stand in for whatever the caller
    already installed — pytest's capture object, a Jupyter shim, an `io.StringIO` — and
    inheriting from a concrete stream class would bring machinery that does not match the
    thing being replaced. Delegating by `__getattr__` keeps every attribute the original had.
    """

    def __init__(self, stream: typing.IO[str], fh: typing.IO[bytes] | None) -> None:
        self._stream = stream
        self._fh = fh

    def write(self, text: str) -> int:
        n = self._stream.write(text)
        if self._fh is not None:
            try:
                # UTF-8 explicitly, and `replace` so an unencodable character degrades the
                # LOG rather than the run — the same rule `_report._write` follows for
                # stderr, learned from a Windows console killing a run inside provenance.
                self._fh.write(text.encode("utf-8", "replace"))
                self._fh.flush()
            except (OSError, ValueError):  # guards-ok: the passthrough above already
                # happened, so the caller's output is safe; only the copy is lost
                pass
        return n

    def __getattr__(self, name: str) -> typing.Any:  # noqa: ANN401 - delegating proxy
        return getattr(self._stream, name)


def _prebound_handlers(out: typing.IO[str], err: typing.IO[str]) -> int:
    """How many `logging` handlers write to these exact stream OBJECTS.

    Only meaningful for the python-level fallback. fd-level capture moves the descriptor
    underneath every writer at once, so a handler bound to any of them is captured with no
    help. Swapping `sys.stdout` does not: a handler holds the object it was constructed
    with, and there is no way to reach it through the name.

    Read-only and never raises. It walks the logging manager, which is stdlib, and a
    failure to introspect it must not take down a capture that is already degrading.
    """
    try:
        import logging

        seen = 0
        loggers = [logging.getLogger()]
        loggers += [
            lg
            for lg in logging.Logger.manager.loggerDict.values()
            if isinstance(lg, logging.Logger)
        ]
        for lg in loggers:
            for handler in list(getattr(lg, "handlers", [])):
                stream = getattr(handler, "stream", None)
                if stream is out or stream is err:
                    seen += 1
        return seen
    except Exception:  # guards-ok: this is diagnostic only. A logging configuration that
        # cannot be walked is not a reason to fail the run, or the capture.
        return 0


def _flush_std() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception as exc:
            # flush has buffered bytes already beyond help, and this runs while fds are
            # mid-swap. Reporting it through `diagnostic` would write to the very stream
            # that just failed, so the exception is named and dropped deliberately.
            del exc


def _write_all(fd: int, data: bytes) -> None:
    """`os.write` may write short. Losing the tail of a line to that is not acceptable."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:  # guards-ok: the terminal is gone; the file copy still has it
            return
        if written <= 0:
            return
        view = view[written:]
