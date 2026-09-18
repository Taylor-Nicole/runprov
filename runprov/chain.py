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
import re
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

#: R-20. The one anchored shape a torn line may still be asked about. `prev` is the FIRST field
#: (R-19), so a fragment that reached this far carries a verifiable claim about its predecessor
#: even though the rest of it is gone. Anchored and exact-shape, and tried only for lines the
#: JSON parser has already refused, so garbage cannot match — this is not a second parser.
_RAW_CLAIM = re.compile(rb'^\{"prev": "(GENESIS|[0-9a-f]{64})"')

#: R-5. The first release that writes a chain. A line with no `prev` whose `tool.version` names
#: something OLDER is a supported writer and a coverage gap; one naming this or later is a
#: break. DERIVED from `__init__.__version__` would be wrong — this is the version at which the
#: behaviour changed, and it does not move when the package does.
CHAINS_FROM = (0, 6, 0)


def _version_of(record: typing.Mapping[str, typing.Any]) -> tuple[int, ...] | None:
    """The runprov version that wrote a record, from its own `tool` block. None if unstated.

    Present since 0.3.0 (U-01). 0.1.0 and 0.2.0 wrote `tool: null`, and those releases predate
    the chain by so much that a line of theirs appearing AFTER a chained one means a three-
    version downgrade — which R-5 treats as a break, because at that point the file is saying
    something a supported workflow cannot produce.
    """
    tool = record.get("tool")
    version = tool.get("version") if isinstance(tool, dict) else None
    if not isinstance(version, str):
        return None
    parts = re.findall(r"\d+", version)[:3]
    return tuple(int(x) for x in parts) if parts else None


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

    R-24: a line that is valid JSON but NOT an object says nothing — `json.loads(b"[1,2,3]")`
    succeeds and has no `.get`. That arm is the only thing between a hand-edited history and an
    uncaught exception, and `verify`'s docstring promises it never raises.

    R-20: when the parser refuses the line entirely, ONE anchored attempt is made on the raw
    bytes. `prev` is the first field (R-19), so a fragment left by a crash usually still carries
    its claim intact — which is what lets R-11's two halves both hold: the torn line is still
    reported unreadable, and its predecessor is still attested.
    """
    try:
        value = json.loads(line).get(FIELD)
    except AttributeError:  # valid JSON, not an object (R-24)
        return None
    except ValueError:  # not JSON at all — the fragment may still carry its claim (R-20)
        found = _RAW_CLAIM.match(line)
        return found.group(1).decode() if found else None
    return value if isinstance(value, str) else None


class Link(typing.NamedTuple):
    """One line's place in the chain."""

    line: int
    status: str
    claimed: str | None = None
    computed: str | None = None
    #: The runprov that wrote it, when the line says so — R-5 judges an unchained line by this.
    wrote: str | None = None

    @property
    def detail(self) -> str:
        """R-10, and every clause of it must be TRUE of the break being reported.

        The counter-intuitive part is that a break at line N means line N-1 is what moved: N's
        claim is a statement about its predecessor. But that reasoning only holds when there IS
        a claim and when there IS a predecessor, and the first version formatted one sentence
        regardless — producing "LINE 0 IS WHAT CHANGED" for a deleted first line, "line 0 hashes
        to GENESIS" with a sentinel presented as a digest, and "LINE N-1 IS WHAT CHANGED" for a
        line that had made no claim at all. A diagnostic that sends a reader to an untouched
        line costs them the time and then their confidence in the answer.
        """
        if self.status != BROKEN:
            return f"line {self.line}: {self.status}"
        if self.claimed is None:
            return (
                f"line {self.line} carries no chain claim, and was written by "
                f"{self.wrote or 'a version it does not name'} — a release that CAN chain must "
                f"not append an unchained line"
            )
        if self.line == 1:
            return (
                f"line 1 claims a predecessor ({self.claimed[:16]}…) but is the first line of "
                f"the file — one or more lines have been removed from the front"
            )
        return (
            f"line {self.line} claims its predecessor was {self.claimed} "
            f"but line {self.line - 1} hashes to {self.computed} — "
            f"LINE {self.line - 1} IS WHAT CHANGED"
        )


