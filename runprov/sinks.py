# Copyright (c) 2026 Hôpital Henri-Mondor and Taylor Thompson
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
        def append(self, record: dict) -> None:
            self.conn.execute("INSERT INTO runs VALUES (?)", [json.dumps(record)])
            self.conn.commit()

    configure(root=ROOT, sink=SqliteSink(conn))

The default remains `JsonlSink`, because a file that git can diff and a reviewer can read
without running anything is the right default for published work.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import typing

if typing.TYPE_CHECKING:  # pragma: no cover
    pass


@typing.runtime_checkable
class RecordSink(typing.Protocol):
    """Somewhere a run record can be appended.

    `runtime_checkable` so a misconfigured project fails at `configure()` with a clear
    message rather than at the end of a long run, when the record is about to be written
    and the work is already done.
    """

    def append(self, record: dict) -> None:
        """Persist one record. MUST NOT raise: a sink that can abort a run gets removed
        from the run, and then nothing is recorded at all."""
        ...  # pragma: no cover


class JsonlSink:
    """One JSON object per line, appended under an exclusive lock, fsync'd.

    JSONL rather than a single YAML/JSON document because the predecessor's single-document
    log became unparseable and eleven repair scripts grew around it. Here a corrupt line
    costs one record and the reader counts what it skipped.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def append(self, record: dict) -> None:
        line = json.dumps(record, default=str) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                with _exclusive(fh):
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
        except Exception as exc:  # never let recording break a run
            print(f"  WARNING: could not append to run history: {exc}")


class MemorySink:
    """Collects records in a list. For tests, and for a caller assembling its own report.

    Included because the alternative is every test monkeypatching file I/O, and a test
    that fakes the thing it is testing is how this project got a fix that could never fail.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


import contextlib  # noqa: E402 - kept next to its only user for readability


@contextlib.contextmanager
def _exclusive(fh: typing.IO[str]) -> typing.Iterator[None]:
    """Exclusive lock for the duration of the block.

    Two implementations, and the Windows half exists only because Windows CI FAILED: 24
    concurrent appends produced 23 lines. `fcntl` raises there, the code fell through to an
    unlocked append, and a record was lost silently. Documenting a fallback is not the same
    as having one that works.

    Measured on the project this came from: history lines median 2,032 bytes, max 7,274,
    and 65 of 1,908 over 4,096 — the size below which POSIX guarantees an O_APPEND write is
    atomic. Above it, concurrent writers interleave into a line that is not JSON.
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
            print(
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
