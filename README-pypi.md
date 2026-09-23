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
that was already there. **`provenance=` is not optional**: it is what arms the record, and a
run without it writes nothing at all — including when the script crashes, which is when you
most want the record.

```python
from runprov import Run, configure

configure(root=".")

with Run("summarise", {"threshold": 5}, provenance="provenance/summarise.json") as run:
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

## Inside the script, and watching it run

```python
@run.step  # digests what a function received and returned
def normalise(rows, factor=1.0):
    return [r * factor for r in rows]
```

A digest says a *file* changed; this says an *argument* changed. Values that cannot be
canonically serialised are recorded as `UNDIGESTIBLE:<type>` — never a `repr`, never a
pickle.

**On Python 3.12+ it also records which of your own functions ran, and how often**, with no
decorator: `sys.monitoring`, scoped to code under the project root, and bounded — the
observer stops itself after 50 000 calls and the record says when. `configure(
auto_steps="arguments")` additionally digests the distinct argument sets each function saw.

Every record carries an `observation` block naming what the run was *able* to observe, so a
record with no steps is distinguishable from one made where steps could not be observed.

```
[00:00] read  data/m1.tsv  1541e29a8301ba21
[00:02] still running — last: read data/m1.tsv
[00:03] wrote out.tsv      c533232884b32c60
```

Progress is on when stderr is a terminal and off otherwise; the heartbeat fires only after
silence, so a run producing events steadily never beats. Both are configured once, never per
script. `run.open_output(p, record_header=True)` records the column names it just wrote.

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

`OK`, `STALE`, `GONE` and `ALTERED` are the four verdicts.

**`runprov chain`** asks the question one level up: **has the run history itself been edited
since it was written?** Each line carries the digest of the one before it, so an edit — or a
deletion or reordering anywhere but the very end — breaks the link that vouches for it, and
the report says plainly what it could not cover. Tamper-**evident**, not tamper-proof, and
checkable with `sha256sum` and nine lines of shell with this package uninstalled.

Also available:
`runprov log` (the run history as YAML or a table), `runprov show` (what one run did),
`runprov capture` (record a script that has no `runprov` calls in it at all), and
`runprov export` (RO-Crate and W3C PROV-JSON).

## Five commands for the questions asked afterwards

**`runprov check`** reads your source and reports entry points that open files and record
nothing — the case a runtime hook can never see, because code that is not imported does not
run. It never imports or executes your code, so it works on a pipeline that has never heard
of `runprov`. Exit 1 on a finding, so it can gate a build.

**`runprov report <artifact>`** is one artifact on one page: the run that produced it, the
commit and whether the tree was clean, which `runprov` recorded it, every input with its
digest, and the current verdict. It is a derived view — every fact comes from the artifact's
own pin and the run history — and it prints what it *cannot* tell you on the page rather than
leaving that to a manual.

**`runprov resources`** answers the question that stands between a laptop and a cluster: how
much did this actually need? Every record now carries what the run consumed, and the command
renders it as Snakemake benchmark columns, a Slurm preamble, or Kubernetes
`requests`/`limits`.

```
$ runprov resources --format slurm
#SBATCH --mem=469M
#SBATCH --cpus-per-task=3
```

**`runprov diff <a> <b>`** — why is today different from last month. Everything needed was
already recorded; what is new is that **a difference and an incomparability are not the same
answer**. A run on 3.11 could not see what a run on 3.12 saw, and a dirty tree's commit does
not name the code that ran — so those dimensions report NOT COMPARABLE rather than
`unchanged`. Exit 0 means comparable *and* identical in every dimension.

**`runprov impact <file>`** — what was derived from these bytes, and in what order it rebuilds.
The forward direction, reported with depth because the answer is an **order**. It answers what
*did* derive, never what *will* break: an empty result reads "no recorded run read these
bytes", never "nothing depends on this". The blind spots print on every answer, not only the
empty one.

**It is a floor and it says so.** Slurm and Kubernetes enforce against the *cgroup* — every
process at once, plus page cache — while `RUSAGE_CHILDREN` is the peak of the largest *single*
child: three children holding ~150 MiB simultaneously report 162 MiB, not 450. Inside a Slurm
step or a container it reads `memory.peak` instead, which **is** the enforced number, and the
record says which mechanism answered.

## What it is not

It records; it does not audit — it cannot tell you that a registered read was the read that
*mattered*. `@run.step` digests what crossed a function's boundary — arguments in, result out
— and **not the dataflow between statements**, which is what noWorkflow reports and what
requires rewriting the AST, at the cost of changing the program it observes.

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
Hôpital Henri-Mondor, and Taylor Nicole Thompson.
