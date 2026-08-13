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
import os
import pathlib
import re
import stat
import tarfile
import typing
import zipfile
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

# The SECOND bound on a block, in characters. A block was `chunk_lines` lines regardless of
# how long they were, which made peak memory a property of the file's line lengths rather
# than of anything this module chose. 8 MiB is large enough that ordinary line-per-record
# text still moves in big blocks, and small enough that a file of few enormous lines costs
# a bounded amount instead of its own size several times over.
_CHUNK_BYTES = 1 << 23

# The write timestamp OOXML puts inside every .xlsx/.docx/.pptx, in a part whose path is
# fixed by the specification. Measured: two `df.to_excel(...)` calls one second apart
# produce archives whose ten entries are byte-identical except this one, so a spreadsheet
# regenerated from unchanged data had a different digest every time.
#
# Scoped to `docProps/core.xml` and to nothing else, which is the ADR-029 R2 lesson: the
# JSON stamp rule once fired on any `*.json` and erased a user's legitimate `mtime_utc`
# key, colliding two different datasets. A rule that rewrites bytes has to know exactly
# whose bytes they are.
OOXML_CORE = "docProps/core.xml"
OOXML_STAMP = re.compile(
    rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)

#: An entry read whole rather than streamed, so a substitution can be applied across it.
#: Only reached for `docProps/core.xml`, which the specification makes small; the bound is
#: here so a hostile archive claiming that name cannot be read into memory unbounded.
_ENTRY_WHOLE_MAX = 1 << 20


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

    "Streamed" then meant BY LINE COUNT ONLY, which is the same claim one level up: a block
    was 8,192 lines however long each one was, so peak memory was a property of the file's
    line lengths rather than of anything chosen here. Measured on this machine, peak RSS for
    one call, before and after the byte bound:

        8,192 contigs of ~30 kb   246 MB file    607 MB -> 43 MB
        one 100 MB line           100 MB file    429 MB -> 334 MB
        6M short lines            142 MB file     22 MB -> 21 MB   (unchanged)

    The middle row is the honest limit and it does not go away: the filter is line-oriented
    and Python hands over one line at a time, so a single enormous line is read whole
    whatever the block size. The cost is now one line rather than one block of them --
    ordinary line-per-record text is bounded, an unwrapped FASTA is proportional to its
    longest sequence. Fixing that too means abandoning line-oriented reading, which changes
    where every match boundary falls, and this function may not move a digest.
    """
    if not path.is_file():
        return None
    try:
        if path.suffix == ".gz":
            return _gzip_digest(path)
        # Detected by CONTENT, not by extension, because the archives that matter here are
        # not named `.zip`: an `.xlsx`, `.docx`, `.odt`, `.whl` and `.npz` are all zip
        # containers, and an extension list would have to guess at the ones nobody thought
        # of. `is_zipfile` reads the central directory and `is_tarfile` the first block —
        # neither walks the file.
        if zipfile.is_zipfile(path):
            return _zip_digest(path)
        if tarfile.is_tarfile(path):
            return _tar_digest(path)
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error, zipfile.BadZipFile, tarfile.TarError):
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


def _stream_digest(
    fh: typing.TextIO, *, is_json: bool, chunk_lines: int, chunk_bytes: int = _CHUNK_BYTES
) -> str:
    """The line filter, streamed, with a carry so a match may span a block boundary.

    BOUNDED BY BYTES AS WELL AS BY LINES, because "streamed" was true only of files whose
    LINES are short. A block was `chunk_lines` lines however long each one was, so the shape
    that defeats it is not a big file but a file with few big lines -- an unwrapped FASTA
    (`seqtk seq -l0`, most assemblers, any single-sequence download), a minified JSON, a
    one-line data dump. Measured before this bound, on a 200 MB unwrapped FASTA of two
    100 MB lines: **850 MB peak**, 4.25x the file, for a function whose job is deciding
    whether the file changed. Under a SLURM memory cgroup that is an OOM kill inside
    provenance capture -- and an OOM kill is SIGKILL, the one ending nothing can record.

    The non-JSON path also stops building the joined block at all and feeds the hash one
    line at a time. The bytes are identical -- `"\\n".join(kept)` and a `\\n` between each
    pair are the same stream -- but the peak drops from a copy of the block to a copy of one
    line. The JSON path still needs a buffer, because `VOLATILE_JSON` matches across line
    boundaries; that buffer is bounded by one block plus `_CARRY`.

    A single line longer than `chunk_bytes` is still read whole, because the filter is
    line-oriented and Python's iterator hands over a line at a time. That is a real limit
    and it is smaller than the one it replaces: the cost is one line, not one block.
    """
    h = hashlib.sha256()
    pending = ""
    first = True
    strip_json = False
    sniffed = False
    while True:
        # Read to whichever bound arrives first. `islice` alone could not see the second.
        block, size = [], 0
        for line in fh:
            block.append(line)
            size += len(line)
            if len(block) >= chunk_lines or size >= chunk_bytes:
                break
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
        if strip_json:
            pending += ("" if first else "\n") + "\n".join(kept)
            first = False
            pending = VOLATILE_JSON.sub(_JSON_BLANK, pending)
            if len(pending) > _CARRY:
                h.update(pending[:-_CARRY].encode())
                pending = pending[-_CARRY:]
        else:
            for line in kept:
                if not first:
                    h.update(b"\n")
                h.update(line.encode())
                first = False
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


def _entry_digest(fh: typing.IO[bytes], chunk: int = 1 << 20) -> str:
    """Digest one archive member's bytes, streamed."""
    h = hashlib.sha256()
    while blk := fh.read(chunk):
        h.update(blk)
    return h.hexdigest()


