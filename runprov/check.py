# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Which entry points in this project do file I/O and record nothing. ADR-0011, T-27.

THE CASE THE RUNTIME CHECK CANNOT REACH. `watch.py` sees a script that creates a `Run` and
then opens files without registering them. It can never see a script that does not import
`runprov` at all — code that is not imported does not run — and it says so in its own
docstring rather than implying otherwise. That case is answerable only from the source.

WHAT IT DELIBERATELY DOES NOT DO. It does not look for unregistered opens inside files that
already use the package. `watch.py` does that at runtime with information a parser cannot
have, and trying it statically produces noise: measured on `examples/format_compatibility.py`,
22 "unwrapped opens" that are all read-backs — `PIL.Image.open(p)` on an artifact just written
through `run.open_output`. Distinguishing those needs alias analysis on arbitrary Python;
`watch.py` gets it free because the path is already in `record["outputs"]`.

THE THREE RULES, EACH FROM A MEASUREMENT ON REAL PIPELINES (ADR-0011):

1. **Scope is derived**, never listed: under the root, not vendored, not this package.
   Without the last clause the first prototype flagged seven of runprov's own modules — a
   library that opens files, obviously.
2. **The subject is an ENTRY POINT**, found from the source rather than from a path
   convention. A library module that opens a file is not analysis code; it is called BY
   analysis code. 59 flagged files became 30 on a real repository.
3. **Reaching runprov is resolved TRANSITIVELY** through the project's own modules. This is
   the rule that would have made the check useless while looking correct: `hcv-acquisition`
   has 3 files importing runprov directly and 96 reaching it through its own `provenance.py`
   wrapper, and wrapping the library is the normal way a platform adopts one. 30 became 2.

WHAT IT CANNOT SEE, stated here so no caller has to infer it: a file that opens nothing
directly and calls a library that does; `getattr(builtins, "open")`, `importlib`, `exec` of a
string; anything outside the root. Its answer is *"these files record nothing"* and never
*"everything else is recorded"* — no static check can make the second claim, and one that
implied it would be the defect this package exists to catch.
"""

from __future__ import annotations

__all__: list[str] = []

import ast
import collections
import pathlib
import typing

#: Directories whose code is somebody else's. Shared intent with `observe.VENDORED`, kept
#: separate because this one also excludes build output, which a running process never has.
VENDORED = ("site-packages", "dist-packages", ".venv", "node_modules", ".git", "build", "dist")

#: Calls that open a path, matched on the CALL NAME so `pd.read_csv` and `open` are both
#: caught without a list of every library function that exists. It is a sample, not a
#: universe — which is why a clean result is reported as "nothing found", never as proof.
OPENERS = frozenset(
    {
        "open",
        "read_csv",
        "read_table",
        "read_parquet",
        "read_excel",
        "read_json",
        "read_fwf",
        "to_csv",
        "to_parquet",
        "to_excel",
        "savetxt",
        "loadtxt",
        "genfromtxt",
    }
)


def called_name(call: ast.Call) -> str | None:
    """`open(...)` -> "open"; `pd.read_csv(...)` -> "read_csv"; anything else -> None."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def is_entry_point(tree: ast.AST) -> bool:
    """Does this module run when executed — `if __name__ == "__main__":`?

    DERIVED FROM THE SOURCE, not from a directory name. `scripts/`, `bin/` and `src/` layouts
    all disagree between projects, and the three repositories this was measured on use two of
    them; the `__name__` guard is the same in all of them.
    """
    return any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in ast.walk(tree)
    )


