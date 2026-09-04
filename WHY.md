# Explaining `runprov` — what it is for, and why it had to exist

Language you can reuse, at four lengths, for four audiences. Every number here is measured
and traceable to a commit in the repository this came out of; none of it is estimated.

---

## One sentence

> `runprov` records what a script read, wrote and ran as — hashing every input at the moment
> it is opened, and writing that record *into* the artifact — so that a result can be
> invalidated when its inputs change, instead of being trusted because nobody noticed.

## One paragraph (a Methods section, or a grant report)

> Provenance for the corpus-preparation pipeline is captured by `runprov`, a small
> dependency-free Python module. Rather than describing its inputs, each step *registers*
> them: `run.input(path)` returns the path, so the ordinary way to open a file is also the
> way it is hashed and recorded. Each generated artifact carries a header naming the script,
> the corpus generation, the git commit, and the SHA-256 of every registered input, and each
> run appends a line to an append-only history holding the environment, the parameters, the
> seeds and the outcome. Because the header is deterministic — it contains no timestamp and
> no run identifier — an artifact whose header has changed is an artifact whose inputs
> genuinely changed, which makes staleness detectable rather than a matter of recollection.

## Five minutes (a lab meeting, or a reviewer's question)

Start with the failure, because the failure is the argument.

**The problem is not that people forget to record provenance. It is that recording it is a
separate act from doing the work.** The project this came from already had a shared logging
utility, `append_log(entry)`, imported by **248 files** — near-total adoption, by any
reasonable measure. Its interface took a dictionary of strings:

```python
append_log(
    {
        "step": "annotate_segmentation_status_advanced",
        "input": f"{args.fasta}, {args.metadata_csv}",
        "script": "src/segmentation/annotate_segmentation_status.py",
    }
)
```

Every field is prose typed by the author. So the record says what someone *believed* the
step read — and in that very entry, the step name and the script name disagree. There are
no hashes, so two runs on different data are indistinguishable. There is no commit, so the
entry does not identify the code. The environment-snapshot function is defined and called
from nowhere. Only successful runs are appended, so "300 runs" means 300 *completed* runs
with an unknown denominator. The resulting log does not parse: `yaml.safe_load_all` raises
at line 14,554 (and `safe_load` at 14,547, on the `---` documents), and **nine** repair scripts exist to heal it — one of which is itself a
step in the pipeline it documents.

**Universal adoption did not produce a trustworthy record.** That is the whole finding.
Distribution was never the problem; the interface was.

`runprov` changes one thing and everything follows from it: **`run.input(p)` returns the
path.** Registering is not an extra line you might forget — it is how you open the file:

```python
df = pd.read_csv(run.input(path))  # registered, hashed, pinned
df = pd.read_csv(path)  # not — and an AST check can see the difference
```

That last clause matters more than it looks. Because registration is syntactically visible,
a checker can fail the build on a read that bypassed it. Prose provenance cannot be checked
by anything except a careful human reading two files side by side.

The second thing to say, before anyone copies anything, is the shape:

```python
with Run("build_labels", vars(args), provenance=PROV) as run:  # provenance= is not optional
    ...
```

`provenance=` on the **constructor** is what makes a crash record. A `Run` built without it
writes nothing at exit no matter how the block ends, so `run.write(PROV)` as the last line
of a script — with or without a `with` block — records nothing when the script dies halfway.
Both wrong shapes are silent. This document's own package taught the first one on its front
page for its entire life; someone integrated it, their step died halfway, and the run was
lost.

## The six properties, and the incident behind each

Nothing here is a design preference. Each property is the scar tissue of a specific defect
that reached a committed artifact.

**1. The pin lives inside the artifact, not only in a sidecar.**
A locked evaluation set recorded the SHA-256 of *its own body* and nothing about the corpus
it was drawn from. Its input then moved four times without the lock being able to say so —
and three fabricated labels sat inside a locked evaluation set the whole time, including two
laboratory constructs standing in for the rarest classes. *A lock is only as good as the
thing it pins. An artifact that cannot say what it was made from cannot be invalidated when
that thing changes.*

