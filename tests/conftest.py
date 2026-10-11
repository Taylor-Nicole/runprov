# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Tier every test by what it actually DRIVES, derived at collection rather than written down.

    python -m pytest -m unit            the fast ones: in-process, nothing but this package
    python -m pytest -m api             drives `Run` / `configure` against real files
    python -m pytest -m cli             drives `cli.main` in process
    python -m pytest -m subprocess      launches a child process
    python -m pytest -m "not subprocess"    everything that stays in this process
    python -m pytest -m repo            the checks whose SUBJECT is this repository's own files

WHY DERIVED AND NOT DECLARED. The suite is one file holding over a thousand test functions.
Writing `@pytest.mark.unit` on each is **a list as long as the suite**, and this repository's
rule is *delete the list, do not extend it* -- a hand-applied marker drifts the first time a
test grows a subprocess call, and nothing would say so. So the tier is computed from the test's
own call graph every time the suite is collected, which means it cannot be stale: change what a
test does and its tier changes with it.

WHAT IS A DECISION AND WHAT IS DERIVED, because the distinction is the whole design. The four
PREDICATES below are a human judgement -- four lines saying what "drives the CLI" means. The
POPULATION is derived: every test pytest collects, none of them named here. That is the same split
`tools/claims.py` makes for the README's numbers, and the reason is the same: a tier's MEANING is
not enumerable by the language, while its members are.

THE CLOSURE IS THE PART THAT MATTERS. A test that shells out through a helper calls no
`subprocess` itself, and reading only the test body misclassifies it. **Measured once, on
2026-09-24, and quoted here as the evidence for the design rather than as a live figure** --
the suite has grown since and nothing recomputes the body-only column, which would need a
second implementation of the thing this file exists to do. Body-only against transitive:

    body only         unit=669  api=373  cli=83  subprocess=23
    with the closure  unit=513  api=503  cli=68  subprocess=64

**176 tests change tier**, and the direction is what matters: 135 `unit` -> `api`, and **41 move
INTO `subprocess`** (21 from `unit`, 15 from `cli`, 5 from `api`). So a body-only derivation would
have run 41 child-process tests under `-m "not subprocess"` -- the selection would have been a
lie, quietly, in the direction that costs time rather than the one that fails loudly.

(Those are FUNCTION counts, and `pytest` collects more ITEMS than that because `parametrize`
expands. Both granularities are right at their own level; the guard checks the item one, since
that is what runs. No count is written here -- `--collect-only -q` is one command and cannot be
stale, and a figure in this docstring would be a second definition of it.)

EXACTLY ONE TIER, AND `unit` IS A COMPLEMENT RATHER THAN A DEFAULT. `unit` means *none of the
other three predicates matched*, so the four are total by construction. What is NOT total is this
module's knowledge of the test: a function pytest collects but the AST walk never saw -- generated
in a loop, built by a factory -- gets NO marker, and
`test_every_collected_test_carries_exactly_one_tier` is what notices. An unmarked test must be a
red, never a quiet `unit`: a tier that silently absorbs what it could not classify is the floor
shape this project keeps finding.

WHAT THE TIERING BUYS, AND WHY NO NUMBER FOR IT IS WRITTEN HERE. The ITEM counts are one
command -- `pytest -m <tier> --collect-only -q` -- and they reproduce exactly. The TIMES do not.

This paragraph used to carry a table of seconds and the claim that `-m unit` is **"about five and
a half times faster"** than the full suite, under the instruction *"do not quote a single tier's
seconds; quote the ratio"*. **That instruction was not enough.** Re-measured 2026-10-10 back to
back in one batch, the ratio came back about FOUR; an independent batch the same week came back
about TWO. On this host the ratio moves as much as the absolutes do, because the machine runs
other people's work and whichever leg lands under a neighbour's build is the leg that looks
expensive -- in that batch the whole-suite leg ran last, beside two other test runs, which is
exactly the comparator the ratio divides by.

So the only things stated here are what reproduced across BOTH batches, and they are ORDERINGS
rather than magnitudes:

    cheapest per item   `unit`
    then                `api` and `cli`, close together
    then                `repo`
    most expensive      `subprocess` -- by a wide margin, several times `unit`

`-m unit` is substantially faster than the whole suite and covers a little under half its items.
If you need an actual number, measure it in one batch on a quiet host, use it, and do not write
the answer back into this file.

