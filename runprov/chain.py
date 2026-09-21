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


#: ADR-0016 R-25. The status of ONE EDGE — the adjacent pair (n-1, n), carrying line n's claim
#: about line n-1. THE EDGE IS THE UNIT OF JUDGEMENT, and that is the change Audit F forced: a
#: line carries two different facts — whether its own claim is correct, and whether its bytes
#: are attested by its successor — and every defect in the judgement half came from those two
#: sharing one variable. An edge has exactly one status, a line's bytes are attested iff the
#: edge above it HOLDS, and the file's verdict is a fold. That model is small enough to
#: enumerate; the per-line one never was, and it was enumerated by intuition three times.
HOLDS = "HOLDS"
HOLDS_TRIVIAL = "HOLDS_TRIVIAL"  # the first line: nothing precedes it, so it attests nothing
UNCHAINED = "UNCHAINED"  # predates the chain — unverifiable and NOT fixable, never decisive
GAP = "GAP"  # a release too old to chain wrote it — unverifiable and FIXABLE, so decisive
UNCHECKABLE = "UNCHECKABLE"  # the evidence is gone; say so rather than guess either way

#: The enumerated inputs. Small, closed, and named so the table can be executed over them.
CLAIMS = ("NONE", "GENESIS", "DIGEST")
PREDECESSORS = ("NONE_FIRST", "READABLE", "UNREADABLE")
AGREEMENTS = ("NA", "MATCHES", "DIFFERS")
WRITERS = ("PRE_CHAIN", "CAPABLE", "UNSTATED", "UNREADABLE")


def classify(claim: str, predecessor: str, agreement: str, writer: str, started: bool) -> str:
    """ADR-0016 R-25: the status of one edge, by the FIRST rule that applies.

    TOTAL over the enumerated inputs, and a test asserts that (R-30) by running all 216
    combinations through it. Two of the rules below were added when the table was first
    executed rather than read — rule 0, because tearing a line must not downgrade an
    impossible `GENESIS` claim, and the split in rule 3, because a torn first line did not
    "predate the chain". Both were wrong in the table as written, and reading it had not
    shown that.
    """
    if claim == "GENESIS" and predecessor != "NONE_FIRST":
        return BROKEN  # rule 0: a sentinel is not a digest, whatever the predecessor's state
    if predecessor == "NONE_FIRST":
        if claim == "GENESIS":
            return HOLDS_TRIVIAL  # rule 1
        if claim == "DIGEST":
            return BROKEN  # rule 2: claims a predecessor and has none — lines removed in front
        return UNCHECKABLE if writer == "UNREADABLE" else UNCHAINED  # rules 3, 3b
    if claim == "NONE":
        if not started:
            return UNCHAINED  # rule 4: R-4's pre-chain prefix
        if writer == "PRE_CHAIN":
            return GAP  # rule 5: names the version; closes when that machine is upgraded
        if writer == "UNREADABLE":
            return UNCHECKABLE  # rule 6: torn, and R-20 recovered nothing — it said nothing
        return BROKEN  # rule 7: a release that can chain wrote no claim — the splice
    if predecessor == "UNREADABLE":
        # rule 8: the honest crash. R-7 repairs the fragment BEFORE the successor reads it, so
        # the claim is over the fragment AS IT SITS — measured, it matches exactly. The first
        # two designs skipped this comparison on a premise that was never checked, and skipping
        # it is what let a run be erased with a text editor under a green verdict.
        # rule 9: and when it differs, a tear that happened AFTER the fact and an edit are
        # indistinguishable from the file alone. Never BROKEN (R-11), never silent (F-01).
        return HOLDS if agreement == "MATCHES" else UNCHECKABLE
    return HOLDS if agreement == "MATCHES" else BROKEN  # rules 10, 11


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
    """The edges, and the fold over them. ADR-0016 R-26, R-27."""

    lines: int
    edges: list[Link]
    chained_from: int | None
    #: R-28. Lines whose terminator was translated after they were written.
    translated: int = 0

    @property
    def status(self) -> str:
        """R-26. The file's verdict is its worst edge, and nothing else.

        The whole rule, in three lines. Every earlier version of this decided the verdict from
        a hand-assembled set of conditions — `broken`, `translated`, `chained_from`,
        `unattested`, a stale-writer check — and every one of them missed a state. A fold over
        an enumerated status cannot.
        """
        kinds = {link.status for link in self.edges}
        if BROKEN in kinds:
            return BROKEN
        if UNCHECKABLE in kinds or GAP in kinds:
            return CANNOT_CHECK
        if self.chained_from is None:
            return CANNOT_CHECK  # nothing in this file is chained; there is no claim to check
        return INTACT

    @property
    def attested(self) -> int:
        """R-27. The number of edges that HOLD — each attests the line beneath it.

        A COUNT OF A STATUS, never a subtraction from a total. The subtraction is how this came
        to overcount lines that made no claim (F-04) and to go negative (E-09); a count of a
        status can do neither. `HOLDS_TRIVIAL` is excluded because the first line attests
        nothing: there is nothing beneath it.
        """
        return sum(1 for link in self.edges if link.status == HOLDS)

    def of(self, status: str) -> list[Link]:
        """Every edge with this status, for the renderer and for callers."""
        return [link for link in self.edges if link.status == status]


