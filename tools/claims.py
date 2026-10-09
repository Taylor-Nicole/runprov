# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Which numbers in README.md are bound to something that measures them, and which are not.

A NUMBER TYPED INTO A DOCUMENT WAS TRUE WHEN IT WAS TYPED. Nothing says which run it came from,
so when the code changes the number stays and the document becomes confident, plausible and
wrong. This repository has now had that twice in the same file: `ci.py`'s `_SCALE_FIGURES` exists
because the README's statement and branch counts drifted FIVE times, and on 2026-10-08 the "What
has actually been run" section still said the hosted matrix had **not run** -- naming the one
action that would close the gap -- while the action had been taken, the matrix ran on every push
and all eight jobs were green.

THE DENOMINATOR IS THE DELIVERABLE. Before this, the bound figures were `_SCALE_FIGURES`' two,
and nobody knew what they were two OF. Measured: README.md holds **605 numeric literals**, of
which about 334 sit in prose that could be making a claim. "Two gates" over an unknown is a
NUMERATOR, which is the shape `guard-shapes` calls a floor -- it says how much was read and
cannot say what was missed.

AND THE VERDICTS ARE ASYMMETRIC, which this prints rather than implies:

  * **UNACCOUNTED is strong.** A measurement-shaped number that resolves to nothing is either
    stale or was computed by hand and never recorded. Every real defect found here was of this
    kind, including both of the ones above.
  * **BOUND is strong in the other direction** -- it is compared against the live value, so it
    can fail -- but it covers only what the registry names.
  * **The classified kinds are WEAK.** `external`, `dated`, `example` and `reference` say *this
    number is not a live claim about this tree*, which is a judgement recorded here so a reader
    can disagree with it. They are not verification and are never counted as such.

A REPORT FIRST, A GATE ONLY WHERE IT CAN PASS. `ci.py claims` is not in the default gate. But a
BOUND claim that disagrees with the live value is a finding, not a backlog item, so it exits 1 --
the contract `tools/torture.py` uses: 0 nothing found, 1 findings, 2 the run is void. A binding
naming a constant that no longer exists is VOID, because at that point it has measured nothing.

WHY A REGISTRY HERE AND NOT A DERIVATION. The repository's rule is *delete the list, do not
extend it*, and it holds wherever the subject is enumerable by the language -- the import graph,
argparse's tables, the suffix maps. **A sentence's meaning is not.** Nothing in the text of
*"Steps are capped at 1 000 per run"* says it is about `Run.MAX_STEPS`; a human decided that, and
the registry is where that decision is written down so the next reader can check it. What the
registry must never do is hold the VALUE: it holds the pair, and the value is read live.

    python tools/claims.py            # the report
    python ci.py claims               # the same, through the gate runner

Related: `ci.py`'s `_SCALE_FIGURES` binds the coverage totals and IS gated, because coverage
writes them on every run; and `test_the_ci_sections_legs_are_the_legs_the_workflow_ACTUALLY_
DECLARES` binds the CI section's leg table to the workflow matrix.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from collections import abc

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
BASELINE = ROOT / "docs" / "claims-baseline.txt"

#: A MEASUREMENT-SHAPED LITERAL. Digits, with the thin spaces and commas this document uses as
#: group separators, optionally a decimal part or a per-cent sign.
#:
#: THE HYPHEN IN THE LOOKBEHIND IS NOT COSMETIC. Without it `UTF-8` is a measurement, and so are
#: `PEP-639`, `pre-PEP-639`, `kill -9` and `exit-2` -- seven lines sat in the denominator for
#: carrying a digit that is part of a NAME. A digit after a hyphen is a name's own character;
#: a digit after a space still counts, so *"a 3-taxon tree"* is measured and `UTF-8` is not.
NUMBER = re.compile(r"(?<![\w.\-])\d[\d  ,]*(?:\.\d+)?%?(?![\w])")

#: INLINE CODE, stripped to ask whether a line makes a numeric claim IN PROSE -- see `classify`.
_CODE_SPAN = re.compile(r"`[^`]*`")

#: (pattern over the README capturing ONE number, module, attribute path, divisor).
#:
#: THE DIVISOR IS HOW A UNIT IS DECLARED instead of being assumed: the README says *64 KiB* and
#: `verify.SCAN_BYTES` is 65536, so the pair is only honest if the conversion is written down.
#: A registry that compared 64 against 65536 would report a drift that is not one, and a reader
#: would learn to ignore the report -- which is the failure `ci.py`'s own docstring describes.
_BOUND: tuple[tuple[str, str, str, int], ...] = (
    (r"Steps are capped at ([\d  ]+?) per run", "runprov.run", "Run.MAX_STEPS", 1),
    (r"turns itself off after ([\d  ]+?) calls", "runprov.observe", "MAX_CALLS", 1),
    #: THE SAME CONSTANT, IN THE PAGE THAT SHIPS. `README-pypi.md` says it in its own words,
    #: and was bound by nothing: a change to `MAX_CALLS` would have redded the gate on `README.md`
    #: and shipped the wrong number to PyPI, where a file can never be replaced.
    (r"stops itself after ([\d\u2009 ]+?) calls", "runprov.observe", "MAX_CALLS", 1),
    (r"begin within the first (\d+) lines", "runprov.verify", "PIN_STARTS_WITHIN", 1),
    (r"a warning naming up to (\d+) files", "runprov.run", "DIRTY_FILES_SHOWN", 1),
    (r"Capped at (\d+) lines a run", "runprov._report", "PROGRESS_MAX_LINES", 1),
    (r"looked for in the \*\*first (\d+) KiB\*\*", "runprov.verify", "SCAN_BYTES", 1024),
    #: THE SUFFIX MAPS, bound by their LENGTH because that is what the sentence claims. Added
    #: 2026-10-09 after this tool reported the heading *"refuses 15 formats and gives 23 a
    #: sidecar"* as bound to nothing: 15 counted the binary suffixes inside `PIN_UNSAFE` and the
    #: refusal had moved to `PIN_BINARY`, which holds 40. A number that was right about a design
    #: the code had left -- the second shipped false claim this file has found in the README.
    (r"refuses (\d+) binary suffixes", "runprov.run", "PIN_BINARY", 1),
    (r"\*\*(\d+) suffixes RAISE\*\*", "runprov.run", "PIN_BINARY", 1),
    (r"\*\*(\d+) suffixes take the pin IN-BAND\*\*", "runprov.run", "PIN_INLINE", 1),
    (r"records a reason for the \*\*(\d+)\*\* that were", "runprov.run", "PIN_UNSAFE", 1),
    #: FOUND BY READING THE CODE, NOT BY SEARCHING FOR THE VALUE. A sweep of all 142 module
    #: constants for their values in unaccounted lines returned 113 hits, essentially all
    #: coincidence; a scoped version requiring the constant's NAME to share a word with the
    #: sentence found one and MISSED this -- `UNREADABLE_SHOWN` names neither "bounded" nor
    #: "characters". No heuristic finds these. A person reads the sentence and then the code.
    (r"bounded at (\d+) characters", "runprov.__main__", "UNREADABLE_SHOWN", 1),
    (r"fewer suffixes than the (\d+) above", "runprov.run", "PIN_BINARY", 1),
)