`repo` IS ORTHOGONAL, not a fifth tier. The tests whose SUBJECT is this repository's own
sources, docs or workflows sit across all four tiers. They are the ones that answer *"does the
documentation still describe the code"*, and wanting to run exactly those is a different
question from wanting the fast ones.

ITS PACKAGE-TREE HALF IS DERIVED, AND ITS REPOSITORY-ROOT HALF CANNOT BE. This was filed as an
open finding -- `_READS_THE_REPO` matched four named helpers, so a test reading the package's own
sources as `pathlib.Path(runprov.__file__).parent` had the same subject and got no marker. That
half is closed: the attribute read is carried as a token (`_PACKAGE_TREE`) and rides the call
closure, which gained **eleven** tests and lost none, every one of them a *does the packaging
still describe the code* check.

THE OTHER HALF WAS TRIED AND OVER-SELECTS, which is worth recording rather than attempting again.
Deriving from `REPO` and `__file__` as well gains **thirty-two**, and about five of those are not
subject-tests at all: a signal test setting `PYTHONPATH=REPO` for a child, a subclass test
locating its own file. The identical expression `str(REPO)` is a SUBJECT in
`ast.parse((REPO / "runprov" / x).read_text())` and a FIXTURE in `env["PYTHONPATH"] = str(REPO)`,
and the difference is what the value flows INTO -- a dataflow property this walk cannot decide.
Excepting the five would put the list back one level over, which is the shape the finding was
about. So the repository-root half stays four helper names, and this paragraph says so instead of
calling it derived.

Nothing selects on `-m repo` in `ci.py` or any workflow, so the residue costs navigation rather
than a false green.

A BARE `pytest` MUST STILL WORK, for a Debian, conda-forge or Nix packager rebuilding from the
sdist -- the same reason `pyproject.toml` keeps coverage flags out of `addopts`. Every marker
applied here is registered under `[tool.pytest.ini_options] markers`, so `--strict-markers` is
satisfied, and nothing here changes which tests RUN: it only labels them.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest

#: WHAT EACH TIER MEANS -- the human decision, four lines of it, and the only list in this file.
#: Matched against both the dotted spelling and the bare attribute, because a helper may call
#: `subprocess.run(...)` or have done `from subprocess import run`.
_DRIVES: tuple[tuple[str, frozenset[str]], ...] = (
    #: ORDERED BY HOW MUCH OF THE STACK IS REAL, because a test can satisfy several and gets
    #: exactly one tier. A test that launches a child AND calls `cli.main` is a `subprocess` test:
    #: the child is the outermost thing it drives, so that is what it is a test OF.
    #:
    #: NOT ORDERED BY COST, AND THIS COMMENT'S COST CLAIM HAS NOW BEEN WRONG TWICE, IN OPPOSITE
    #: DIRECTIONS. Version one said the child-process tier is "what makes it slow". Version two
    #: corrected that to `api` holding the time, and called `subprocess` "the cheap 2% of the
    #: suite". Measured 2026-10-10, and again in an independent batch the same week: BOTH are
    #: wrong. `subprocess` is the MOST expensive tier PER ITEM, by several times, and it takes a
    #: double-digit share of the suite's wall time off about five percent of its items -- so it
    #: is not "the cheap 2%" on either reading. What version two had right is that `api` is the
    #: largest single BLOCK of time, because it holds the most items doing real file I/O; what it
    #: got wrong was inferring from that which tier is EXPENSIVE. A share of the total and a cost
    #: per item are different questions, and version one was closer on the second of them. No
    #: figure is quoted here; the tiering docstring above says why.
    ("subprocess", frozenset({"subprocess.run", "subprocess.Popen", "subprocess.check_output"})),
    ("cli", frozenset({"cli.main"})),
    ("api", frozenset({"runprov.Run", "runprov.configure", "cli.Run"})),
)

#: THE PACKAGE TREE, CARRIED AS A SYNTHETIC TOKEN because a callee sweep cannot see it. Several
#: tests reach the package's own sources as `pathlib.Path(runprov.__file__).parent`, and the CALL
#: there is `pathlib.Path` -- which every test in the suite makes, so it separates nothing. What
#: identifies the subject is the attribute READ, so that is recorded as a token and then rides the
#: same closure as every real callee, through helpers, for free.
#:
#: IN ANGLE BRACKETS so it cannot collide with a callee spelling: `_called` below emits dotted
#: names and bare attributes, and no Python call produces a name with a space or a bracket in it.
#: A test asserts that, rather than leaving it as something this comment merely says.
_PACKAGE_TREE = "<the package tree>"

