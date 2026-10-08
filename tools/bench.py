# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Break a check on purpose, and prove the experiment was not VOID.

A guard that has never been broken on purpose is a guard nobody has measured. This project
already requires that -- *"break every check on purpose and say in the commit message how"* --
and has done it by hand every time, which is why the record contains **twenty-two proposed
remedies that would have shipped broken**: three built a check that could not pass, two could
not have failed at all, one did not import, one was dead inside its own test, one tested a
spelling instead of the thing. Not one was caught by a gate. Doing it by hand is not the
problem; doing it by hand means the CONTROLS are optional, and every one of those twenty-two
was found by a control somebody remembered to run.

THIS FILE IS NOT A MUTATION GENERATOR. It does not invent the mutation -- you do, because
choosing what to break is the reviewing judgement this instrument has no business making. It
supplies the RIGOUR: the tree, the proof that the tree is what ran, the proof that the mutation
reached the parser, the proof that the mutated statement was EXECUTED, and a baseline that was
green before any of it. Each of those answers a trap measured in this repository's own audit
record, and each is named at its control below.

    python tools/bench.py --test <-k expr> --revert PATH --from SHA
    python tools/bench.py --test <-k expr> --edit PATH --old STR --new STR
    python tools/bench.py selfcheck

Exit: 0 the mutation was CAUGHT (the guard works), 1 it SURVIVED (a finding about the guard),
2 the run is VOID -- a control did not fire, so nothing was measured. The three are the same
contract `tools/torture.py` uses, for the same reason: *"no findings" and "nothing was actually
damaged" are the same green.*

THE SEVEN CONTROLS, AND THE MEASURED FAILURE EACH ONE ANSWERS
-------------------------------------------------------------
Every one of these has silently produced a wrong answer in this project's history. They are not
defensive programming; they are the list of ways this exact experiment has already lied.

1. THE WORK ROOT IS NOT INSIDE THE REPOSITORY. The suite enumerates its own sources as
   `REPO.rglob("*.py")` minus three names, so a `.py` tree inside the repository joins that
   sweep: a green HEAD goes red, and `assert len(sources) >= 32` -- a FLOOR -- is satisfied by
   the copy, so it stops being able to notice a shrunken source set. Measured 2026-10-08, four
   copies inside the repo, 128 stray sources, one unrelated test red.

2. THE COPY IS THE WORKING TREE, NOT THE COMMITTED ONE. `git archive HEAD` is the obvious way
   to build a clean tree and it silently measures the tree WITHOUT your uncommitted work: a
   bench run came back `rc=5`, nothing collected, because the guard under examination was still
   unstaged. So this copies the working tree and ASSERTS the copied target is byte-identical to
   the one on disk.

3. THE COPY IS WHAT RAN. An editable install writes an ABSOLUTE path into `site-packages/*.pth`,
   so a copied tree run with the same interpreter imports the ORIGINAL package -- every mutation
   then appears to have no effect and every result reads "survived", clean. `PYTHONPATH` is set
   to the copy and the proof is `runprov.__file__`, printed, asserted to be under the copy.
   And `__pycache__` is deleted, because `copytree` preserves mtimes so a copied cache stays
   "valid" and its `co_filename` still names the original tree.

4. THE BASELINE IS GREEN BEFORE THE MUTATION IS APPLIED. A red on the mutant means nothing if
   the same selection was already red. This project endorsed a refusal on exactly that reasoning
   once and the red turned out to be a TRUE POSITIVE -- so the baseline is measured, not
   assumed, and a red baseline is VOID rather than an answer.

5. THE MUTATION REACHED THE PARSER AS EXECUTABLE STRUCTURE. Two measured failures, one shape:
   a plant harness inserted its variants INSIDE A DOCSTRING, so all six read green against the
   fixed guard and nearly refuted a true row; and a "revert" that replaced 2 of 3 uses left the
   tree byte-identical and the test reported `1 passed`. The control compares the two files'
   ASTs WITH EVERY DOCSTRING STRIPPED, so a prose-only edit and a no-op edit are both VOID.

