# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""The fixed scenario, run ONCE PER RELEASED VERSION inside that version's own virtualenv.

WHAT THIS FILE IS FOR. Every other test in this project builds a record with the code that
reads it. That cannot detect the one failure a provenance package must never have: **a record
written last year that this year's code reads differently, or refuses.** Audit C found exactly
that (C-03: a fix meant to make `sha256_tree` machine-independent instead CHANGED it, so every
directory input pinned before it verified STALE with nothing on disk touched) and the suite was
green through all of it, because the suite had no record older than itself.

So: install 0.1.0, run this, keep the tree. Install 0.2.0, run this, keep the tree. The result
is a corpus of real records produced by real released wheels, and the gate is that TODAY's
package still reads every one of them and still agrees with every digest in them.

IT RUNS UNDER EVERY VERSION FROM 0.1.0 ONWARD, which is the whole constraint on how it is
written. The API has grown — `@run.step` arrived in 0.2.0, `env_snapshot_dir` and several
other `Project` fields arrived later — so **every optional call is capability-detected and
every skip is RECORDED**. A capability that was silently skipped and one that was exercised
and passed produce the same green, which is the defect class this corpus exists to close; so
`scenario.json` names what ran and what did not, and the tests assert against that rather than
against a hand-maintained table of which version could do what.

DETERMINISM IS THE POINT. Every byte of input is a literal below, so the digests a 0.1.0 wheel
recorded are comparable to the ones a 0.4.0 wheel recorded, and any difference between them is
a finding rather than noise. Nothing here reads the clock, the network, or a random source.

PATHS ARE RELATIVE, DELIBERATELY. Measured before this was written: with `root="."` and
relative arguments, the only absolute values that reach a record are `cwd` and `command` —
the artifacts themselves carry relative input names and therefore RELOCATE WITHOUT THEIR
DIGESTS MOVING. That is what makes `runprov verify` a meaningful oracle against a tree
captured on another machine a year ago; if the artifacts had to be rewritten to be moved, the
one thing worth checking would have been destroyed by the checking.

Usage (normally through `tools/corpus.py generate`, not by hand):

    python tools/corpus_scenario.py <root-directory>
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

import runprov

#: Every byte the scenario reads, as literals. A generated or downloaded input would make the
#: recorded digests depend on the day the corpus was built, which is the one thing it may not
#: depend on: the corpus is a MEASUREMENT of what each version wrote, and a measurement whose
#: subject moves measures nothing. `data/refs` is a DIRECTORY on purpose — it is the only way
#: to exercise `sha256_tree`, and `sha256_tree` is the digest that has actually moved once.
INPUTS = {
    "data/m.tsv": "sample\tdepth\nA\t12\nB\t7\nC\t31\n",
    "data/lookup.csv": "code,label\n1,alpha\n2,beta\n",
    "data/refs/ref.fa": ">ref\nACGTACGTACGT\n",
    "data/refs/ref.fa.fai": "ref\t12\t5\t12\t13\n",
    # THESE THREE NAMES ARE THE WHOLE POINT OF THE DIRECTORY, and they are chosen rather than
    # illustrative. C-03 was a directory digest that moved because the sort key changed from
    # the RENDERED STRING to the path's PARTS, and the two orders differ only where a
    # separator competes with a character that sorts near it: `/` is 0x2f, `.` is 0x2e and
    # `-` is 0x2d. Flat names cannot show it — `ref.fa` and `ref.fa.fai` sort identically
    # under both keys, so a corpus built from those alone would LOOK like it covered the
    # defect it was built for and would not. Measured, not reasoned:
    #
    #   as strings : panel-v2/a.fa  <  panel.old/a.fa  <  panel/a.fa
    #   as parts   : panel/a.fa     <  panel-v2/a.fa   <  panel.old/a.fa
    #
    # Completely reversed, so any change to the sort key changes `sha256_tree` and every
    # artifact pinning this directory goes STALE — which is exactly the alarm C-03 needed and
    # did not have. Verified as a positive control: reinstating the old sort turns all twelve
    # corpus artifacts STALE.
    "data/refs/panel/a.fa": ">p1\nAAAA\n",
    "data/refs/panel.old/a.fa": ">p2\nCCCC\n",
    "data/refs/panel-v2/a.fa": ">p3\nGGGG\n",
    # AND MIXED CASE, which the first version of this fixture did not have — D-11 of Audit D.
    # The three names above were chosen so that `/` competes with `.` and `-`, and every one
    # of them is lowercase, so the OTHER way a tree order can move was invisible here:
    # `PurePath.__lt__` compares a CASE-FOLDED key on Windows and only there, so `README.md`
    # and `data/` order one way on that platform and another on POSIX. The corpus was built to
    # catch a digest that moves between versions and could not have caught the one that
    # actually had (D-08), because folded and unfolded coincide for every all-lowercase name.
    #
    # Measured on a real project tree: 20 % of directories order differently under the two
    # keys. With these two entries this fixture is one of them, so a Windows corpus leg now
    # re-derives a digest that a case-folded sort would not produce.
    "data/refs/README.md": "# reference panel\n",
    "data/refs/Panel-QC/report.tsv": "metric\tvalue\npass\t1\n",
}