#: AND THE ORTHOGONAL ONE: the test's subject is this repository's own files.
#:
#: THE PACKAGE-TREE HALF IS DERIVED NOW, which is test-1 closed [2026-10-11]. It used to be four
#: helper names and nothing else, so a test reading the package's own sources inline got no
#: marker -- measured: ELEVEN did, every one of them a *does the packaging still describe the
#: code* check (the citation file, the licence, `py.typed`, the console script, the four source
#: sweeps). Adding `_PACKAGE_TREE` here gains exactly those eleven and loses none.
_READS_THE_REPO = frozenset({"_repo_root", "_readme", "_ci_module", "_bench_module", _PACKAGE_TREE})

_TIERS = (*(name for name, _ in _DRIVES), "unit")


def _called(node: ast.AST) -> set[str]:
    """Every callee spelling in one function body, dotted and bare -- plus the package-tree token.

    THE TOKEN IS EMITTED HERE rather than in a second walk, because the body is parsed once and
    walked once and this is where that walk is. It is not a callee and is spelled so that it
    cannot be mistaken for one; see `_PACKAGE_TREE`.
    """
    out: set[str] = set()
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == "__file__"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "runprov"
        ):
            #: `runprov.__file__` AND NOT ANY `X.__file__`. Measured on the first draft of this:
            #: matching the bare attribute caught `fake.__file__`, `good.__file__` and
            #: `bad.__file__` -- module objects the suite BUILDS in `tmp_path`, whose subject is
            #: not this repository at all. A pattern that matches legitimate content.
            out.add(_PACKAGE_TREE)
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            out.add(func.id)
        elif isinstance(func, ast.Attribute):
            base = func.value
            owner = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            out.add(f"{owner}.{func.attr}" if owner else func.attr)
            out.add(func.attr)
    return out


@functools.cache
def _reachable(path: str) -> dict[str, frozenset[str]]:
    """For each top-level function in one test module, everything it can reach IN THAT MODULE.

    CACHED PER FILE because the suite is one very large module and collection must not pay for
    it once per collected item. Measured when the fixed point replaced a per-name recursive
    walk: **the great majority of the cost is `ast.parse`**, with the callee sweep and the
    closure itself minor beside it -- so the cost is READING the file rather than resolving it,
    and the recursive version's extra time was pure waste. The absolute seconds are not quoted
    because this host's load swings them by a factor of two or three; re-measure in one batch
    if the question comes up again. Paid once per `pytest` run, `-k one_test` included.

    THE CLOSURE IS OVER FUNCTIONS DEFINED HERE ONLY -- it does not follow into the package, which
    is deliberate: the question is what the TEST drives, and `runprov`'s own internals calling
    `subprocess` somewhere does not make a unit test a subprocess test.
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    bodies = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct = {name: _called(node) for name, node in bodies.items()}

    #: A FIXED POINT, NOT A RECURSIVE WALK PER NAME. The first version recomputed each name's
    #: whole subtree and cost 4.05 s on this suite's 1 294 functions -- paid on EVERY `pytest`
    #: invocation, including `-k one_test`. Iterating to a fixed point shares the work and also
    #: handles a helper cycle without a `seen` set: the answer for a cycle is the union of what
    #: its members reach, which is what "everything this test can reach" means anyway.
    reach = {name: set(called) for name, called in direct.items()}
    spreading = True
    while spreading:
        spreading = False
        for got in reach.values():
            grown = set(got)
            for callee in got:
                grown |= reach.get(callee, frozenset())
            if grown != got:
                got |= grown
                spreading = True
    return {name: frozenset(got) for name, got in reach.items()}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply one tier marker, and `repo` where it applies. Never changes what runs."""
    for item in items:
        function = getattr(item, "function", None)
        module = getattr(getattr(item, "module", None), "__file__", None)
        if function is None or module is None:  # pragma: no cover - no such collector here
            continue
        reach = _reachable(module).get(function.__name__)
        if reach is None:  # pragma: no cover - a dynamically built test; the guard names it
            continue
        for tier, spellings in _DRIVES:
            if reach & spellings:
                item.add_marker(getattr(pytest.mark, tier))
                break
        else:
            item.add_marker(pytest.mark.unit)
        if reach & _READS_THE_REPO:
            item.add_marker(pytest.mark.repo)
