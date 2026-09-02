# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""A record is written whole or not at all. See ADR-0005.

WHAT THIS REPLACES. Every provenance write in this package was a plain `Path.write_text`,
which truncates the destination and then fills it. A process that dies between those two
things leaves a PREFIX: a sidecar that is half a JSON document, a marker that names a run
and stops before saying which host it is on. `tools/torture.py` measured it at six of the
six write sites the package owns, at 0%, 50% and 90% of each — the finding is not that one
site was careless, it is that none of them had been thought about. There were SEVEN: the
census enumerated `Path.write_text` and the seventh site spells it `Path.write_bytes`, so
the harness that was built to stop this being a hand-typed list had a hand-typed list of
one method name inside it. See `atomic_write_bytes` below.

WHY IT MATTERS HERE MORE THAN ELSEWHERE. A truncated cache entry is a slow program. A
truncated provenance record is a run whose account of itself is gone, produced by exactly
the event the record exists to survive: the machine stopping. The readers all handle a torn
file correctly today — `verify`, `show`, `log`, `lineage` and `prune` were held to the
exit-code contract over ninety torn trees and none of them so much as printed a traceback —
so nothing was BROKEN. What was missing is that the crash could destroy the previous, whole
record rather than leaving it in place, and no reader can recover what is no longer there.

HOW. Write a temporary file beside the destination, `fsync` it, then `os.replace` onto the
destination. POSIX guarantees the rename is atomic: a concurrent reader sees the old file or
the new one, never a mixture, and a crash at any instant leaves one of the two on disk.
`fsync` of the file comes BEFORE the rename so the bytes are durable before the name points
at them; `fsync` of the DIRECTORY comes after, because a rename that is not itself synced
can be lost even though the data it points at was written. The second is the step usually
skipped, and skipping it makes the guarantee "atomic but not durable", which is not the one
claimed above.

BESIDE THE DESTINATION, NOT IN /tmp: `os.replace` is only atomic within one filesystem, and
`tempfile.gettempdir()` is frequently a different one — on any machine with a tmpfs, every
machine in this hospital's cluster, and every CI runner this project uses.