#: EACH KIND CARRIES ITS REASON, because an unexplained exemption is how a gap hides. These say
#: *not a live claim about this tree*, and each is a judgement a reader may reject.
_KINDS: tuple[tuple[str, str, str], ...] = (
    (
        #: `#` WAS HERE AND IT EXEMPTED EVERY MARKDOWN HEADING, which is how this tool missed a
        #: false claim it was built to catch: *"refuses 15 formats and gives 23 a sidecar"* sat in
        #: an `##` heading and was reported as an ILLUSTRATION. A heading is the most-read line of
        #: a section and makes claims like any other. The alternative was meant for a shell
        #: comment in a transcript, and it was never needed: fenced blocks are skipped entirely
        #: and an indented transcript is caught by the four-space alternative. Measured before
        #: removing it -- exactly two lines matched `#` and nothing else, both of them headings.
        "example",
        r"^\s*(?:\$|>>>|\||    )",
        "inside a transcript, a table rule or an indented block -- an illustration, not a claim",
    ),
    (
        "ordinal",
        #: `\bstep \d+\b` ADDED 2026-10-09, when the surface grew to every shipped page.
        #: `GETTING-STARTED.md` is built out of `## Step 1` .. `## Step 8` and refers back to them
        #: in prose, and `^#{1,6} \d` needs the digit immediately after the hashes -- so eleven
        #: positions read as measurements. Narrow on purpose: it requires `step` then a space then
        #: digits, so *"Steps are capped at 1 000 per run"* and *"32 of 55 steps covered"* are
        #: untouched. A heading pattern like `^#{1,6} \w+ \d` would have exempted any heading
        #: ending in a number, which is where the real claims live.
        r"^\*\*\d+\.\s|^\d+\.\s|^#{1,6} \d|^\s*[-*] \*\*\d+\.|\bstep \d+\b",
        "a numbered heading or list item -- the number is the position, not a measurement",
    ),
    (
        "reference",
        r"ADR-\d|PEP ?\d|ISO ?\d|10\.5281|zenodo|doi|MSR \d|ISO-8601|SHA-?256|BSD|SPDX|clause \d",
        "an identifier: a decision record, a standard, a DOI, a licence clause",
    ),
    (
        "version",
        r"\b\d+\.\d+(\.\d+)?\b.*\b(Python|CPython|Poetry|pkginfo|Biopython|metadata|uv|conda)\b"
        r"|\b(Python|CPython|Poetry|pkginfo|Biopython|metadata|uv|conda)\b.*\b\d+\.\d+",
        "a version of this or another tool -- pinned by `pyproject.toml`, not measured here",
    ),
    (
        "dated",
        r"20\d\d-\d\d-\d\d|\bas of\b|\bMeasured (on|against) run\b"
        r"|run `?\d{8,}|\bat `[0-9a-f]{7,}`",
        "a DATED measurement that names when or where it was taken -- must NOT be re-derived",
    ),
    (
        "external",
        r"source project|project this came from|hcv-genotyping|Sumatra|the source project's|"
        r"another project|adopters|files import it|Seven pipelines",
        "measured on a tree that is not this one, so nothing here can resolve it",
    ),
    (
        #: WIDENED 2026-10-09 after the ratchet made the residue readable. `\bexit code\b` could
        #: not match *"Three exit codes"*, and the `[012] (intact|...)` list named four outcome
        #: words out of the dozen this document actually uses -- so eight lines that say nothing
        #: but *which exit status means what* were counted as claims nothing accounts for.
        #:
        #: WHAT IS DELIBERATELY NOT HERE: a bare `[012] in the`. It would have absorbed line 1762,
        #: whose `exits` sits on the PREVIOUS line -- a wrapped sentence, and the unit here is the
        #: line. One real line is left in the denominator rather than widened to a phrase that
        #: would exempt any `1 in the ...` anywhere in the document. An over-absorbing kind is
        #: worse than the gap it closes, because the gap is visible and the exemption is not.
        "exit-code",
        r"\bexit(s|ed)? [012]\b|\bexit codes?\b|\bexit status\b|`--exit-code`"
        r"|\b[012] (intact|broken|could not|clean|usable|OK)\b"
        r"|\b[012] (?:means|a rule|a name|on any|the walk|no history|otherwise|when)\b"
        #: NO `\b` BEFORE THE BACKTICK. A word boundary needs a word character on one side, and
        #: a backtick is not one -- so `\b`[012]`` could never match anything at all. It was
        #: written in the same commit that widened this kind and tested nothing: three lines
        #: stayed in the denominator behind an alternative that was incapable of firing.
        r"|`[012]` (?:against|family)",
        "a documented exit code -- a behavioural contract, held by the suite rather than a value",
    ),
    (
        #: PLACED LAST ON PURPOSE, so it can only take lines from `unaccounted` and never from a
        #: kind above it. A line naming both an exit status and a descriptor is an exit-code line.
        "file-descriptor",
        r"file descriptors?\b|\bfds? 1\b|descriptors 1 and 2",
        "a file descriptor NUMBER -- 0, 1 and 2 are POSIX identifiers, not measurements",
    ),
)


