# Copyright (c) 2026 Hôpital Henri-Mondor and Taylor Thompson
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
import pathlib
import re

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


def content_digest(path: pathlib.Path) -> str | None:
    """SHA-256 with volatile stamps removed. Binary falls back to the raw hash."""
    if not path.is_file():
        return None
    try:
        if path.suffix == ".gz":
            return hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()
    except (OSError, EOFError, gzip.BadGzipFile):
        return sha256(path)
    try:
        # encoding="utf-8" EXPLICITLY. Without it Python uses the locale default, which is
        # cp1252 on Windows, so a UTF-8 artifact is decoded wrongly and then re-encoded as
        # UTF-8 below -- producing a DIFFERENT content digest for the same bytes depending
        # on which machine ran. For a provenance tool that is not a portability nit: it
        # means two honest runs disagree about whether a file changed. Windows CI caught it.
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return sha256(path)
    body = "\n".join(ln for ln in text.splitlines() if not VOLATILE.match(ln))
    if path.suffix == ".json":
        body = VOLATILE_JSON.sub('""', body)
    return hashlib.sha256(body.encode()).hexdigest()


def describe(path: pathlib.Path) -> dict:
    """Everything recorded about one input or output. Directories are hashed as a tree."""
    st = path.stat()
    rec = {
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
