# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""The survivor registry: accepted mutation survivors, and the rule that identifies one.

A mutation campaign produces survivors faster than anyone can pay them, and most of what it
produces is not debt at all -- a mutant that does not parse, an annotation never evaluated under
PEP 563, a mutation whose effect is provably unobservable. Triaging those once and then losing
the verdict is how the same list gets re-read every few months, so the verdicts are **declared,
with their reasons, in a file**.

A declaration like that is an exemption, and an exemption list is normally a one-way ratchet:
entries go in when something cannot pass and stay long after it can, because nobody re-runs the
check on the exempt subject. **So this one retires itself from both ends**, which is what makes
having it safe:

* **the subject is GONE** -- the line this entry is about no longer exists in the source, so the
  entry covers nothing and must be deleted. Cheap, pure, and gated (see below).
* **the debt was PAID** -- the mutant is killable now, so the entry is a lie. Needs a mutation
  run, so it CANNOT be gated; `ci.py survivors` is that end.

THE TWO ENDS HAVE DIFFERENT COSTS, AND THAT IS THE WHOLE DESIGN
---------------------------------------------------------------
The structural question for anything that wants to gate is *can a test recompute this quantity,
from scratch, while the suite is running?* For "is it killable now" the answer is no: it needs
mutmut, a copied tree and minutes per mutant. For "does the subject still exist" the answer is
**yes, in milliseconds, from the source alone** -- and that was measured rather than assumed, see
`occurrence_of` below. So the two ends live in different tiers instead of one end dragging the
other out of the gate, and the cheap end runs on every commit.

THE KEY, AND WHY THE LINE NUMBER IS NOT IN IT
---------------------------------------------
    (file, line text, which occurrence of that text, mutation index)

A line number is the one property of a mutant that moves for reasons having nothing to do with
the mutant. Measured: the single permanently-ungeneratable row in this project's campaign sat at
line 942 in the campaign cache and 976 in the re-check, with the mutant unchanged -- so a
registry keyed on the number would have reported the subject as gone and retired a true entry.
The number is still useful to a human and is reported; it is never matched on.

`occurrence` is what keeps two identical lines apart. `        )` appears **12 times** in
`run.py`, and without an ordinal an entry about one of them is an entry about all twelve.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import inspect
import json
import pathlib
import sys
import typing

#: (file, line text, which occurrence of that text, mutation index).
Key = tuple[str, str, int, int]

#: The registry, beside the claims baseline and for the same reason: a declaration that something
#: is accepted belongs in the repository it is accepted in, under review, not in a tool's memory.
DEFAULT_REGISTRY = (
    pathlib.Path(__file__).resolve().parent.parent / "docs" / "survivor-registry.jsonl"
)

#: THE CLASSES, DECLARED. An entry whose class is not one of these is refused rather than counted:
#: a free-text class is how "accepted" quietly comes to mean "not looked at".
#:
#: Each value says what makes the class PERMANENT, because that is the claim the entry is making.
#: Two of them are properties of the mutant (it cannot run, or it cannot be observed) and one is a
#: property of the tool (it cannot be built at all). None of them is "we decided not to bother" --
#: that is not an accepted survivor, that is an unpaid debt, and it belongs in a count and not in
#: here. **If a reason for an entry would be "low priority", the entry does not belong.**
CLASSES: dict[str, str] = {
    "UNCOMPILABLE": "the mutant is not valid Python, so no test can execute it and no suite can "
    "ever kill it. mutmut reports it as SURVIVED because it classifies by test "
    "outcome and a mutant that cannot be imported fails nothing.",
    "ANNOTATION": "the mutation is inside an annotation and this module carries "
    "`from __future__ import annotations`, so the annotation is a string that is "
    "never evaluated. The mutant and the original are the same program.",
    "EQUIVALENT": "the mutated expression is provably unobservable -- a dead default, a "
    "short-circuit that cannot be reached, a branch whose condition is constant. "
    "The reason must say WHY it is unreachable and name the measurement.",
    "UNGENERATABLE": "mutmut does not produce this mutant at all, so the row exists in its cache "
    "with no verdict and no run can give it one. The reason must name the "
    "measurement against a pristine source.",
}


@dataclasses.dataclass(frozen=True)
class Entry:
    """One accepted survivor, with the reason it is accepted."""

    file: str
    text: str
    occurrence: int
    index: int
    verdict: str
    reason: str

    @property
    def key(self) -> Key:
        return (self.file, self.text, self.occurrence, self.index)

    def describe(self) -> str:
        return (
            f"{self.file} occurrence {self.occurrence} of {self.text.strip()[:60]!r} [{self.index}]"
        )


