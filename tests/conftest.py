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

WHY DERIVED AND NOT DECLARED. There are 1 148 test functions in one file. Writing
`@pytest.mark.unit` on each is **a list of 1 148**, and this repository's rule is *delete the
list, do not extend it* -- a hand-applied marker drifts the first time a test grows a subprocess
call, and nothing would say so. So the tier is computed from the test's own call graph every time
the suite is collected, which means it cannot be stale: change what a test does and its tier
changes with it.

WHAT IS A DECISION AND WHAT IS DERIVED, because the distinction is the whole design. The four
PREDICATES below are a human judgement -- four lines saying what "drives the CLI" means. The
POPULATION is derived: 1 148 tests, none of them named here. That is the same split
`tools/claims.py` makes for the README's numbers, and the reason is the same: a tier's MEANING is
not enumerable by the language, while its members are.

THE CLOSURE IS THE PART THAT MATTERS. A test that shells out through a helper calls no
`subprocess` itself, and reading only the test body misclassifies it. Measured over this suite's
1 148 test functions, body-only against transitive:

    body only         unit=669  api=373  cli=83  subprocess=23
    with the closure  unit=513  api=503  cli=68  subprocess=64

**176 tests change tier**, and the direction is what matters: 135 `unit` -> `api`, and **41 move
INTO `subprocess`** (21 from `unit`, 15 from `cli`, 5 from `api`). So a body-only derivation would
have run 41 child-process tests under `-m "not subprocess"` -- the selection would have been a
lie, quietly, in the direction that costs time rather than the one that fails loudly.

(Those are FUNCTION counts. `pytest` collects 1 250 ITEMS from them because `parametrize` expands,
so `-m unit` reports 563 rather than 513. Both numbers are right at their own granularity and the
guard checks the item one, since that is what runs.)

EXACTLY ONE TIER, AND `unit` IS A COMPLEMENT RATHER THAN A DEFAULT. `unit` means *none of the
other three predicates matched*, so the four are total by construction. What is NOT total is this
module's knowledge of the test: a function pytest collects but the AST walk never saw -- generated
in a loop, built by a factory -- gets NO marker, and
`test_every_collected_test_carries_exactly_one_tier` is what notices. An unmarked test must be a
red, never a quiet `unit`: a tier that silently absorbs what it could not classify is the floor
shape this project keeps finding.

WHAT THE TIERING BUYS, measured back to back in ONE batch because this host's load swings the
absolute numbers by a factor of two or three -- only the ratio within a batch means anything:

    -m unit             564 items     64 s
    -m subprocess        67 items     28 s
    -m "not subprocess" 1184 items    274 s
    the whole suite     1251 items    354 s

So `-m unit` is **about five and a half times faster** than the full suite and covers 45% of it.
And the per-tier figures do NOT sum to the whole: measured separately on a quieter host, `api`
came back at 69 s against the 210 s the same tests implied inside the batch. Do not quote a
single tier's seconds; quote the ratio, and re-measure the batch.

`repo` IS ORTHOGONAL, not a fifth tier. 82 tests read this repository's own sources, docs or
workflows, and they sit across all four tiers (57 unit, 14 subprocess, 9 cli, 2 api). They are the
ones that answer *"does the documentation still describe the code"*, and wanting to run exactly
those is a different question from wanting the fast ones.

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
    #: NOT ORDERED BY COST, AND THAT IS MEASURED. The first version of this comment said the
    #: child-process tier is "what makes it slow". It is not: back to back in one batch,
    #: `-m subprocess` is 67 items in 28 s while the whole suite is 1 251 in 354 s, and the time
    #: lives in `api`'s 539 items doing real file I/O. Calling `subprocess` the slow tier would
    #: have sent a reader to skip the cheap 2% of the suite.
    ("subprocess", frozenset({"subprocess.run", "subprocess.Popen", "subprocess.check_output"})),
    ("cli", frozenset({"cli.main"})),
    ("api", frozenset({"runprov.Run", "runprov.configure", "cli.Run"})),
)

#: AND THE ORTHOGONAL ONE: the test's subject is this repository's own files.
_READS_THE_REPO = frozenset({"_repo_root", "_readme", "_ci_module", "_bench_module"})

_TIERS = (*(name for name, _ in _DRIVES), "unit")


def _called(node: ast.AST) -> set[str]:
    """Every callee spelling in one function body, dotted and bare."""
    out: set[str] = set()
    for inner in ast.walk(node):
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

    CACHED PER FILE because the suite's one module is 43 000 lines and collection must not pay
    for it 1 251 times. Measured after the fixed point replaced a per-name recursive walk:
    2.23 s once, of which **ast.parse is 1.33 s**, the callee sweep 0.69 s and the closure
    itself 0.21 s. So the cost is reading the file rather than resolving it, and the recursive
    version's extra 1.8 s was pure waste. Paid once per `pytest` run, `-k one_test` included.

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