**2. The pin is deterministic — no timestamp, no run identifier.**
The first version embedded the run id. A run id contains a UTC stamp, so every pinned
artifact differed on every run: two identical runs over identical inputs both reported **80
artifacts CHANGED**. A permanently red check is worse than no check, because it teaches
everyone to ignore it. The same logic one level down produced `content_digest()`, which
hashes with volatile build stamps stripped — before it existed, 25 artifacts oscillated
forever across identical runs. Gzip files are decompressed first, because the gzip header
stores a compression mtime and a `.gz` rewritten from identical bytes never hashes the same
twice.

**3. Failures are recorded — but only in one of the three shapes you might write.**
`write()` is the last line of a script, so a step that dies halfway records nothing — which
is exactly the flaw diagnosed in the predecessor. This package shipped with the same hole
for one commit before it was caught by testing it against its own criticism. `Run` is now a
context manager, and `with Run(..., provenance=PROV) as run:` writes `status: "failed"`, the
exception, the traceback tail, and every registered-but-unproduced output listed as
`MISSING` — usually the most informative line in the record. The exception is always
re-raised, and a clean `SystemExit(0)` is not counted as a failure.

The hole did not fully close with the context manager, and the honest version of this
property says so: `__exit__` writes only when `provenance=` was given to the constructor.
`with Run(...) as run:` plus `run.write(PROV)` at the end still records nothing on a crash.
Two plausible shapes, both silent, one correct — which is a defect in the interface as much
as in the docs, and until it is one, the documentation has to carry it.

**4. Concurrent writes cannot corrupt the history — as a portability property.**
Measured on the source project's own history: 2,086 lines, median 2,017 bytes, **max 7,274,
and 107 lines over 4,096** — the size below which POSIX guarantees an append is atomic. The
first version of this section stopped there and claimed that above the bound two parallel
runs interleave into a line that is not JSON. **That is wrong on Linux and the correction is
worth stating rather than quietly deleting**: with locking disabled entirely, 8 processes ×
20 appends of 9 KB produced 160/160 intact records, because Linux holds the inode lock
across the whole `write()`. The `flock` earns its place elsewhere — NFS and CIFS do not
honour the POSIX guarantee at all, and Windows has no `O_APPEND` semantics of this kind,
where CI caught 24 concurrent appends producing 23 lines. It is a portability property, and
the mechanism originally cited for it was not the one that fails.

**5. The history is one continuous file, and it can be read back.**
Created once, appended forever, never rewritten — the property the predecessor had and
deserves to keep. What it did not have was durability: its writer appended YAML documents
into a file that began as a list, so the whole record became unparseable and nine repair
scripts grew around it. JSONL degrades one line at a time. `python -m runprov log --format
yaml` renders it back in the old file's own field names, so nothing is lost in the move;
measured on the current history, 2,079 runs render to 5.0 MB that `yaml.safe_load` parses in
6.6 s, from a source file that does not parse at all. (An earlier reading of this said 1,938
runs, 4.7 MB, 3.0 s — the same file, smaller, on a different machine. The per-run size is
unchanged at ~2.4 kB; the parse time is hardware, not a property of the format.)

**6. Every run records the command that produced it** — interpreter, arguments and working
directory, shell-quoted so it can be pasted back. This one was a regression at first: it
recorded only the script's basename, where the log it replaces had the full
`/…/envs/hcv_genotyping_env/bin/python3 src/preprocessing/x.py --inputs …`. Which python ran
it is most of the answer to "why did this run differ".

## What it is *not*, and say this before someone else does