#: HOW A CORRECTION NOTE DECLARES ITSELF. A blockquote matching any of these is quoting figures
#: it has already withdrawn, and those figures MUST NOT resolve -- the project's practice is to
#: quote the superseded number rather than delete it, so a reader who cited it can find out.
#:
#: WITHOUT THIS KIND THE DENOMINATOR PUNISHES THE PRACTICE. Eleven lines were unaccounted purely
#: for being inside a correction note, seven of them written before this tool existed, and every
#: future correction would have added more -- so making a claim honest would have *raised* the
#: count of claims nothing accounts for. That is a ratchet pushing the wrong way.
#:
#: THE BLOCK MUST SAY SO ITSELF, which is why this is not "any blockquote": an undeclared `>`
#: block carrying a number stays UNACCOUNTED, because a blockquote is also how this document
#: writes an aside, and an aside can make a live claim.
_SUPERSEDED_DECLARES = re.compile(
    r"CORRECTED|superseded|An earlier version|used to say|"
    r"This (?:heading|section|paragraph|line|table) read",
    re.I,
)

#: Stateful rather than per-line, so it cannot be an entry in `_KINDS`; the reason lives here.
#: Stateful like `superseded`, so it cannot be a `_KINDS` entry; the reason lives here.
_CODE_LITERAL_REASON = (
    "every number on the line is inside inline `code` -- an argument, a literal or an example, "
    "so the line makes no numeric claim in prose"
)

_SUPERSEDED_REASON = (
    "quoted inside a correction note that withdrew it -- kept on purpose, must NOT resolve"
)


#: WHICH DOCUMENTS ARE NOT AUDITED, each with its reason, because an unexplained exclusion is how
#: a gap hides. Everything else matching `*.md` at the repository root IS audited -- derived from
#: the filesystem rather than listed, so a new document arrives covered.
#:
#: WHY THIS STOPPED BEING `README.md` ALONE. The claim surface covered one document and this
#: project ships seven. `README-pypi.md` is *frozen into the wheel at upload and is the only page
#: most people who find this project will read*, and it stated `observe.MAX_CALLS` in its own
#: words -- a constant bound in `README.md` and unbound there, so a change to it would have redded
#: the gate on one page and shipped the wrong number on the other, to a file PyPI can never
#: replace. The scope pattern again: a rule that was right, reaching one of the places it applies.
_NOT_AUDITED = {
    "CHANGELOG.md": (
        "a log of the past -- every figure in it is historical by construction, and its correction "
        "notes quote superseded ones on purpose"
    ),
    "CODE_OF_CONDUCT.md": "adopted text (the Contributor Covenant), not this project's claims",
}


def _shipped_markdown() -> list[str]:
    """Root-level markdown that SHIPS, which is not the same as what is on this disk.

    ASKS GIT FIRST, AND THAT IS A CORRECTION. A bare `ROOT.glob("*.md")` picked up `LICENSING.md`
    -- a file that is **not tracked and not in the sdist** -- so the claim surface would have
    audited a page no reader ever receives, and would adopt any stray `.md` left in the root. The
    subject is *what ships*.

    FALLS BACK TO THE GLOB WITHOUT APOLOGY, because the fallback is only reached in an unpacked
    sdist, where there is no `.git` AND the tree contains nothing but shipped files. Each method is
    correct exactly where the other is unavailable.
    """
    asked = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if asked.returncode == 0 and asked.stdout.strip():
        return [name for name in asked.stdout.split() if "/" not in name]
    return [path.name for path in ROOT.glob("*.md")]  # pragma: no cover - an unpacked sdist


def documents() -> dict[str, str]:
    """Every audited document, name -> text. Derived, so a new page is covered on arrival."""
    found = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in sorted(_shipped_markdown())
        if name not in _NOT_AUDITED and (ROOT / name).is_file()
    }
    #: NOT VACUOUS: a one-document survey is the state this layer was built to end, and an empty
    #: one would make every count below zero and every assertion true.
    if len(found) < 2:
        raise RuntimeError(f"only {sorted(found)} audited; the document derivation has stopped")
    return found


def survey() -> tuple[dict[str, list[str]], list[str]]:
    """Classify every audited document at once. Keys are `name:line`, so a row names its page.

    COMPOSED, NOT REWRITTEN. Each per-document function keeps its own contract and this walks the
    documents, which is why the detection rule and the registries did not have to change shape to
    cover seven pages instead of one.
    """
    buckets: dict[str, list[str]] = {}
    notes: list[str] = []
    for name, text in documents().items():
        credited, unattributed = bound_lines(text)
        notes += [f"{name}: {note}" for note in unattributed]
        known, _stale = historical_lines(text)
        #: `_SCALE_FIGURES` NAMES README FIGURES SPECIFICALLY, so it is asked of that page only.
        elsewhere = elsewhere_lines(text) if name == "README.md" else {}
        one = classify(text, credited, superseded_lines(text), elsewhere, known)
        for bucket, rows in one.items():
            buckets.setdefault(bucket, []).extend(f"{name}:{row}" for row in rows)
    return buckets, notes


def superseded_lines(text: str) -> set[int]:
    """Line numbers inside a blockquote that declares itself a correction.

    Scoped to the BLOCK: a run of `>` lines is one note, and one declaration anywhere in it
    covers the whole note, because the superseded figures are usually a line or two below the
    sentence that withdraws them.
    """
    out: set[int] = set()
    block: list[int] = []
    declared = False
    for n, line in enumerate([*text.split("\n"), ""], 1):
        if line.lstrip().startswith(">"):
            block.append(n)
            declared = declared or bool(_SUPERSEDED_DECLARES.search(line))
            continue
        if declared:
            out.update(block)
        block, declared = [], False
    return out


#: WHY A FIGURE CAN NEVER BE RE-MEASURED. Each category is a general reason; each entry names the
#: specific subject. Both are required, because a category alone is an excuse with a label on it.
_WHY_HISTORICAL = {
    "fixed": "it measured a defect since repaired, so the condition cannot be recreated",
    "other-tree": "it was measured on a file or repository that is not this one",
    "this-host": "it is a timing, memory or platform fact of one machine at one moment",
    "third-party": "it needs a library or tool this project deliberately does not install",
    "interpreter": "it is a property of one CPython version, and four are supported",
    "illustration": "it is a hypothetical figure chosen to make a point, and measures nothing",
    "ci-history": "it is a point-in-time query of the hosted CI, which moves on every push",
}

