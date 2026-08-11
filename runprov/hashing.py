# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Two hashes, because "did this change?" and "is this the file?" are different questions.

`sha256` is what is on disk — what git sees and what a file-integrity check needs.

`content_digest` strips the volatile build stamps artifacts write on purpose. Without it,
an artifact carrying `# built_utc: ...` differs on every run, so every artifact pinning it
differs on every run, and so does everything downstream. Measured before this existed: 25
non-figure artifacts oscillating forever across identical runs, the same 10 stages each
time — a permanently red check, which trains a reader to ignore the check.

The gzip case is separate and worse: the gzip header stores the compression mtime, so a
.gz rewritten from byte-identical data hashes differently every single time. Decompress
first, or the file can never hash the same twice.

LINE ENDINGS ARE DELIBERATELY NOT CONTENT (ADR-029, council row R12)
--------------------------------------------------------------------
`content_digest` reads in text mode and drops the terminator, so **a trailing newline and
CRLF-vs-LF do not move the digest**. That is intended, and it is stated here because it was
previously an accident of `open(..., encoding=...)` plus `rstrip("\n")` that nothing
documented — which is a different defect from the behaviour itself.

The reasoning: this function's whole job is ignoring differences that are not content, and a
file converted LF→CRLF by a Windows checkout, or given a trailing newline by an editor, is
the same data. `sha256` is recorded beside it in every record and preserves the exact bytes,
so nothing is lost — the two questions have two answers and both are kept.

Measured before deciding: making terminators significant moves **1,189 of 1,305 artifacts
(91.11%)**. That is not a bugfix, it is a format change for an entire repository, and it
would move every pin at once — the "80 artifacts CHANGED" failure this module exists to end.
If it is ever revisited it belongs to a major version, with all pinned artifacts re-pinned in
one announced pass.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import itertools
import os
import pathlib
import re
import stat
import typing
import zlib

# Volatile stamps as HEADER COMMENTS.
VOLATILE = re.compile(r"^#\s*(built_utc|generated_utc|run_utc|built_at)\b")

# The same stamps as JSON KEYS. This half was missing at first, and it mattered because a
# provenance JSON is itself a declared output: it carries started_utc, finished_utc and a
# per-output mtime_utc, so it was inherently unhashable-twice and the stage that wrote it
# could never reproduce.
VOLATILE_JSON = re.compile(
    r'"(built_utc|generated_utc|run_utc|built_at|started_utc|finished_utc|drawn_utc'
    r'|mtime_utc|acquired_utc)"\s*:\s*"[^"]*"'
)

# ADR-029 R2. The rule above exists for the timestamps RUNPROV ITSELF writes, and it fired
# on any `*.json` -- so a user's data file with a legitimate key named `mtime_utc` had it
# erased from its digest and two genuinely different datasets collided. Scoped by sniffing
# the record's own marker: runprov's files say so in their first block, and nobody else's do.
OWN_RECORD = re.compile(r'"schema"\s*:\s*"runprov\.')

# ADR-029 R2 again: the replacement KEEPS the key and blanks only the value. Erasing both
# destroyed the structure, and -- load-bearing for R1 below -- it made the substitution
# non-idempotent, so it could not be applied twice to an overlapping window.
_JSON_BLANK = r'"\1": ""'

# ADR-029 R1. Carried between blocks so a `"started_utc": "..."` split across an 8,192-line
# boundary is still matched. Comfortably longer than any stamp the rule above can match.
_CARRY = 4096