def _zip_digest(path: pathlib.Path) -> str:
    """Digest a zip by its CONTENTS, not by its container bytes.

    A zip stores a modification time per entry, so an archive rewritten from identical
    files is a different byte sequence every time — the same defect as the gzip header
    mtime, and `.xlsx`, `.docx` and `.npz` are all zips. Measured: two `df.to_excel()`
    calls a second apart produced different digests for the same two rows.

    Name and entry digest, NUL-separated, in sorted order: sorted because zip entry order
    is a property of the writer rather than of the content, and NUL-separated for the
    ADR-029 R9 reason — `name || digest` concatenated with no separator is not injective
    by construction.
    """
    h = hashlib.sha256()
    with zipfile.ZipFile(path) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            h.update(info.filename.encode("utf-8", "surrogateescape"))
            # The NUL is what makes this injective (ADR-029 R9): without it two entries
            # `a`,`b` and one entry `ab` build the same byte string. No type flag is added
            # beside it, because a zip directory IS a name ending in `/` -- `is_dir()`
            # tests exactly that -- so the name already carries the distinction and a flag
            # would be a branch no test could ever reach.
            h.update(b"\0")
            if info.is_dir():
                continue
            if info.filename == OOXML_CORE and info.file_size <= _ENTRY_WHOLE_MAX:
                with z.open(info) as fh:
                    body = OOXML_STAMP.sub(rb"\1\2", fh.read())
                h.update(hashlib.sha256(body).hexdigest().encode())
            else:
                with z.open(info) as fh:
                    h.update(_entry_digest(fh).encode())
            h.update(b"\0")
    return h.hexdigest()


def _tar_digest(path: pathlib.Path) -> str:
    """Digest a tar by its CONTENTS. Same argument as `_zip_digest`.

    A tar header carries mtime, uid, gid and mode. Measured: `tar.add()` of an unchanged
    file one second later produced a different archive, so mtime alone is enough to make
    a tarred artifact unhashable-twice. Ownership is deliberately out too — the same tree
    packed by two people is the same content.
    """
    h = hashlib.sha256()
    with tarfile.open(path) as t:
        for member in sorted(t.getmembers(), key=lambda m: m.name):
            # No separator after the name: the one-byte type flag below is followed by its
            # own NUL, so the first NUL of every record lands immediately after the flag
            # and the name field is delimited by construction. THAT NUL is load-bearing --
            # without it `name "a" -> link "Lb"` and `name "aL" -> link "b"` build the same
            # bytes -- and a second one here would be a byte no test could reach.
            h.update(member.name.encode("utf-8", "surrogateescape"))
            if member.isfile():
                fh = t.extractfile(member)
                if fh is None:
                    # A regular member that cannot be opened as a stream. Substituting a
                    # marker would put a digest in the record that describes an archive
                    # nobody read; the raw hash of the container is the honest answer, and
                    # it is the same fallback a corrupt gzip or zip takes.
                    raise tarfile.TarError(f"member {member.name!r} could not be streamed")
                # `F\0` is REDUNDANT here and kept on purpose: a file's payload is a
                # fixed-width digest, so the name is recoverable without it. The other two
                # kinds carry variable-length payloads and genuinely need the flag, and
                # encoding all three the same way is what makes the injectivity argument
                # one sentence instead of three cases.
                h.update(b"F\0" + _entry_digest(fh).encode())
            elif member.issym() or member.islnk():
                # The target IS the content of a link. Two links pointing elsewhere are
                # not the same archive.
                h.update(b"L\0" + member.linkname.encode("utf-8", "surrogateescape"))
            else:
                h.update(b"O\0" + str(member.type).encode())
            h.update(b"\0")
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