def occurrence_of(lines: typing.Sequence[str]) -> list[int]:
    """For each line, its ordinal among the lines with identical text. Parallel to `lines`.

    THE ORDINAL IS OVER EVERY LINE OF THE FILE, AND IT IS **NOT** THE ORDINAL A mutmut CACHE
    USES. That distinction is the whole reason this function has a docstring, because getting it
    wrong produces an entry that points confidently at a different mutant.

    mutmut's cache has a `Line` table holding every line of the file -- measured 2026-10-22 on
    this project's re-check cache, `run.py` 3 985 rows against 3 985 source lines and
    `hashing.py` 851 against 851, exact for all 4 064 distinct texts. **That fact is true and it
    is irrelevant**, which is what made it dangerous: the audit tool that pairs two caches builds
    its ordinal from a join through the `Mutant` table, so its population is the lines that BEAR
    A MUTANT -- and only **906 of run.py's 3 985 lines do.**

    Worked, on the one line this mattered for. The text `        )` occurs **12 times** in
    `run.py`; **5** of those occurrences bear a mutant. So:

        source line 977  ->  ordinal 2 here   (third `        )` in the file)
        source line 977  ->  ordinal 0 there  (first MUTANT-BEARING `        )`)

    An entry copied from the audit tool's `(file, text, 0, 1)` into this registry unchanged would
    name source line 360 -- a line with no mutants at all -- and the check below would read the
    subject as present and the entry as healthy. **A registry entry that silently describes the
    wrong line is worse than a missing one**, so the two ordinals are deliberately NOT shared:
    this one is a function of the source and can therefore be verified without a cache, which is
    what buys the cheap end its place in the gate. The conversion is the generator's job, and it
    is the generator that holds both the cache line number and the source.
    """
    seen: collections.Counter[str] = collections.Counter()
    out: list[int] = []
    for line in lines:
        out.append(seen[line])
        seen[line] += 1
    return out


def locate(lines: typing.Sequence[str], text: str, occurrence: int) -> int | None:
    """The 1-indexed line number of that occurrence of that text, or None if it is not there.

    None is the SUBJECT-IS-GONE verdict and it is the whole point of the cheap end. It covers
    both "the text was edited or deleted" and "the text is still here but there are fewer copies
    of it than there were", which are the same statement as far as an entry about the third copy
    is concerned.
    """
    for number, (line, nth) in enumerate(zip(lines, occurrence_of(lines), strict=True), start=1):
        if line == text and nth == occurrence:
            return number
    return None


def rule_digest() -> str:
    """A digest of the MATCHING RULE's own source, so a change to it is attributable.

    The registry file records this. A ratchet or a declaration whose rule moves silently is a lie
    with a number on it: this project's claims baseline already carries the lesson, where a
    sibling's count fell 696 -> 335 and about half of that was the detection rule tightening
    rather than work being done.

    Here the stakes are the reverse of a count: if the key construction changes, every entry's
    identity changes, and entries that silently stop matching would read as **subject gone** and
    be retired as if the code had moved on. So the digest covers exactly the two functions that
    construct and resolve a key, and `ci.py survivors` refuses to retire anything when it has
    moved -- it asks for a re-verification instead.
    """
    source = inspect.getsource(occurrence_of) + inspect.getsource(locate)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse(text: str) -> tuple[list[Entry], str | None]:
    """The registry's entries and the rule digest it was written against.

    ONE JSON OBJECT PER LINE, with `#` comments. JSON because a line of Python source is the key
    and it can contain anything -- tabs, quotes, trailing whitespace that MATTERS because the
    cache stores the line verbatim. A delimited format would need an escaping convention, and an
    escaping convention is a second place for the identity of an entry to be decided.
    """
    entries: list[Entry] = []
    digest: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            marker = "# rule-digest:"
            if line.startswith(marker):
                digest = line[len(marker) :].strip()
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{DEFAULT_REGISTRY.name} line {number} is not JSON: {exc}") from exc
        missing = {"file", "text", "occurrence", "index", "class", "reason"} - set(row)
        if missing:
            raise ValueError(f"{DEFAULT_REGISTRY.name} line {number} is missing {sorted(missing)}")
        extra = set(row) - {"file", "text", "occurrence", "index", "class", "reason", "line"}
        if extra:
            raise ValueError(
                f"{DEFAULT_REGISTRY.name} line {number} has unknown field(s) {sorted(extra)}"
            )
        entries.append(
            Entry(
                file=row["file"],
                text=row["text"],
                occurrence=int(row["occurrence"]),
                index=int(row["index"]),
                verdict=row["class"],
                reason=row["reason"],
            )
        )
    return entries, digest