#: The chain, three deep, so `lineage`, `impact` and `report` have a real DAG to walk rather
#: than one isolated run. Each step reads the previous step's artifact, which is what makes
#: the join-on-digest connectivity testable at all.
CHAIN = ("align", "genotype", "summarise")


def _fields(cls: type) -> set[str]:
    """The settings this version's `Project` actually has. DERIVED, never listed.

    `Project` gained fields in almost every release. A hand-written per-version table would be
    the scope pattern that this project has now shipped seven times: written correct, then
    outgrown, then silently wrong. Asking the class is correct forever.
    """
    return {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1]).resolve()
    (root / "out").mkdir(parents=True, exist_ok=True)
    for name, text in INPUTS.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    import os

    os.chdir(root)

    wanted = {
        "root": ".",
        "run_log": "prov/history.jsonl",
        "env_snapshot_dir": "prov/env",
        "auto_steps": "off",  # an observed-call census is interpreter-dependent, not a record
        "progress": False,
    }
    have = _fields(runprov.Project)
    used = {k: v for k, v in wanted.items() if k in have}
    skipped = sorted(set(wanted) - set(used))
    runprov.configure(**used)

    capabilities: dict[str, object] = {
        "runprov": runprov.__version__,
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "project_settings_used": sorted(used),
        "project_settings_unavailable": skipped,
    }

    previous: str | None = None
    for depth, script in enumerate(CHAIN):
        prov = f"prov/{script}.prov.json"
        with runprov.Run(script, {"stage": depth}, provenance=prov) as run:
            if previous is None:
                # THE DIRECTORY INPUT, and it is first because it is the reason this exists.
                run.input("data/refs")
                with open(run.input("data/m.tsv"), encoding="utf-8") as fh:
                    rows = fh.read().splitlines()[1:]
                # UNREGISTERED ON PURPOSE. `unregistered_reads` is a field readers use to
                # decide whether an absence is evidence, so a corpus in which it is always
                # empty cannot show that the field survived a version change.
                with open("data/lookup.csv", encoding="utf-8") as fh:
                    fh.read()
            else:
                with open(run.input(previous), encoding="utf-8") as fh:
                    rows = [x for x in fh.read().splitlines() if not x.startswith("#")][1:]

            if hasattr(run, "step"):  # 0.2.0 and later
                capabilities["step"] = True

                @run.step
                def tally(rows: list[str], factor: int = 1) -> int:
                    return len(rows) * factor

                count = tally(rows, factor=1)
            else:
                capabilities["step"] = False
                count = len(rows)

            run.note("rows_seen", count)
            previous = f"out/{script}.tsv"
            with run.open_output(previous) as out:
                out.write("stage\trows\n")
                out.write(f"{script}\t{count}\n")

    produced = sorted(str(p.relative_to(root).as_posix()) for p in (root / "out").glob("*.tsv"))
    capabilities["artifacts"] = produced
    capabilities["chain"] = list(CHAIN)
    # ASSERTED HERE, WHERE IT CAN STILL FAIL LOUDLY. A scenario that produced nothing would be
    # captured as a valid empty corpus and every test over it would pass by examining nothing.
    if len(produced) != len(CHAIN):
        raise SystemExit(f"scenario produced {produced}, expected one artifact per {CHAIN}")
    (root / "scenario.json").write_text(
        json.dumps(capabilities, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