#: (pattern matching the sentence, category, what was measured and where).
#:
#: THIS IS A WORK LIST, NOT AN EXEMPTION LIST, and the distinction is in the arithmetic: these
#: lines are counted SEPARATELY and are never added to the accounted-for total. `unaccounted` is
#: the residue nobody has looked at; `historical` is the residue somebody examined and found
#: permanently unverifiable. Both are ratcheted. Moving a line from the first to the second is a
#: judgement recorded, not a check gained, and the report says so.
#:
#: WHAT IS DELIBERATELY ABSENT. A figure that COULD be checked does not belong here however
#: inconvenient it is to check. *"it reports an edit to line 3 as line 4 breaking"*,
#: *"`ambiguous` is 0 by construction"* and *"digests `True` as the integer 1"* are behavioural
#: claims about live code: they stay in `unaccounted` until somebody writes the test, which is the
#: whole point of keeping the two numbers apart.
_HISTORICAL: tuple[tuple[str, str, str], ...] = (
    # --- it measured a defect that has since been repaired
    (
        r"runs over identical inputs both report \d+ artifacts CHANGED",
        "fixed",
        "the uuid was in the pin then and is not now",
    ),
    (
        r"caught \d+ concurrent appends producing \d+ lines before",
        "fixed",
        "taken before the `msvcrt` branch existed",
    ),
    # --- another file or repository
    (
        r"It holds \*\*[\d,]+ entries\*\*",
        "other-tree",
        "`transformation_log.yml`, which the README pins by sha256 and does not ship",
    ),
    (r"appended \d+ further records \*\*without their", "other-tree", "the same predecessor log"),
    (r"recovery gets \*\*\d+ of [\d,]+\*\* entries back", "other-tree", "the same predecessor log"),
    (r"and [\d,]+ carry `params`", "other-tree", "an untracked `runs.jsonl`"),
    (
        r"\*\*[\d,]+ records\*\*\. That file is appended to daily",
        "other-tree",
        "an untracked `runs.jsonl`, and the sentence says so itself",
    ),
    (r"That matters: every one of the [\d,]+", "other-tree", "the same untracked history"),
    (r"would cost \*\*[\d.]+%\*\* of an", "other-tree", "one project's sidecar"),
    (r"\([\d,]+ input/output entries\)", "other-tree", "the same sidecar"),
    (
        r"reported that ~ ?[\d  ,]+ of its records said",
        "other-tree",
        "a downstream project's history",
    ),
    (
        r"on disk that is \*\*\d+ files, \d+ of \d+ steps covered",
        "other-tree",
        "one pipeline's environment snapshots",
    ),
    (
        r"median [\d,]+ bytes, max [\d,]+",
        "other-tree",
        "an untracked history, and the sentence says it is appended to daily",
    ),
    (r'"\d+ runs" meant \d+ \*completed\* runs', "other-tree", "the predecessor system"),
    # --- this host, at that moment
    (r"on call-bound code: \*\*`census` [\d.]+\u00d7", "this-host", "observation overhead"),
    (r"A run of [\d  ]+ calls pays \d+ ms", "this-host", "observation overhead"),
    (r"at once report \*\*\d+ MiB, not \d+\*\*", "this-host", "peak RSS of a process tree"),
    (r"desktop session at [\d  ]+ MiB", "this-host", "one desktop's memory"),
    (r"\*\*\+[\d.]+ ms\*\* for a realistic run", "this-host", "unregistered-read overhead"),
    (r"it includes the interpreter's own ~\d+ MB", "this-host", "one interpreter's footprint"),
    (r"with the history: \d+\u2013\d+ MB against a \d+ MB file", "this-host", "`show` memory"),
    (r"as \*\*[\d.]+ s\*\*\. It is [\d.]+ s", "this-host", "`show` wall-clock"),
    (r"\*\*\d+ MB\*\*; consuming them one at a time costs", "this-host", "`diff` memory"),
    (r"re-reading \d+ MB costs seconds", "this-host", "`--stale` memory"),
    (
        r"\*\*[\d.]+ s → [\d.]+ s\*\*\. The four added fields cost",
        "this-host",
        "write time on one machine",
    ),
    (
        r"on Linux ext4, \d+ processes \u00d7 \d+ appends",
        "this-host",
        "a lock-free append race, Linux-only by construction",
    ),
    (r"produced \d+/\d+ intact records, because Linux", "this-host", "the same Linux-only race"),
    # --- a library or tool this project does not install
    (
        r"emits \*\*[\d.]+\*\*\. Measured against this wheel",
        "third-party",
        "`hatchling`, which is a build-time requirement and is not in the venv",
    ),
    (r"emits [\d.]+ just the same, which was measured", "third-party", "the same build backend"),
    (r"Snakemake [\d.]+ records", "third-party", "`snakemake`, not a dependency"),
    (r"differs at exactly one byte, offset \d+", "third-party", "`pyreadr`"),
    (r"carries a random \d+-byte sync marker", "third-party", "`fastavro`"),
    (r"read a \d+-taxon tree back with \d+ terminals", "third-party", "`biopython`"),
    (r"sees \*\*\d+ rows and not \d+\*\*", "third-party", "`pandas`"),
    (r"then reports \d+ rows for a \d+-row file", "third-party", "`pandas`"),
    # --- one CPython version
    (
        r"produces \*\*[\d  ]+ Python calls, [\d  ]+ of them inside",
        "interpreter",
        "`csv.py`'s internals, which differ by version",
    ),
    (r"the project root gives \*\*\d+\*\*, all yours", "interpreter", "the same call sweep"),
    (
        r"a registered input: \*\*\d+ opens observed",
        "interpreter",
        "import machinery, which differs by version",
    ),
    # --- a hypothetical, measuring nothing
    (r"a sweep that parsed \d+ files is a clean bill", "illustration", "a made-up sweep size"),
    (r"send you into a [\d,]+-line history", "illustration", "a made-up history size"),
    (r"every byte of a \d+ GB BAM", "illustration", "a made-up file size"),
    (r"Change line \d+ of `runs\.jsonl`", "illustration", "a worked example's line number"),
    (r"an artifact changed on \d+ March", "illustration", "a worked example's date"),
    # --- the hosted CI's own history
    (r"queried without a window, \d+ runs", "ci-history", "a run-count query"),
    (
        r"windows \*\*\d+/\d+\*\* — figures from a suite",
        "ci-history",
        "a per-leg tally the sentence itself calls superseded",
    ),
)