6. THE MUTATED STATEMENT WAS EXECUTED BY THE SELECTED TEST. A plant appended AFTER
   `raise SystemExit(main())` is syntactically present, passes an AST dump, and raises
   `NameError` if anything ever reaches it -- which nothing does. Control 5 cannot see that and
   neither can reading the file. So the mutant runs under coverage and the changed lines are
   intersected with what coverage says EXECUTED. A mutation on a line the selected test never
   runs cannot teach you anything, whatever colour the result is.

7. THE VERDICT IS THE RETURN CODE, AND ONLY THE CODES THAT MEAN SOMETHING. `0` passed, `1` a
   test failed, **`5` nothing was collected** -- a selector that matched nothing -- and anything
   else is an ERROR. A harness that treats every non-zero code as a catch reports a mutation
   killed by a suite that never ran. `5` and the rest are VOID here, never CAUGHT.

WHAT IT DOES NOT CLAIM. That a CAUGHT verdict means the guard is correct -- only that this
mutation does not slip past it. A guard can catch the mutation you thought of and miss the five
you did not, which is the whole argument of `guard-shapes` and is not a thing any harness can
answer for you. It reports which test failed, so "it went red" can be checked against "the
assertion I meant went red" -- the two came apart in this project when a census assertion fired
ahead of the named defect and every plant reported the wrong fact at `rc=1`.

NOT IN THE GATE. It builds a tree per run and runs pytest twice. It belongs to whoever is
measuring something, not to every push. `ci.py bench` runs `selfcheck`, which is this file
proving it can still tell the five verdicts apart.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: NEVER COPIED INTO A BENCH TREE. `.git` and `.venv` are the two that bit: a `cp -a` that
#: skipped both produced a nested repository copy whose suite reported five failures that had
#: nothing to do with the change. The caches are excluded because a copied `__pycache__` is the
#: stale-bytecode trap in control 3, and `.audit-scratch` because copying a tree of trees is how
#: a 14 MB experiment becomes a 1 GB one.
_SKIP = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".audit-scratch",
    "htmlcov",
}

CAUGHT, SURVIVED = "CAUGHT", "SURVIVED"