def sha256(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    """Streamed, so multi-GB inputs are fine."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while blk := fh.read(chunk):
            h.update(blk)
    return h.hexdigest()


def content_digest(path: pathlib.Path, chunk_lines: int = 8192) -> str | None:
    """SHA-256 with volatile stamps removed. Binary falls back to the raw hash.

    STREAMED, line by line. The first version did `path.read_text()` and hashed the
    result, while the module docstring promised that "hashing is streamed, so multi-GB
    inputs are fine" -- true of `sha256`, false here, and this is the function called on
    every registered input. A 4 GB TSV would have taken 4 GB of memory to decide whether
    it had changed.
    """
    if not path.is_file():
        return None
    try:
        if path.suffix == ".gz":
            return _gzip_digest(path)
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
        # `zlib.error` is NOT an OSError, and it is the one decompression actually raises on
        # a corrupt or truncated member -- so this clause listed three exceptions and missed
        # the only one that fires. It escaped `content_digest`, inside provenance capture,
        # on a registered input.
        return sha256(path)

    try:
        with open(path, encoding="utf-8") as fh:
            return _stream_digest(fh, is_json=path.suffix == ".json", chunk_lines=chunk_lines)
    except (UnicodeDecodeError, OSError):
        return sha256(path)


def _stream_digest(fh: typing.TextIO, *, is_json: bool, chunk_lines: int) -> str:
    """The line filter, streamed, with a carry so a match may span a block boundary."""
    h = hashlib.sha256()
    pending = ""
    first = True
    strip_json = False
    sniffed = False
    while True:
        block = list(itertools.islice(fh, chunk_lines))
        if not block:
            break
        if not sniffed:
            # ONLY runprov's own records get their timestamps stripped (R2). The marker is
            # in the first object, so the first block always decides it.
            strip_json = is_json and bool(OWN_RECORD.search("".join(block[:200])))
            sniffed = True
        kept = [ln.rstrip("\n") for ln in block if not VOLATILE.match(ln)]
        if not kept:
            continue
        pending += ("" if first else "\n") + "\n".join(kept)
        first = False
        if strip_json:
            pending = VOLATILE_JSON.sub(_JSON_BLANK, pending)
            if len(pending) > _CARRY:
                h.update(pending[:-_CARRY].encode())
                pending = pending[-_CARRY:]
        else:
            h.update(pending.encode())
            pending = ""
    if strip_json:
        pending = VOLATILE_JSON.sub(_JSON_BLANK, pending)
    h.update(pending.encode())
    return h.hexdigest()


def _gzip_digest(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    """Decompress in fixed blocks -- the gzip header stores a compression mtime, so a .gz
    rewritten from identical bytes never hashes the same twice, and reading it whole to
    work around that would defeat the streaming above."""
    # ADR-029 R3: the SAME line filter as the plain-text path. Without it a gzipped
    # artifact carrying `# built_utc:` differed on every run, and so did everything pinning
    # it -- the exact oscillation the plain-text path was written to stop.
    try:
        with gzip.open(path, "rt", encoding="utf-8") as tfh:
            return _stream_digest(tfh, is_json=path.name.endswith(".json.gz"), chunk_lines=8192)
    except (UnicodeDecodeError, OSError, EOFError, gzip.BadGzipFile, zlib.error):
        # A gzipped BINARY must not be decoded as text -- that is exactly the mistake
        # ADR-029 rejects R13 for. Hash the decompressed bytes instead.
        pass
    h = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        while blk := fh.read(chunk):
            h.update(blk)
    return h.hexdigest()


def describe(path: pathlib.Path) -> dict[str, typing.Any]:
    """Everything recorded about one input or output. Directories are hashed as a tree.

    Raises `ValueError` for anything that is neither a regular file nor a directory. That is
    not fussiness: `open()` on a FIFO BLOCKS until a writer appears, so registering one hung
    the run forever — no message, inside provenance capture, before the work began. A
    provenance module that hangs the run it is describing is the worst failure available to
    it, because there is then no record AND no process. Sockets and devices are refused for
    the same reason.
    """
    st = path.stat()
    link_target = os.readlink(path) if path.is_symlink() else None
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        raise ValueError(
            f"cannot register {path}: not a regular file or directory "
            f"(mode {stat.filemode(st.st_mode)}). Hashing a FIFO, socket or device would "
            f"block until a writer appears, which hangs the run inside provenance capture."
        )
    rec: dict[str, typing.Any] = {
        "path": str(path),
        # A RECORD THAT CANNOT TELL A FILE FROM A LINK TO IT is incomplete in a way that
        # matters: the link can be repointed afterwards and every hash in the record stays
        # valid while describing different bytes. The digest below is the TARGET's, which is
        # right -- that is what was read -- so the record has to say that is what happened.
        "symlink": link_target is not None,
        **({"symlink_target": link_target} if link_target is not None else {}),
        "size_bytes": st.st_size,
        "mtime_utc": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if path.is_dir():
        # os.walk with `onerror`, NOT rglob. `rglob` skips a directory it cannot enter and
        # says nothing, so the tree hash changed while the tree did not -- the silent-skip
        # class, inside the function whose job is saying what a run read. The count is
        # always present so "0" and "this record predates the check" stay distinguishable.
        unreadable: list[str] = []
        skipped: list[str] = []
        files: list[pathlib.Path] = []
        for root, _dirs, names in os.walk(
            path, onerror=lambda e: unreadable.append(str(getattr(e, "filename", e)))
        ):
            for n in names:
                fp = pathlib.Path(root) / n
                if fp.is_file():
                    files.append(fp)
                else:
                    # A FIFO, socket, device or dangling symlink INSIDE the tree. It cannot
                    # be hashed -- opening it is the hang `describe` refuses at the top --
                    # but dropping it without a word is the same silent skip as the
                    # unreadable directory above. Counted, so the tree hash's population is
                    # stated rather than assumed.
                    skipped.append(str(fp))
        files.sort()
        rec["kind"] = "directory"
        rec["n_files"] = len(files)
        rec["n_unreadable_dirs"] = len(unreadable)
        rec["n_skipped_nonregular"] = len(skipped)
        if skipped:
            rec["skipped_nonregular"] = sorted(skipped)
        if unreadable:
            rec["unreadable_dirs"] = sorted(unreadable)
        h = hashlib.sha256()
        for f in files:
            # ADR-029 R9. `name || hex-digest` with no separator is not injective by
            # construction. The review called the second preimage "trivial"; it is not --
            # it needs a preimage attack on SHA-256, and one could not be built. The NUL
            # costs nothing and removes the argument rather than defending it. NUL because
            # it is the one byte a POSIX filename cannot contain.
            h.update(f.relative_to(path).as_posix().encode() + b"\0")
            h.update(sha256(f).encode() + b"\0")
        rec["sha256_tree"] = h.hexdigest()
    else:
        rec["kind"] = "file"
        rec["sha256"] = sha256(path)
        rec["content_sha256"] = content_digest(path)
        # STAT AFTER HASHING. `size_bytes` was sampled before, so a file written while it
        # was being read recorded a size that never went with that digest -- and said
        # nothing. Re-stat, publish the size that belongs to the hash, and flag the window
        # when it moved. This does not close the race (nothing can, short of a lock); it
        # makes the race VISIBLE, which is the difference between a wrong record and a
        # record that knows it is wrong.
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            rec["unstable_during_hash"] = True
        rec["size_bytes"] = after.st_size
        rec["mtime_utc"] = dt.datetime.fromtimestamp(after.st_mtime, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return rec