def historical_lines(text: str) -> tuple[dict[int, str], list[str]]:
    """Which README lines are permanently unverifiable, and which entries no longer apply.

    A DEAD ENTRY IS REPORTED, never ignored: an exemption whose sentence has gone is an excuse
    outliving its subject, and this file has already shipped one decoration that checked nothing.
    """
    lines = text.split("\n")
    found: dict[int, str] = {}
    stale: list[str] = []
    for pattern, category, subject in _HISTORICAL:
        hits = [n for n, line in enumerate(lines, 1) if re.search(pattern, line)]
        if not hits:
            #: NOT reported as stale here: a pattern matching nothing in THIS document may match in
            #: another, and only the whole survey can tell. `dead_historical_entries` decides.
            continue
        #: SEVERAL MATCHES ARE NOW ALLOWED, and that is a deliberate relaxation. The one-match rule
        #: was right while the subject was a single page; several pages repeat the same sentence --
        #: *"three children holding ~150 MiB"* is in `README.md` and `README-pypi.md` both -- and
        #: one reason explains every copy. What is still refused is an entry explaining NOTHING.
        for n in hits:
            found[n] = f"{category} — {subject}"
    return found, stale


def dead_historical_entries() -> list[str]:
    """Entries matching no line in ANY audited document — an excuse outliving its subject."""
    texts = documents().values()
    return [
        f"{category}/{subject}: no line in any audited document matches {pattern!r}"
        for pattern, category, subject in _HISTORICAL
        if not any(re.search(pattern, text) for text in texts)
    ]


def _test_functions() -> int:
    """`^def test` in the suite's one module -- the same walk `tests/conftest.py` does."""
    return len(
        re.findall(r"^def test", (ROOT / "tests" / "test_runprov.py").read_text("utf-8"), re.M)
    )


def _tmp_path_uses() -> int:
    """The README gives this derivation itself: `grep -oE '\btmp_path\b' ... | wc -l`."""
    return len(re.findall(r"\btmp_path\b", (ROOT / "tests" / "test_runprov.py").read_text("utf-8")))