def imported_names(tree: ast.AST) -> set[str]:
    """Every name this module imports, including the last component of a dotted path.

    Deliberately over-inclusive. A module graph built by resolving packages exactly would be
    right more often and wrong catastrophically: a resolution failure DROPS an edge, and a
    dropped edge reports a recording script as unrecorded. Over-inclusion errs the other way
    — towards saying nothing — which is the direction a build gate must fail in.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
                out.add(alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
                out.add(node.module.rsplit(".", 1)[-1])
            out.update(alias.name for alias in node.names)
    return out


def reaches_runprov(
    imports: dict[pathlib.Path, set[str]], package: str = "runprov"
) -> set[pathlib.Path]:
    """Every file that imports `package`, directly or through the project's own modules.

    THE RULE THAT DECIDES WHETHER THIS CHECK IS USABLE. A platform adopts a library by
    wrapping it: measured on `hcv-acquisition`, 3 files import runprov and 96 reach it through
    one internal `provenance.py`. Asking only about direct imports reports all 28 of its
    recording entry points as unrecorded.

    A fixed point rather than recursion: the import graph of real code has cycles, and a
    depth-first walk over one either needs its own visited set or does not terminate.
    """
    by_stem: dict[str, list[pathlib.Path]] = collections.defaultdict(list)
    for path in imports:
        by_stem[path.stem].append(path)

    found = {path for path, names in imports.items() if package in names}
    growing = True
    while growing:
        growing = False
        for path, names in imports.items():
            if path in found:
                continue
            if any(other in found for name in names for other in by_stem.get(name, ())):
                found.add(path)
                growing = True
    return found


class Report(typing.NamedTuple):
    """What the sweep found, and what it examined to find it.

    `examined` is not decoration. A sweep that parsed nothing reports the same empty
    `flagged` as a clean project, and this package exists because those two produce the same
    green. The caller prints it, always.
    """

    examined: int
    entry_points: int
    flagged: list[pathlib.Path]
    unparseable: list[pathlib.Path]

    @property
    def ok(self) -> bool:
        """Nothing to report — and nothing that went unread while looking clean."""
        return not self.flagged and not self.unparseable


def sources(root: pathlib.Path, own: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every Python file under `root` that is this project's own. Scope, derived."""
    mine = own if own is not None else pathlib.Path(__file__).resolve().parent
    out = []
    for path in sorted(root.rglob("*.py")):
        if any(part in VENDORED for part in path.parts):
            continue
        # THE PACKAGE IS NEVER THE SUBJECT. In a user's project runprov lives in the
        # virtualenv and VENDORED covers it; in a checkout it sits under the root, and the
        # first prototype flagged seven of its modules for opening files, which is its job.
        if path.resolve().is_relative_to(mine):
            continue
        out.append(path)
    return out


def scan(root: pathlib.Path, own: pathlib.Path | None = None) -> Report:
    """Sweep `root` and report the entry points that do file I/O and reach no runprov."""
    trees: dict[pathlib.Path, ast.AST] = {}
    unparseable: list[pathlib.Path] = []
    for path in sources(root, own):
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            # REPORTED, NEVER SKIPPED. A file that could not be read was not checked, and
            # folding "not checked" into "clean" is the whole failure this guards against.
            unparseable.append(path)

    imports = {path: imported_names(tree) for path, tree in trees.items()}
    recording = reaches_runprov(imports)

    flagged, entry_points = [], 0
    for path, tree in trees.items():
        if not is_entry_point(tree):
            continue
        entry_points += 1
        if path in recording:
            continue
        if any(
            isinstance(node, ast.Call) and called_name(node) in OPENERS for node in ast.walk(tree)
        ):
            flagged.append(path)
    return Report(len(trees), entry_points, sorted(flagged), sorted(unparseable))


def render(report: Report, root: pathlib.Path) -> list[str]:
    """The report as lines. Separate from printing so the wording itself is testable."""
    lines = [
        f"checked {report.examined} Python file(s) under {root}, "
        f"{report.entry_points} of them entry points"
    ]
    if report.unparseable:
        lines.append(f"{len(report.unparseable)} file(s) could NOT be parsed, so NOT checked:")
        lines += [f"    {p}" for p in report.unparseable]
    if report.flagged:
        lines.append(
            f"{len(report.flagged)} entry point(s) open files and reach no runprov, so nothing "
            f"they read or wrote is recorded:"
        )
        lines += [f"    {p}" for p in report.flagged]
        lines.append("Record them with `run.input(path)` and `run.open_output(path)`.")
    elif not report.unparseable:
        # WHAT WAS LOOKED AT, not just what was found. "no entry point records nothing" and
        # "no entry point was examined" are the same sentence without the count above.
        lines.append("no entry point opens files without recording them")
    return lines
