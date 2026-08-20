# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Where the run history goes — the one extension point that earns its keep.

A PROTOCOL, not an abstract base class
--------------------------------------
`RecordSink` is a `typing.Protocol`: anything with a matching `append` is a sink, with no
inheritance and no import of this module. That is deliberate and it is the smaller design.

An ABC would require every implementer to subclass, which drags `runprov` into their type
hierarchy and gives nothing back — there is no shared behaviour to inherit, only a shape to
agree on. Structural typing states the shape and stops. Deep hierarchies in a 700-line
package would be decoration, and this package's entire claim is that it is the smallest
thing that does the job.

Why an extension point exists here and nowhere else
---------------------------------------------------
Everything else in `runprov` has exactly one correct implementation: a SHA-256 is a
SHA-256. **Where records are kept is the one genuine variable.** One project wants a
git-tracked JSONL beside the code; a lab running many pipelines wants one shared database
so a question can be asked across all of them. Both are right, and neither should have to
fork the package.

    class SqliteSink:
        def append(self, record: dict[str, typing.Any]) -> None:
            self.conn.execute("INSERT INTO runs VALUES (?)", [json.dumps(record)])
            self.conn.commit()

    configure(root=ROOT, sink=SqliteSink(conn))

The default remains `JsonlSink`, because a file that git can diff and a reviewer can read
without running anything is the right default for published work.
"""

from __future__ import annotations

# A MODULE'S `__all__` RATIFIES THE PACKAGE'S PROMISE; IT NEVER MAKES ONE.
# A name belongs here if and only if `runprov/__init__.py` re-exports it and lists it in the
# package `__all__` (22 names, decided 2026-08-19, ledger L-24). Nothing else qualifies:
# cross-module use inside `runprov/` is INTERNAL and `__all__` neither describes nor protects
# it; tests reach into internals on purpose and prove nothing; and prose that documents a
# printed string, a CLI flag or a record key is not an instruction to call a name.
# Adding or withdrawing a promise is a package-level decision taken in `__init__.py`.
# `TeeSink` and `YamlLogSink` are not promised: no document tells a reader to compose
# sinks, and `YamlLogSink` writes `transformation_log.yml` specifically — a name whose
# docstring needs a paragraph to correct it should be renamed before it is frozen.
__all__ = ["JsonlSink", "RecordSink"]

import json
import os
import pathlib
import sys
import typing

from ._report import diagnostic

if typing.TYPE_CHECKING:  # pragma: no cover
    pass


@typing.runtime_checkable
class RecordSink(typing.Protocol):
    """Somewhere a run record can be appended.

    `runtime_checkable` so a misconfigured project fails at `configure()` with a clear
    message rather than at the end of a long run, when the record is about to be written
    and the work is already done.
    """

    def append(self, record: dict[str, typing.Any]) -> None:
        """Persist one record. MUST NOT raise: a sink that can abort a run gets removed
        from the run, and then nothing is recorded at all."""
        ...  # pragma: no cover


class JsonlSink:
    """One JSON object per line, appended under an exclusive lock, fsync'd.

    JSONL rather than a single YAML/JSON document because the predecessor's single-document
    log became unparseable and nine repair scripts grew around it. Here a corrupt line
    costs one record and the reader counts what it skipped.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def append(self, record: dict[str, typing.Any]) -> None:
        line = json.dumps(record, default=str) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # BINARY, and explicitly UTF-8 encoded here. Text mode cannot seek to inspect the
            # last byte without a decode, and the encoding is not the platform's business:
            # a history whose bytes depend on the writer's locale is the defect `header()`
            # already learned.
            with open(self.path, "ab+") as fh:
                with _exclusive(fh):
                    # R7. A process SIGKILLed mid-append leaves a line with no terminator --
                    # measured, 5 of 12 trials. O_APPEND then puts the NEXT write at that
                    # fragment's end, so the new record is concatenated onto it and BOTH are
                    # unreadable. The torn one was lost anyway; the next one is collateral,
                    # and it is the one that had nothing wrong with it. Worse, the reader
                    # counts the result as ONE unreadable line, understating the loss by
                    # exactly the record it did not know it had destroyed.
                    #
                    # One newline closes it: the fragment becomes its own unreadable line,
                    # the new record lands intact on the next, and the count is true again.
                    # Inside the lock, because a concurrent writer must not see the file
                    # between the check and the fix.
                    fh.seek(0, os.SEEK_END)
                    if fh.tell():
                        fh.seek(-1, os.SEEK_END)
                        if fh.read(1) != b"\n":
                            fh.write(b"\n")
                    fh.write(line.encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())
        except Exception as exc:  # never let recording break a run
            # DIAGNOSTIC: a record that was meant to be kept was not kept. stderr, and
            # RUNPROV_QUIET does not reach it -- losing history silently is the failure
            # this whole package exists to make impossible.
            diagnostic(f"  WARNING: could not append to run history: {exc}")