def _writers(records: dict[int, dict[str, typing.Any]]) -> dict[str, str]:
    """`{run_uid: version}` from every record that states both. ADR-0016 R-29.

    THE WRITER IS A PROPERTY OF THE RUN, NOT THE LINE, and this is the miss that cost the most:
    no released version writes a `tool` block into a `runprov.start.v1` line, which is half of
    every history. Reading the line alone makes every start line `UNSTATED`, and rule 7 calls
    that a splice — so one ordinary run by a colleague on a released wheel reported BROKEN.
    Measured against the real bytes in `tests/corpus/0.5.0`.
    """
    found: dict[str, str] = {}
    for record in records.values():
        tool = record.get("tool")
        version = tool.get("version") if isinstance(tool, dict) else None
        uid = record.get("run_uid") or record.get("run_id")
        if isinstance(version, str) and isinstance(uid, str):
            found.setdefault(uid, version)
    return found


def _writer_of(record: dict[str, typing.Any] | None, by_run: dict[str, str]) -> str:
    """One of `WRITERS`. Total over any JSON shape — a `tool` that is a string, a list or
    null resolves to `UNSTATED` and never raises (F-05)."""
    if record is None:
        return "UNREADABLE"
    tool = record.get("tool")
    version = tool.get("version") if isinstance(tool, dict) else None
    if not isinstance(version, str):
        uid = record.get("run_uid") or record.get("run_id")
        version = by_run.get(uid) if isinstance(uid, str) else None
    if not isinstance(version, str):
        return "UNSTATED"
    parts = re.findall(r"\d+", version)[:3]
    if not parts:
        return "UNSTATED"
    return "PRE_CHAIN" if tuple(int(x) for x in parts) < CHAINS_FROM else "CAPABLE"


def verify(path: str | pathlib.Path) -> Report:
    """Walk the edges. Never raises for a missing or unreadable file — that is `CANNOT_CHECK`."""
    p = pathlib.Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return Report(0, [], None)

    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the final newline is a terminator, not an empty line
    if not lines:
        return Report(0, [], None)

    # R-28. CRLF IS A PER-LINE FACT. A raw carriage return cannot appear inside a JSON line —
    # JSON escapes control characters — so a trailing one is always a terminator that was
    # translated after the line was written. Deciding this per line rather than per FILE is what
    # stops one stray byte discarding every finding and printing "not tampering" (F-03).
    translated = [line.endswith(b"\r") for line in lines]

    records: dict[int, dict[str, typing.Any]] = {}
    for index, line in enumerate(lines, start=1):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):  # R-24: valid JSON that is not an object says nothing
            records[index] = parsed
    by_run = _writers(records)

    edges: list[Link] = []
    chained_from: int | None = None
    for index, line in enumerate(lines, start=1):
        claimed = claimed_by(line)
        claim = "NONE" if claimed is None else ("GENESIS" if claimed == GENESIS else "DIGEST")
        if claim != "NONE" and chained_from is None:
            chained_from = index

        terminator_only = False
        if index == 1:
            predecessor, agreement = "NONE_FIRST", "NA"
        else:
            predecessor = "READABLE" if (index - 1) in records else "UNREADABLE"
            if claim == "NONE":
                agreement = "NA"
            else:
                agreement = "MATCHES" if claimed == digest_of(lines[index - 2]) else "DIFFERS"
                if agreement == "DIFFERS" and translated[index - 2]:
                    # R-28, and Audit G's G-02. The predecessor's terminator was translated, so
                    # its bytes AS THEY SIT cannot hash to what was claimed. The edge is owed
                    # exactly ONE further question — does the claim match those bytes with the
                    # `\r` stripped? — because a match PROVES the terminator was the only thing
                    # that changed, and nothing weaker does.
                    #
                    # WHAT STOOD HERE WAS A PRE-EMPTION: every edge behind a translated line was
                    # declared UNCHECKABLE before the table saw it. So appending one `\r` to a
                    # line you had just forged turned BROKEN/exit 1 into CANNOT_CHECK/exit 2
                    # printing "not tampering", and inside a genuine `core.autocrlf=true`
                    # checkout — where every edge was pre-empted — no edit was detectable at
                    # all, ever. F-03's file-level bail-out, one size smaller.
                    terminator_only = claimed == digest_of(lines[index - 2][:-1])
                    agreement = "MATCHES" if terminator_only else "DIFFERS"

        writer = _writer_of(records.get(index), by_run)
        status = classify(claim, predecessor, agreement, writer, chained_from is not None)
        named = None
        if terminator_only:
            # R-28. The proof buys the predecessor its innocence and nothing else: the bytes on
            # disk are not the bytes that were written, so this edge attests nobody. One edge,
            # named rather than accused, and it suppresses no other line's judgement.
            status, named = UNCHECKABLE, "translated"
        elif status in (GAP, BROKEN):
            # NAMED ON A BREAK TOO, not only on a gap. R-10 requires the message to be true of
            # the break it found, and "written by a version it does not name" is false when the
            # record names one — it is the difference between a reader chasing a machine and a
            # reader chasing a ghost.
            record = records.get(index) or {}
            tool = record.get("tool")
            named = tool.get("version") if isinstance(tool, dict) else None
            named = named or by_run.get(str(record.get("run_uid") or record.get("run_id")))
        edges.append(
            Link(
                index,
                status,
                claimed,
                None if predecessor == "NONE_FIRST" else digest_of(lines[index - 2]),
                wrote=named,
            )
        )
    return Report(len(lines), edges, chained_from, sum(translated))


