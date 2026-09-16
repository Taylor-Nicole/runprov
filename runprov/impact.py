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

    @property
    def artifacts(self) -> int:
        return sum(len(s.outputs) for s in self.steps)


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
    if not chain.steps:
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
    out.append("    any script that never imported runprov — `runprov check` finds those")
    out.append("    any run whose records were pruned, or written to another history")
    return out