#: NAMED FOR THE WORD THIS PROJECT ALREADY USES. `tools/torture.py` reports *"the run is
#: void (a control did not fire)"* and exits 2 for it; an `-Error` suffix would make it read
#: like a failure of the thing under test, which is the one thing it never is.
class Void(Exception):  # noqa: N818 - `void` is this project's word for it, see above
    """A control did not fire, so this run measured nothing. Never a verdict about the guard."""


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """The same tree with every docstring replaced by a fixed marker.

    SO THAT A PROSE EDIT IS NOT A MUTATION. `ast.dump` of two files differing only in a
    docstring differs, because a docstring IS a `Constant` node -- which is precisely the trap
    in control 5: six plant variants landed inside a docstring, were present in the dump, and
    read green against the guard they were meant to break.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                first.value.value = "<docstring>"
    return tree


def executable_difference(before: str, after: str) -> bool:
    """Do these two sources differ in anything but their docstrings? Control 5."""
    a = ast.dump(strip_docstrings(ast.parse(before)))
    b = ast.dump(strip_docstrings(ast.parse(after)))
    return a != b


def changed_lines(before: str, after: str) -> set[int]:
    """Line numbers IN `after` that `before` does not have. Control 6's left-hand side.

    Numbered in the MUTANT because that is the file coverage will report on. A whole-file revert
    changes many lines and most of them are prose; control 6 intersects this with the statements
    coverage knows about, so the prose drops out there rather than here.
    """
    out: set[int] = set()
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def classify(returncode: int, page: str = "") -> str:
    """What a pytest return code means. Control 7.

    `5` IS THE ONE THAT MATTERS. It is "no tests collected" -- a selector that matched nothing --
    and a harness that reads any non-zero code as a catch reports a mutation killed by a suite
    that never ran. A false pass closes the question; a false failure does not.
    """
    if returncode == 0:
        return SURVIVED
    if returncode == 1:
        return CAUGHT
    if returncode == 5:
        raise Void("pytest collected NOTHING (rc=5): the `--test` selector matched no test")
    #: THE PAGE COMES WITH IT. A bare "pytest exited 4" sent the author of this file looking
    #: at the wrong tree for twenty minutes; pytest's usage errors say exactly what is wrong
    #: and throwing them away made this instrument's own diagnostic worse than pytest's.
    raise Void(
        f"pytest exited {returncode}, which is an ERROR and not a result"
        + (f"\n  {page.strip()[-600:]}" if page else "")
    )


def _copy_working_tree(dest: pathlib.Path) -> None:
    """The WORKING tree, not the committed one. Control 2."""
    shutil.copytree(
        ROOT, dest, ignore=lambda _d, names: [n for n in names if n in _SKIP], symlinks=True
    )


def _clear_pycache(tree: pathlib.Path) -> None:
    for cache in list(tree.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def _prove_the_copy_is_what_runs(tree: pathlib.Path, py: str) -> str:
    """Control 3: import the package and insist its `__file__` is under the copy."""
    env = dict(os.environ, PYTHONPATH=str(tree))
    proc = subprocess.run(  # noqa: S603 - argv is built here, no shell
        [py, "-c", "import runprov, sys; sys.stdout.write(runprov.__file__)"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Void(f"the copy will not import: {proc.stderr.strip()[:300]}")
    where = pathlib.Path(proc.stdout.strip()).resolve()
    if tree.resolve() not in where.parents:
        raise Void(
            f"PYTHONPATH did not win: the copy at {tree} imported {where} instead. An editable "
            f"install writes an absolute path into `site-packages/*.pth`, which is the trap "
            f"control 3 exists for -- every mutation would read as having no effect"
        )
    return str(where)


#: `-q` DOES NOT PRINT "collected N items", so the first version of this regex matched nothing
#: and every run reported `0 collected` -- beside a GREEN baseline, which is the shape of a
#: number that lies. The summary counts are what `-q` does print.
_OUTCOMES = re.compile(r"(\d+) (?:passed|failed|skipped|error|errors|xfailed|xpassed)\b")
_FAILED = re.compile(r"^FAILED (\S+)", re.M)


def coverage_source(target: str) -> str:
    """What to give `--cov` so that coverage reports on `target`. DERIVED, never listed.

    THE LIST THAT WAS HERE COULD NOT SEE THIS FILE. `--cov=runprov --cov=tests` was hard-coded,
    so control 6 refused every mutation outside those two trees -- including every mutation of
    `tools/bench.py` itself, which is how the tests for this harness were meant to be
    demonstrated. The diagnostic said so plainly, which is the only reason it was cheap to find;
    a harness that had simply reported SURVIVED would have been worse than useless.

    So the source is the target's own top directory, and a file at the repository root gets `.`,
    because `--cov=ci.py` is a path and `--cov` wants a module or a directory.
    """
    parent = pathlib.PurePosixPath(target.replace("\\", "/")).parent
    return "." if str(parent) in (".", "") else str(parent).split("/")[0]


def _run_selection(
    tree: pathlib.Path, py: str, kexpr: str, cov: pathlib.Path | None, source: str = "."
) -> tuple[int, int, list[str], str]:
    """Run the selected tests in `tree`. Returns (rc, collected, failed node ids, output).

    THE STATUS IS READ FROM THE PROCESS, never from the tail of a pipeline. `cmd | grep` returns
    grep's code, and that exact shape hid a `ruff format --check` failure in this project twice.
    """
    #: NO `-q` HERE, BECAUSE `pyproject.toml` ALREADY SETS IT. `addopts = "-q"` plus our own `-q`
    #: is `-qq`, which suppresses the `N passed` summary line -- the exact line this function parses
    #: for the test count. Measured: every run reported `0 collected` beside a GREEN baseline, which
    #: is a number that lies in the most reassuring possible way. A flag the project already sets is
    #: not a free flag to repeat.
    args = [py, "-m", "pytest", "tests", "-k", kexpr, "-p", "no:randomly"]
    if cov is not None:
        args += [f"--cov={source}", f"--cov-report=json:{cov}", "--cov-fail-under=0"]
    env = dict(os.environ, PYTHONPATH=str(tree))
    #: THE INTERPRETER IS PART OF THE COMMAND. An earlier version printed `args[1:]`, which
    #: hid WHICH python was running pytest -- and the one failure this file hit while being
    #: written was exactly that: a python without `pytest-cov`, reported as
    #: `unrecognized arguments: --cov`. A printed command that cannot be pasted back is not
    #: a reproducible command.
    print(f"\n$ {' '.join(args)}   (cwd={tree})", flush=True)
    proc = subprocess.run(  # noqa: S603 - argv is built here, no shell
        args, cwd=tree, env=env, capture_output=True, text=True
    )
    page = proc.stdout + proc.stderr
    ran = sum(int(n) for n in _OUTCOMES.findall(page))
    return proc.returncode, ran, _FAILED.findall(page), page


def _executed_lines(cov: pathlib.Path, target: str) -> tuple[set[int], set[int]]:
    """What coverage says about one file: (executed, every statement it has)."""
    if not cov.exists():
        raise Void("coverage wrote no report, so control 6 cannot be evaluated")
    data = json.loads(cov.read_text(encoding="utf-8"))
    want = target.replace("\\", "/")
    for name, body in data.get("files", {}).items():
        if name.replace("\\", "/") == want:
            ran = set(body["executed_lines"])
            return ran, ran | set(body["missing_lines"])
    raise Void(
        f"coverage never saw {target}, although `--cov={coverage_source(target)}` was derived "
        f"from it. Control 6 needs the mutated file in the report"
    )


def _refuse_a_work_root_inside_the_repo(work: pathlib.Path) -> None:
    """CONTROL 1, and it runs BEFORE anything is created.

    It is first because it is the one that invalidates the others silently -- and the first
    version of it ran AFTER `main()` had already called `work.mkdir()`, so refusing the work root
    still left a directory inside the repository. A control that creates the hazard it refuses is
    not a control.
    """
    here, there = ROOT.resolve(), work.resolve()
    if here == there or here in there.parents:
        raise Void(
            f"the work root {work} is inside the repository {ROOT}. The suite enumerates its own "
            f"sources with `REPO.rglob('*.py')`, so a tree here joins that sweep: an unrelated "
            f"test goes red, and a `>= 32` source FLOOR starts being satisfied by the copy"
        )


def bench(
    *,
    test: str,
    target: str,
    mutate: Callable[[str], str],
    work: pathlib.Path,
    py: str,
    label: str,
) -> int:
    """One experiment. Returns the process exit code: 0 CAUGHT, 1 SURVIVED. Raises `Void`."""
    _refuse_a_work_root_inside_the_repo(work)
    base, mutant = work / "base", work / "mutant"
    for tree in (base, mutant):
        if tree.exists():
            shutil.rmtree(tree)
        _copy_working_tree(tree)
        _clear_pycache(tree)

    #: CONTROL 2: the copy carries the working tree, uncommitted work included.
    #: A MISSING TARGET IS VOID, NOT A VERDICT. Reading it bare raised `FileNotFoundError` and
    #: exited 1 -- which is this harness's SURVIVED code, so a typo in `--edit` read to any caller
    #: as "the guard did not notice". A traceback where a message belongs, in an instrument whose
    #: whole purpose is to make an experiment legible.
    if not (ROOT / target).is_file():
        raise Void(f"{target} is not a file in {ROOT}: nothing to mutate")
    source = (ROOT / target).read_text(encoding="utf-8")
    if (base / target).read_text(encoding="utf-8") != source:
        raise Void(f"the copy of {target} is not byte-identical to the working tree's")

    after = mutate(source)
    (mutant / target).write_text(after, encoding="utf-8")

    #: CONTROL 5: in the parse, and not only in the prose.
    #:
    #: AND A TARGET THIS CANNOT PARSE IS A VOID RUN, NOT A VERDICT. Controls 5 and 6 both rest on
    #: Python -- an AST diff and coverage -- so this is a PYTHON-SOURCE instrument and it has to
    #: say so. Pointing `--edit` at `README.md` raised a bare `SyntaxError` on the first non-Python
    #: character and exited 1, which is the SURVIVED code: the same shape as the missing-target
    #: defect fixed above, and the second time in this file that an instrument failure wore a
    #: verdict's exit code. A Markdown or YAML guard must be demonstrated by hand, in a copied
    #: tree, and the message says that rather than leaving the reader to guess.
    try:
        differs = executable_difference(source, after)
    except SyntaxError as exc:
        raise Void(
            f"{target} is not parseable as Python ({exc.msg} at line {exc.lineno}), and controls 5 "
            f"and 6 both need a Python AST and coverage. This harness benches PYTHON sources only; "
            f"demonstrate a Markdown, YAML or TOML guard by hand in a copied tree"
        ) from exc
    if not differs:
        raise Void(
            f"the mutation changed nothing in {target} that the parser can see -- either it is "
            f"a no-op, or it landed inside a DOCSTRING. Both have reported `1 passed` in this "
            f"repository while measuring an unmodified tree"
        )
    touched = changed_lines(source, after)

    print(f"\n=== {label} ===")
    print(f"work   {work}")
    print(f"target {target}, {len(touched)} line(s) changed in the mutant")
    #: CONTROL 3, on both trees, because either could be the one that silently did not win.
    print(f"base   imports {_prove_the_copy_is_what_runs(base, py)}")
    print(f"mutant imports {_prove_the_copy_is_what_runs(mutant, py)}")

    #: CONTROL 4.
    rc0, n0, failed0, page0 = _run_selection(base, py, test, None)
    if rc0 == 5:
        raise Void(f"the selector {test!r} collected NOTHING on the UNMUTATED tree (rc=5)")
    if rc0 != 0:
        raise Void(
            f"the baseline is not green: {test!r} exits {rc0} on the unmutated tree "
            f"({', '.join(failed0) or 'no FAILED line'}). A red on the mutant would say nothing "
            f"about the mutation.\n{page0[-1200:]}"
        )
    print(f"baseline GREEN, {n0} test(s) collected")

    cov = work / "mutant-coverage.json"
    rc1, n1, failed1, page1 = _run_selection(mutant, py, test, cov, coverage_source(target))
    verdict = classify(rc1, page1)

    #: CONTROL 6, evaluated AFTER the run because it needs the run's coverage, and asserted
    #: before the verdict is printed because a verdict over an unexecuted line is not a verdict.
    ran, statements = _executed_lines(cov, target)
    changed_statements = touched & statements
    if not changed_statements:
        raise Void(
            f"none of the {len(touched)} changed line(s) is a STATEMENT in {target} -- the "
            f"mutation is comment or prose only, so nothing about it can run"
        )
    if not (changed_statements & ran):
        raise Void(
            f"no changed statement in {target} was EXECUTED by {test!r}: changed statements "
            f"{sorted(changed_statements)[:12]}, none of them in coverage's executed set. A "
            f"plant after `raise SystemExit(main())` looks exactly like this and is invisible "
            f"to an AST check"
        )
    print(
        f"executed {len(changed_statements & ran)} of {len(changed_statements)} changed "
        f"statement(s) under the selection"
    )

    print(f"\nVERDICT: {verdict}  (pytest rc={rc1}, {n1} collected)")
    if verdict == CAUGHT:
        print("failed: " + (", ".join(failed1) or "(rc=1 with no FAILED line -- read the page)"))
        print(
            "READ WHICH ASSERTION FIRED. `rc=1` is not `the assertion I meant fired`: a census "
            "clause once fired ahead of the named defect and every plant reported the wrong fact."
        )
        return 0
    print(
        f"the guard did NOT notice this mutation. That is a finding about the guard, not about "
        f"the mutation.\n{page1[-800:]}"
    )
    return 1


def _revert(target: str, sha: str) -> Callable[[str], str]:
    proc = subprocess.run(  # noqa: S603 - argv is built here, no shell
        ["git", "show", f"{sha}:{target}"],  # noqa: S607 - git is on PATH by design here
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Void(f"git show {sha}:{target} failed: {proc.stderr.strip()[:200]}")
    old = proc.stdout
    return lambda _source: old


def _substitute(old: str, new: str) -> Callable[[str], str]:
    """One exact substitution, and the ANCHOR MUST BE UNIQUE.

    A mutation applied at two sites when you meant one is a different experiment from the one
    you are about to report, and a mutation applied at zero sites is control 5's no-op wearing
    a successful-looking command line.
    """

    def apply(source: str) -> str:
        seen = source.count(old)
        if seen != 1:
            raise Void(
                f"--old occurs {seen} times in the target; the anchor must be unique, because a "
                f"mutation applied at {seen} sites is not the experiment being reported"
            )
        return source.replace(old, new, 1)

    return apply


#: THE SELFCHECK CASES. Each one exists because the verdict it asks for has been reported WRONGLY
#: by a hand-run version of this experiment in this repository. `expect` is the harness's own
#: answer, so a case that stops reproducing is a defect in the harness, not in the package.
_SELFCHECK = [
    #: (label, -k selector, target, mutation kind, expected verdict, required reason fragment).
    #: THE REASON FRAGMENT IS THE POINT. A first version asserted only "VOID", and TWO of its three
    #: VOID cases passed for the WRONG REASON -- one on a non-unique anchor and one on
    #: `pytest exited 4` from an interpreter without `pytest-cov` -- while printing `ok`. A control
    #: that fires for a reason you did not name is not a control, which is the same finding this
    #: repository recorded when a census clause fired ahead of the named defect.
    (
        "CAUGHT -- a real guard notices a real break",
        "test_prune_does_not_forge_a_line_from_a_marker_filename",
        "runprov/_report.py",
        None,
        CAUGHT,
        "",
    ),
    (
        "SURVIVED -- executed, unnoticed, and that is a fact about the GUARD",
        "test_prune_does_not_forge_a_line_from_a_marker_filename",
        "runprov/prune.py",
        "unnoticed",
        SURVIVED,
        "",
    ),
    (
        "VOID control 5 -- the edit is PROSE ONLY",
        "test_prune_does_not_forge_a_line_from_a_marker_filename",
        "runprov/_report.py",
        "docstring-only",
        "VOID",
        "landed inside a DOCSTRING",
    ),
    (
        "VOID control 6a -- the plant lands where COVERAGE CANNOT SPEAK",
        "test_prune_does_not_forge_a_line_from_a_marker_filename",
        "runprov/__main__.py",
        "after-systemexit",
        "VOID",
        "is a STATEMENT",
    ),
    (
        "VOID control 6b -- a TRACKED statement the selection never runs",
        "test_prune_does_not_forge_a_line_from_a_marker_filename",
        "runprov/chain.py",
        "tracked-but-unrun",
        "VOID",
        "was EXECUTED by",
    ),
    (
        "VOID control 7 -- the selector matches NOTHING (rc=5)",
        "test_a_name_no_test_in_this_repository_has",
        "runprov/_report.py",
        None,
        "VOID",
        "collected NOTHING",
    ),
]


def _selfcheck_mutation(kind: str | None) -> Callable[[str], str]:
    if kind is None:
        #: `printable` STOPS ESCAPING. The package's one escaping primitive, made the identity --
        #: the break every forgery guard in this project exists to notice, and the one Audit Q
        #: proved these guards must see at RUNTIME rather than by syntax.
        return _substitute(
            '    return "".join(\n'
            '        c if c.isprintable() or c == " " else c.encode("unicode_escape").decode()'
            " for c in text\n"
            "    )",
            "    return text  # bench selfcheck: printable made the identity",
        )
    if kind == "docstring-only":
        #: INSERTED INSIDE THE MODULE DOCSTRING, which is exactly where a plant harness put six
        #: variants once and read all six green against the guard they were meant to break.
        return _substitute(
            "Where runprov's OWN messages go.",
            "Where runprov's OWN messages go (bench selfcheck prose edit).",
        )
    if kind == "unnoticed":
        #: EXECUTED BY THE SELECTION AND NOT ASSERTED BY IT. The selected guard asks one question --
        #: does a bare line equal the sentinel -- so the page's WORDING is nothing to it, and this
        #: substitution runs on every leg of it and changes no answer it gives. That is a SURVIVED,
        #: and it is a fact about the guard rather than about the package: other tests in this suite
        #: DO assert this wording, and a wider selection would catch it. The verdict is always
        #: relative to `--test`, which is why the harness prints the selector with it.
        #:
        #: AND THIS CASE EXISTS BECAUSE A VERDICT NEVER PRODUCED IS A VERDICT NEVER MEASURED. Every
        #: other case here ends CAUGHT or VOID; without this one the harness could be structurally
        #: incapable of reporting SURVIVED -- the shape this project refuses in a check that cannot
        #: fail -- and the selfcheck would still have printed all-ok.
        return _substitute(
            "in-flight marker(s) from ",
            "in-flight marker(s) found under ",
        )
    if kind == "tracked-but-unrun":
        #: A REACHABLE STATEMENT IN A MODULE THIS SELECTION NEVER TOUCHES, and it has to be
        #: reachable: coverage.py DROPS UNREACHABLE CODE ENTIRELY. Measured -- a statement planted
        #: after a `return` inside `printable` appears in coverage's executed, missing AND excluded
        #: sets as absent from all three, so control 6 refuses it on the earlier clause and the
        #: `was EXECUTED` clause is unreachable that way. The only thing that reaches it is a line
        #: coverage TRACKS and this test does not RUN: `chain.py` has 215 such lines under the
        #: prune selection.
        #:
        #: AND THIS ANCHOR IS A LIST OF ONE, DELIBERATELY. Deriving a semantically valid mutation
        #: automatically is a different project; if this line drifts the selfcheck fails loudly
        #: with `--old occurs 0 times`, which is the correct failure for a control somebody owns.
        return _substitute(
            '    parts = re.findall(r"\\d+", version)[:3]',
            '    parts = re.findall(r"\\d+", version)[:4]',
        )
    if kind == "after-systemexit":
        #: INDENTED, INSIDE `if __name__ == "__main__":` AND AFTER THE `raise`. Syntactically
        #: present, in the AST, and never executed -- the plant that passed an AST dump and raised
        #: `NameError` if anything ever reached it. At COLUMN ZERO it is something else entirely:
        #: module-level code that runs on IMPORT. The first version of this line was, and it raised
        #: `ZeroDivisionError` during collection (`rc=2`), which is a different experiment wearing
        #: this one's name -- and the selfcheck's reason clause is what refused it.
        return _substitute(
            "    raise SystemExit(main())",
            "    raise SystemExit(main())\n    _bench_unreachable = 1 / 0",
        )
    raise Void(f"unknown selfcheck mutation {kind!r}")


def selfcheck(work: pathlib.Path, py: str) -> int:
    """Prove this file can still tell its verdicts apart. `ci.py bench` runs this.

    A HARNESS IS AN INSTRUMENT AND AN INSTRUMENT NEEDS A CONTROL. Every verdict below was
    reported wrongly at least once by a hand-run version of this experiment, which is why each
    is asserted here rather than described in the docstring: a docstring is not a contract until
    something checks it.
    """
    rows, bad = [], 0
    for label, test, target, kind, expect, because in _SELFCHECK:
        try:
            code = bench(
                test=test,
                target=target,
                mutate=_selfcheck_mutation(kind),
                work=work / re.sub(r"\W+", "-", label)[:40],
                py=py,
                label=label,
            )
            got, why = (CAUGHT if code == 0 else SURVIVED), ""
        except Void as exc:
            got, why = "VOID", str(exc)
        #: THE VERDICT AND ITS REASON, both. `expect` alone was not enough -- see the table.
        ok = got == expect and (not because or because in why)
        bad += not ok
        #: THE FULL REASON IS STORED. Truncating it here made the verdict read the whole string
        #: while the printout below read 96 characters of it, so a correct case printed `ok` AND
        #: `WRONG REASON` together -- a diagnostic contradicting its own verdict.
        rows.append((("ok" if ok else "WRONG"), expect, got, label, why, because))
    print("\n=== selfcheck ===")
    for state, expect, got, label, why, because in rows:
        print(f"  {state:<5} expect {expect:<8} got {got:<8} {label}")
        if why:
            print(f"        reason: {why.splitlines()[0][:96]}")
        if because and because not in why:
            print(f"        WRONG REASON — this case requires {because!r} and did not get it")
    if bad:
        print(
            f"\n{bad} of {len(rows)} selfcheck case(s) did not reproduce — this harness is not "
            f"measuring what it claims to measure, so no verdict it prints can be trusted"
        )
        return 2
    print(f"\nall {len(rows)} verdicts reproduce")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/bench.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mode", nargs="?", choices=["selfcheck"], help="run the harness's own controls"
    )
    parser.add_argument(
        "--test", help="a pytest `-k` expression selecting the guard under examination"
    )
    parser.add_argument("--revert", metavar="PATH", help="replace PATH with its content at --from")
    parser.add_argument("--from", dest="sha", help="the commit --revert takes PATH from")
    parser.add_argument("--edit", metavar="PATH", help="apply one unique substitution to PATH")
    parser.add_argument("--old", help="the exact text to replace; must occur EXACTLY once")
    parser.add_argument("--new", default="", help="what to replace it with")
    #: THE DEFAULT IS A TEMPORARY DIRECTORY AND NOT A PATH IN THIS REPOSITORY, for control 1.
    #: `RUNPROV_BENCH_ROOT` is how a machine with a fast disk points this somewhere better; this
    #: repository's audit workflow sets it to a directory BESIDE the repository, never inside.
    parser.add_argument(
        "--work", default=os.environ.get("RUNPROV_BENCH_ROOT"), help="where to build the trees"
    )
    parser.add_argument(
        "--python", default=sys.executable, help="the interpreter to run pytest with"
    )
    args = parser.parse_args(argv)

    work = (
        pathlib.Path(args.work)
        if args.work
        else pathlib.Path(tempfile.mkdtemp(prefix="runprov-bench-"))
    )
    #: INSIDE THE `try`, because a `Void` raised outside it escapes as an uncaught traceback
    #: exiting 1 -- which is this harness's SURVIVED code. The first version of this line sat
    #: ABOVE the `try` and reported a refused work root as a verdict about the guard.
    try:
        _refuse_a_work_root_inside_the_repo(work)
        work.mkdir(parents=True, exist_ok=True)
    except Void as exc:
        print(f"\nVOID — this run measured NOTHING:\n  {exc}", file=sys.stderr)
        return 2
    #: A RELATIVE `--python` IS RESOLVED HERE, BEFORE ANY cwd CHANGES. Every subprocess below
    #: runs with `cwd` set to a bench tree, so `--python .venv/bin/python` -- the spelling a
    #: reader naturally types from the repository root -- resolved against the TREE and raised
    #: `FileNotFoundError`. Measured while writing this file, and it would otherwise have read
    #: as "the copy will not import". A bare name is left alone so `--python python3.11` still
    #: means "find it on PATH".
    py = args.python
    if os.sep in py or (os.altsep and os.altsep in py):
        #: `absolute()`, NEVER `resolve()`, AND THIS COST AN HOUR. A virtualenv's `bin/python` is
        #: a SYMLINK to the base interpreter, and `resolve()` follows it -- which throws the
        #: virtualenv away, because the venv is found from the path you INVOKE, not from the file
        #: you land on. The first version of this line resolved `.venv/bin/python` to the mise
        #: interpreter, whose site-packages has `pytest` but not `pytest-cov`, so:
        #:
        #:   * the BASELINE still ran and still reported GREEN -- `pytest` alone is enough for it;
        #:   * only the coverage run failed, as `unrecognized arguments: --cov`.
        #:
        #: So a harness without control 6 would have printed a CAUGHT or SURVIVED verdict measured
        #: BY THE WRONG INTERPRETER, with a green baseline agreeing. Control 6 caught it as a side
        #: effect of needing coverage, which is luck, so the guard is here as well: the resolved
        #: interpreter is printed with every command below.
        py = str(pathlib.Path(py).absolute())
    try:
        if args.mode == "selfcheck":
            return selfcheck(work, py)
        if not args.test:
            parser.error("--test is required: name the guard whose break you are measuring")
        if args.revert and args.edit:
            parser.error("--revert and --edit are two different experiments; run them separately")
        if args.revert:
            if not args.sha:
                parser.error("--revert needs --from SHA")
            target, mutate = args.revert, _revert(args.revert, args.sha)
            label = f"revert {args.revert} to {args.sha}"
        elif args.edit:
            if args.old is None:
                parser.error("--edit needs --old")
            target, mutate = args.edit, _substitute(args.old, args.new)
            label = f"edit {args.edit}"
        else:
            parser.error("say what to break: --revert PATH --from SHA, or --edit PATH --old ...")
        return bench(test=args.test, target=target, mutate=mutate, work=work, py=py, label=label)
    except Void as exc:
        print(f"\nVOID — this run measured NOTHING:\n  {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
