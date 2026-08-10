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
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import itertools
import pathlib
import re
import typing

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
    except (OSError, EOFError, gzip.BadGzipFile):
        return sha256(path)

    h = hashlib.sha256()
    is_json = path.suffix == ".json"
    first = True
    try:
        with open(path, encoding="utf-8") as fh:
            while True:
                block = list(itertools.islice(fh, chunk_lines))
                if not block:
                    break
                kept = [ln.rstrip("\n") for ln in block if not VOLATILE.match(ln)]
                if not kept:
                    continue
                body = "\n".join(kept)
                if is_json:
                    body = VOLATILE_JSON.sub('""', body)
                h.update((("" if first else "\n") + body).encode())
                first = False
    except (UnicodeDecodeError, OSError):
        return sha256(path)
    return h.hexdigest()


def _gzip_digest(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    """Decompress in fixed blocks -- the gzip header stores a compression mtime, so a .gz
    rewritten from identical bytes never hashes the same twice, and reading it whole to
    work around that would defeat the streaming above."""
    h = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        while blk := fh.read(chunk):
            h.update(blk)
    return h.hexdigest()


def describe(path: pathlib.Path) -> dict[str, typing.Any]:
    """Everything recorded about one input or output. Directories are hashed as a tree."""
    st = path.stat()
    rec: dict[str, typing.Any] = {
        "path": str(path),
        "size_bytes": st.st_size,
        "mtime_utc": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        rec["kind"] = "directory"
        rec["n_files"] = len(files)
        h = hashlib.sha256()
        for f in files:
            h.update(f.relative_to(path).as_posix().encode())
            h.update(sha256(f).encode())
        rec["sha256_tree"] = h.hexdigest()
    else:
        rec["kind"] = "file"
        rec["sha256"] = sha256(path)
        rec["content_sha256"] = content_digest(path)
    return rec