def _mtime_utc(st: os.stat_result) -> str:
    return dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PIN_DIGEST_CHARS = 16

#: What a pin written BESIDE an artifact is called: `<artifact><suffix>`.
#:
#: Here rather than in `run.py` because it is shared: the writer appends it, and the
#: verifier has to strip it to learn which file a `.prov.txt` is speaking for. A verifier
#: that did not know this convention checked the sidecar as though the sidecar were the
#: artifact, and reported OK over a deleted file. Two copies of the string would be the same
#: defect deferred -- the same argument that keeps `PIN_DIGEST_CHARS` in one place.
PIN_SIDECAR_SUFFIX = ".prov.txt"


def pin_digest(entry: dict[str, typing.Any]) -> str:
    """The digest a PIN carries for one described file — exactly as `header()` renders it.

    Shared with the verifier deliberately, and that is the whole reason it is a function.
    A checker that recomputes this precedence independently is a checker that can disagree
    with the pin it is checking, and the disagreement would read as a stale artifact: a
    permanently red check over files nobody changed, which is the failure this package
    already learned once when the run id was in the pin. One expression, two callers, no
    way for them to drift.

    Content digest FIRST, so two runs over the same data pin identically even when the
    bytes differ by a volatile stamp. `sha256_tree` is the directory case. `MISSING` is
    rendered rather than an empty string, because a pin that silently omits an input is
    the defect the pin exists to prevent — the verifier reports it as unverifiable rather
    than treating an absent digest as agreement.
    """
    return str(
        entry.get("content_sha256") or entry.get("sha256") or entry.get("sha256_tree") or "MISSING"
    )[:PIN_DIGEST_CHARS]


def moved_since(rec: dict[str, typing.Any], base: pathlib.Path | None = None) -> str | None:
    """Has this file changed since `describe()` recorded it? A reason, or None.

    `base` is the directory a RELATIVE recorded path is relative to — the run's recorded
    `cwd`, never the current one. Without it this stat'd the recorded spelling against
    wherever the process happened to be standing at the end of the run, so an ordinary
    `chdir` between registration and `write()` pointed the check at a different file, or at
    no file. Both directions were wrong and both were silent-ish: a file that never moved
    was reported "gone" — a permanent false `changed_after_registration` in the sidecar and
    the history — and a file that genuinely WAS rewritten stat'd a nonexistent path,
    returned None, and left the record asserting it still matched. That second one is
    precisely the race this function was added to close, reopened by the path handling.

    The unclosed half of the R10 race. `unstable_during_hash` catches a file rewritten
    WHILE it was being read; nothing caught one rewritten a second later, so a run could
    pin `sha256: abc…` and finish beside a file that no longer had those bytes — with the
    record asserting, in good faith, something no longer true of anything on disk.

    A STAT, not a re-hash. Re-reading every input at the end of a run would double the I/O
    on a multi-GB corpus to answer a question a `stat` answers, and a provenance module
    that doubles the cost of the work gets removed from the work.

    Resolution is the honest limit and it is stated rather than hidden: `mtime_utc` is
    recorded to the second, so a rewrite within the same second that preserves the byte
    count is invisible here. This makes the common case VISIBLE; it does not make the race
    impossible, which nothing short of a lock does.
    """
    raw = rec.get("path")
    if not raw or rec.get("kind") not in ("file", None) or "size_bytes" not in rec:
        return None  # a directory tree, or an entry that never carried a stat to compare
    p = pathlib.Path(raw)
    try:
        st = (p if p.is_absolute() or base is None else base / p).stat()
    except FileNotFoundError:
        return "gone"
    except OSError:  # guards-ok: unreadable now is a fact worth reporting, but it is not
        # evidence the CONTENT moved, and claiming it did would be the overstatement this
        # function exists to avoid.
        return None
    if st.st_size != rec.get("size_bytes"):
        return "size"
    if _mtime_utc(st) != rec.get("mtime_utc"):
        return "mtime"
    return None
