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
site was careless, it is that none of them had been thought about.

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
    # THROUGH A SYMLINK, as `open(path, "w")` does. Replacing the link itself would silently
    # relocate a record the user had deliberately pointed somewhere else — and `realpath` of
    # a dangling link is the file `open` would have created, so this matches on that too.
    dest = pathlib.Path(os.path.realpath(path)) if path.is_symlink() else path
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:12]}{TEMP_SUFFIX}")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # THE DESTINATION'S OWN MODE, if it has one. `write_text` truncates in place and so
        # keeps whatever permissions the file already had; a rename brings the temporary
        # file's instead, which would silently re-open a record somebody had restricted.
        if dest.is_file():
            os.chmod(tmp, dest.stat().st_mode & 0o7777)
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