* **It is not novel, and the pitch should not claim it is.** `sumatra`, `recipy`,
  `noWorkflow` and `dvc` all address versions of this. The honest positioning: *the smallest
  possible one, with each design choice traceable to a specific failure in a real project.*
  Most of the alternatives ask you to run a daemon, adopt a workflow engine, or restructure
  your pipeline. This one asks you to change `open(p)` to `open(run.input(p))`.

  **And "the provenance travels inside the artifact" is not novel either.** That was the
  claim this file was building toward, and a literature check on 2026-09-04 narrowed it — see
  *Related work, checked* below. Say the narrowed version or a reviewer will say it for you.

  Zero runtime dependencies, and the quickstart uses two of the 18 exported names. On size,
  the honest figure is where the statements sit rather than the total: **about 1,500 record**
  (`run`, `hashing`, `project`, `sinks`, `environment`, `terminal`) and **about 945 read the
  record back** (`show`, `__main__`, `verify`). A further ~120 are in neither group —
  `__init__`, `_report` and `watch` — which the earlier version of this bullet quietly
  omitted, so the two numbers did not add up to the package. Adopting it costs you the first number; the second is
  a CLI you can ignore. This bullet claimed **514 statements** until 2026-08-18 — measured
  before `show`, `verify`, `exec` and the terminal capture existed, which made a stale number
  the evidence in the sentence about smallness.

* **It records; it does not audit.** It cannot tell you a registered read was the read that
  *mattered*, and it cannot see a rule reimplemented as control flow. The half that makes the
  record trustworthy is a separate checker that fails the build on an unregistered read.
* **It does not version your data.** That is DVC's job, and they compose fine.

## Related work, checked

**Checked on 2026-09-04 against current sources, not from memory.** Every row is dated
because a maintenance status goes stale and an unverified comparison is worse than none — a
reviewer who finds one wrong stops trusting the rest.

### The tools that do what this does

| | latest release | where the record lives |
|---|---|---|
| **Sumatra** | **0.8.1, 2025-07-14**, Python 3.9–3.13 | a project database (`.smt/`) |
| **noWorkflow** | **2.1.3, 2024-12-11**, Python ≥3.8 | a SQLite database, from AST instrumentation + tracing |
| **recipy** | 0.3.0, **2016-09-13** | MongoDB, by monkeypatching library readers |
| **DVC** | actively developed | git-backed; versions DATA, does not record what a script read |

**Two corrections this check produced, and both were mine to make.**

**Sumatra is maintained.** I had assumed it was abandoned — the documentation a search
surfaces first is from 2015 — and it released 0.8.1 in July 2025 supporting Python 3.13. So
*"why not just use Sumatra?"* is a live question and cannot be answered on maintenance
grounds. The answer is where the record lives, and that it wants to launch your runs.

**noWorkflow is maintained too** (2.1.3, December 2024) and it captures far more than this
does: definition, deployment and execution provenance, by instrumenting the AST. That is the
real trade — it can tell you which value came from which, and it **changes the program it
observes**. This package has a test section headed *"provenance must not change the program
it observes"*, and for a record that may end up in a clinical result that is the side to be
on. Say the trade; do not claim the coverage.

**recipy has not been released in ten years.** It captures observed reads, like `capture`
does, and only through the libraries somebody remembered to patch — an unpatched reader is
invisible, silently. The audit hook sees every `open` because the interpreter emits it.

### The claim that had to be narrowed

**In-band provenance in a scientific artifact is standard, and predates this by about
fifteen years.** Verified in the specifications:

* **SAM/BAM `@PG`** records the program name (`PN`), version (`VN`) and **exact command line
  (`CL`)**, chained through `PP` so the header carries the whole sequence of programs that
  touched the file.
* **SAM/BAM `@SQ M5`** carries the **MD5 digest of the reference sequence**, in the header.
  So a content digest of an input, written into the artifact, is not new.
* **VCF `##source` and `##reference`** name the producing program and the reference used.

So *"the provenance travels with the file"* is a claim a bioinformatician will meet with
*"yes, `@PG` does that"* — and they will be right.

**What those do not do, and this is the defensible form:**