class Report(typing.NamedTuple):
    """What the chain says, and what it was able to look at.

    R-9. `attested` and `unattested` are not decoration: "intact" over a file whose chain covers
    three of nine hundred lines is the vacuous pass this project has fixed in five places, and
    the only defence is that the count of what was examined travels with the verdict.
    """

    lines: int
    chained_from: int | None
    #: Lines written before the chain existed, or by a release too old to write one (R-5).
    unchained: list[Link]
    broken: list[Link]
    unreadable: list[Link]
    #: Lines whose bytes NOTHING verified because their successor was unreadable and carried no
    #: surviving claim (R-11). THE LAST LINE IS NOT IN HERE: it is unattested by construction —
    #: nothing follows it yet — which is a standing fact about every history, disclosed by
    #: `render` under R-23 and never a verdict. Putting it here made an untouched history report
    #: CANNOT_CHECK, so `chain` could never return 0: the gate that cannot pass, which this
    #: project has now built four times and caught the fifth before it shipped.
    unattested: list[Link]
    #: R-21. Set when the file's line endings were translated after it was written.
    translated: bool = False

    @property
    def status(self) -> str:
        """R-8. BROKEN dominates; then anything unverifiable; then INTACT.

        The first version consulted only `broken`, so a file in which ONE link of six could be
        checked printed INTACT and exited 0 — the vacuous pass, delivered as a green gate, by
        the command built to detect exactly that.
        """
        if self.broken:
            return BROKEN
        if self.translated or self.chained_from is None or self.unattested:
            return CANNOT_CHECK
        # R-5, and this is where Taylor's decision (2026-09-18) has its effect. A line written
        # after the chain began by a release too old to chain is UNVERIFIABLE AND FIXABLE: the
        # machine can be upgraded and the gap closes. So it decides the verdict, and a gate
        # stays non-zero until somebody does it — which is the whole reason the strict reading
        # was chosen over tolerating mixed versions.
        #
        # Lines BEFORE `chained_from` are unverifiable and NOT fixable — no upgrade can chain a
        # line that was already written — so they are disclosed and never decisive. Making them
        # decisive would mean no project that predates the feature could ever exit 0, which is
        # the gate that cannot pass.
        if any(link.wrote for link in self.unchained):
            return CANNOT_CHECK
        return INTACT

    @property
    def attested(self) -> int:
        """Lines whose bytes a successor's claim actually proved unchanged.

        EDGES, NOT COMPARISONS, and the difference is not pedantry: line 1's comparison is
        against the `GENESIS` sentinel, which attests no bytes — a forger writes
        `"prev": "GENESIS"` for free. Counting it made the report claim `N of N` coverage that
        the mechanism cannot have, and the arithmetic could go negative besides.
        """
        if self.chained_from is None:
            return 0
        failed = {link.line for link in self.broken} | {link.line for link in self.unreadable}
        lost = {link.line for link in self.unattested}
        # Line n-1 is attested when line n made a claim that was checked and held. The last
        # line is never attested — nothing follows it — so the range stops one short of it.
        return sum(
            1
            for n in range(max(self.chained_from, 2), self.lines + 1)
            if n not in failed and (n - 1) not in lost
        )