def render(report: Report, path: pathlib.Path) -> list[str]:
    """The report. R-9: states what it checked, not only what it found."""
    out = [f"# chain — {path}"]
    if report.lines == 0:
        return [*out, "  CANNOT CHECK: no history to read."]
    if report.chained_from is None:
        return [
            *out,
            f"  CANNOT CHECK: {report.lines} line(s), none of them chained. This history was "
            f"written before the chain existed; the next run to append will anchor it.",
        ]

    out.append(
        f"  {report.status}: {report.attested} line(s) attested of {report.lines}, "
        f"chained from line {report.chained_from}"
    )
    if report.translated:
        # R-28. Named, and scoped to the lines it actually affected — not a verdict for the file.
        # IT NO LONGER SAYS "not tampering" (G-02): that was a verdict for the whole file
        # dressed as a note about some of its lines, and it printed unchanged over a file
        # carrying a forgery. The sentence now has to be able to stand on the same page as a
        # BROKEN row without contradicting it, so it reports the cause and stops there.
        out.append(
            f"    {report.translated} line(s) had their endings translated to CRLF after they "
            f"were written, so their digests cannot match — consistent with a git checkout with "
            f"`core.autocrlf=true`. Add `{path.name} -text` to `.gitattributes`. It is a fact "
            f"about those lines and suppresses no finding below."
        )
    stale: dict[str, int] = {}
    for link in report.of(GAP):
        stale[link.wrote or "a version it does not name"] = (
            stale.get(link.wrote or "a version it does not name", 0) + 1
        )
    for version, count in sorted(stale.items()):
        out.append(
            f"    {count} line(s) written by runprov {version}, which cannot chain — "
            f"upgrade that machine to close the gap"
        )
    if report.of(UNCHAINED):
        out.append(f"    {len(report.of(UNCHAINED))} line(s) predate the chain")
    for link in report.of(UNCHECKABLE):
        if link.wrote == "translated":
            continue  # already summarised above
        out.append(
            f"    COULD NOT CHECK  line {link.line}: nothing readable vouches for line "
            f"{link.line - 1}, and a tear that happened later cannot be told from an edit"
        )
    for link in report.of(BROKEN):
        out.append(f"    BROKEN  {link.detail}")

    # R-23, always: the newest line is attested by nothing, because a line cannot contain its
    # own digest and nothing follows it yet.
    out.append(
        f"    line {report.lines} is the newest and nothing attests it yet; a later run will. "
        f"Truncation of the tail cannot be seen from this file alone."
    )
    if report.of(BROKEN):
        out.append(
            "  A break means the history was edited after it was written, OR that a line was "
            "deleted or reordered. It does not say which, and it cannot say who."
        )
    return out
