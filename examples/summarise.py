# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""A COMPLETE, RUNNABLE script — the whole shape, with nothing left to assemble.

    $ pip install runprov
    $ python examples/summarise.py
    $ python -m runprov log
    $ python -m runprov verify results/

Every example in the README is a fragment, because each one is making a point about one
decision. This file exists because a fragment cannot be run, and a reader who has never
used the package needs to see the parts fit together before the reasoning means anything.

It uses only the standard library, so it runs anywhere the package does.

THE FOUR THINGS THAT MATTER, and they are the only four:

1. `configure(root=...)` once, near the top.
2. `with Run(name, params, provenance=PATH) as run:` — `provenance=` on the CONSTRUCTOR is
   what makes a crash record. Without it, `__exit__` has nowhere to write and a run that
   dies halfway leaves nothing.
3. `run.input(p)` returns `p`, so registering a read IS how you open it.
4. `run.open_output(p)` registers the write, embeds the provenance pin, and forces UTF-8 —
   the three things that are easy to remember separately and easy to forget one of.
"""

import argparse
import csv
import pathlib

from runprov import Run, configure

# The project root. Everything in the record is named relative to this, which is what makes
# a record from your machine comparable with a record from someone else's.
ROOT = pathlib.Path(__file__).resolve().parent.parent

# NO `run_log=` HERE, deliberately. It defaults to `<root>/provenance/runs.jsonl`, which is
# where `python -m runprov log` looks when you do not pass `--log`. Point it somewhere else
# and both halves still work, but every CLI call then needs `--log that/path` -- so leave it
# alone until you have a reason, and the tools agree with each other for free.
configure(root=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="examples/data/measurements.tsv")
    parser.add_argument("--output", default="examples/results/summary.tsv")
    parser.add_argument("--threshold", type=int, default=5)
    args = parser.parse_args()

    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)

    # `vars(args)` records what argparse PARSED, with its types intact -- so the record
    # says `threshold: 5`, not `"5"`, and not a sentence somebody typed about it.
    with Run("summarise", vars(args), provenance=out.with_name("summary.prov.json")) as run:
        # `run.input(...)` RETURNS THE PATH, so this is just how you open the file. A read
        # that skips registration is then a visible omission rather than a silent one.
        with open(run.input(ROOT / args.input), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))

        kept = [r for r in rows if int(r["value"]) >= args.threshold]

        # Registers the output, writes the pin into the file, and forces UTF-8. Doing it by
        # hand is `open(run.output(p), "w", encoding="utf-8")` then `fh.write(run.header())`.
        with run.open_output(out) as fh:
            writer = csv.DictWriter(fh, fieldnames=["sample", "value"], delimiter="\t")
            writer.writeheader()
            writer.writerows(kept)

        # Whatever a reader would want to know that the files do not say themselves.
        #
        # `columns` is a CONVENTION worth adopting rather than a feature. The digest says
        # this file changed; it cannot say that a column appeared, and runprov will not open
        # your artifact to find out -- guessing at your schema is how a tool ends up wrong
        # about it. Recording the shape you wrote makes "when did that column arrive" a
        # question `runprov show summarise` answers, dated, without opening anything.
        run.note("columns", ["sample", "value"])
        run.note("rows_read", len(rows))
        run.note("rows_kept", len(kept))

    # AFTER the block: `__exit__` is where outputs are hashed and the status becomes known,
    # so nothing here needs to write the record and nothing should try.
    print(f"kept {len(kept)} of {len(rows)} rows -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
