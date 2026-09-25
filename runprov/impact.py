# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""What was derived from these bytes, and in what order it rebuilds. ADR-0015, T-31.

THE QUESTION. The reference genome is updated. There are seven pipelines and 903 463 data
files. What is now invalid, and what has to be rebuilt first? `verify` answers it per artifact
— but only for artifacts you already thought to name, which is the hard part — and `lineage`
holds the DAG and walks it BACKWARDS.

IT IS ASKED ABOUT THE FUTURE AND CAN ONLY ANSWER ABOUT THE PAST, and that gap is the whole of
this module's honesty. *"What will break if I change this"* is not answerable from a history.
What is answerable is *"which recorded artifacts were derived, transitively, from these
bytes"*, and the difference between them is exactly what this package cannot see:

* a script that never imported `runprov` — `runprov check` finds those;
* a read that bypassed registration inside a run that did record;
* a run whose records were pruned, or that wrote to another history;
* a pipeline that WILL read the file tomorrow and never has.

So an empty result is reported as **"no recorded run read this"**, never as "nothing depends on
this". The second would be a green light to overwrite a reference.

CONNECTIVITY COMES FROM `_lineage`, NOT FROM A SECOND WALK. The digest rule (index on
`content_sha256`, `sha256` AND `sha256_tree`, because preferring one and falling back compares
two different keys and invents orphans) and the address rule both live there, nested. Rather
than copy either, `_lineage` fills two out-parameters during the passes it already makes — the
convention `bad` and `scripts` already follow — and everything here is a walk over what it
produced. Two traversals of one history that disagree about what is connected is a defect this
project keeps finding.
"""

from __future__ import annotations

__all__: list[str] = []

import pathlib
import typing


class Step(typing.NamedTuple):
    """One run in the chain, and how far it is from the file that changed."""

    depth: int
    address: str
    script: str
    outputs: list[str]


class Chain(typing.NamedTuple):
    """What derives from a digest, and the size of what could not be seen."""

    digest: str
    seeds: list[str]
    steps: list[Step]
    unregistered: int
    runs_examined: int
    #: C-06 of Audit C, corrected by D-07 of Audit D. The SUM over the runs examined of each
    #: run's distinct-dropped-path count — which is a count of (run, path) drop events, and is
    #: NOT a count of distinct paths across the history and NOT a floor on one. A hundred runs
    #: that each dropped the same 2 000 system paths sum to 200 000 for 2 000 files, and the
    #: page said "at least 200 000 path(s) were dropped": the same overstatement C-06 removed
    #: per-run, reintroduced by the reader written for it. The number is fine; the sentence
    #: about it was wrong, and the name says what it is now.
    #:
    #: DEFAULTED, because `Chain` is constructed positionally in `__main__` and in a dozen
    #: tests, and a required sixth field would have made adding the blind spot a breaking
    #: change nobody made.
    watch_drops: int = 0

    @property
    def artifacts(self) -> int:
        return sum(len(s.outputs) for s in self.steps)

    @property
    def truncated(self) -> bool:
        """Runs read these bytes and the walk stopped before reaching any of them.

        [ADR-0017 R-1]. DERIVED ONCE, HERE, because it was derived twice before: `render`
        asked `not chain.steps and chain.seeds` to choose its sentence and `_impact` asked
        `chain.seeds and not chain.steps` to choose its exit code — the same predicate, written
        with the operands in the opposite order, in two files. They agreed, and nothing held
        them together; a payload re-deriving it would have been the third copy.

        THE DISTINCTION IT CARRIES IS A-09 AND C-07, WHICH WERE THE SAME DEFECT TWICE. An empty
        `steps` means *no recorded run read these bytes* when `seeds` is empty too, and means
        *the answer was cut off before it could be given* when it is not. A-09 fixed only the
        sentence and left the exit code returning 0 for both, so
        `runprov impact ref.fa --depth 0 || abort` went green over a file with three derived
        artifacts in the history. A consumer re-deriving a verdict from `steps` alone gets that
        same wrong answer, which is why this travels in the payload rather than being inferred
        from it.
        """
        return bool(self.seeds) and not self.steps


#: R-5. The payload's own version, named HERE rather than in `__main__`, because it names THIS
#: module's answer and `--format json` is only one rendering of it. `diff.SCHEMA`, `chain.SCHEMA`
#: and `report.SCHEMA` state the same argument.
SCHEMA = "runprov.impact.v1"


def _plain(value: typing.Any) -> typing.Any:  # noqa: ANN401 - whatever the chain holds
    """A `Step` becomes an object with the field names it already has. R-9."""
    if isinstance(value, Step):
        return dict(zip(Step._fields, value, strict=True))
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def payload(chain: Chain) -> dict[str, typing.Any]:
    """The chain as one object, for a reader that is not a person. R-1, R-5, R-7, R-9, R-14.

    DERIVED FROM `_asdict()`, never listed. A field added to `Chain` reaches this without anyone
    remembering to add it, which is the only reason the two renderings cannot drift apart. The
    thing to guard is not a new field — that is free — it is somebody reinstating a hand-written
    list, which is the defect this package has now found a dozen times.

    THE BLIND SPOTS ARE ALWAYS HERE, AND THIS IS WHERE THE TWO RENDERINGS DIFFER. The page prints
    the `unregistered` and `watch_drops` lines only when they are non-zero, because a reader does
    not need to be told that nothing was missed. A consumer does: `unregistered: 0` is *looked,
    and found none*, and a key that simply was not there would be indistinguishable from *this
    version did not look*. So the page is the one that says less, in the safe direction, and the
    asymmetry is asserted as a set rather than left to be found.

    NOTHING IS ABSENT, and that is a fact about `impact` rather than a shortcut. Every run in the
    history is examined on every call, so all three of `runs_examined`, `unregistered` and
    `watch_drops` are always integers. R-8's other reading — a key missing because this version
    did not look — has nowhere to arise here.

    `truncated` TRAVELS RATHER THAN BEING INFERRED. A consumer deriving a verdict from `steps`
    alone reads an empty list as *nothing derives from these bytes*, which over a truncated walk
    is C-07's exit 0 reproduced in a second place — the green light to overwrite a reference that
    three recorded artifacts depend on.

    PATHS ARE AS RECORDED, NOT AS DISPLAYED. `shorten` makes a list of several hundred readable
    by trimming the project root off, which is right for a page and wrong for a payload: the
    record holds the full path, R-9 says a consumer sees the record's own names, and a relative
    path is meaningless to a reader that does not know the root it was taken from.
    """
    return {
        "schema": SCHEMA,
        **{name: _plain(value) for name, value in chain._asdict().items()},
        "artifacts": chain.artifacts,
        "truncated": chain.truncated,
    }


def walk(
    digest: str,
    consumers: typing.Mapping[str, list[str]],
    edges: typing.Iterable[tuple[str, str]],
    scripts: typing.Mapping[str, str],
    outputs_by: typing.Mapping[str, list[str]],
    max_depth: int | None = None,
) -> list[Step]:
    """Every run reachable forward from the runs that read `digest`, breadth first.

    BREADTH FIRST AND BY DEPTH, because the answer is an ORDER, not a set. A list of nine
    filenames does not say which to rebuild first, and rebuilding out of order means doing it
    twice.

    A CYCLE CANNOT LOOP THIS. A build graph should be acyclic, but paths are rewritten and a
    history spans years; `seen` is what makes the traversal total rather than an assumption
    about somebody else's pipeline being well-formed.
    """
    forward: dict[str, list[str]] = {}
    for producer, consumer in edges:
        forward.setdefault(producer, []).append(consumer)

    steps: list[Step] = []
    seen = set(consumers.get(digest, ()))
    frontier = [(1, address) for address in consumers.get(digest, ())]
    while frontier:
        depth, address = frontier.pop(0)
        if max_depth is not None and depth > max_depth:
            continue
        steps.append(
            Step(depth, address, str(scripts.get(address, "?")), list(outputs_by.get(address, ())))
        )
        for nxt in forward.get(address, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((depth + 1, nxt))
    return steps


def shorten(name: str, root: pathlib.Path | None) -> str:
    """A path relative to the project root, when it is under it.

    NOT COSMETIC AT THIS SCALE. Measured on a real 3 536-line history: one reference file had
    294 readers and 697 derived artifacts, every line an absolute path 90 characters long
    before the part that identifies it. `show` prints paths as recorded, which is right for a
    page about one run; a list of several hundred is a different problem, and an answer nobody
    can read is an answer nobody uses.
    """
    if root is None:
        return name
    try:
        # `.as_posix()`, NOT `str()`. Audit B follow-on: the record stores every path
        # `_posix`-normalised, so a display that used the native separator disagreed with the
        # thing it was displaying — `data\\ref.fa` on Windows for a record holding
        # `data/ref.fa`. Found by the Windows leg, where my test had asserted the POSIX form
        # and the code produced the native one; the test was right about what it wanted.
        return pathlib.Path(name).relative_to(root).as_posix()
    except ValueError:  # outside the root: the absolute path IS the useful name
        return name


def render(chain: Chain, root: pathlib.Path | None = None) -> list[str]:
    """The chain, and — every time, not only when empty — what this could not see."""
    out = [f"{chain.digest[:16]}", ""]
    if chain.truncated:
        # A-09. `--depth 0` truncated the walk to nothing while `seeds` was non-empty, and the
        # page then printed the ONE sentence this module's docstring says must never be false.
        # A truncation that reads as an absence is the defect this command exists to avoid,
        # arriving through an option added for convenience.
        out.append(
            f"  {len(chain.seeds)} recorded run(s) read these bytes, and the walk was "
            "TRUNCATED before any of them — raise --depth to see what derives from it."
        )
    elif not chain.steps:
        # NOT "nothing depends on this". The distinction is the whole point: one is a fact
        # about the history, the other is a claim about the world, and only the first is true.
        out.append("  no recorded run read these bytes.")
    else:
        out.append(
            f"  {len(chain.seeds)} recorded run(s) read these bytes, and "
            f"{chain.artifacts} artifact(s) derive from them:"
        )
        out.append("")
        for step in chain.steps:
            head = f"  {step.depth}{'  ' * step.depth}{step.script}"
            if not step.outputs:
                out.append(f"{head}  (wrote nothing recorded)")
            out += [f"{head} → {shorten(name, root)}" for name in step.outputs]
        out += ["", "  Rebuild in that order; `runprov verify <artifact>` confirms each one."]

    out += ["", "  NOT SEEN BY THIS QUERY:"]
    if chain.unregistered:
        out.append(
            f"    {chain.unregistered} read(s) bypassed registration in the "
            f"{chain.runs_examined} run(s) examined — those files are not in any pin"
        )
    if chain.watch_drops:
        # C-06. `unregistered` above counts reads the runs REPORTED; this counts the ones they
        # could not report, and the two are different blind spots. Without it a history whose
        # runs all hit the cap prints a NOT SEEN block that omits the largest thing not seen.
        #
        # D-07: THE SENTENCE NOW MATCHES THE QUANTITY. It said "at least N path(s) were
        # dropped", which a sum over runs is not — a hundred runs dropping the same 2 000
        # system paths would have printed "at least 200 000", for 2 000 files. It is a count of
        # DROP EVENTS, the number of runs is already on the line, and a reader can see that the
        # per-run figure is what is bounded. Overstating a blind spot is not a false green, but
        # it is the exact class of claim C-06 was filed to remove.
        out.append(
            f"    {chain.watch_drops} read(s) were dropped by the watch itself across the "
            f"{chain.runs_examined} run(s) examined — those reads were never reported, and "
            f"the paths behind them may repeat between runs"
        )
    out.append("    any script that never imported runprov — `runprov check` finds those")
    out.append("    any run whose records were pruned, or written to another history")
    return out
