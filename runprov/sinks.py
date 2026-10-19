# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
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
# package `__all__` (decided 2026-08-19, ledger L-24; the list, and its count, live
# there and nowhere else). Nothing else qualifies:
# cross-module use inside `runprov/` is INTERNAL and `__all__` neither describes nor protects
# it; tests reach into internals on purpose and prove nothing; and prose that documents a
# printed string, a CLI flag or a record key is not an instruction to call a name.
# Adding or withdrawing a promise is a package-level decision taken in `__init__.py`.
# `TeeSink` and `YamlLogSink` are not promised: no document tells a reader to compose
# sinks, and `YamlLogSink` writes `transformation_log.yml` specifically — a name whose
# docstring needs a paragraph to correct it should be renamed before it is frozen.
__all__ = ["JsonlSink", "RecordSink"]

import errno
import json
import os
import pathlib
import sys
import threading
import typing

from . import chain
from ._report import diagnostic

if typing.TYPE_CHECKING:  # pragma: no cover
    pass


@typing.runtime_checkable
class RecordSink(typing.Protocol):
    """Somewhere a run record can be appended.

    `runtime_checkable` FOR CALLERS, and not for anything this package does. The sentence here
    used to say it was what made `configure()` fail early, and that stopped being true when the
    guard was rewritten: `isinstance` against a protocol checks attribute PRESENCE, so
    `isinstance([], RecordSink)` is `True` and the check could never have validated a sink.
    `configure()` binds the real signature against a specimen record instead. The README has
    said so for some time; this docstring did not, and a reader reaches for the docstring.

    The decorator stays because the protocol is PUBLIC -- `runprov.RecordSink` -- and a caller
    writing their own sink may reasonably ask `isinstance(mine, RecordSink)`. That affordance is
    the only thing it provides, and it is what the test named below holds it to.
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

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def append(self, record: dict[str, typing.Any]) -> None:
        # THE LINE IS SERIALISED INSIDE THE LOCK NOW, and it used to be serialised here. ADR-0016
        # R-6: `prev` is the digest of the line this one will physically follow, so it cannot be
        # known until the file is held — and computing it outside the lock is a race in which two
        # concurrent runs chain to the same predecessor and the second silently orphans the first.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # BINARY, and explicitly UTF-8 encoded here. Text mode cannot seek to inspect the
            # last byte without a decode, and the encoding is not the platform's business:
            # a history whose bytes depend on the writer's locale is the defect `header()`
            # already learned.
            with open(self.path, "ab+") as fh:
                with _exclusive(fh) as holds_lock:
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
                    _close_torn_line(fh)
                    # AND THE CHAIN IS READ AFTER THAT REPAIR, ADR-0016 R-7. A fragment closed
                    # by the newline above IS the predecessor this line follows; chaining to
                    # what was there before the repair would re-anchor the chain past a torn
                    # line and turn one broken link at a known place into a silent join.
                    #
                    # NOT MUTATED INTO THE CALLER'S DICT. `prev` is a property of this FILE and
                    # not of the run: the in-memory record, the sidecar and the pin must not
                    # carry it (R-17, R-18), and a copy is how that stays true by construction
                    # rather than by everyone downstream remembering.
                    # `prev` FIRST IN THE LINE, ADR-0016 R-19, and the position is the whole
                    # of it. It used to be written last — measured at 46 % of the way in, with
                    # 75 bytes of digest trailing it — so a truncation destroyed the one field
                    # whose entire purpose is to survive one. With it first, a torn line usually
                    # still carries a verifiable claim about its predecessor: a crash costs
                    # nothing, and an edit hidden behind a deliberate truncation is UPGRADED to
                    # a detection, because the surviving claim disagrees with the edited line.
                    #
                    # NO LOCK, NO CLAIM (R-22). Under the documented unlocked fallback two
                    # writers read the same tail and both claim it, so the chain would call a
                    # complete and correct file tampered — measured with ENOLCK: 80/80 records
                    # present, four false accusations. An honest absence is a coverage gap.
                    if holds_lock:
                        chained = {chain.FIELD: chain.previous_digest(fh), **record}
                    else:
                        chained = dict(record)
                    line = json.dumps(chained, default=str) + "\n"
                    # T-47(F). WITHOUT A LOCK THE WRITE IS PROVED, not assumed. `YamlLogSink`
                    # deliberately does NOT get this: it is a regenerable VIEW by its own
                    # documented design -- "a bad entry here costs the YAML and not the
                    # record" -- and `python -m runprov log --format yaml` rebuilds it. This
                    # file is the record of truth, so it is the one that must not lose a line.
                    _write_verified(fh, line.encode("utf-8"), holds_lock=holds_lock)
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

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def append(self, record: dict[str, typing.Any]) -> None:
        from .run import START_SCHEMA  # local: `sinks` -> `run` -> `project` -> `sinks`
        from .show import _yaml_entry, _yaml_header  # local: `show` must not import sinks

        # A NARRATIVE OF COMPLETED RUNS. The history carries a start line for every run that
        # begins, so that one which never ends is still on record; rendering those here would
        # put TWO entries per ordinary run into the file a person reads, and the second would
        # say nothing the first does not. The JSONL beside it keeps both, and this file states
        # in its own banner that it is a view and can be rebuilt.
        #
        # THE CONSTANT, NOT THE STRING. This tested `== "runprov.start.v1"` as a literal while
        # `__main__` used the imported `START_SCHEMA` — one sentence in two places, which is
        # what `076b770` and ADR-0003 removed for `PIN_ANCHOR`. Measured: bumping the schema
        # to v2 and running ONE ordinary run gives two `- step:` entries in
        # `transformation_log.yml`, the first of them hollow — verbatim the outcome this
        # filter exists to prevent, in the file the README positions as the successor to the
        # predecessor's manifest, where any `yaml.safe_load` tally would double.
        #
        # A LOCAL IMPORT because a top-level one cycles, measured rather than assumed:
        # `sinks` -> `run` -> `project` -> `sinks` raises `ImportError: cannot import name
        # 'OTHER_FILES_KEPT' from partially initialized module`. Same shape, and the same
        # reason, as the `show` import on the line above.
        if record.get("schema") == START_SCHEMA:
            return
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
                        _close_torn_line(fh)
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


def _close_torn_line(fh: typing.BinaryIO) -> None:
    """Terminate an unterminated final line, so the next append cannot be concatenated onto it.

    THE RECORD THAT GOVERNS THIS IS ADR-0016 R-7, which requires the repair to run BEFORE the
    predecessor is read -- an ordering the CALL SITES hold, not this function. The decision about
    whole-file writes, *"a record is written whole or not at all"*, explicitly EXEMPTS this
    module (*"`sinks.py` ... was already correct"*): it is about truncate-then-fill, and this is
    an append. **A citation is a claim about scope**, and the first draft of this docstring cited
    that record by number -- whose own governs-list refused it, which the suite said so plainly
    that the mistake cost one run. It is named by its title here for the same reason.

    A process SIGKILLed mid-append leaves a line with no terminator -- measured,
    5 of 12 trials. `O_APPEND` then puts the NEXT write at that fragment's end, so the new
    record is glued onto it and BOTH become unreadable. The torn one was lost anyway; the next
    one is collateral, and it is the one that had nothing wrong with it. Worse, a reader counts
    the result as ONE unreadable line, understating the loss by exactly the record it did not
    know it had destroyed. One newline closes it: the fragment becomes its own unreadable line,
    the new record lands intact on the next, and the count is true again.

    MUST BE CALLED INSIDE THE LOCK, because a concurrent writer must not see the file between
    the check and the fix.

    ONE DEFINITION, WHICH IT WAS NOT. This repair was written twice -- once in `JsonlSink` and
    once in `YamlSink`, whose copy's own comment read *"Same torn-line repair as `JsonlSink`,
    for the same reason"*. The duplication was acknowledged in prose rather than removed, and
    eleven surviving mutants sat on the duplicated lines: a test exercising one copy leaves the
    other unmeasured, and the mutation ids could not even be attributed to a site because the
    two blocks are textually identical. A rule that guards the property this package exists to
    protect had two implementations, which is how it drifts by a whole case.

    TOTAL, so a caller need not ask first: an empty file has no final line to close, and
    `seek(-1)` on one raises. The callers' own `tell()` remains where it decides something
    else -- `YamlSink` writes its banner when the file is new.
    """
    fh.seek(0, os.SEEK_END)
    if not fh.tell():
        return
    fh.seek(-1, os.SEEK_END)
    if fh.read(1) != b"\n":
        fh.write(b"\n")


#: ONE PROCESS SERIALISES ITSELF BEFORE IT ASKS THE OS. T-47(E).
#:
#: The matrix failure that opened T-47 was EIGHT THREADS IN ONE PROCESS appending to one
#: history: `23 of 24 records survived`. An OS file lock is the wrong tool for that -- it is
#: per-descriptor and the retry budget is what ran out -- and this makes the case exact rather
#: than merely rarer. It is also correct whether or not Windows' `"ab"` append is atomic,
#: which is the hypothesis that could not be tested on the host where this was written.
#:
#: THE PACKAGE'S OWN IDIOM, not a new mechanism: `terminal._LIVE_LOCK`, `run._SIDECARS_LOCK`
#: and `_report._WRITE_LOCK` are the same shape.
_APPEND_LOCK = threading.Lock()

#: AND IT IS BOUNDED, because a sink that must not ABORT a run must not HANG one either --
#: `RecordSink.append`'s own contract, one hazard along. Nothing inside the locked block
#: re-enters `_exclusive` today (checked: `_close_torn_line`, `chain.previous_digest` and
#: `diagnostic` contain no sink call), so this cannot deadlock now; the timeout is what keeps
#: that true for whoever edits those. On timeout the append proceeds WITHOUT the in-process
#: lock and says so, which `_write_verified` then makes safe rather than merely hopeful.
_APPEND_LOCK_WAIT = 30.0

#: `errno` values that mean THE LOCK WAS CONTENDED, not THIS PLATFORM CANNOT LOCK. T-47(B).
#:
#: The conflation of those two was the trigger for T-47: both arrive as `OSError`, both took
#: the same branch, and the second then appended unlocked. `msvcrt.locking(.., LK_LOCK, ..)`
#: retries ten times at one-second intervals and then raises EDEADLOCK, which is contention
#: and nothing else.
#:
#: ENOLCK IS DELIBERATELY ABSENT. "No locks available" is what an NFS mount without a lock
#: manager reports, and that IS a capability statement -- it is the fallback's entire
#: justification. A test raises exactly `OSError(errno.ENOLCK)` to hold this distinction.
#:
#: A LIST, AND SAID SO: this is the PLATFORM's enumeration rather than one invented here, and
#: it will still be incomplete. So it decides only what the NOTICE SAYS. Correctness rests on
#: `_write_verified`, which does not consult it.
#: A SET LITERAL OF LOOKUPS, not a comprehension, and the declared-table sweep is why: it
#: recurses through a one-argument wrapper like `frozenset(...)` and a `GeneratorExp` is a
#: form it has no decision for -- it said so, by name. Members that are not literal constants
#: put this outside the population of FIXED tables, which is correct: the set is
#: PLATFORM-CONDITIONAL, since a name this platform does not define resolves to `None`.
#: `exc.errno` CAN ITSELF BE `None`, AND THAT NEEDS NO GUARD -- which a control established
#: rather than a reading. The sentinel for "not contended" IS `None`, so when the input is
#: `None` the function returns `None` whichever branch it takes, and the caller reaches the
#: CAPABILITY notice either way. A guard was written for this and then deleted: removing it
#: changed no behaviour and its test could not fail, which is a control that cannot fire.
_CONTENDED_ERRNOS = {
    getattr(errno, "EDEADLOCK", None),
    getattr(errno, "EDEADLK", None),
    getattr(errno, "EACCES", None),
    getattr(errno, "EAGAIN", None),
    getattr(errno, "EWOULDBLOCK", None),
}


def _contended(err: int | None) -> int | None:
    """The errno if it means THE LOCK WAS CONTENDED, else `None` for a capability statement."""
    return err if err in _CONTENDED_ERRNOS else None


#: How many times an UNLOCKED write may be re-attempted after read-back shows it did not land.
_UNLOCKED_WRITE_ATTEMPTS = 3


def _write_verified(fh: typing.IO[typing.Any], payload: bytes, *, holds_lock: bool) -> None:
    """Append `payload`, and WITHOUT a lock prove it actually landed. T-47(F).

    WHY THIS EXISTS AT ALL. `_exclusive`'s own measurement says the unlocked fallback loses
    nothing on Linux -- "8 processes x 20 appends of 9 KB produced 160/160 intact records,
    because Linux holds the inode lock across the whole write()" -- and the Windows job
    produced 23 of 24. So the fallback's documented harm, *writers may interleave*, is true on
    POSIX and an UNDERSTATEMENT where a record can be destroyed. The remedy is not to refuse
    the append, which loses the record always rather than sometimes, and which
    `RecordSink.append` cannot even signal: its contract is MUST NOT RAISE and it returns
    `None`. The remedy is to make the unlocked append SAFE.

    THE WINDOW IS "EVERYTHING APPENDED SINCE WE LOOKED", not the file. `start` is our
    observation of the end before writing; appends only grow the file, so our bytes land at or
    after it, and reading `start`..EOF covers them plus whatever arrived concurrently. That is
    bounded by concurrent activity rather than by history length -- the O(n)-per-append cost
    `YamlLogSink`'s docstring names as the defect to avoid.

    FILE SIZE CANNOT SERVE, and this is why the obvious check was rejected: two writers that
    seek to the same end E and write L each leave a file of size E+L. BOTH observe a growth of
    exactly L. Neither can tell it was overwritten.

    THE RESIDUAL, STATED. A false POSITIVE is possible if `payload` occurs byte-identically
    elsewhere in the window; every history record carries a `run_uid` (24 of 24 released
    records do), so this needs two runs with the same uid and the same bytes. A false positive
    only returns the behaviour to what it was before this function existed.

    ON THE LOCKED PATH THIS COSTS NOTHING: no read-back, no extra syscall.
    """
    if holds_lock:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
        return
    for attempt in range(1, _UNLOCKED_WRITE_ATTEMPTS + 1):
        start = fh.seek(0, os.SEEK_END)
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
        fh.seek(start)
        if payload in fh.read():
            return
        # ONLY WHEN A RETRY WILL ACTUALLY FOLLOW. Caught by its own guard: this announced
        # "being retried" after the LAST attempt too, which promises an action that is not
        # going to happen -- the same overstatement as the notice this change exists to fix.
        if attempt < _UNLOCKED_WRITE_ATTEMPTS:
            diagnostic(
                f"  NOTE: an unlocked append did not land and is being retried "
                f"(attempt {attempt} of {_UNLOCKED_WRITE_ATTEMPTS})."
            )
    diagnostic(
        f"  WARNING: a run history record could not be appended without a lock after "
        f"{_UNLOCKED_WRITE_ATTEMPTS} attempts; it is NOT in the history."
    )


@contextlib.contextmanager
def _exclusive(fh: typing.IO[typing.Any]) -> typing.Iterator[bool]:
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
    # T-47(E). IN-PROCESS FIRST, so threads never race each other into the OS lock. Bounded,
    # and a timeout is not a failure to swallow: it proceeds unlocked and says so.
    serialised = _APPEND_LOCK.acquire(timeout=_APPEND_LOCK_WAIT)
    if not serialised:
        diagnostic(
            f"  NOTE: waited {_APPEND_LOCK_WAIT:.0f}s for this process's own history lock and "
            f"gave up; appending without it rather than hanging the run."
        )
    locked: str | None = None
    contended: int | None = None
    try:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = "fcntl"
        except ImportError:
            contended = None  # the module is absent: a CAPABILITY statement, not contention
        except OSError as exc:
            contended = _contended(exc.errno)
        if locked is None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    locked = "msvcrt"
            except ImportError:
                contended = None
            except OSError as exc:
                # T-47(B). THE SPLIT THAT WAS MISSING. This arm used to be `pass`, which made
                # "could not take the lock just now" indistinguishable from "this filesystem
                # cannot lock" -- and only the second justifies an unlocked append.
                # `LK_LOCK` raises EDEADLOCK after ten one-second attempts.
                contended = _contended(exc.errno)
        if locked is None:
            # OUTSIDE the platform branches, and that is the whole fix. This notice used to
            # sit inside the win32-only inner `except`, so a POSIX `flock` that raised -- an
            # NFS or CIFS mount, a container without the syscall -- degraded to an unlocked
            # append and said NOTHING. Those filesystems are the lock's entire justification,
            # so the one case that most deserved announcing was the one case that could not.
            # Verified by stubbing fcntl to raise: the line was written, no notice appeared.
            #
            # AND THE CLAIM IS CORRECTED. It said "Concurrent writers may interleave", which
            # is true on POSIX -- measured in this docstring, 160/160 -- and an UNDERSTATEMENT
            # on a platform where a concurrent unlocked append can DESTROY a record rather
            # than reorder one. A disclosure that understates the harm is worse than none.
            if contended is None:
                diagnostic(
                    "  NOTE: no file locking available; run history appended unlocked. "
                    "On some platforms a concurrent unlocked append can LOSE a record and "
                    "not merely interleave, so each unlocked write is read back."
                )
            else:
                diagnostic(
                    f"  NOTE: the run history lock was CONTENDED and could not be taken "
                    f"(errno {contended}); appended unlocked. On some platforms a concurrent "
                    f"unlocked append can LOSE a record, so each one is read back."
                )
        try:
            # YIELDS WHETHER A LOCK IS ACTUALLY HELD. ADR-0016 R-22: chaining under the
            # unlocked fallback lets two writers claim the same predecessor, so a file in
            # which every record landed correctly gets reported as tampered. The caller has
            # to be able to tell.
            yield locked is not None
        finally:
            if locked == "fcntl":
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif locked == "msvcrt" and sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        if serialised:
            _APPEND_LOCK.release()