class YamlLogSink:
    """The project-wide transformation log: one YAML entry per run, appended forever.

    THE FILE THIS PACKAGE EXISTS BECAUSE OF, written the way that file should have been.
    `transformation_log.yml` is what a person opens to read the story of a project, and the
    predecessor's copy is also the file that stopped parsing at line 14,547 of 24,300 and
    grew nine repair scripts around it. So this is a VIEW and not the record: `runs.jsonl`
    stays the source of truth, and if this file is ever damaged it can be regenerated from
    the history with `python -m runprov log --format yaml`.

    APPENDED, NEVER REGENERATED, and that is the whole design. Rewriting the file after each
    run would read the entire history to write one entry -- O(n) per run and O(n^2) over a
    project, which is the cost that surfaces in year two, on exactly the long-lived history
    this is for. One entry is appended, under the same lock and with the same torn-line
    repair as `JsonlSink`, so the cost of recording run 10,000 is the cost of recording
    run 1.

    ENTRIES, NOT DOCUMENTS. The file is one YAML list -- `- step:` per run, the shape the
    original transformation log used -- so `yaml.safe_load` returns every run in one list
    and a reader needs no `safe_load_all`. Every scalar is quoted (see `show._q`), which is
    precisely the defect that killed the predecessor: it quoted only what its author thought
    needed quoting, and one hand-typed `Note:` inside a description ended the file.

    A BAD ENTRY HERE COSTS THE YAML AND NOT THE RECORD. That is the reason the truth lives
    in the JSONL: a corrupt line there costs one line, while a corrupt block in a single
    YAML document costs everything after it. Both files are written; only one is trusted.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def append(self, record: dict[str, typing.Any]) -> None:
        from .show import _yaml_entry, _yaml_header  # local: `show` must not import sinks

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab+") as fh:
                with _exclusive(fh):
                    fh.seek(0, os.SEEK_END)
                    if not fh.tell():
                        # The banner, once, when the file is created. It says what the file
                        # is and -- more importantly -- what it is NOT: the record of truth.
                        fh.write(
                            _yaml_header(
                                "MAINTAINED by runprov — one entry appended per run. This is a "
                                "VIEW;\n# the record of truth is the run history beside it, and "
                                "this file can be\n# rebuilt at any time with `python -m runprov "
                                "log --format yaml`"
                            ).encode("utf-8")
                        )
                    else:
                        # Same torn-line repair as `JsonlSink`, for the same reason: a
                        # process killed mid-append leaves a fragment, and O_APPEND would
                        # concatenate the next entry onto it and lose both.
                        fh.seek(-1, os.SEEK_END)
                        if fh.read(1) != b"\n":
                            fh.write(b"\n")
                    fh.write(_yaml_entry(record).encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())
        except Exception as exc:  # never let recording break a run
            diagnostic(f"  WARNING: could not append to the transformation log: {exc}")


class TeeSink:
    """Every record to each sink in turn. One run, several destinations.

    Exists so the project can keep `runs.jsonl` as the record of truth AND maintain the
    human-readable `transformation_log.yml` beside it, without either knowing about the
    other. A failing sink does not stop the ones after it: each already swallows its own
    errors and says so on stderr, and a YAML view that could not be written must not cost
    the JSONL line that is the actual record.
    """

    def __init__(self, *sinks: RecordSink) -> None:
        self.sinks = sinks

    def append(self, record: dict[str, typing.Any]) -> None:
        for sink in self.sinks:
            sink.append(record)


class MemorySink:
    """Collects records in a list. For tests, and for a caller assembling its own report.

    Included because the alternative is every test monkeypatching file I/O, and a test
    that fakes the thing it is testing is how this project got a fix that could never fail.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, typing.Any]] = []

    def append(self, record: dict[str, typing.Any]) -> None:
        self.records.append(record)


import contextlib  # noqa: E402 - kept next to its only user for readability


@contextlib.contextmanager
def _exclusive(fh: typing.IO[typing.Any]) -> typing.Iterator[None]:
    """Exclusive lock for the duration of the block.

    Two implementations, and the Windows half exists only because Windows CI FAILED: 24
    concurrent appends produced 23 lines. `fcntl` raises there, the code fell through to an
    unlocked append, and a record was lost silently. Documenting a fallback is not the same
    as having one that works.

    Measured on the project this came from -- `hcv-genotyping-release/reports/audit/runs.jsonl`,
    2026-08-18: 2,453 lines, median 2,149 bytes, max 10,661, and 205 over 4,096, the size
    below which POSIX *guarantees* an O_APPEND write is atomic. That file is appended to
    daily, so this is a SNAPSHOT and re-measuring gives different numbers; an earlier
    reading here said 1,908 lines and 65 over, which is the same file three weeks younger.
    The durable claim is that some lines exceed PIPE_BUF, true at every measurement.

    CORRECTION, because the first version of this comment overstated the case: on Linux
    ext4 that bound is not what bites. With locking disabled entirely, 8 processes x 20
    appends of 9 KB produced 160/160 intact records, because Linux holds the inode lock
    across the whole write(). The lock is still right to have -- POSIX promises atomicity
    only below PIPE_BUF, NFS and CIFS do not honour it at all, and Windows has no O_APPEND
    semantics of this kind -- but it is a PORTABILITY property, not a Linux one, and the
    mechanism originally cited here is not the one that fails.
    """
    locked: str | None = None
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        locked = "fcntl"
    except (ImportError, OSError):
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = "msvcrt"
        except (ImportError, OSError):
            pass
    if locked is None:
        # OUTSIDE the platform branches, and that is the whole fix. This notice used to
        # sit inside the win32-only inner `except`, so a POSIX `flock` that raised -- an
        # NFS or CIFS mount, a container without the syscall -- degraded to an unlocked
        # append and said NOTHING. Those filesystems are the lock's entire justification,
        # so the one case that most deserved announcing was the one case that could not.
        # Verified by stubbing fcntl to raise: the line was written, no notice appeared.
        diagnostic(
            "  NOTE: no file locking available; run history appended unlocked. "
            "Concurrent writers may interleave."
        )
    try:
        yield
    finally:
        if locked == "fcntl":
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif locked == "msvcrt" and sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