def _format_cases() -> int:
    """`len(CASES)` in the format matrix, LOADED rather than counted by pattern.

    Counting `^CASE(` gives **57** and the module builds **60**: three calls are indented, one
    inside a loop and one inside a docstring. A pattern count would have bound the sentence to a
    number the program never produces -- a binding that is wrong in the same way the claim was.

    SAFE TO LOAD, and that is measured rather than hoped: every optional library the matrix uses
    (`h5py`, `anndata`, `zarr`, `pyarrow`, `torch`, `openpyxl`, `pyreadr`, `onnx`, `safetensors`)
    is ABSENT from the gate's virtualenv and the module still imports and builds its 60 cases,
    because `requires=` defers every one of them. So this cannot fail on a matrix leg for want of
    a library.
    """
    spec = importlib.util.spec_from_file_location(
        "_formats_for_claims", ROOT / "examples" / "format_compatibility.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return len(module.CASES)


#: (pattern capturing ONE number, what it is, how to derive it, fractional tolerance).
#:
#: A SECOND REGISTRY, BECAUSE THESE ARE DERIVATIONS AND NOT CONSTANTS. `_BOUND` reads a module
#: attribute; these count something about the repository, which no attribute holds. The split is
#: the honest one: the registry still holds the PAIR -- a sentence and how to compute it -- and
#: still never holds the value.
#:
#: THE TOLERANCE IS THE DOCUMENT'S OWN CONVENTION, not a convenience. The README says these
#: figures convey SCALE and states the rule beside them; `ci.py`'s `SCALE_TOLERANCE` is the same
#: 10% with the same argument -- *a number 5% out still conveys it; one 17% out does not*. An
#: EXACT binding on "about 1,200 tests" would be red on the next test added, and a guard that is
#: red on correct work is one that gets deleted.
_DERIVED: tuple[tuple[str, str, object, float], ...] = (
    (r"\*\*about ([\d,]+) tests\*\*", "tests/test_runprov.py `^def test`", _test_functions, 0.10),
    (r"\*\*about ([\d,]+)\*\* uses of `tmp_path`", "`tmp_path` occurrences", _tmp_path_uses, 0.10),
    #: EXACT, because this one is a table of cases and not a scale figure: `len(CASES)` is a
    #: number the program states about itself, so "about" would be an evasion.
    (
        r"\*\*(\d+) formats, \d+ failures",
        "examples/format_compatibility.py `CASES`",
        _format_cases,
        0.0,
    ),
)


def _live(module: str, attribute: str) -> int:
    """The current value of a bound constant, or raise so the run is VOID rather than green.

    A SIZED CONSTANT IS BOUND BY ITS LENGTH, because *"refuses 40 binary suffixes"* is a claim
    about `len(PIN_BINARY)` and there is no integer constant beside it. Adding one would write
    the value down twice, which is the defect this whole file exists to find -- so the length
    is read from the collection itself and the collection stays the single definition.
    """
    target = importlib.import_module(module)
    for part in attribute.split("."):
        target = getattr(target, part)
    if isinstance(target, int) and not isinstance(target, bool):
        return target
    if isinstance(target, abc.Sized):
        return len(target)
    raise TypeError(
        f"{module}.{attribute} is {type(target).__name__}, which is neither an int nor sized"
    )


def bound_lines(text: str) -> tuple[dict[int, str], list[str]]:
    """Which README lines carry a BOUND number, plus the ones this attribution could not make.

    WHY THIS EXISTS, AND IT IS A CORRECTION. `classify` knew only about `_KINDS`, so a line
    whose number is bound by the strongest mechanism here fell through to `unaccounted` --
    **five of the six did**. The denominator was therefore wrong in the one direction that
    matters: *binding a claim did not reduce it*, so a ratchet built on it would have been
    insensitive to exactly the work it exists to encourage.

    A LINE IS CREDITED ONLY IF IT CARRIES ONE NUMBER. The unit everywhere in this file is the
    line, and a line holding a bound number beside an unbound one would be credited whole --
    `resolved` is the weak verdict, and over-crediting it is how a gap hides. Such a line stays
    UNACCOUNTED and is named in the second return value instead of being silently absorbed.
    Today all six carry exactly one number; that is checked here rather than assumed.
    """
    lines = text.split("\n")
    credited: dict[int, str] = {}
    unattributed: list[str] = []
    pairs = [
        (pattern, f"{module.removeprefix('runprov.')}.{attribute}")
        for pattern, module, attribute, _d in _BOUND
    ]
    pairs += [(pattern, what) for pattern, what, _how, _tol in _DERIVED]
    for pattern, what in pairs:
        for n, line in enumerate(lines, 1):
            if not re.search(pattern, line):
                continue
            if len(NUMBER.findall(line)) == 1:
                credited[n] = what
            else:
                unattributed.append(f"{what}: line {n} carries more than one number")
            break
        else:
            if re.search(pattern, text):  # pragma: no cover - no binding spans a line today
                unattributed.append(f"{what}: matches the document but no single line")
    return credited, unattributed


def bound_claims(text: str) -> list[tuple[str, int | None, int, str]]:
    """Every registry entry resolved against the document AND the live value.

    Returns (what, stated-or-None, live, verdict). A registry entry whose SENTENCE has gone is
    reported too: a binding to a number the document no longer makes is a decoration, not a
    check, and this repository has shipped one of those -- `scale_drift` reports the same thing
    for the same reason, and its own docstring says the thing that must never happen quietly is
    the check becoming vacuous.
    """
    out: list[tuple[str, int | None, int, str]] = []
    for pattern, module, attribute, divisor in _BOUND:
        live = _live(module, attribute)
        found = re.search(pattern, text)
        what = f"{module.removeprefix('runprov.')}.{attribute}"
        if not found:
            out.append((what, None, live, "VACUOUS — the sentence quoting it is gone"))
            continue
        stated = int(found.group(1).replace(",", "").replace("\u202f", "").replace(" ", ""))
        expected = live // divisor
        out.append((what, stated, expected, "ok" if stated == expected else "DRIFTED"))

    for pattern, what, how, tolerance in _DERIVED:
        live = how()
        found = re.search(pattern, text)
        if not found:
            out.append((what, None, live, "VACUOUS — the sentence quoting it is gone"))
            continue
        stated = int(found.group(1).replace(",", "").replace("\u202f", "").replace(" ", ""))
        #: THE SLACK IS AGAINST THE LIVE VALUE, not the stated one: the question a reader asks is
        #: "how far is the document from the truth", and dividing by the document's own figure
        #: would let a drifting number widen its own window.
        allowed = live * tolerance
        ok = abs(stated - live) <= allowed
        how_far = "" if not live else f", {abs(stated - live) / live:.1%} out"
        verdict = "ok" if ok else f"DRIFTED beyond {tolerance:.0%}{how_far}"
        out.append((what, stated, live, verdict))
    return out


def registry_verdicts() -> list[tuple[str, str, str]]:
    """Every registry entry's verdict ACROSS the audited documents: (what, verdict, where).

    THE VERDICT CANNOT BE TAKEN PER DOCUMENT, and the refactor that widened the surface proved it
    by exiting 1: an entry bound to a sentence that exists only in `README-pypi.md` reads as
    VACUOUS against `README.md`. An entry is vacuous when **no audited page** states its sentence;
    it has drifted when a page states it and the live value disagrees. Those are different repairs
    -- a rename to follow versus a document to correct -- so they stay separate here too.
    """
    pages = documents()
    out: list[tuple[str, str, str]] = []
    for name, text in pages.items():
        for what, stated, live, verdict in bound_claims(text):
            if stated is not None:
                out.append((what, verdict, f"{name} says {stated:,}, live {live:,}"))
    stated_anywhere = {what for what, _v, _w in out}
    for what, _stated, live, _verdict in bound_claims(next(iter(pages.values()))):
        if what not in stated_anywhere:
            out.append((what, "VACUOUS — no audited page states it", f"live {live:,}"))
    return out


def classify(
    text: str,
    bound: dict[int, str] | None = None,
    superseded: set[int] | None = None,
    elsewhere: dict[int, str] | None = None,
    historical: dict[int, str] | None = None,
) -> dict[str, list[str]]:
    """Every numeric literal in prose, bucketed. `unaccounted` is the one that matters.

    BOUND WINS OVER EVERY `_KINDS` PATTERN, because it is the only verdict here that can fail.
    One of the six also matches an exemption pattern, and without this precedence it would have
    been reported as a weak `example` while actually being compared against the live value.
    """
    bound = {} if bound is None else bound
    superseded = set() if superseded is None else superseded
    elsewhere = {} if elsewhere is None else elsewhere
    historical = {} if historical is None else historical
    buckets: dict[str, list[str]] = {"bound": [], "superseded": [], "elsewhere": []}
    buckets.update({name: [] for name, _, _ in _KINDS})
    buckets["code-literal"] = []
    buckets["historical"] = []
    buckets["unaccounted"] = []
    fenced = False
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not NUMBER.search(line):
            continue
        if n in bound:
            buckets["bound"].append(f"{n}: {bound[n]}")
            continue
        if n in superseded:
            buckets["superseded"].append(f"{n}: {line.strip()[:96]}")
            continue
        #: AFTER `superseded` DELIBERATELY: a correction note quoting *"about 5,500 statements"*
        #: must stay withdrawn rather than be credited to the registry that holds the live one.
        if n in elsewhere:
            buckets["elsewhere"].append(f"{n}: {elsewhere[n]}")
            continue
        for name, pattern, _reason in _KINDS:
            if re.search(pattern, line, re.I):
                buckets[name].append(f"{n}: {line.strip()[:96]}")
                break
        else:
            #: LAST, AFTER EVERY PATTERN KIND, so a more specific reason always wins: *"the one
            #: that matters is `1` against `2`"* is an exit-code line, not a code literal, and
            #: recording the weaker reason would mislabel it.
            if not NUMBER.search(_CODE_SPAN.sub(" ", line)):
                buckets["code-literal"].append(f"{n}: {line.strip()[:96]}")
            #: LAST OF ALL, because it is the weakest verdict this file produces: a human looked
            #: and recorded why nothing can check it. It is not accounted for; it is explained.
            elif n in historical:
                buckets["historical"].append(f"{n}: {historical[n]}")
            else:
                buckets["unaccounted"].append(f"{n}: {line.strip()[:96]}")
    return buckets


#: THE HEADER LIVES HERE, NOT IN THE FILE BEING REWRITTEN. `ci.py surface` learned this the hard
#: way: it read its own explanation back out of the file it regenerates, so deleting the file made
#: the regenerate command raise `FileNotFoundError` and took the explanation with it.
_BASELINE_HEADER = """# HOW MANY README LINES CARRY A NUMBER THAT NOTHING ACCOUNTS FOR.
#
# Generated by `python ci.py claims-baseline`. Enforced by
# `test_the_unaccounted_claim_count_is_exactly_the_committed_baseline`, which is in the DEFAULT
# gate -- unlike the report, because this number is green the day it arrives and the report is not.
#
# THE RULE DIGEST IS THE POINT OF THIS FILE. A ratchet whose denominator moves silently is a lie
# with a number on it: a sibling project's first baseline fell 696 -> 335 and only about half was
# work, the rest was the literal-detection rule tightening. So the count is stored WITH a digest of
# the rule that produced it, and the test refuses to compare across a change rather than reporting
# a fall that nobody earned. Move the rule and the test tells you to re-measure; it does not quietly
# accept the new number.
#
# TWO DIGESTS, BECAUSE ONE COULD NOT ATTRIBUTE. `detection` covers what counts as a claim --
# `NUMBER`, the `_KINDS` name/pattern pairs, `_SUPERSEDED_DECLARES` -- and a change to it moves the
# count with NO WORK DONE, so the gate refuses to compare. `bindings` covers `_BOUND` and `ci.py`'s
# `_SCALE_FIGURES`, and a change there lowers the count because somebody bound a claim, which IS
# the work. One digest fired "not comparable" on both, so honest work tripped the alarm meant for a
# fall nobody earned -- and an alarm that fires on good news is one people learn to silence.
# Neither covers the kinds' REASONS: documentation for a reader, changing no verdict.
#
# AN EQUALITY, NOT A CEILING. `<=` would let the file rot upward while real work went unrecorded,
# and a ratchet that never tightens is not a ratchet. Going DOWN is as red as going up, and the
# failure message says which -- down means regenerate this file, up means a claim arrived that
# nothing accounts for.
"""


def scale_patterns() -> dict[str, str]:
    """`ci.py`'s `_SCALE_FIGURES` patterns, READ rather than copied.

    Two README figures are bound and gated already -- by `ci.py`'s `scale_drift`, against the
    coverage JSON, inside `ci.py test`. This tool has no coverage data, so it cannot VERIFY them;
    what it can do is stop calling a line *accounted for by nothing* when something holds it. That
    is the same correction as crediting `_BOUND`'s own lines, in the second registry.

    IMPORTED, NOT RESTATED. A copy of those two patterns here would be a second definition of one
    claim, which is the defect this file exists to find.
    """
    spec = importlib.util.spec_from_file_location("_ci_for_claims", ROOT / "ci.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {label: pattern for label, (pattern, _key) in module._SCALE_FIGURES.items()}


def elsewhere_lines(text: str) -> dict[int, str]:
    """README lines whose number is bound by `ci.py` rather than by this file's registry."""
    out: dict[int, str] = {}
    lines = text.split("\n")
    for label, pattern in scale_patterns().items():
        for n, line in enumerate(lines, 1):
            if re.search(pattern, line):
                out[n] = f"ci.py `_SCALE_FIGURES` [{label}]"
                break
    return out


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def detection_digest() -> str:
    """A digest of what COUNTS as a claim: `NUMBER`, the `_KINDS` patterns, the correction rule.

    SPLIT FROM `bindings_digest` BECAUSE ONE DIGEST COULD NOT ATTRIBUTE. Both kinds of change
    move the count, and they mean opposite things: tightening a `_KINDS` pattern lowers it with
    **no work done at all**, while adding a binding lowers it because somebody bound a claim. A
    single digest fired *"not comparable"* on both, so honest work tripped the alarm meant for a
    fall nobody earned -- and an alarm that fires on good news is one people learn to silence.

    The reasons are deliberately NOT in here: they are documentation for a reader and change no
    verdict.
    """
    return _digest(
        {
            "number": NUMBER.pattern,
            "superseded": _SUPERSEDED_DECLARES.pattern,
            "kinds": [[name, pattern] for name, pattern, _reason in _KINDS],
            "code_span": _CODE_SPAN.pattern,
            #: THE EXCLUSIONS ARE A RULE DECISION and belong here: excusing a document lowers the
            #: count with no work done, exactly like loosening a pattern.
            #:
            #: THE DERIVED DOCUMENT LIST DELIBERATELY DOES NOT. A new `.md` arriving should red the
            #: POPULATION assertion -- *"the denominator moved"*, which names what happened -- not
            #: make the gate refuse to compare, which says only that something did.
            "not_audited": sorted(_NOT_AUDITED),
        }
    )


def bindings_digest() -> str:
    """A digest of what is BOUND -- this file's registry and `ci.py`'s, which is work, not rule."""
    return _digest(
        {
            "bound": [list(entry) for entry in _BOUND],
            #: THE CALLABLE IS NOT DIGESTED, only the pair and the tolerance. A derivation's body
            #: changing is a code change, and the VALUE it returns is what the gate compares --
            #: digesting the function would make every refactor of it read as a rule change.
            "derived": [[pattern, what, tol] for pattern, what, _how, tol in _DERIVED],
            "elsewhere": sorted(scale_patterns().items()),
            #: THE HISTORICAL REGISTRY BELONGS HERE AND NOT IN `detection`, because adding an
            #: entry is a judgement somebody recorded about one sentence -- the same kind of act
            #: as binding one -- while `detection` is what counts as a claim at all.
            "historical": [[pattern, cat] for pattern, cat, _subject in _HISTORICAL],
        }
    )


def counts() -> tuple[int, int, int]:
    """(population, unaccounted, historical). One call, so the report and the test agree.

    THE POPULATION IS RATCHETED TOO, and that is not belt-and-braces. The unaccounted figure is a
    NUMERATOR, and a numerator alone is the floor shape this repository keeps finding: tightening
    `NUMBER` drops lines out of the population entirely, which lowers the numerator while nothing
    was accounted for. Measured on the way in -- 113 -> 84 came with 284 -> 267, so seven of the
    twenty-nine were lines that stopped carrying a number at all. Recording both makes that
    visible instead of letting it read as work.
    """
    buckets, _notes = survey()
    return (
        sum(len(v) for v in buckets.values()),
        len(buckets["unaccounted"]),
        len(buckets["historical"]),
    )


def unaccounted_count() -> int:
    """The denominator alone, for callers that do not need the population."""
    return counts()[1]


def read_baseline() -> tuple[str, str, int, int, int] | None:
    """The committed digests and counts, or None if the file cannot be read.

    None covers an absent file AND one written before the digest was split, because both mean the
    same thing to a caller: there is nothing here that can be compared, so re-measure.
    """
    if not BASELINE.is_file():
        return None
    fields = dict(
        line.split(None, 1)
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    wanted = {"detection", "bindings", "population", "unaccounted", "historical"}
    if not wanted <= set(fields):
        return None
    return (
        fields["detection"].strip(),
        fields["bindings"].strip(),
        int(fields["population"]),
        int(fields["unaccounted"]),
        int(fields["historical"]),
    )


def write_baseline() -> tuple[int, int, int]:
    """Rewrite the committed baseline from the live counts."""
    population, count, known = counts()
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        f"{_BASELINE_HEADER}detection {detection_digest()}\n"
        f"bindings {bindings_digest()}\n"
        f"population {population}\n"
        f"unaccounted {count}\n"
        f"historical {known}\n",
        encoding="utf-8",
    )
    return population, count, known


def report() -> int:
    pages = documents()
    print(f"=== {len(pages)} SHIPPED DOCUMENT(S) AUDITED: {', '.join(sorted(pages))} ===")
    print("\n=== BOUND — compared against the live value, so these can fail ===")
    rows = registry_verdicts()
    for what, verdict, where in rows:
        print(f"  {verdict:<34} {what:<42} {where}")
    print("  ci.py `_SCALE_FIGURES` binds 2 more (statements, branches) and IS gated by `test`")

    buckets, unattributed = survey()
    for note in unattributed:
        print(f"  NOT ATTRIBUTED TO A LINE — stays unaccounted: {note}")
    for note in dead_historical_entries():
        print(f"  DEAD HISTORICAL ENTRY — an excuse outliving its subject: {note}")
    print("\n=== CLASSIFIED — a judgement recorded, never verification ===")
    bound_note = "the rows above, compared live and so removed from the denominator"
    print(f"  {len(buckets['bound']):>4}  bound        {bound_note}")
    print(f"  {len(buckets['superseded']):>4}  superseded   {_SUPERSEDED_REASON}")
    print(f"  {len(buckets['code-literal']):>4}  code-literal {_CODE_LITERAL_REASON}")
    print(
        f"  {len(buckets['elsewhere']):>4}  elsewhere    bound and gated by `ci.py`, which owns "
        f"the coverage data this tool cannot read"
    )
    for name, _pattern, reason in _KINDS:
        print(f"  {len(buckets[name]):>4}  {name:<12} {reason}")
    historical = buckets["historical"]
    print(
        f"\n=== HISTORICAL — {len(historical)} line(s) examined and found permanently "
        f"unverifiable. A WORK LIST, NOT A CLEARANCE ==="
    )
    for category, why in _WHY_HISTORICAL.items():
        #: NOT `rows`. It was, and it SHADOWED the registry verdicts computed above, so the exit
        #: code below filtered strings by `r[1]` -- the second CHARACTER of a line, not a tuple
        #: field. No error, just nonsense: the tool reported *"2 bound claim(s) no longer hold"*
        #: when every binding held, and the 2 was the size of the last category printed. A
        #: confident wrong exit code, found only by asking WHICH two.
        in_category = [h for h in historical if f"{category} —" in h]
        if in_category:
            print(f"  {len(in_category):>4}  {category:<13} {why}")
    for line in historical:
        print(f"    {line}")

    unaccounted = buckets["unaccounted"]
    print(f"\n=== UNACCOUNTED — {len(unaccounted)} line(s). NOBODY HAS LOOKED AT THESE ===")
    for line in unaccounted:
        print(f"  {line}")

    bad = [(what, verdict, where) for what, verdict, where in rows if verdict != "ok"]
    if bad:
        print(f"\n{len(bad)} bound claim(s) no longer hold. A bound claim that drifts is a")
        print("FINDING, not a backlog item, which is why this exits 1 rather than reporting it.")
        return 1
    print(
        f"\nevery bound claim holds. The residue is {len(unaccounted) + len(historical)} line(s): "
        f"{len(unaccounted)} nobody has examined and {len(historical)} examined and permanently "
        f"unverifiable. Recording a reason is NOT a check gained, so the two are counted apart "
        f"and the sum is what has to fall. This exits 0: it reports, it does not gate."
    )
    #: THE DENOMINATOR MOVES WHEN THE DETECTION RULE MOVES, and saying so is the difference
    #: between a ratchet and a lie with a number on it. A sibling project's first ratchet fell
    #: 696 -> 335 and only about half of that was work; the rest was the literal-detection rule
    #: tightening. So the figure above is *this rule's* count, the rule is the `_KINDS` table
    #: plus `NUMBER`, and a baseline committed from it must be re-measured whenever either moves.
    print(
        "That figure is THIS rule's count. Tightening `_KINDS` or `NUMBER` moves it without any "
        "work being done, so a baseline taken from it must name the rule it was taken under."
    )
    committed = read_baseline()
    if committed is None:
        print(f"no baseline committed yet — `python ci.py claims-baseline` writes {BASELINE.name}")
    else:
        detection, bindings, recorded_population, recorded, recorded_known = committed
        moved = [
            name
            for name, was, now in (
                ("detection", detection, detection_digest()),
                ("bindings", bindings, bindings_digest()),
            )
            if was != now
        ]
        if "detection" in moved:
            print(
                f"the baseline's DETECTION rule was {detection} and is now {detection_digest()}: "
                f"its {recorded} is not comparable with the figure above"
            )
        elif moved:
            print(
                f"the bindings moved ({bindings} -> {bindings_digest()}) and the detection rule "
                f"did not, so a fall from {recorded} to {len(unaccounted)} is WORK"
            )
        else:
            print(
                f"baseline {BASELINE.name}: residue {recorded + recorded_known} of "
                f"{recorded_population}, and nothing in the rule has moved"
            )
    return 0


if __name__ == "__main__":
    try:
        if "--baseline" in sys.argv[1:]:
            pop, written, known = write_baseline()
            print(
                f"wrote {BASELINE.name}: residue {written + known} of {pop} line(s) carrying a "
                f"number — {written} unexamined, {known} historical — detection "
                f"{detection_digest()}, bindings {bindings_digest()}"
            )
            raise SystemExit(0)
        raise SystemExit(report())
    except (AttributeError, ModuleNotFoundError, TypeError) as exc:
        print(
            f"VOID — a binding names something that is not there any more: {exc}. The registry "
            f"holds the PAIR and reads the value live, so this is a rename to follow, not a "
            f"drift to report.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
