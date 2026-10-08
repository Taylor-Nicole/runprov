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

import importlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

#: A MEASUREMENT-SHAPED LITERAL. Digits, with the thin spaces and commas this document uses as
#: group separators, optionally a decimal part or a per-cent sign.
NUMBER = re.compile(r"(?<![\w.])\d[\d  ,]*(?:\.\d+)?%?(?![\w])")

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
)

#: EACH KIND CARRIES ITS REASON, because an unexplained exemption is how a gap hides. These say
#: *not a live claim about this tree*, and each is a judgement a reader may reject.
_KINDS: tuple[tuple[str, str, str], ...] = (
    (
        "example",
        r"^\s*(?:\$|>>>|#|\||    )",
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
        "exit-code",
        r"\bexit(s|ed)? [012]\b|\bexit code\b|\b[012] (intact|broken|could not|clean|usable|OK)\b",
        "a documented exit code -- a behavioural contract, held by the suite rather than a value",
    ),
)


def _live(module: str, attribute: str) -> int:
    """The current value of a bound constant, or raise so the run is VOID rather than green."""
    target = importlib.import_module(module)
    for part in attribute.split("."):
        target = getattr(target, part)
    if not isinstance(target, int):
        raise TypeError(f"{module}.{attribute} is {type(target).__name__}, not an int")
    return target


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
    return out


def classify(text: str) -> dict[str, list[str]]:
    """Every numeric literal in prose, bucketed. `unaccounted` is the one that matters."""
    buckets: dict[str, list[str]] = {name: [] for name, _, _ in _KINDS}
    buckets["unaccounted"] = []
    fenced = False
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not NUMBER.search(line):
            continue
        for name, pattern, _reason in _KINDS:
            if re.search(pattern, line, re.I):
                buckets[name].append(f"{n}: {line.strip()[:96]}")
                break
        else:
            buckets["unaccounted"].append(f"{n}: {line.strip()[:96]}")
    return buckets


def report() -> int:
    text = README.read_text(encoding="utf-8")
    rows = bound_claims(text)
    print("=== BOUND — compared against the live value, so these can fail ===")
    for what, stated, live, verdict in rows:
        said = "—" if stated is None else f"{stated:,}"
        print(f"  {verdict:<44} {what:<28} README {said:>9}  live {live:,}")
    print("  ci.py `_SCALE_FIGURES` binds 2 more (statements, branches) and IS gated by `test`")

    buckets = classify(text)
    print("\n=== CLASSIFIED — a judgement recorded, never verification ===")
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
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(report())
    except (AttributeError, ModuleNotFoundError, TypeError) as exc:
        print(
            f"VOID — a binding names something that is not there any more: {exc}. The registry "
            f"holds the PAIR and reads the value live, so this is a rename to follow, not a "
            f"drift to report.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