`sinks.py` DOES NOT USE THIS AND SHOULD NOT. The history is append-only under an exclusive
lock, with its own torn-line repair, and it is already `fsync`ed; rewriting a
forever-growing file to append one line would be the wrong shape by a wide margin. That the
one file written correctly was the one somebody had thought hard about is the whole shape of
this finding.
"""

from __future__ import annotations

import os
import pathlib
import uuid

#: The suffix on a file this module is part-way through writing. `verify` skips it, and it
#: reads that name FROM HERE rather than repeating the string: the writer and the reader
#: agreeing about debris is one fact, and one fact in two files is this repository's
#: most-repaired defect.
TEMP_SUFFIX = ".runprov-tmp"


def atomic_write_text(path: pathlib.Path, text: str, encoding: str = "utf-8") -> None:
    """Write `text` to `path` so that a crash leaves the old file, not half the new one.

    Raises whatever `open` and `os.replace` raise, unchanged and at the same moments, so
    every caller's existing `except OSError` still catches exactly what it caught before.
    Does not create the parent directory — the callers that need one already make it, and
    a helper that quietly created directories would hide a mistyped path.
    """
    _write_atomically(path, text, encoding)


def atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    """The same guarantee for bytes. `Path.write_bytes` truncates and fills exactly as
    `Path.write_text` does, and this package had a seventh site doing it.

    THE SEVENTH SITE, MISSED BY ADR-0005 because the census that found the other six was
    looking for one spelling. `environment.archive_lockfiles` copies a lock file into the
    snapshot directory with `target.write_bytes(source.read_bytes())`, and it is WORSE than
    the six that were converted: the target is CONTENT-ADDRESSED (`lock-<sha256[:16]>-uv.lock`)
    and the next run's `rec["reused"] = target.is_file()` asks only whether that name exists.
    So a crash part-way through the copy leaves a prefix under a name that claims a digest,
    every later run sees the name, sets `reused: true`, and never writes it again — while
    the record beside it goes on asserting the sha256 of the WHOLE source file. A truncated
    sidecar is a record that is visibly broken; this is a record that is quietly wrong, for
    ever, and the only thing that would notice is somebody re-hashing the archive by hand.
    """
    _write_atomically(path, data, None)


def _destination_mode(dest: pathlib.Path) -> int | None:
    """The mode the destination already has, or None if it has none we can read.

    THE DESTINATION'S OWN MODE, if it has one. `write_text` truncates in place and so keeps
    whatever permissions the file already had; a rename brings the temporary file's instead,
    which would silently re-open a record somebody had restricted.

    ONE `stat`, AND ITS FAILURE IS NOT FATAL — both halves of that are a repair. This was
    `if dest.is_file(): os.chmod(tmp, dest.stat().st_mode & 0o7777)`, which asks the
    filesystem the same question twice: a concurrent `unlink` landing between the two raised
    `FileNotFoundError` out of a helper whose caller had already been given the bytes, the
    `except BaseException` below removed the temporary file, and NEITHER the old record nor
    the new one was left on disk. The same removal one syscall later — between the `stat` and
    `os.replace` — is harmless, because `os.replace` does not care whether the destination is
    there. A window that destroys a write depending on which of two adjacent syscalls it
    falls between is not a policy; losing the record was never the intent, so the lookup
    answers "I do not know" instead of raising, and the write proceeds at the umask default,
    which is what a destination that does not exist gets anyway.
    """
    try:
        # `os.stat` RATHER THAN `dest.stat()` so this module's own `os` is the seam a test
        # can drive: `pathlib.Path.stat` is global, and patching it also catches the
        # `is_symlink` above, which would make a race test pass without racing anything.
        return os.stat(dest).st_mode & 0o7777
    except OSError:  # guards-ok: no readable destination, no mode to preserve
        return None


def _write_atomically(path: pathlib.Path, payload: str | bytes, encoding: str | None) -> None:
    """Temp beside the destination, fsync, rename. `encoding=None` writes bytes.

    THE TEMPORARY FILE IS NARROWED BEFORE IT HOLDS ANYTHING, and that ordering is the whole
    of the second repair here. It used to be `open(tmp, "w")` — which creates at
    `0o666 & ~umask` — with the narrowing `os.chmod` running only after the text had been
    written AND fsynced. Measured under umask 0002 against a `0o600` destination: at fsync
    time the temporary file was mode 0o664 and already held the complete record. Anyone in
    the group could read it, and `fsync` is not a fast call. `write_text` truncated the
    destination in place and never widened the mode for any instant, so the file this module
    replaced did not have this window — it is a regression against the guarantee stated
    three lines above the chmod, not an inherited one.

    Creating with `os.open(..., mode)` alone is not enough: that mode is masked by the umask
    too, so a `0o666` destination under umask 0022 would come back 0o644. The `chmod` still
    happens — it just happens on an EMPTY file, before the first byte goes in.
    """
    # THROUGH A SYMLINK, as `open(path, "w")` does. Replacing the link itself would silently
    # relocate a record the user had deliberately pointed somewhere else — and `realpath` of
    # a dangling link is the file `open` would have created, so this matches on that too.
    dest = pathlib.Path(os.path.realpath(path)) if path.is_symlink() else path
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:12]}{TEMP_SUFFIX}")
    mode = _destination_mode(dest)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
        # `os.fdopen` RATHER THAN A SECOND `open`, so the descriptor that was created with
        # `O_EXCL` is the one that gets written: re-opening by name would hand the mode back
        # to the umask and reintroduce the window this ordering exists to close. TEXT MODE
        # WHERE THERE IS AN ENCODING, because `open(tmp, "w")` defaulted to `newline=None`
        # and this must keep doing whatever that did: on Windows that translates every "\n"
        # to "\r\n", so writing these records in binary would silently change the bytes of
        # every sidecar on one platform, which is not a change this repair is making.
        with os.fdopen(fd, "wb" if encoding is None else "w", encoding=encoding) as fh:
            if mode is not None:
                os.chmod(tmp, mode)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        # INCLUDING KeyboardInterrupt. The debris is this module's to clean up whatever
        # ended the write, and leaving it would put a file `verify` has to be taught to
        # ignore into a directory of results.
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(dest.parent)


def _sync_dir(directory: pathlib.Path) -> None:
    """Make the rename itself durable. Silent where a directory cannot be opened.

    Windows cannot open a directory as a file at all, and some network filesystems refuse
    the `fsync`. Neither is a reason to fail a write that has already succeeded: the
    atomicity above holds regardless, and this only decides whether the rename survives a
    power cut. Failing here would turn a durability nicety into a lost record.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # guards-ok: no directory handle, no sync — the write still stands
        return
    try:
        os.fsync(fd)
    except OSError:  # guards-ok: as above; some filesystems simply do not support it
        pass
    finally:
        os.close(fd)
