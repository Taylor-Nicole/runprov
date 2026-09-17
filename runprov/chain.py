# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Whether a history has been edited since it was written. ADR-0016.

`verify` answers *does this artifact still follow from the inputs it names*, from the artifact's
own pin. This answers the question one level up, and nothing answered it before: **has the run
history itself been edited?** Change line 40 — a parameter, a digest, a status — and `log`,
`show`, `diff`, `impact` and `report` all repeat the new value with no sign anything moved.

TAMPER-EVIDENT, NOT TAMPER-PROOF, and the distinction is the whole honesty of the thing. Each
line's digest is public, so whoever can edit the file can also append a well-formed forged line,
or rewrite from a chosen point and re-chain everything after it. What this detects is
RETROACTIVE EDITING BY SOMEONE WHO DID NOT RE-CHAIN — which is the realistic case: a number
looks wrong, someone opens the history in an editor to fix a "typo" in a parameter, and the
record now describes a run that never happened. Claiming more needs a key, a timestamp
authority or an append-only store, each rejected in the ADR with its reason.

THE CHAIN IS VERIFIABLE WITH `sha256sum` AND NOTHING ELSE (R-12), which is why `prev` is a plain
top-level string and why the digest is over the line's bytes AS WRITTEN rather than over any
canonical form. Measured before the design was settled — this nine-line shell loop reports an
edit to line 3 as line 4 breaking:

    n=0; prev="GENESIS"
    while IFS= read -r line; do
      n=$((n+1))
      claimed=$(printf '%s' "$line" | sed -n 's/.*"prev": *"\\([^"]*\\)".*/\\1/p')
      [ "$claimed" = "$prev" ] || echo "line $n breaks the chain (line $((n-1)) changed)"
      prev=$(printf '%s' "$line" | sha256sum | cut -d' ' -f1)
    done < history.jsonl

A chain that needed this package to check it would contradict the claim the README leads with,
and would be worth less than no chain — because it would be believed.
"""

from __future__ import annotations

__all__: list[str] = []

import hashlib
import json
import os
import pathlib
import typing

#: R-3. The first line of a file has no predecessor. A SENTINEL rather than `null` or an absent
#: key, because three states must stay apart: the key ABSENT means the line was written before
#: this feature existed; `GENESIS` means it is the first line of a chain; a digest means it is
#: chained. Collapsing the first two would make every pre-feature history read as tampered on
#: the day it was upgraded — the population problem this project has met twice already.
GENESIS = "GENESIS"

#: The field. Top-level and plainly named, because a shell one-liner has to find it (R-12).
FIELD = "prev"

#: Read backwards in blocks of this size looking for the previous line's start. The history is
#: appended to forever, so reading the whole file to find its last line would be O(n) per append
#: and O(n^2) over a project — the exact cost `YamlLogSink`'s docstring refuses, on the exact
#: file it refuses it for.
_TAIL_BLOCK = 4096

#: Verdicts. The same three the package already uses everywhere: checked and clean, checked and
#: something is wrong, could not check.
INTACT = "INTACT"
BROKEN = "BROKEN"
CANNOT_CHECK = "CANNOT_CHECK"


def digest_of(line: bytes) -> str:
    """The digest of one history line. R-1, R-2: the bytes AS WRITTEN, no trailing newline.

    No canonicalisation, no key reordering, no re-encoding. A verifier hashes what it reads, so
    the chain cannot be broken by a later version of this package formatting its JSON
    differently — and `sha256sum` over the same bytes gives the same answer, which is R-12.
    """
    return hashlib.sha256(line).hexdigest()


def previous_digest(fh: typing.IO[bytes]) -> str:
    """The digest of the last complete line in an open binary file, or `GENESIS` if empty.

    CALLED INSIDE THE APPEND'S OWN LOCK (R-6) and AFTER the torn-line repair (R-7). Outside the
    lock it is a race in which two concurrent runs chain to the same predecessor and the second
    silently orphans the first; before the repair it would hash a fragment that is about to stop
    being the last line.

    Reads backwards in blocks rather than reading the file. See `_TAIL_BLOCK`.
    """
    fh.seek(0, os.SEEK_END)
    end = fh.tell()
    if end == 0:
        return GENESIS
    # The repair guarantees a final newline, so the last line ends at `end - 1`.
    stop = end - 1
    if stop == 0:  # a file containing only a newline: an empty last line, which is still a line
        return digest_of(b"")
    low = stop
    buf = b""
    while low > 0:
        step = min(_TAIL_BLOCK, low)
        low -= step
        fh.seek(low)
        buf = fh.read(step) + buf
        cut = buf.rfind(b"\n")
        if cut != -1:
            return digest_of(buf[cut + 1 :])
    return digest_of(buf)


def claimed_by(line: bytes) -> str | None:
    """What a line says its predecessor's digest was, or None if it says nothing.

    PARSED AS JSON, not with a regex, because this is the reader inside the package and it has
    a parser. The shell recipe in the module docstring uses `sed` and that is fine for it: a
    one-liner that is wrong about an escaped quote reports a break, which is the safe direction.
    Here a false break would be an accusation.
    """
    try:
        value = json.loads(line).get(FIELD)
    except (ValueError, AttributeError):  # unreadable, or not an object
        return None
    return value if isinstance(value, str) else None


class Link(typing.NamedTuple):
    """One line's place in the chain."""

    line: int
    status: str
    claimed: str | None = None
    computed: str | None = None

    @property
    def detail(self) -> str:
        """R-10. Names both digests AND says which line changed.

        The counter-intuitive part is that a break at line N means line N-1 is what moved: N's
        claim is a statement about its predecessor. A reader told only "line N breaks" inspects
        the wrong line, which is worse than no message because it costs them the time and then
        their confidence in the answer.
        """
        if self.status == BROKEN:
            return (
                f"line {self.line} claims its predecessor was {self.claimed or '<absent>'} "
                f"but line {self.line - 1} hashes to {self.computed} — "
                f"LINE {self.line - 1} IS WHAT CHANGED"
            )
        return f"line {self.line}: {self.status}"