def load(path: pathlib.Path | None = None) -> tuple[list[Entry], str | None]:
    """The registry, or an empty one if the file is absent.

    AN ABSENT REGISTRY IS EMPTY AND NOT AN ERROR. The alternative -- refusing when the file is
    missing -- makes the gate's own precondition a thing that can fail for an unrelated reason,
    and an empty registry is a perfectly good state: it means nothing has been accepted.
    """
    path = path or DEFAULT_REGISTRY
    if not path.is_file():
        return [], None
    return parse(path.read_text(encoding="utf-8"))


def check(root: pathlib.Path, entries: typing.Iterable[Entry]) -> list[str]:
    """Every objection to the registry that the SOURCE alone can raise. Empty means clean.

    This is the gated end, and it is deliberately total over the entries rather than a sample or
    a floor: a floor over a declaration list is satisfied by the declarations that happen to
    still be valid, which is the one population that needs no checking.
    """
    objections: list[str] = []
    by_file: dict[str, list[str]] = {}
    for entry in entries:
        if entry.verdict not in CLASSES:
            objections.append(
                f"{entry.describe()}: class {entry.verdict!r} is not declared; "
                f"choose from {sorted(CLASSES)}"
            )
        if not entry.reason.strip():
            objections.append(
                f"{entry.describe()}: no reason recorded, so nothing can re-examine it"
            )
        path = root / entry.file
        if entry.file not in by_file:
            if not path.is_file():
                objections.append(
                    f"{entry.describe()}: {entry.file} does not exist, so this entry is about "
                    f"a file that is gone -- delete it"
                )
                by_file[entry.file] = []
            else:
                by_file[entry.file] = path.read_text(encoding="utf-8").splitlines()
        lines = by_file[entry.file]
        if lines and locate(lines, entry.text, entry.occurrence) is None:
            #: TWO DIFFERENT FAILURES, AND THEY NEED DIFFERENT ANSWERS FROM A HUMAN. The text
            #: being gone entirely means the code moved on and the entry is spent. The text
            #: still being present with fewer copies means the entry's ORDINAL has shifted --
            #: identical lines were removed or reordered above it -- and the subject may well
            #: still be there under a different ordinal. Reporting both as "gone" would invite
            #: deleting a live exemption, so the count is printed and the two are named apart.
            present = sum(1 for line in lines if line == entry.text)
            if present == 0:
                objections.append(
                    f"{entry.describe()}: THE SUBJECT IS GONE -- this line text is not in "
                    f"{entry.file} at all any more, so the entry covers nothing. Delete it; "
                    f"it is not evidence that the debt was paid."
                )
            else:
                objections.append(
                    f"{entry.describe()}: THE ORDINAL NO LONGER RESOLVES -- the text is still "
                    f"present {present} time(s) but occurrence {entry.occurrence} is not among "
                    f"them, so identical lines above it were removed or reordered. DO NOT just "
                    f"delete this: re-measure which occurrence the mutant is on now, because "
                    f"the subject is probably still here under another ordinal."
                )
    return objections


#: EVERY CONTROL, IN THE FILE, because a control a person remembers to run is a control that is
#: optional. This project's own record holds twenty-two proposed remedies that would have shipped
#: broken, not one caught by a gate and every one caught by a control somebody remembered.
#:
#: Each case asserts WHICH objection fired, never merely that one did: two of a sibling
#: selfcheck's three "void" cases once passed for the wrong reason while printing `ok`. And there
#: is a case per verdict INCLUDING the clean one, because a verdict never produced is a verdict
#: never measured.
#: A line text that genuinely occurs, SEVERAL TIMES, in `runprov/run.py`. It has to be real, or
#: the clean control passes for the wrong reason; it has to repeat, or the ordinal control is
#: indistinguishable from the subject-gone one. `selfcheck` asserts both properties before using
#: it -- the first draft of these controls used `    pass`, which is not in the file at all, and
#: the VOID branch is what said so rather than three controls quietly passing.
CONTROL_TEXT = "        )"

