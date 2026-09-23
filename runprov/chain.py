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

#: Audit G, G-03. A CRASH TRUNCATES A LINE; IT NEVER CONCATENATES TWO. So an unreadable line
#: that still contains a record BOUNDARY inside it — a closing brace against an opening one, or
#: a second `prev` anchor — is not a fragment: it is two records whose separating terminator was
#: destroyed. Overwrite one `\n` and two runs vanish from `log`, `show` and `report` while every
#: byte of both survives, which is the single-byte edit R-31 exists to forbid and the only one
#: that ever escaped it.
#:
#: REPORTED AS `CANNOT_CHECK`, NEVER AS AN ACCUSATION, and this is not merely caution. What the
#: two merged records used to say cannot be read back from the line, so there is nothing R-10
#: could print that would be true of the break. And a destroyed interior terminator has an
#: innocent producer: Audit G's skeptic built the post-crash byte state and an `fsck` zero-
#: filling a block it cannot recover — the recovery path this repository's own parent directory
#: is named after — turns the `\n` inside that block into a NUL, which joins the two lines
#: exactly as a hand edit would. `re.S` is what makes the pattern see that NUL. So the true
#: statement is the narrow one: a TRUNCATING crash cannot produce this. Something else might.
#:
#: Anchored and exact-shape like `_RAW_CLAIM`, and tried ONLY on lines the JSON parser has
#: already refused, which is what keeps R-20/R-11's honest crash free: a fragment is a PREFIX of
#: one record, so it contains no boundary.
#:
#: SWEPT BEFORE THIS SHIPPED, because a false positive here costs a user a spurious
#: CANNOT_CHECK over an intact file, which is the class two waves of repairs have just removed.
#: Every runprov history reachable from this machine — the five corpus versions, each of them
#: plus one append by the current code, every `*.jsonl` in the tree, every `*.jsonl` blob ever
#: committed to any ref, and generated histories carrying lists of objects, nested records and
#: strings containing the boundary text itself — and EVERY truncation of every one of their
#: lines, which is the whole space a tear can leave behind: **zero matches**, over 42 sources,
#: 206 history lines and 194 101 crash fragments.
#:
#: The reason is structural rather than luck: a `"` inside a JSON string is always escaped to
#: `\"`, so the two bytes `{"` cannot occur inside a string VALUE at all. The one shape that
#: does match is a list of objects serialised with COMPACT separators (`},{"`) — and it is not
#: hypothetical: 1 294 780 lines of NCBI `datasets` output on this same drive match. Every one
#: of them is accepted by `json.loads`, so this pattern is never consulted for any of them, and
#: none is a runprov history. `json.dumps` with its default `", "` writes two bytes between the
#: braces, and `JsonlSink` passes no `separators`. If it ever does, this pattern must be
#: re-measured before the change lands.
_BOUNDARY = re.compile(rb'\}.?\{"|.\{"prev": "(?:GENESIS|[0-9a-f]{64})"', re.S)

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
#: R-32. A release that could have chained wrote no claim. DECISIVE — it forces CANNOT_CHECK,
#: so it can never be mistaken for a clean bill — but NEVER an accusation, because the same
#: bytes are produced by an append that could not take the lock (R-22) and by a start line
#: whose run is still going. Rule 7 used to call all three a splice, and the two innocent
#: classes are permanent: R-13 gives nothing the power to clear a finding, so the only way to
#: remove it was to edit the history — the act this feature exists to detect.
#:
#: IT IS A PROPERTY OF THE JUDGEMENT AND NOT A NEW THING TO WRITE INTO THE FILE, which is why
#: it costs nothing: `CLAIMS` keeps its three values and the table its 216 cells. The rejected
#: remedy was a fourth `CLAIMS` value the sink would write on the unlocked path — measured and
#: refused, because it helps no history already on disk, contradicts R-22's "no claim at all",
#: and hands a tail-forger a free BROKEN -> CANNOT_CHECK downgrade for the price of typing it.
UNCLAIMED = "UNCLAIMED"

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
        # rule 7: a release that could have chained wrote no claim. DETECTED AND DISCLOSED,
        # NEVER AN ACCUSATION (R-32). It returned BROKEN until Audit G, and what that bought
        # was one narrow detection — a TAIL splice by a forger who did not compute `prev`,
        # while the nine-line `sha256sum` recipe for computing it is in this module's own
        # docstring. A mid-file splice is still BROKEN at the NEXT edge, by rule 11. What it
        # charged for that was two classes of permanent false accusation over complete,
        # correct files: G-01, an append that could not take the lock, and G-05, a start line
        # whose run is still in flight or was killed.
        return UNCLAIMED
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

        A FOURTH SHAPE (Audit G, G-09), and the only one reachable with no forger at all.
        Rule 0 breaks the edge when a line's `prev` is the GENESIS SENTINEL and something
        precedes it — which is what concatenating two rotated histories leaves (R-14):
        `cat old.jsonl new.jsonl > merged.jsonl`, each half INTACT alone, not a byte edited.
        The rule-11 sentence was formatted over it and read "line 3 claims its predecessor
        was GENESIS but line 2 hashes to c7cd… — LINE 2 IS WHAT CHANGED" over a line 2 that
        is byte-for-byte what the old file held. The VERDICT was right; the sentence sent the
        reader to an untouched line in an untouched file, which is the cost R-10 exists to
        buy off, at the one break shape E-07 did not enumerate.

        THE "no chain claim" ARM IS GONE, and its absence is the shape of R-32. Since rule 7
        answers `UNCLAIMED`, a BROKEN edge can only come from rules 0, 2 and 11 — every one of
        which has a claim — so the arm was not merely wrong, it was unreachable. Deleted
        rather than left behind a guard: the branch-coverage floor would have failed on it,
        and a sentence nothing can print is a sentence nobody maintains.
        """
        if self.status != BROKEN:
            return f"line {self.line}: {self.status}"
        # Never None on this path, per the paragraph above; `or ""` satisfies the type checker
        # without an `assert`, which under the 100 % branch floor would be a branch that can
        # never take its other arm.
        claimed = self.claimed or ""
        if claimed == GENESIS:
            # G-09, rule 0. TWO CLAUSES HAD TO GO AND THEY WERE WRONG DIFFERENTLY. "LINE N-1
            # IS WHAT CHANGED" was flatly false. "claims its predecessor was GENESIS" was
            # literally TRUE — its fault is its FORM, setting a sentinel opposite a 64-hex
            # digest as though the two were comparable quantities. Nothing hashes to the
            # literal string, so there is no comparison to make and none is offered.
            #
            # AND THE CAUSE STAYS OPEN. Two histories joined and a line inserted by hand that
            # was never chained here leave the same bytes, and this file cannot tell them
            # apart. Naming one would be the same defect one layer along.
            return (
                f"line {self.line} declares itself the first line of a chain — its "
                f"`{FIELD}` is the {GENESIS} sentinel, not a digest — but {self.line - 1} "
                f"line(s) precede it. Nothing hashes to the literal string {GENESIS}, so "
                f"this is not a mismatch to investigate at line {self.line - 1}: it is two "
                f"histories joined, or a line inserted that was never chained here."
            )
        if self.line == 1:
            # G-22. THE WHOLE DIGEST, because R-10's both-digests clause held for rule 11 and
            # not for this one. Sixteen hex characters and an ellipsis is not a value anyone
            # can paste into `sha256sum`, and R-12 is the reason a digest is printed at all.
            # There is no computed digest here — the predecessor this line claims is not in
            # the file — so the one digest that exists is the one that has to be whole.
            #
            # THE DEFECT IS THE INCONSISTENCY, not a reader who cannot recover: the value is
            # the `prev` field of line 1 sitting in front of them, and `--format json` emits
            # it whole either way. Two sentences from one `detail`, one obeying R-10 and one
            # not, is how a reader learns to distrust both.
            return (
                f"line 1 claims a predecessor ({claimed}) but is the first line of "
                f"the file — one or more lines have been removed from the front"
            )
        return (
            f"line {self.line} claims its predecessor was {claimed} "
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
    #: G-03. Lines the parser refused that still contain a record boundary — two records whose
    #: terminator was destroyed. NOT AN EDGE: the edge model judges the link between two lines,
    #: and this is a fact about the bytes of one line, which is precisely why the gate could
    #: not see it. Decisive, and never an accusation — see `_BOUNDARY`.
    merged: tuple[int, ...] = ()
    #: G-17/G-08. Lines that carry no readable runprov record — the parser refused them, or
    #: they are valid JSON that is not an object (R-24). NOT AN EDGE either, and for the same
    #: reason `merged` is not: this is a fact about one line's own bytes, and the edge model
    #: judges the link between two. The newest line is the predecessor of no edge, so when
    #: R-20 recovered its claim from its raw bytes the file read INTACT/exit 0 while `log`,
    #: `show` and `report` could not read that record at all, and nothing anywhere said so.
    #:
    #: DISCLOSURE ONLY, and deliberately not folded into `status` — unlike `merged`. R-20's
    #: recovery genuinely is more evidence, so a torn tail whose claim survived and one torn
    #: one byte further ARE different situations and keep their different verdicts; the
    #: defect was that the difference was illegible, not that it was wrong. Making this
    #: decisive was measured and refused: it moves the ordinary crash off exit 0, which is
    #: the one thing R-11 and this module's own test ("a crash must not cost a project its
    #: gate, for ever") forbid.
    unreadable: tuple[int, ...] = ()

    @property
    def status(self) -> str:
        """R-26. The file's verdict is its worst edge, and nothing else.

        The whole rule, in three lines. Every earlier version of this decided the verdict from
        a hand-assembled set of conditions — `broken`, `translated`, `chained_from`,
        `unattested`, a stale-writer check — and every one of them missed a state. A fold over
        an enumerated status cannot.

        `UNCLAIMED` joined the second line under R-32. It is decisive for the same reason
        `GAP` is — nothing about the file has been checked at that edge — and non-accusing
        for the reason `GAP` is not: the cause may be innocent and there is no way to tell.

        `unreadable` IS DELIBERATELY ABSENT FROM THIS FOLD (G-17/G-08). It is disclosure and
        nothing else, and the field's own comment records why making it decisive was refused.
        """
        kinds = {link.status for link in self.edges}
        if BROKEN in kinds:
            return BROKEN
        if UNCHECKABLE in kinds or GAP in kinds or UNCLAIMED in kinds:
            return CANNOT_CHECK
        if self.merged:
            # G-03, and it is folded in HERE rather than expressed as an edge status because a
            # destroyed terminator is a fact about one line's bytes, not about the link between
            # two — the edges on either side of a merged line are untouched and both still
            # HOLD, which is exactly how INTACT/exit 0 was reached over it.
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
    every history. Reading the line alone makes every start line `UNSTATED`, which sends it to
    rule 7 — so one ordinary run by a colleague on a released wheel reported BROKEN. Measured
    against the real bytes in `tests/corpus/0.5.0`.

    Rule 7 no longer accuses (R-32), so the cost of failing to resolve a writer here is now a
    `CANNOT_CHECK` rather than a false accusation. Resolving it is still the point: without
    this, a completed 0.5.0 run reports `GAP` and names the machine to upgrade, which is
    actionable, and an unresolved one reports `UNCLAIMED`, which is not.
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
    merged: list[int] = []
    for index, line in enumerate(lines, start=1):
        try:
            parsed = json.loads(line)
        except ValueError:
            # G-03. The parser has refused it, so R-20's question is asked of it below; this
            # one is asked here, of the same lines and nowhere else. See `_BOUNDARY`.
            if _BOUNDARY.search(line):
                merged.append(index)
            continue
        if isinstance(parsed, dict):  # R-24: valid JSON that is not an object says nothing
            records[index] = parsed
    by_run = _writers(records)
    # G-17/G-08. READ OFF `records`, which the loop above has just built, so it cannot drift
    # from what the walk treats as readable: a line is unreadable here exactly when
    # `_writer_of` will call it UNREADABLE below. No new input, no second parse.
    unreadable = tuple(i for i in range(1, len(lines) + 1) if i not in records)

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
        elif status == UNCHECKABLE and claim == "DIGEST":
            # G-13. RULE 9, MARKED AT CONSTRUCTION, for the reason `translated` is: the walk
            # knows which rule answered and the renderer cannot recover it from the status
            # alone. Rules 3, 6 and 9 all answer UNCHECKABLE and they are different findings
            # — 3 and 6 are lines that said nothing readable, 9 is a line that said something
            # EXPLICIT about its predecessor and disagrees with it. One sentence served all
            # three and was true of one.
            #
            # `claim == "DIGEST"` IS EXACTLY RULE 9 among the three, and it needs no further
            # test: rules 3 and 6 are both inside `claim == "NONE"`, and a GENESIS claim is
            # intercepted by rule 0 before rule 9 can see it. So the claimed digest printed
            # below is always a well-formed 64-hex value.
            named = "unvouched"
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
    return Report(len(lines), edges, chained_from, sum(translated), tuple(merged), unreadable)


def _findings(report: Report, path: pathlib.Path) -> list[str]:
    """Every sentence the edges themselves earn, in R-25's own order.

    LIFTED OUT OF `render` FOR G-16. It used to sit inline under the "attested of" header,
    which is the whole of why the `chained_from is None` early return could print none of it
    and printed a guess instead. Both callers now render the same findings from the same
    code, so a file cannot be described two ways depending on which branch reached it.
    """
    out: list[str] = []
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
    for link in report.of(UNCLAIMED):
        # R-32. NAME THE CAUSE IT CANNOT DISTINGUISH rather than pick one. There are two
        # innocent explanations and one guilty, the file cannot tell them apart, and the
        # sentence says exactly that. It must never say "upgrade that machine": that is
        # rule 5's advice, it names a machine this line does not name, and for a run still
        # in flight there is nothing to upgrade.
        out.append(
            f"    UNCLAIMED  line {link.line} carries no chain claim. A run that could not "
            f"take the file lock writes none (it prints a NOTE when that happens), and a run "
            f"still in flight has not written its completion record yet — but so would a line "
            f"inserted by hand. This is not evidence of an edit."
        )
    for number in report.merged:
        # G-03. The verdict it forces would otherwise appear on the page with no cause named,
        # and R-9 requires the report to state what it checked. It says what is true of the
        # bytes and stops: the records that were joined cannot be read back, so nothing here
        # can be checked either way, and R-32's rule is that this tool does not accuse unless
        # it is certain.
        out.append(
            f"    COULD NOT CHECK  line {number} is not readable, and a record boundary sits "
            f"inside it — two records whose separating terminator is gone. Neither can be read "
            f"back, so neither can be checked. A truncating crash cannot produce this, but a "
            f"lost or zero-filled block can. This is not evidence of an edit."
        )
    for link in report.of(UNCHECKABLE):
        # G-13. THREE RULES, THREE SENTENCES. What stood here was one sentence — "nothing
        # readable vouches for line N-1" — printed for all of them. It is true at rule 6,
        # FALSE at rule 9, where line N is readable and vouches for N-1 explicitly and
        # disagrees, and NONSENSE at rule 3, where it named "line 0". And it printed no
        # digest at all, so R-12's by-hand check was impossible at the one edge where the
        # tool hands the reader the decision it cannot make.
        #
        # "CARRIES NO READABLE RUNPROV RECORD", NEVER "COULD NOT BE READ". Rules 3 and 6 are
        # reachable by a line `json.loads` parses perfectly: `[1, 2, 3]` is valid JSON that
        # is not an object, which R-24 treats as saying nothing. A sentence that says the
        # parser refused it is false over that file, which is a new wrong sentence in place
        # of the old one — the way this feature has already been "fixed" twice.
        if link.wrote == "translated":
            continue  # already summarised above
        if link.wrote == "unvouched":
            # Rule 9. BOTH DIGESTS (R-10, R-12): what line N says its predecessor was, and
            # what the bytes now sitting there actually hash to. Those are the two values a
            # reader has to compare, and they were the two never printed.
            out.append(
                f"    COULD NOT CHECK  line {link.line} claims its predecessor hashed to "
                f"{link.claimed}, but line {link.line - 1} carries no readable runprov "
                f"record and the bytes now at line {link.line - 1} hash to {link.computed}. "
                f"A tear that happened after line {link.line} was written and an edit to "
                f"line {link.line - 1} cannot be told apart from this file alone."
            )
        elif link.line == 1:
            # Rule 3: the first line is torn. There is no line 0 to vouch for, and the
            # first line attests nothing even when it CAN be read — that is what
            # HOLDS_TRIVIAL says — so losing it puts no other line's bytes in doubt.
            out.append(
                "    COULD NOT CHECK  line 1 carries no readable runprov record, so what it "
                "claimed is unknown. It is the first line, so it vouched for nothing in any "
                "case."
            )
        else:
            # Rule 6: line N said nothing readable, so the only statement anything made
            # about line N-1 is gone. R-11's second half — it must not ABSOLVE either.
            out.append(
                f"    COULD NOT CHECK  line {link.line} carries no readable runprov record, "
                f"so it made no claim about line {link.line - 1}; nothing else can vouch for "
                f"line {link.line - 1}'s bytes."
            )
    # G-17/G-08. THE LINES WHOSE OWN BYTES ARE UNREADABLE AND WHICH NOTHING ABOVE HAS NAMED.
    # Every sentence so far is about an EDGE, and an edge is a statement about the link between
    # two lines; a line the parser refused is a fact about one line, and the newest line is the
    # predecessor of no edge at all. So when R-20 recovered its claim from its raw bytes the
    # page read INTACT with no mention of it, while `log`, `show` and `report` could not read
    # the record. The same silence covers any unreadable line whose edge came out HOLDS,
    # HOLDS_TRIVIAL, BROKEN or UNCHAINED rather than UNCHECKABLE.
    #
    # THE SENTENCES THAT DO NAME IT ARE SUBTRACTED rather than duplicated, because a page that
    # says the same thing twice in two wordings is the confusion this row exists to remove:
    # rules 3 and 6 name the line of their own edge, rule 9 the line BENEATH its edge,
    # and `merged` has already said something stronger and more specific.
    named = set(report.merged)
    for link in report.of(UNCHECKABLE):
        named.add(link.line - 1 if link.wrote == "unvouched" else link.line)
    for number in report.unreadable:
        if number not in named:
            # "CARRIES NO READABLE RUNPROV RECORD", never "could not be read" — wave 4's
            # wording trap. `[1, 2, 3]` is valid JSON that is not an object, which R-24 treats
            # as saying nothing, so it arrives here with the parser never having complained.
            out.append(
                f"    UNREADABLE  line {number} carries no readable runprov record, so "
                f"`log`, `show` and `report` cannot read it back. DISCLOSURE, NOT A FINDING: "
                f"it changes no verdict, and every edge this line takes part in was judged on "
                f"the evidence that survived."
            )
    for link in report.of(BROKEN):
        out.append(f"    BROKEN  {link.detail}")

    return out


def render(report: Report, path: pathlib.Path) -> list[str]:
    """The report. R-9: states what it checked, not only what it found."""
    out = [f"# chain — {path}"]
    if report.lines == 0:
        return [*out, "  CANNOT CHECK: no history to read."]
    if report.chained_from is None:
        # G-16. THE EARLY RETURN PRINTED THE SENTENCE RULE 3 EXISTS TO PREVENT. It decided
        # from `chained_from` alone and never looked at the edges, so over a torn first line
        # in front of a pre-chain history it said "written before the chain existed" — F-06's
        # wrong message, in the renderer, after the table had been amended to stop the walk
        # saying it. The `--format json` rendering of the same `Report` carried the
        # UNCHECKABLE edge, so one report was described two contradictory ways.
        #
        # DELETING THE RETURN IS WORSE, and that was measured rather than argued: the general
        # header below then prints "chained from line None". So it is KEPT for the file it is
        # true of — every edge UNCHAINED, nothing unreadable, nothing merged — and every
        # other file gets its findings under a header that claims nothing about the cause.
        head = f"  CANNOT CHECK: {report.lines} line(s), none of them chained."
        if all(link.status == UNCHAINED for link in report.edges):
            return [
                *out,
                f"{head} This history was written before the chain existed; the next run "
                f"to append will anchor it.",
            ]
        # R-7's repair closes a fragment with a newline rather than discarding it, so the
        # next record lands BELOW the last line this file has and carries its digest. The
        # line number is named because "will anchor it" is the clause a reader acts on.
        return [
            *out,
            head,
            *_findings(report, path),
            f"    The next run to append will anchor the file from line {report.lines + 1}.",
        ]

    out.append(
        f"  {report.status}: {report.attested} line(s) attested of {report.lines}, "
        f"chained from line {report.chained_from}"
    )
    out.extend(_findings(report, path))

    # R-23, always: the newest line is attested by nothing, because a line cannot contain its
    # own digest and nothing follows it yet.
    #
    # G-17. THE SECOND CLAUSE WAS PRINTED OVER FILES IT WAS FALSE OF. "Truncation of the tail
    # cannot be seen from this file alone" is R-23's true statement about whole lines being
    # REMOVED — nothing in a file says how long it used to be. It is not true of a tail that is
    # torn part-way through: there `json.loads` refused the line, so the damage is visible in
    # this file and the report was asserting the opposite of what it had just measured. Both
    # halves are now said separately, and only the half that holds is claimed.
    if report.lines in report.unreadable:
        out.append(
            f"    line {report.lines} is the newest and nothing attests it yet; a later run "
            f"will. It is named above as carrying no readable runprov record, and THAT much "
            f"is visible from this file; what is not is whether whole lines were removed "
            f"after it."
        )
    else:
        out.append(
            f"    line {report.lines} is the newest and nothing attests it yet; a later run "
            f"will. Truncation of the tail cannot be seen from this file alone."
        )
    if report.of(BROKEN):
        out.append(
            "  A break means the history was edited after it was written, OR that a line was "
            "deleted or reordered. It does not say which, and it cannot say who."
        )
    return out
