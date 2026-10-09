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
        r"^\*\*\d+\.\s|^\d+\.\s|^#{1,6} \d|^\s*[-*] \*\*\d+\.",
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


def classify(
    text: str,
    bound: dict[int, str] | None = None,
    superseded: set[int] | None = None,
    elsewhere: dict[int, str] | None = None,
) -> dict[str, list[str]]:
    """Every numeric literal in prose, bucketed. `unaccounted` is the one that matters.

    BOUND WINS OVER EVERY `_KINDS` PATTERN, because it is the only verdict here that can fail.
    One of the six also matches an exemption pattern, and without this precedence it would have
    been reported as a weak `example` while actually being compared against the live value.
    """
    bound = {} if bound is None else bound
    superseded = set() if superseded is None else superseded
    elsewhere = {} if elsewhere is None else elsewhere
    buckets: dict[str, list[str]] = {"bound": [], "superseded": [], "elsewhere": []}
    buckets.update({name: [] for name, _, _ in _KINDS})
    buckets["code-literal"] = []
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
        }
    )


def counts(text: str) -> tuple[int, int]:
    """(population, unaccounted) under the current rule. One call, so report and test agree.

    THE POPULATION IS RATCHETED TOO, and that is not belt-and-braces. The unaccounted figure is a
    NUMERATOR, and a numerator alone is the floor shape this repository keeps finding: tightening
    `NUMBER` drops lines out of the population entirely, which lowers the numerator while nothing
    was accounted for. Measured on the way in -- 113 -> 84 came with 284 -> 267, so seven of the
    twenty-nine were lines that stopped carrying a number at all. Recording both makes that
    visible instead of letting it read as work.
    """
    credited, _unattributed = bound_lines(text)
    buckets = classify(text, credited, superseded_lines(text), elsewhere_lines(text))
    return sum(len(v) for v in buckets.values()), len(buckets["unaccounted"])


def unaccounted_count(text: str) -> int:
    """The denominator alone, for callers that do not need the population."""
    return counts(text)[1]


def read_baseline() -> tuple[str, str, int, int] | None:
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
    if not {"detection", "bindings", "population", "unaccounted"} <= set(fields):
        return None
    return (
        fields["detection"].strip(),
        fields["bindings"].strip(),
        int(fields["population"]),
        int(fields["unaccounted"]),
    )


def write_baseline() -> tuple[int, int]:
    """Rewrite the committed baseline from the live counts. Returns (population, unaccounted)."""
    population, count = counts(README.read_text(encoding="utf-8"))
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        f"{_BASELINE_HEADER}detection {detection_digest()}\n"
        f"bindings {bindings_digest()}\n"
        f"population {population}\n"
        f"unaccounted {count}\n",
        encoding="utf-8",
    )
    return population, count


def report() -> int:
    text = README.read_text(encoding="utf-8")
    rows = bound_claims(text)
    print("=== BOUND — compared against the live value, so these can fail ===")
    for what, stated, live, verdict in rows:
        said = "—" if stated is None else f"{stated:,}"
        print(f"  {verdict:<44} {what:<28} README {said:>9}  live {live:,}")
    print("  ci.py `_SCALE_FIGURES` binds 2 more (statements, branches) and IS gated by `test`")

    credited, unattributed = bound_lines(text)
    for note in unattributed:
        print(f"  NOT ATTRIBUTED TO A LINE — stays unaccounted: {note}")

    buckets = classify(text, credited, superseded_lines(text), elsewhere_lines(text))
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
    unaccounted = buckets["unaccounted"]
    print(f"\n=== UNACCOUNTED — {len(unaccounted)} line(s). THIS IS THE DENOMINATOR ===")
    for line in unaccounted:
        print(f"  {line}")

    bad = [r for r in rows if r[3] != "ok"]
    if bad:
        print(f"\n{len(bad)} bound claim(s) no longer hold. A bound claim that drifts is a")
        print("FINDING, not a backlog item, which is why this exits 1 rather than reporting it.")
        return 1
    print(
        f"\nevery bound claim holds; {len(unaccounted)} line(s) are accounted for by nothing. "
        f"This exits 0: it reports, it does not gate."
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
        detection, bindings, recorded_population, recorded = committed
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
                f"baseline {BASELINE.name}: {recorded} of {recorded_population}, and nothing in "
                f"the rule has moved"
            )
    return 0


if __name__ == "__main__":
    try:
        if "--baseline" in sys.argv[1:]:
            pop, written = write_baseline()
            print(
                f"wrote {BASELINE.name}: {written} unaccounted of {pop} line(s) carrying a "
                f"number, detection {detection_digest()}, bindings {bindings_digest()}"
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
