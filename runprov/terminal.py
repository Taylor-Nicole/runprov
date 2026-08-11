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
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import typing

from ._report import diagnostic

#: Read size for the mirror thread. Large enough that a chatty run does not spin, small
#: enough that output reaches the terminal promptly — a capture that batches a progress bar
#: into invisibility has changed the thing it was meant to observe.
_CHUNK = 65536


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
        self._fh: typing.IO[bytes] | None = None
        self._saved: dict[int, int] = {}
        self._thread: threading.Thread | None = None
        self._py_saved: tuple[typing.IO[str], typing.IO[str]] | None = None

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
        return True

    def _pump(self, rfd: int, mirror: int) -> None:
        """Read the pipe until it closes; write every byte to the file AND the terminal."""
        try:
            while True:
                chunk = os.read(rfd, _CHUNK)
                if not chunk:
                    return
                if self._fh is not None:
                    try:
                        self._fh.write(chunk)
                        self._fh.flush()
                    except OSError:  # guards-ok: a full disk must not eat the output —
                        # the mirror below still runs, so the terminal keeps working and
                        # only the RECORD is short. The reverse would lose the run's output.
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
                # ORDER IS LOad-BEARING, and getting it wrong loses output silently.
                # `_restore_fds` puts fds 1 and 2 back, which drops the last write ends of
                # the pipe so the pump's `os.read` returns empty and the thread finishes.
                # The SAVED fds must stay open across that join: they are the mirror
                # target, and closing them first makes every remaining `_write_all` fail
                # with EBADF -- into a guard that returns quietly. Measured before this was
                # split: the tail of the run reached the log file and never the terminal.
                self._restore_fds()
                if self._thread is not None:
                    # Bounded: the pipe's write ends are closed above, so the read returns
                    # empty and the thread exits. The timeout is a backstop against a
                    # subprocess that inherited the fd and is still holding it open — a
                    # provenance module must not hang the run it is describing.
                    self._thread.join(timeout=5.0)
                    if self._thread.is_alive():
                        self.error = "mirror thread did not finish within 5 s"
                        diagnostic(
                            "  WARNING: terminal capture: a process still holds the "
                            "captured output open; the log may be short."
                        )
                self._close_saved()
            else:
                self._restore_python()
            self._close_file()
        except Exception as exc:  # guards-ok: this runs while an exception may already be
            # in flight; it must never become the failure the caller sees
            self.error = f"stopping capture failed: {exc}"
            diagnostic(f"  WARNING: {self.error}")
        return self.describe()

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