_CONTROLS: list[tuple[str, dict[str, object], str | None]] = [
    (
        "a clean entry raises nothing",
        {
            "file": "runprov/run.py",
            "text": CONTROL_TEXT,
            "occurrence": 0,
            "index": 0,
            "class": "EQUIVALENT",
            "reason": "a control",
        },
        None,
    ),
    (
        "a text that is not in the file at all is SUBJECT GONE",
        {
            "file": "runprov/run.py",
            "text": "    raise NotImplementedError('nowhere')",
            "occurrence": 0,
            "index": 0,
            "class": "EQUIVALENT",
            "reason": "a control",
        },
        "THE SUBJECT IS GONE",
    ),
    (
        "a text that is present but with too few copies is an ORDINAL failure, not a gone subject",
        {
            "file": "runprov/run.py",
            "text": CONTROL_TEXT,
            "occurrence": 10**6,
            "index": 0,
            "class": "EQUIVALENT",
            "reason": "a control",
        },
        "THE ORDINAL NO LONGER RESOLVES",
    ),
    (
        "an undeclared class is refused",
        {
            "file": "runprov/run.py",
            "text": CONTROL_TEXT,
            "occurrence": 0,
            "index": 0,
            "class": "LOW_PRIORITY",
            "reason": "a control",
        },
        "is not declared",
    ),
    (
        "an entry with no reason is refused",
        {
            "file": "runprov/run.py",
            "text": CONTROL_TEXT,
            "occurrence": 0,
            "index": 0,
            "class": "EQUIVALENT",
            "reason": "   ",
        },
        "no reason recorded",
    ),
    (
        "an entry naming a file that is gone is refused",
        {
            "file": "runprov/nope.py",
            "text": CONTROL_TEXT,
            "occurrence": 0,
            "index": 0,
            "class": "EQUIVALENT",
            "reason": "a control",
        },
        "does not exist",
    ),
]


def selfcheck(root: pathlib.Path) -> int:
    """Prove each branch of `check` fires, and fires for the stated reason."""
    failures = 0
    #: THE CLEAN CASE'S PREMISE IS ASSERTED FIRST. Every control below is keyed to the text
    #: `    pass` existing in `run.py`; if it ever does not, the clean case passes for the wrong
    #: reason and three others become unfalsifiable.
    lines = (root / "runprov" / "run.py").read_text(encoding="utf-8").splitlines()
    present = sum(1 for line in lines if line == CONTROL_TEXT)
    if present < 2:
        print(
            f"VOID: the controls need {CONTROL_TEXT!r} to occur at least twice in "
            f"runprov/run.py and it occurs {present} time(s)",
            file=sys.stderr,
        )
        return 2

    for name, row, expect in _CONTROLS:
        entries, _ = parse(json.dumps(row))
        objections = check(root, entries)
        if expect is None:
            ok = not objections
            got = "; ".join(objections) or "<none>"
        else:
            ok = any(expect in objection for objection in objections)
            got = "; ".join(objections) or "<none>"
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            failures += 1
            print(f"       expected {expect!r}, got {got[:160]}")
    print(
        f"selfcheck {'ok' if not failures else f'FAILED {failures} control(s)'} "
        f"-- {len(_CONTROLS)} control(s)"
    )
    return 0 if not failures else 1


def main(argv: typing.Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report the survivor registry's state.")
    ap.add_argument("--registry", type=pathlib.Path, default=None)
    ap.add_argument("--root", type=pathlib.Path, default=DEFAULT_REGISTRY.parent.parent)
    ap.add_argument(
        "--selfcheck", action="store_true", help="prove every branch of the check fires"
    )
    args = ap.parse_args(argv)

    if args.selfcheck:
        return selfcheck(args.root)

    entries, digest = load(args.registry)
    print(f"registry: {len(entries)} accepted survivor(s)")
    counts: collections.Counter[str] = collections.Counter(e.verdict for e in entries)
    for name in sorted(CLASSES):
        print(f"   {name:<14} {counts.get(name, 0)}")
    live = rule_digest()
    print(f"rule digest: {live[:16]}  recorded: {(digest or '<none>')[:16]}")
    if digest and digest != live:
        print(
            "REFUSE: the matching rule has changed since this registry was written. Every entry's "
            "identity is derived from that rule, so a mismatch cannot be read as 'the subject "
            "moved' -- re-verify the registry and update the digest.",
            file=sys.stderr,
        )
        return 3
    objections = check(args.root, entries)
    for objection in objections:
        print(f"  E  {objection}")
    print("survivors ok" if not objections else f"survivors: {len(objections)} objection(s)")
    return 1 if objections else 0


if __name__ == "__main__":
    raise SystemExit(main())
