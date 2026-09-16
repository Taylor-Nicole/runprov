# 15. `impact` answers what *did* depend on this, never what *will*

Date: 2026-09-16 · Status: **accepted**, implemented as `runprov/impact.py` · Ledger: T-31

## Context

The reference genome is updated. There are seven pipelines and 903 463 data files. **What is
now invalid, and in what order does it have to be rebuilt?**

`verify` answers this per artifact — but only for artifacts you already thought to name, which
is the hard part. `lineage` holds the DAG and walks it **backwards**: given a run, which run
produced what it read. The forward direction is the expensive question and is not exposed.

The machinery is already there and is the right shape. `_lineage` **joins on the digest, not
the path** — because a path is rewritten by many runs over a project's life, which is the
defect L1 records — and it builds its index in two streaming passes, measured at 500 000 runs:
310 MB for the index against 2.2 GB for the records. `impact` needs the mirror of pass one:
**digest → consumers** instead of digest → producers.

## The honesty problem, which is different from `diff`'s

`diff` can be wrong about whether two records may be compared. `impact` can be wrong about
something more basic: **it is asked a question about the future and can only answer one about
the past.**

*"What will break if I change this file"* is not answerable from a history. What is answerable
is *"which recorded artifacts were derived, transitively, from these bytes"* — and the gap
between those two is exactly the set of things this package cannot see:

* a script that **never imported `runprov`**, which is why `runprov check` exists;
* a read that **bypassed registration** inside a run that did record — `unregistered_reads`
  names them when it saw them, and `watch.py` cannot see a script it was never in;
* a run whose records were **pruned**, or that wrote to a different history;
* a pipeline that **will** read the file tomorrow and never has.

A tool that answered "nothing depends on this" for a file no recorded run has read would be
confidently, dangerously wrong — that is a green light to overwrite a reference.

## Decision

**`impact` reports the recorded derivation chain, states the size of what it could not see, and
never reports an empty result as safety.**

1. **It walks digests, not paths**, for the same reason `lineage` does. Changing a file changes
   its digest, so the question is really *which runs read these bytes* — and a path that was
   rewritten five times answers nothing.
2. **The chain is reported in order, with depth**, not as a flat set. You need to know what to
   re-run *first*; a set of nine filenames does not tell you that.
3. **An empty result is reported as "no recorded run read this", never as "nothing depends on
   this".** Those are different sentences and only the first is true.
4. **It prints what it could not see**, every time, not only when empty: the count of
   unregistered reads in the traversed history, and a pointer to `runprov check` for the case
   it is structurally blind to. This is `observation` applied to a query rather than a record.
5. **It reuses `_lineage`'s index rather than a second one.** Two traversals of the same history
   that disagree about what is connected is the defect this project keeps finding, and the
   cheapest way to guarantee they agree is to have one of them.

## Sketch

```
runprov impact <path-or-digest> [--log L] [--format text|json] [--depth N]
```

```
$ runprov impact refs/hcv_ref.fa
refs/hcv_ref.fa  a3d2c39b016f6a7e

  3 recorded run(s) read these bytes, and 9 artifact(s) derive from them:

  1  align          → results/aligned.bam        2026-09-01
  2    genotype     → results/genotypes.tsv      2026-09-01
  3      summarise  → results/summary.tsv        2026-09-02
  3      figure     → figures/fig3.png           2026-09-02
  …

  Rebuild in that order. `runprov verify <artifact>` confirms each one afterwards.

  NOT SEEN BY THIS QUERY: 4 unregistered read(s) in the runs traversed, and any script
  that never imported runprov — `runprov check` finds those.
```

## What it must never claim

* That the list is **complete**. See the honesty problem above; the footer is not decoration.
* That everything listed **must** be rebuilt. A run that read the file may not depend on the
  part that changed, and nothing in the record knows which part mattered. It records; it does
  not audit.
* That an artifact **is** stale. `impact` says it *derives from* the changed bytes; `verify`
  says whether it still follows from what it was made from. Reporting the first as the second
  would put a verdict in a command that never hashed anything.

## Alternatives considered

**Extend `lineage` with a `--from` filter.** Tempting, and rejected on the output contract:
`lineage --format json` is a documented shape a consumer parses, and a filtered graph is a
different answer wearing the same name. A separate verb whose result is a chain, not a graph.

**Answer it with `verify` over the whole project.** This is what people do today — verify
everything and read the STALE lines — and it is correct and expensive: it hashes every input of
every artifact. `impact` is the cheap index lookup that says which artifacts are worth hashing.

**Compute forward staleness directly, and report verdicts.** Rejected per the third bullet
above: it would mean hashing, which makes the cheap query expensive, and it would collapse two
questions that are worth keeping apart.