1. They record **what ran**, not **what it read**. `@PG` has no digest of the inputs; `@SQ M5`
   digests the reference and nothing else. So a `@PG` chain cannot answer *"is this result
   still valid?"* — it cannot be re-derived and compared.
2. They are **per format**. There is no `@PG` for a TSV, a CSV, a parquet, a figure or a
   JSON. The pin here is format-agnostic, and where a format cannot hold a comment the
   package says so and writes a sidecar rather than pretending.
3. They have **no checker**. `runprov verify` re-derives every pinned digest and reports
   `STALE`, `GONE` or `ALTERED` from the artifact's own bytes, with no history, no database
   and no network.

**The sentence that survives all of it:** *a format-agnostic pin that carries the content
digest of every input, plus a checker that re-derives them from the artifact alone.* That is
narrower than "provenance in the file" and it is true.

### What was NOT checked

RO-Crate and W3C PROV are emitted (ADR-0009) and were **not** compared as alternatives —
they are formats, not tools, and this package produces them rather than competing with them.
ReproZip, Sacred, MLflow and datalad were named in review and not investigated; they address
adjacent problems (environment packing, experiment tracking, dataset versioning) and if one
is going into a paper it needs its own check. **Do not cite a tool this section does not
list.**

## Where it sits relative to Snakemake, Nextflow, Prefect and Dagster

The question every reviewer asks, and "it is smaller" is not the answer. Three differences
are in kind rather than in degree, and only the third is about cost.

**1. A workflow engine records what a rule DECLARED. This records the read where it happens.**
Measured, not argued — Snakemake **9.25.2**, 2026-08-18, and the experiment is in the suite
as `test_a_workflow_engine_cannot_see_an_undeclared_read` (it skips when `snakemake` is not
on `PATH`, so you can re-run it against whatever version you have):

```
rule build: input "declared.tsv" -> output "out.tsv", running a script that ALSO
            opens "lookup.csv", which the rule does not mention

lookup.csv edited (ALPHA -> OMEGA), then `snakemake --cores 1`:
    "Nothing to be done (all requested files are present and up to date)"
    out.tsv still says ALPHA
```

The artifact is stale and the pipeline reports itself up to date. It corrected itself only
later, by accident, when an unrelated change to the DECLARED input forced a re-run.

**Snakemake does hash** — its metadata holds
`input_checksums: {'declared.tsv': 'sha256:6edd1748…'}` — so "workflow engines do not hash"
is false and should never be said. It hashes what was declared. `lookup.csv` appears nowhere.

**And the honest limit, which belongs in the same breath: runprov does not see that read
either.** Measured the same way — an unregistered `open("lookup.csv")` is absent from both
the artifact header and the history. Anyone claiming otherwise is selling something.

The difference is WHERE THE DECLARATION SITS. A Snakefile states the inputs in a file
separate from the code that reads them, so the two drift apart silently and nothing connects
them. `run.input(p)` sits inside the read itself — `open(run.input(p))` — so the record and
the act are one expression, an omission is visible in the diff of the code rather than in a
second file nobody re-reads, and a checker can fail the build on the reads that bypassed it.
Same shape as the defect that produced this package: `append_log(entry)` recorded what its
author BELIEVED the step read. A rule declaration is a far better statement of intent than a
hand-typed dict — checked, versioned, acted on — but it is a statement of intent, and this
one is a record of an act.

**2. The engine's record lives beside the pipeline. The pin lives inside the artifact.**
`.snakemake/`, `work/`, a Prefect or Dagster database: all of it stays home when the file
leaves. Email a TSV to a collaborator, upload a matrix to Zenodo, attach a supplementary
file to a submission — the provenance does not travel with any of them. A header naming the
script, the commit and the SHA-256 of every input does. That is the moment provenance is most
needed and least available, and it is the reason the pin is in-band rather than only in a
sidecar (property 1 above, which came from a locked evaluation set that could not say what it
was drawn from).

