# runprov

**Records what a Python script actually read, wrote and ran — and writes that record into the
result file itself.**

A hand-maintained log describes what an author believed they did. It cannot describe what the
program did. `runprov` records the reads and writes as they happen, and writes the record in
two forms that need no software to read: a YAML log whose digests are in `sha256sum`'s own
format, and a comment header inside the artifact.

One command, `runprov verify`, re-derives every recorded digest and reports whether a result
still follows from the inputs it was made from — or has itself been edited — **reading only
the file**. No database, no history, no network.

- **Zero runtime dependencies.** Python 3.10+.
- **Observed, not declared.** A CPython audit hook sees every `open`, including those made by
  a library nobody thought to instrument; unregistered reads are recorded rather than ignored.
- **The record does not depend on the tool that wrote it.** With the package uninstalled,
  `cat` reads the history, `grep` finds every run that touched a file, and `sha256sum -c`
  verifies the digests.

```
pip install runprov
```

## Adoption is one call

`run.input(p)` **returns the path it was given**, so it is written on the way to the `open`
that was already there:

```python
from runprov import Run, configure

configure(root=".")

with Run("summarise", {"threshold": 5}) as run:
    with open(run.input("data/measurements.tsv"), encoding="utf-8") as fh:
        rows = fh.read().splitlines()
    with run.open_output("results/summary.tsv") as out:
        out.write(f"n\t{len(rows)}\n")
    run.note("rows_read", len(rows))
```

The artifact then carries its own provenance, as a comment block above its first line:

```
# provenance — this artifact and what produced it
#   script     : summarise
#   commit     : b02d309
#   body       : d8c46f515971fb75
#   inputs (1), content digest:
#     17ffe7054ecf0cc8  data/measurements.tsv
```

## Checking it later

```
$ runprov verify results/summary.tsv
OK      results/summary.tsv

# after one line is appended to the input:
$ runprov verify results/summary.tsv
STALE   results/summary.tsv
        STALE  data/measurements.tsv
        (pinned 17ffe7054ecf0cc8, now a3d2c39b016f6a7e)
```

`OK`, `STALE`, `GONE` and `ALTERED` are the four verdicts. Also available:
`runprov log` (the run history as YAML or a table), `runprov show` (what one run did),
`runprov capture` (record a script that has no `runprov` calls in it at all), and
`runprov export` (RO-Crate and W3C PROV-JSON).

## What it is not

It records; it does not audit — it cannot tell you that a registered read was the read that
*mattered*. It captures no intra-function dataflow, which noWorkflow does by instrumenting the
AST, at the cost of changing the program it observes.

**It identifies versions; it does not store them.** A digest says an input changed, and says
which version a result was built from — it cannot give those bytes back. That is DVC's job,
and the two compose: DVC stores content, `runprov` records which content a run actually read.

Provenance written into a scientific artifact is not new either: SAM/BAM `@PG` has carried
program, version and command line for over a decade. What is narrower and true here is *a
format-agnostic pin carrying the content digest of every input, plus a checker that re-derives
them from the artifact alone.*

## Documentation

The full documentation is one long README in the repository — the complete API, every
supported format, the failure each design decision came from, and what has actually been run:

**<https://github.com/Taylor-Nicole/runprov>**

| | |
|---|---|
| Why it exists, and how it compares | [`WHY.md`](https://github.com/Taylor-Nicole/runprov/blob/main/WHY.md) |
| Design decisions | [`docs/adr/`](https://github.com/Taylor-Nicole/runprov/tree/main/docs/adr) |
| Changes | [`CHANGELOG.md`](https://github.com/Taylor-Nicole/runprov/blob/main/CHANGELOG.md) |
| Security policy | [`SECURITY.md`](https://github.com/Taylor-Nicole/runprov/blob/main/SECURITY.md) |

## Licence

BSD 3-Clause. Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
Hôpital Henri-Mondor, and Taylor Thompson.