class Report(typing.NamedTuple):
    """What the chain says, and what it was able to look at.

    R-9. `lines` and `unchained` are not decoration: "intact" over a file whose chain covers
    three of nine hundred lines is the vacuous pass this project has fixed in five places, and
    the only defence is that the count of what was examined travels with the verdict.
    """

    lines: int
    chained_from: int | None
    unchained: int
    broken: list[Link]
    unreadable: list[Link]

    @property
    def status(self) -> str:
        if self.broken:
            return BROKEN
        if self.chained_from is None:
            return CANNOT_CHECK  # nothing in this file is chained; there is no claim to check
        return INTACT

    @property
    def checked(self) -> int:
        """Links actually verified — never inferred from `lines`."""
        if self.chained_from is None:
            return 0
        return self.lines - self.chained_from + 1 - len(self.unreadable)


def verify(path: str | pathlib.Path) -> Report:
    """Walk the chain. Never raises for a missing or unreadable file — that is `CANNOT_CHECK`."""
    p = pathlib.Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return Report(0, None, 0, [], [])
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the final newline is a terminator, not an empty line

    chained_from: int | None = None
    unchained = 0
    broken: list[Link] = []
    unreadable: list[Link] = []
    for index, line in enumerate(lines, start=1):
        claimed = claimed_by(line)
        if claimed is None:
            try:
                json.loads(line)
            except ValueError:
                # R-11. A torn or corrupt line cannot be asked what it claims, and calling that
                # tampering would have this package accuse a user the first time a disk goes
                # bad. Corruption and editing are different findings.
                unreadable.append(Link(index, CANNOT_CHECK))
                continue
            if chained_from is None:
                unchained += 1  # R-3/R-4: written before the feature existed
                continue
            # R-5. Once a file contains a chained line, every later line must carry one. A line
            # without it here is what splicing an old-format record in would look like.
            broken.append(Link(index, BROKEN, None, digest_of(lines[index - 2])))
            continue
        if chained_from is None:
            chained_from = index
        if index == 1:
            expected = GENESIS
        else:
            expected = digest_of(lines[index - 2])
        if claimed != expected:
            broken.append(Link(index, BROKEN, claimed, expected))
    return Report(len(lines), chained_from, unchained, broken, unreadable)


def render(report: Report, path: pathlib.Path) -> list[str]:
    """The report, stating what was checked and not only what was found (R-9)."""
    out = [f"# chain — {path}"]
    if report.status == CANNOT_CHECK and report.lines == 0:
        out.append("  CANNOT CHECK: no history to read.")
        return out
    if report.chained_from is None:
        out.append(
            f"  CANNOT CHECK: {report.lines} line(s), none of them chained. This history was "
            f"written before the chain existed; the next run to append will anchor it."
        )
        return out
    out.append(
        f"  {report.status}: {report.checked} link(s) checked of {report.lines} line(s), "
        f"chained from line {report.chained_from}"
        + (f", {report.unchained} line(s) predate the chain" if report.unchained else "")
    )
    for link in report.unreadable:
        out.append(f"    COULD NOT CHECK  {link.detail}")
    for link in report.broken:
        out.append(f"    BROKEN  {link.detail}")
    if report.broken:
        out.append(
            "  A break means the history was edited after it was written, OR that a line was "
            "deleted or reordered. It does not say which, and it cannot say who."
        )
    return out