**3. An engine needs the DAG to exist. Exploration is where the shape is the unknown.**
You cannot declare a graph for an analysis you have not worked out yet — and the exploratory
phase is exactly where nothing is recorded and where a wrong number enters a manuscript. By
the time a Snakefile exists, the decisions that need explaining have already been made. So
this is adoptable EARLIER, and nothing is wasted at the migration: records written during
exploration stay valid and readable afterwards, and `runprov exec` returns the wrapped
command's own exit code so it drops inside a Snakemake rule or a Nextflow process without
becoming a second provenance system.

Say PHASE and never SIZE. "Engines are for big teams with infrastructure" is false — plenty
of three-person labs run Nextflow, `nf-core` is excellent, and the first bioinformatician who
reads it will say so. The defensible form: nobody writes a Nextflow pipeline to try an idea
on a Tuesday afternoon, including the people whose production pipeline is Nextflow. `nf-core`
exists for the analyses that have SETTLED; the unsettled ones are where provenance is hardest
to reconstruct afterwards, and where there is currently nothing.

The cost argument is real but secondary, and worth stating precisely. The binding constraint
is usually ADOPTION rather than RAM: restructuring code into rules, making everything
file-driven and re-runnable, and everyone agreeing to work that way. Where the resource point
does bite is the shared login node and the locked-down institutional VM — Nextflow needs a
JVM, Prefect and Dagster want a server or daemon — and there the obstacle is often permission
rather than performance. This is the standard library and one import.

**Say the limitation first, because a reviewer will.** It does not execute, schedule,
parallelise or submit to a cluster, and it offers no re-execution guarantee: a workflow engine
makes what happens REPEATABLE, this gives an ACCOUNT of what happened. Both are worth having,
and they compose. If you already run Snakemake with conda environments and reports, you have
much of this — the remaining difference is the in-artifact pin and observed-versus-declared
reads.

(Check the tool specifics against current documentation before citing them in a paper.
Nextflow, for one, does hash inputs for `-resume`, so "they do not hash" would be wrong. The
three claims above are about WHAT is observed, WHERE the record lives, and WHEN it can be
adopted.)

### And Jupyter?

A notebook records the NARRATIVE — cell source, the outputs those cells produced, an
`execution_count`, a kernel name. It does not record the DEPENDENCY: no hash, no statement of
which file `pd.read_csv("data.csv")` read or what was in it, no package versions, no commit,
and nothing about a previous session, since re-running a cell overwrites yesterday's output.
There is no append-only record to read back.

The export is where it gets worse rather than better. `df.to_csv("results.csv")` leaves the
notebook entirely: the notebook keeps its inline copy, the file a collaborator receives
carries nothing. The figure in the manuscript came from a file that cannot say what made it.

Notebooks also have a reproducibility problem of their own, unrelated to provenance: cells run
out of order, so the saved outputs may be unreachable from a top-to-bottom re-run. (Pimentel
et al., *A Large-Scale Study About Quality and Reproducibility of Jupyter Notebooks*, MSR
2019. Read the figures there; do not quote anyone's recollection of them, including mine.)

It works inside a notebook, which is the point rather than a caveat: exploration is what
notebooks are for, so they are the strongest case for adopting this before any DAG exists.
Checked with no `__file__` and an `ipykernel`-shaped `argv` — inputs hashed, outputs pinned,
`script_file` recorded as `None` with the stated fallback rather than an invented path. The
one wrinkle: `command` records the kernel launch and not your cell, so name the run yourself.

## Why it matters for a thesis or a paper specifically

A Methods section that says "all analyses are reproducible" is an assertion. A reviewer
cannot check it, and increasingly they do not believe it.

An artifact whose header names its script, its commit and the SHA-256 of every input is a
different kind of claim: **it is falsifiable.** A reader can recompute the hashes and find
out. And when an input legitimately changes mid-project — as it will — the artifacts derived
from it announce themselves instead of quietly persisting into the manuscript.

That is the argument, and it is worth making plainly: this is not infrastructure for its own
sake. Every property above exists because its absence had already put a wrong number in
front of someone.