def verify(path: str | pathlib.Path) -> Report:
    """Walk the chain. Never raises for a missing or unreadable file — that is `CANNOT_CHECK`."""
    p = pathlib.Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return Report(0, None, [], [], [], [])

    # R-21. A git checkout with `core.autocrlf=true` — the Windows default — rewrites every line
    # ending, so every digest taken over LF bytes disagrees with the CRLF bytes now on disk.
    # Reproduced with a real clone: EVERY line from the second onward reported BROKEN, each
    # naming a specific innocent line. The bytes genuinely did change, so INTACT would be false;
    # what is false is calling it tampering. Name the cause and the fix instead.
    if b"\r\n" in raw:
        return Report(raw.count(b"\n"), None, [], [], [], [], translated=True)

    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the final newline is a terminator, not an empty line

    chained_from: int | None = None
    unchained: list[Link] = []
    broken: list[Link] = []
    unreadable: list[Link] = []
    readable: set[int] = set()

    for index, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
            readable.add(index)
        except ValueError:
            # R-11, and it must not ACCUSE. A torn line is a fact about a disk, not about a
            # person; the package that confuses them is disbelieved the first time one goes bad.
            unreadable.append(Link(index, CANNOT_CHECK))
            record = {}
        claimed = claimed_by(line)

        if claimed is None:
            if chained_from is None:
                unchained.append(Link(index, CANNOT_CHECK))  # R-3/R-4: predates the chain
                continue
            if index in unreadable_lines(unreadable):
                continue  # already counted; its predecessor is handled below
            # R-5, judged by the version that wrote it rather than by the absence alone.
            wrote = _version_of(record) if isinstance(record, dict) else None
            named = (record.get("tool") or {}).get("version") if isinstance(record, dict) else None
            if wrote is not None and wrote < CHAINS_FROM:
                unchained.append(Link(index, CANNOT_CHECK, wrote=str(named)))
                continue
            broken.append(Link(index, BROKEN, None, None, wrote=str(named) if named else None))
            continue

        if chained_from is None:
            chained_from = index
        if index > 1 and (index - 1) not in readable:
            # R-11, and this is the half that survived the first repair. A claim about a line
            # that is ITSELF torn cannot be checked: the bytes on disk are a fragment, so the
            # digest will differ for a reason that has nothing to do with editing. Comparing
            # anyway is how a bad disk sector came to print "LINE 3 IS WHAT CHANGED". The link
            # is unverifiable and the predecessor is unattested — both are said, neither accuses.
            continue
        expected = GENESIS if index == 1 else digest_of(lines[index - 2])
        if claimed != expected:
            broken.append(Link(index, BROKEN, claimed, expected))

    # R-11's other half, and R-23. A line is UNATTESTED when the line after it could not be read
    # AND that fragment carried no surviving claim — `prev` is written first (R-19) precisely so
    # that is rare. The last line is unattested by construction: nothing follows it.
    unattested: list[Link] = []
    if chained_from is not None:
        for link in unreadable:
            previous = link.line - 1
            if previous < chained_from or previous < 1:
                continue
            # Line `previous` is unattested when the line after it could not vouch for it —
            # either because that line carried no surviving claim (R-20 failed to recover one),
            # or because `previous` is itself torn, so there are no original bytes to compare.
            if previous not in readable:
                # Already reported as unreadable, which is a STRONGER statement about it than
                # "unattested". Saying both would pad the list with the same finding twice and
                # make a shredded file look like ten problems instead of one.
                continue
            if claimed_by(lines[link.line - 1]) is None:
                unattested.append(Link(previous, CANNOT_CHECK))
    return Report(len(lines), chained_from, unchained, broken, unreadable, unattested)


def unreadable_lines(links: list[Link]) -> set[int]:
    """The line numbers in a list of links. Named so the walk above reads as prose."""
    return {link.line for link in links}


def render(report: Report, path: pathlib.Path) -> list[str]:
    """The report, stating what was checked and not only what was found (R-9, R-23)."""
    out = [f"# chain — {path}"]
    if report.translated:  # R-21
        return [
            *out,
            "  CANNOT CHECK: this file's line endings were translated after it was written (CRLF).",
            "  Every digest was taken over the bytes as written, so none of them can match. This",
            "  is a git checkout with `core.autocrlf=true`, not tampering — add a line to",
            "  `.gitattributes` so the history is never translated:",
            "",
            f"      {path.name} -text",
        ]
    if report.lines == 0:
        return [*out, "  CANNOT CHECK: no history to read."]
    if report.chained_from is None:
        out.append(
            f"  CANNOT CHECK: {report.lines} line(s), none of them chained. This history was "
            f"written before the chain existed; the next run to append will anchor it."
        )
        return out

    out.append(
        f"  {report.status}: {report.attested} line(s) attested of {report.lines}, "
        f"chained from line {report.chained_from}"
    )
    # R-5. Name the version and the count, so the finding is something a person can act on
    # rather than a state they must accept.
    stale = [link for link in report.unchained if link.wrote]
    if stale:
        by_version: dict[str, int] = {}
        for link in stale:
            by_version[link.wrote or "?"] = by_version.get(link.wrote or "?", 0) + 1
        for version, count in sorted(by_version.items()):
            out.append(
                f"    {count} line(s) written by runprov {version}, which cannot chain — "
                f"upgrade that machine to close the gap"
            )
    predate = len(report.unchained) - len(stale)
    if predate:
        out.append(f"    {predate} line(s) predate the chain")
    for link in report.unreadable:
        out.append(f"    COULD NOT CHECK  {link.detail}")
    for link in report.broken:
        out.append(f"    BROKEN  {link.detail}")

    # R-23, always, and not only when something is wrong: the newest line is attested by
    # nothing, because a line cannot contain its own digest and nothing follows it yet.
    out.append(
        f"    line {report.lines} is the newest and nothing attests it yet; a later run will. "
        f"Truncation of the tail cannot be seen from this file alone."
    )
    for link in report.unattested:
        out.append(
            f"    NOT ATTESTED  line {link.line}: line {link.line + 1} is unreadable and "
            f"carried no surviving claim, so nothing vouches for these bytes"
        )
    if report.broken:
        out.append(
            "  A break means the history was edited after it was written, OR that a line was "
            "deleted or reordered. It does not say which, and it cannot say who."
        )
    return out
