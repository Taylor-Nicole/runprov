# runprov

Record what a script read, wrote and ran as — in a form a checker can verify.

A **whole script**, standard library only, that you can paste into a file and run. A test
extracts this block from this README and runs it, so the block below cannot quietly
stop working. A fuller variant — the same four calls with `main()`, repo-relative defaults
and a `columns` note — is
[`examples/summarise.py`](https://github.com/Taylor-Nicole/runprov/blob/main/examples/summarise.py)
**in the repository and the sdist, but deliberately not in the wheel**: `pip install runprov`
gives you the package, not a copy of its examples. A second test runs that one:

```python
import argparse, csv, pathlib
from runprov import Run, configure

ROOT = pathlib.Path(__file__).resolve().parent
configure(root=ROOT)  # history -> <root>/provenance/runs.jsonl, where the CLI looks

parser = argparse.ArgumentParser()
parser.add_argument("--input", default="data/measurements.tsv")
parser.add_argument("--output", default="results/summary.tsv")
parser.add_argument("--threshold", type=int, default=5)
args = parser.parse_args()

OUT = ROOT / args.output

# provenance=... is what makes a crash record. Without it, __exit__ writes nothing.
with Run("summarise", vars(args), provenance=OUT.with_name("summary.prov.json")) as run:
    with open(run.input(ROOT / args.input), encoding="utf-8") as fh:  # registering IS
        rows = list(csv.DictReader(fh, delimiter="\t"))  # how you open it

    kept = [r for r in rows if int(r["value"]) >= args.threshold]

    with run.open_output(OUT) as fh:  # registers + pins the artifact + utf-8
        writer = csv.DictWriter(fh, fieldnames=["sample", "value"], delimiter="\t")
        writer.writeheader()
        writer.writerows(kept)

    run.note("rows_read", len(rows))
    run.note("rows_kept", len(kept))
```

```bash
$ python summarise.py
$ python -m runprov log            # what ran, on what, as what
$ python -m runprov verify         # do the artifacts still match what they pin?
```

That is the whole ceremony: `configure(...)` once, at import of your paths module, and one
`with Run(..., provenance=...)`. **There is no `run.write()` call and you do not need
one** — `__exit__` writes the sidecar and appends the history line, on success and on a
crash, and it is the only place the final status is known.

**A sidecar per run, if you want every one kept.** `provenance=` names one path, so the
tenth run of a script overwrites the ninth. The append-only history still holds all ten —
no *record* is ever lost — but the file sitting beside the artifact answers only for the
last run, and "when did this column appear" is a question about the ones it replaced.

```python
configure(root=ROOT, sidecar_per_run=True)
# out/summary.prov.json  ->  out/summary.20260813T135658Z.83469069.prov.json
#                            out/summary.20260813T135659Z.b0ce8d9c.prov.json
#                            out/summary.20260813T135700Z.b967537f.prov.json
```

Time first so sorting by name sorts by run; `run_uid` second so two runs inside one second
are still two files. The stamp goes in front of the **whole** compound suffix, so
`*.prov.json` still finds them — inserting before `.json` alone breaks that glob, which is
how the first version of this was caught.

It also makes `show --stale` answerable for older runs: that check reads the *producing*
run's sidecar for its stat fields, and reports `?` when a later run has overwritten it.

**Leave `run_log` alone unless you have a reason.** It defaults to
`<root>/provenance/runs.jsonl`, which is exactly where `python -m runprov log` looks when
you do not pass `--log`, so the two halves of the package agree for free. Point it
elsewhere and both still work — but every CLI call then needs `--log that/path`, and this
README taught that trap on its own front page for a while.

If your artifacts are not `#`-commented text — parquet, `.npy`, BAM, a PNG — use
`run.output(p)` instead of `open_output`, and see
[the pin is not a comment everywhere](#the-pin-is-not-a-comment-everywhere-so-open_output-refuses-some-formats).
The run is still recorded and hashed; only the in-artifact pin is given up.

`encoding="utf-8"` is not decoration either. Without it the artifact is written in the
machine's locale encoding, and `header()` contains an em dash. Measured on one header: 189
bytes under UTF-8, 187 under cp1252 — one em dash, three bytes against one. A header with
TWO of them measures 253 against 249. The exact figures depend on the header, which is why
the durable statement is the one that follows: different bytes, so a different SHA-256 for the same
artifact — and `UnicodeEncodeError` outright under cp932 or ascii. A provenance package
whose artifact hashes depend on the writer's locale has one job and does not do it, which is
why every file `runprov` writes itself pins UTF-8.

**[GETTING-STARTED.md](https://github.com/Taylor-Nicole/runprov/blob/main/GETTING-STARTED.md)**
walks through install-to-first-record for someone who has not used a package like this before —
every command in it was run, and every block of output is real.
**[WHY.md](https://github.com/Taylor-Nicole/runprov/blob/main/WHY.md)** explains what this
is for at four lengths, with the incident behind each design choice.
**[docs/adr/](https://github.com/Taylor-Nicole/runprov/tree/main/docs/adr)** records the
decisions: what was chosen, what it rests on, and what it cost.

## Installing it

Zero dependencies and pure Python, so there is not much to say — but it was verified rather
than assumed, wheel and sdist, each installed and imported and `python -m runprov log` run:

| | |
|---|---|
| `pip install runprov` | wheel and sdist both ✓ |
| `uv pip install runprov` | ✓ |
| conda / mamba prefix, via `pip` | ✓ — and see [Environment snapshots](#environment-snapshots), which reads `conda-meta` |
| **Poetry with `pkginfo` &lt; 1.11** | **cannot consume it** — an old environment, not an old Poetry |

That last row is worth the detail because the error names nothing useful: Poetry resolves the
dependency and then reports `Unable to create package with no name`, leaving an environment
that installed cleanly and cannot import. The cause is `pkginfo` &lt; 1.11, which returns
`name = None` for any wheel whose `Metadata-Version` is newer than it knows; `hatchling`
emits **2.5**. Measured against this wheel:

| `pkginfo` | `Wheel(…).name` |
|---|---|
| 1.9.6 | `None` |
| 1.10.0 | `None` |
| 1.11.0 | `'runprov'` — warns `NewMetadataVersion`, then parses as 2.3 |
| 1.12.1.2 | `'runprov'` — warns `NewMetadataVersion` too, then parses as 2.4 |

**The predicate is the installed `pkginfo`, not the Poetry version**, and an earlier draft of
this table got that wrong. Poetry declares only a lower bound, so what breaks is an
environment locked or installed before `pkginfo` 1.11 (May 2024) — not a release of Poetry.
Measured from PyPI metadata and by resolving each one today:

| Poetry | declares | a fresh install today resolves |
|---|---|---|
| 1.8.0, 1.8.2 | `pkginfo >=1.9.4,<2.0` | 1.12.1.2 — works |
| 1.8.3, 1.8.4 | `pkginfo >=1.10,<2.0` | 1.12.1.2 — works |
| 1.8.5 | `pkginfo >=1.12,<2.0` | **cannot hit the bug at all** |

So `pip install -U pkginfo` in the Poetry environment fixes it, as does Poetry 1.8.5, as does
Poetry 2.x. Nothing here needs changing, and nothing here can change it short of a different
build backend.

It is **not** the licence metadata — a build with the pre-PEP-639 `license = {text = ...}`
emits 2.5 just the same, which was measured before this paragraph was written.

## Three shapes that record nothing, and the one that does

All three look like they are recording. Measured, running the same failure through each:

| what the script does | crash halfway |
|---|---|
| `run = Run(...)` … `run.write(PROV)` at the end | **nothing.** No sidecar, no history line, no output |
| `with Run(...) as run:` … `run.write(PROV)` at the end | **nothing.** The `with` block is not enough |
| `run = Run(..., provenance=PROV)` — no `with`, no `write()` | **nothing, ever** — including on a clean finish |
| `with Run(..., provenance=PROV) as run:` | `status: "failed"`, the exception type, message, traceback tail, and every registered-but-unproduced output as `MISSING` |

The first shape was this README's front page for the package's whole life, and someone
integrating it copied it, their script died halfway, and the record was lost — the exact
defect the package exists to eliminate, taught by its own quickstart. The second is worse
because it looks like the fix: `__exit__` only writes when `provenance=` was passed to the
**constructor**, so adding `with` while leaving `write()` at the end buys nothing.

The third loses the record even when nothing goes wrong, and it is the likelier mistake now
rather than a rarer one — every page here presses `provenance=` on the constructor, so the
half left to forget is the `with`. Worse, the artifact is still written and still carries a
pin naming a record that does not exist. **This one now says so**, at interpreter exit:

```
  NOTHING WAS RECORDED for Run('summarise'): `provenance=` was given but the run was never
  written. Use `with Run(..., provenance=P) as run:` — or call `run.write(P)`
```

It warns rather than writing the record for you: writing at interpreter shutdown would make
`with` optional and would hash your outputs while imports are being torn down. Your work is
already on disk; what is missing is the record, and you can still fix the script.

Calling `run.write(P)` *inside* a `with Run(..., provenance=PROV)` block is fine and
sometimes useful (a caller may want the record at a second path); the history is still
appended exactly once, at exit, with the true status. It is `write()` **instead of**
`provenance=` that loses the run.

## Why this exists rather than the obvious thing

The obvious thing already existed. `hcv_genotyping/src/utils/log_transformation.py`
exposes `append_log(entry: dict)` and **248 files import it.** It still produced a
provenance log that needs a tolerant line-wise recovery parser and nine
`fix_transformation_log_*.py` heal scripts — one of which is itself a step in the pipeline
it was meant to document.

Distribution was never the problem. The interface was:

```python
append_log(
    {
        "step": "annotate_segmentation_status_advanced",
        "input": f"{args.fasta}, {args.metadata_csv}",  # a string
        "output": f"{output_csv_path}, {segments_output_path}",
        "script": "src/segmentation/annotate_segmentation_status.py",
    }
)
```

Every field is prose the author typed. It records what someone *believed* the step read,
which is why that `script:` names a different file from the step that wrote it. There is
no hash, so two runs on different data are indistinguishable; no commit, so the entry does
not identify the code; and the env snapshot function is defined and never called.

`run.input(p)` **returns the path.** That one decision is the whole design: the natural way
to open a file is the recorded way, and a read that skips registration is a visible
omission an AST check can find.

## Isn't this what Snakemake, Nextflow, Prefect or Dagster are for?

They overlap, they compose, and the difference is worth stating precisely — because two of
the three ways people phrase it are wrong.

**Not "they don't hash".** Snakemake 9.25.2 records
`input_checksums: {'declared.tsv': 'sha256:6edd1748…'}`; Nextflow hashes inputs to decide
what `-resume` can skip. Anyone who uses these tools will know that, and the claim would cost
you the argument.

**Not "they're heavyweight" either**, at least not first. That is true of a JVM on a shared
login node and of a Prefect or Dagster daemon on a locked-down institutional VM — where the
obstacle is often *permission* rather than performance — but Snakemake is pure Python and the
real cost of adopting an engine is restructuring your code into rules, not RAM.

The three differences that hold up:

**1. An engine records what a rule DECLARED. This records the read where it happens.**
Measured, not argued — a rule declaring `declared.tsv`, running a script that also opens an
undeclared `lookup.csv`:

```
$ # lookup.csv edited: ALPHA -> OMEGA
$ snakemake --cores 1
Nothing to be done (all requested files are present and up to date).
$ cat out.tsv
label   ALPHA          # the artifact is stale; the pipeline reports itself up to date
```

It hashes what was declared. `lookup.csv` appears nowhere in its metadata. The experiment is
in the test suite and skips unless `snakemake` is on your `PATH`, so you can re-run it against
your own version rather than trusting this paragraph.

**And the honest half: runprov does not see that read either** — an unregistered
`open("lookup.csv")` is absent from the artifact and the history both. The difference is
*where the declaration sits*. A Snakefile names the inputs in a file separate from the code
that reads them, so the two drift apart with nothing connecting them. `run.input(p)` sits
inside the read — `open(run.input(p))` — so the record and the act are one expression, an
omission shows up in the diff of the code rather than in a second file nobody re-reads, and
an AST check can fail the build on the reads that bypassed it.

**2. The engine's record lives beside the pipeline; the pin lives inside the artifact.**
`.snakemake/`, `work/`, a Prefect database — none of it travels when the file is emailed to a
collaborator, uploaded to Zenodo or attached to a submission. That is when provenance is most
needed and least available. A header naming the script, the commit and the SHA-256 of every
input travels with the file, and `runprov verify` reports `STALE` from the artifact alone,
without the pipeline, the engine or a re-run.

**3. An engine needs the DAG to exist.** Exploratory work is where the shape of the analysis
is the unknown — and it is also where nothing gets recorded and where a wrong number enters a
manuscript. By the time a Snakefile exists, the decisions that need explaining have already
been made. So this is adoptable earlier, and nothing is wasted at the migration: records
written during exploration stay valid and readable, and `runprov exec` returns the wrapped
command's own exit code so it drops inside a rule without becoming a second system.

**This is about the phase, not the size of the group.** Plenty of three-person labs run
Nextflow — `nf-core` exists and is excellent — so "engines are for big teams with
infrastructure" is not the argument and will be contradicted by the first bioinformatician
who reads it. The argument is that nobody writes a Nextflow pipeline to try an idea on a
Tuesday afternoon, including the people whose production pipeline is Nextflow. `nf-core`
exists for the analyses that have *settled*. The unsettled ones are where provenance is
hardest to reconstruct afterwards and where there is currently nothing.

**What it does not do, said before you find out.** It does not execute, schedule, parallelise
or submit to a cluster, and it offers no re-execution guarantee. An engine makes what happens
**repeatable**; this gives an **account of what happened**. If you already run Snakemake with
conda environments, you have much of this — the remainder is the in-artifact pin and
observed-versus-declared reads.

### Doesn't Jupyter already do this?

A notebook records the **narrative**; it does not record the **dependency**. Both are useful
and they are not substitutes.

What the `.ipynb` holds is cell source, the outputs those cells produced, an
`execution_count` per cell, and a kernel name. What it does not hold: any hash, any statement
of *which file* `pd.read_csv("data.csv")` actually read or what was in it at the time, the
package versions, the commit, or anything at all about a previous session — re-run a cell and
yesterday's output is overwritten. There is no append-only record to read back.

The export is where it gets worse rather than better. `df.to_csv("results.csv")` leaves the
notebook entirely: the notebook keeps its inline copy, and the file you send a collaborator
carries nothing. That is the in-artifact pin argument again, one notch sharper — the figure in
the manuscript came from a file that cannot say what made it.

And notebooks have a reproducibility problem of their own that has nothing to do with
provenance: cells run out of order, so `execution_count` is routinely non-linear and the saved
outputs may be unreachable from a top-to-bottom re-run. (Pimentel et al., *A Large-Scale Study
About Quality and Reproducibility of Jupyter Notebooks*, MSR 2019, is the standard reference;
read the figures there rather than quoting anyone's recollection of them.)

**It works inside a notebook**, and that is the point rather than a caveat — exploration is
what notebooks are for, which makes them the strongest case for adopting this before any DAG
exists. Checked with no `__file__` and an `ipykernel`-shaped `argv`: inputs are hashed,
outputs pinned, and `script_file` records `None` with the stated fallback rather than
inventing a path. One thing to know — `command` records the *kernel launch*, not your cell,
so name the run yourself:

```python
with Run("explore_thresholds", {"cutoff": 5}, provenance=OUT.with_suffix(".prov.json")) as run:
    df = pd.read_csv(run.input(RAW))
```

## It tells you when a read bypassed registration

`run.input(p)` makes registration the ordinary way to open a file. It cannot make it the only
way — a plain `open(p)` still works, the read is absent from the record and from the pin, and
until 0.1.0 **nothing said so**. A record that looks complete while being incomplete is worse
than an obviously missing one, because it invites trust it has not earned.

Now it says so, once, at the end of the run:

```
  UNREGISTERED READ: this run opened 1 data file(s) it did not register, so they are
  NOT in the record and NOT in the pin: conf/app.json. Open them with `run.input(path)`
  to include them, or set `warn_unregistered_reads=False` if this is deliberate.
```

**And it goes in the record**, as `unregistered_reads`, in the sidecar and in the history —
because a warning scrolls past and a field is still there in three years, when someone asks
whether that number was ever fully accounted for.

It uses `sys.addaudithook`, so it sees opens from C extensions too — `pandas`, `h5py`,
`pyarrow` — not just `builtins.open`.

**What it cannot see, said plainly.** A script that never imports `runprov` at all. Code that
is not imported does not run, and nothing inside this package can observe a process it was
never part of. That case needs a static check over your source, or `runprov exec` wrapping the
command. Anything claiming otherwise would have to hook every Python process on your machine,
which this deliberately does not do.

**Measured, because a noisy check is worse than none.** On a run that imports four stdlib
modules, sets a locale, reads a config and a registered input: **13 opens observed, 1
reported** — the config, which was genuinely unregistered. Zero false positives. The
interpreter's own imports, locale data, `site-packages`, `.py` files and everything this
package writes are all filtered out. Cost is paid once at exit and is proportional to the
number of *distinct* unregistered files: **+0.4 ms** for a realistic run, +21 ms for a
pathological 400.

Turn it off per project with `configure(warn_unregistered_reads=False)` — for a step that
deliberately reads files it does not want recorded.

## The three properties, and the defect each one prevents

| property | what its absence caused |
|---|---|
| **Registration is the ergonomic path** | Provenance as prose: 248 adopters, no verifiable record |
| **The pin lives in the artifact**, not only in a sidecar | A locked holdout recorded the hash of its own body and nothing about the corpus it came from. Its input moved four times without the lock being able to say so, and three fabricated labels sat inside a locked evaluation set |
| **The pin is deterministic** — no timestamp, no run id | The first version embedded the run id, which contains a UTC stamp. Two identical runs over identical inputs both reported **80 artifacts CHANGED**. A permanently red check trains everyone to ignore it |

`content_digest()` is the same idea one level down: hash with volatile build stamps
stripped, so an artifact carrying `# built_utc:` does not make everything downstream of it
differ on every run. Gzip is decompressed first, because the gzip header stores a
compression mtime and a `.gz` rewritten from identical bytes never hashes the same twice.

The pin that lands in the artifact looks like this — deterministic, and carrying the
generation but not the run id:

```
# provenance — this artifact and what produced it
#   script     : build_labels
#   generation : v2026_5839
#   commit     : 0d435be
#   inputs (1), sha256:
#     c1263ad3556572f4  data/labels.tsv
```

## One continuous history, and reading it back

`runs.jsonl` is created by the first recorded run and **appended to forever** — every run
this project has ever recorded, in order, never rewritten. That is the same job the sibling
project's `transformation_log.yml` did, and the continuity is the property worth keeping.

It is JSONL rather than YAML because the predecessor's file **stopped being readable**.

Every number below is measured on ONE named file, because that turned out to matter: several
copies of this log exist on the machine it came from, with different line counts and
different failure offsets, and a reviewer holding a different copy produced three confident,
wrong corrections to this section. Naming the file is the same discipline the package is
about — a measurement that does not say what it measured is not checkable.

```
path    hcv_genotyping/transformation_log.yml   (flaviviridae_20260424_FULL)
bytes   2,147,154                               (2.1 MB, 24,300 lines)
sha256  bc0a72ca0dcea3fc25c32fbdd960349e66f44f31a852b440bd102c9a722e3c71
```

It holds **295 entries** — lines beginning `- step:` — and one of them runs to **6,544
lines**, because a repair script (`- step: fix_transformation_log_hybrid_heal_plus_env`)
appended 243 further records **without their `- ` list markers**, so YAML reads them as more
keys of the entry above rather than as records of their own. The repair is what did that.

| | |
|---|---|
| `yaml.safe_load` | `ComposerError` at line **14,547** — the writer appended `---` documents into a file that began as a list, and there are **252** of them |
| `yaml.safe_load_all` | `ScannerError` at line **14,554** — an unquoted `Note:` inside a description. A colon in prose someone typed |

Two different defects, and the second is the one that matters here: it is not the multi-
document problem at all. It is a hand-written value that happened to contain `: `, in a
writer whose quoting was correct only for the values its author had thought of. That is why
`_yaml` in this package quotes **every scalar unconditionally** rather than sniffing for
characters that need it — and why the same string round-trips through it cleanly. Nine
`fix_transformation_log_*.py` repair scripts exist because of all this, one of which is
itself a step in the pipeline the log documents.

A tolerant line-wise recovery gets **293 of 295** entries back. In JSONL every line stands
alone: a corrupt line costs one record, never the file, and the reader counts what it could
not read instead of pretending it was not there.

Read it back with the CLI — a log nobody reads is a log nobody checks:

```bash
python -m runprov log                          # timeline, oldest first
python -m runprov log --format yaml            # the transformation-log shape
python -m runprov log --failed                 # only the runs that died
python -m runprov log --script build_labels --limit 5
```

**The CLI does not know what your scripts passed to `configure()`.** With no `--log` it
reads `<detected root>/provenance/runs.jsonl`, so if you set `run_log` — as the quickstart
above does — every one of those commands needs `--log reports/runs.jsonl` or it will
correctly report that nothing has been recorded at the default path.

`--format yaml` deliberately keeps the old field names — `step`, `script`, `input`, `output`,
`run_command`, `date`, `params`, `summary`, `requirements_file` — so anyone who could read the
old file can read this one. What changed is where the values come from: observed and hashed,
rather than typed by hand.

| old field | where the value now comes from |
|---|---|
| `step` | the name passed to `Run(...)` |
| `script` | `script_file` — the caller file **walked off the stack**, not re-typed. In the source project the hand-typed `script:` names `proteins_ns5b_domains/…` for a step whose `run_command` runs `…_3utr/v65/…`: two different files in one entry |
| `params` | `parameters` — what argparse parsed, with its types intact |
| `summary` | `notes` — `run.note()` values, where the old log had a hand-written `description:` paragraph |
| `requirements_file` | the content-addressed `env-<sha16>.txt`, one file per **distinct** environment rather than one timestamped pip freeze per invocation (87 files, 8 distinct contents, on disk in the source project) |

Two deliberate departures from the old shape. Every scalar is **quoted**, so `date` loads as
an ISO-8601 string rather than a bare YAML timestamp — the unconditional-quoting rule is what
makes the renderer total, and carving a date-shaped exception into it is how the predecessor's
writer became correct only for the values its author thought of. And a key is **omitted** when
the run did not use the feature, rather than filled with a default: `requirements_file`
appears only when the project configures snapshots. `script` is the exception that proves the
rule — it is always emitted, because a record that cannot name the file that ran should say
so rather than fall back to `step`, which would reproduce the very defect the field replaces.

Measured on the source project's history, the same 2,121 records rendered by both versions
(2026-08-11): **5.14 MB → 5.94 MB**, and `yaml.safe_load` parses all 2,121 entries in
**3.1 s → 4.9 s**. The four added fields cost 15.6% of the file. Of those records 2,117 carry
`summary` and 1,386 carry `params`; **none carries `script` or `requirements_file`**, because
both reach the history line only from this version onward — an old run cannot be back-filled
and is not pretended into one. The file it replaces does not parse at all.

## Lineage: which run produced what this one read

```bash
python -m runprov lineage --log reports/runs.jsonl
python -m runprov lineage --format json          # for a consumer
```

The history already records, per run, the exact set of files read and written **with the
hashes taken at the moment of use**. That is the raw material of a complete DAG, and it was
unusable for one reason: the obvious way to match a consumer's input to a producer's output
is **by path**, and a path is rewritten by many runs over a project's life. Every read then
has as many candidate producers as there were writes.

**Join on the digest instead.** A path is a name; a digest is a fact — those bytes were
produced exactly where they were produced. Two rules make it total:

1. match on `content_sha256`, falling back to the raw hash;
2. the producer must have **finished before the consumer started**. Two runs writing
   identical content is ordinary (a rebuild that reproduces), so the digest alone can have
   several candidates; time settles it, and the latest such producer wins.

Measured 2026-08-18 on a real history — `hcv-genotyping-release/reports/audit/runs.jsonl`,
**2,453 records**. That file is appended to daily, so re-measuring moves every number; what
does not move is the `ambiguous` column:

| | resolvable | ambiguous | orphan |
|---|---:|---:|---:|
| join on the path *(the heuristic)* | 3,773 | **3,271** | 4,864 |
| join on the digest *(this)* | **3,781** | **0** | 4,856 |

`ambiguous` is 0 **by construction** rather than by luck — and it is still printed, because
a count that can only be zero is one nobody should trust without seeing it.

**Orphans went up, and that is the honest direction.** An input whose *path* was produced by
some run, but whose *bytes* were not, is now an orphan instead of a false edge. An orphan is
not a failure either: a corpus fetched outside the history is legitimately one.

`run_uid` (uuid4) gives each run a unique address, because `run_id` is a **chain** id that
thirty stages of one pass share. It is in the sidecar and the history and **never in the
pin** — a uuid is a timestamp wearing a different name, and embedding one made two identical
runs over identical inputs both report 80 artifacts CHANGED.

Records written before `run_uid` existed are addressed by sidecar path plus start time, and
the count of those derived addresses is reported. That matters: every one of the 2,196
records above predates the field, so a reader that insisted on it would have produced a
graph of zero edges over the entire corpus it was written to read.

## Does it work with your formats? Run the matrix and see

```bash
python examples/format_compatibility.py
```

It writes an artifact in each format through `runprov`, reads it back with that format's
**real library**, and reports where the pin went, whether the artifact still parses, whether
the run hashed it, and whether two writes give the same digest. **A format whose library is
absent is SKIPPED and says so** — a check that goes green because it did not run is the
failure this package is about.

Measured here, **60 formats, 0 failures, nothing skipped**:

| pin placement | formats |
|---|---|
| in-band | the whole allowlist: `.tsv` `.csv` `.tab` `.txt` `.md` `.bed` `.bedgraph` **`.gff` `.gff3` `.gtf`** `.yaml` `.yml` `.toml` `.ini` `.cfg` `.conf` `.properties` — plus SQL and TeX on request, which comment with `-- ` and `%` |
| in the document | JSON (`output_json`, a top-level key) |
| sidecar | everything else — FASTA, FASTQ, GenBank, PDB, Stockholm, PHYLIP, Nexus, Matrix Market, JSONL, VCF, BCF, BAM, CRAM, bigWig, PLINK, mzML, SVG, Newick, Parquet (file *and* partitioned directory), Feather/Arrow, ORC, Avro, Pickle, joblib, cloudpickle, `.npy`, `.npz`, HDF5, AnnData `.h5ad`, Zarr, NetCDF, `.xlsx`, Stata, SPSS, R `.rds`, DuckDB, SQLite, MessagePack, **GGUF**, ONNX, safetensors, PyTorch `.pt`, XGBoost, LightGBM, a SavedModel-shaped **directory**, `.mat`, PNG, TIFF, gzip |

Everything above round-trips: written through runprov, read back by `h5py`, `anndata`,
`zarr`, `xarray`, `pysam`, `pyreadr`, `onnx`, `safetensors`, `torch`, `openpyxl`, `pyarrow`,
`joblib` and the rest. Nothing is skipped and nothing fails.

**Three results worth reading rather than skimming**, all measured rather than inferred:

* **gzip** is byte-unstable and content-stable. Its header stores an mtime so the raw bytes
  move on every write, and `content_digest` decompresses first. That is the two-hash design
  doing its job, visible.
* **Four formats move on every run**, and the causes are not the same. `.mat` writes
  `Created on: <date>`; SPSS `.sav` differs at exactly one byte, offset 108, the creation
  time; CRAM differs across two writes of identical records with an identical reference
  path; and **Avro carries a random 16-byte sync marker per file** — not a clock, so
  freezing time would not help it. Prefer `.npz` over `.mat` and Parquet over Avro; for
  CRAM, pin the BAM.
* **A PyTorch `.pt` digest depends on its FILENAME.** A `.pt` is a zip and torch names the
  entries after the file stem, so `m.pt` contains `m/data.pkl` — identical weights saved as
  `model_v1.pt` and `model_v2.pt` hash differently, and renaming a checkpoint changes its
  digest. That is torch's doing, not runprov's, and it is worth knowing before a rename
  reads as a retrain.

Adding your own format is three lines, and the file says how.

## The notebook: `show`

`log` is a timeline and `lineage` is a graph. Neither answers the question you actually have
three weeks in — **which script expects which input, and does the thing I need already
exist?** Rebuilding an artifact that was already correct, because nobody could tell, is the
expensive mistake when a build takes hours.

```bash
python -m runprov show                 # the project page
python -m runprov show build_labels    # every run of one script
python -m runprov show results/mid.tsv # every run that read or wrote one artifact
python -m runprov show --format yaml
```

The project page is built per **script**, because that is what the question is about:

```
  build    2 run(s)
    last 2026-08-13T11:35:13Z    first 2026-08-13T11:34:39Z
    file src/build.py
    expects:
      data/in.tsv   04bdbee1490cec52 7f21c0a91b3e5d44
    writes:
      out/mid.tsv
    params:  mode
    notes:   rows

── artifacts on record (2) ───────────────────────────────────────────────────

  bd6f7dc7a8d29a50  out/mid.tsv
                    by build  2026-08-13T11:35:13Z
```

Two digests beside one input path means that file has been read at **two different
versions** — which is the fact, and a script that has read forty is summarised as
`[40 versions]` rather than printed. An artifact produced by a run that failed says so.

### When did I add that column?

The digest tells you an artifact changed on 12 March and which run and command changed it.
It does not tell you *what* changed — that a column appeared, or a statistic was added.
`runprov` will not open your files to find out; inspecting content is not its job, and a
package that guesses at your schema is a package that is wrong about it eventually.

**One line makes it answerable.** Record the shape you produced, as a note:

```python
run.note("columns", list(df.columns))  # the schema you wrote
run.note("n_rows", len(df))
run.note("dtypes", {c: str(t) for c, t in df.dtypes.items()})  # if it matters
```

Then the history dates it for you:

```
── build_stats  [ok] ───────────────────────────────────────────
  started      2026-08-13T14:03:39Z
  columns ["sample", "value"]
── build_stats  [ok] ───────────────────────────────────────────
  started      2026-08-13T14:03:40Z
  columns ["sample", "value", "qval"]            <- qval arrives here
── build_stats  [ok] ───────────────────────────────────────────
  started      2026-08-13T14:03:41Z
  columns ["sample", "value", "qval", "log2_fc"] <- and log2_fc here
```

`python -m runprov show build_stats` is the whole query. The project page lists the note
KEYS a script records, so `notes: columns, n_rows` tells a reader what questions this
script's history can answer before they ask one.

**Conventions worth keeping**, because a note key that changes name is a note key you cannot
grep across three years of history:

| key | what it answers |
|---|---|
| `columns` | when a field appeared or was removed |
| `n_rows`, `n_dropped` | when a filter changed what it kept |
| `dtypes` | when a column changed type under you |
| `model`, `temperature`, `prompt_sha256` | what a non-deterministic step was asked |
| `tool_version` | what a subprocess reported about itself |

Anything JSON-able works — `note()` takes `Any` and normalises numpy and pandas scalars, so
`run.note("n", df["v"].sum())` records `6` and not the STRING `"6"`. That example is chosen
carefully: `df.shape[0]` is already a plain `int`, so it would demonstrate nothing.
`numpy.float64` subclasses `float` and survives anyway, but `numpy.int64` and `numpy.bool_`
subclass neither `int` nor `bool`, and without normalisation they fall through to the
record's `default=str` — a count recorded as `"6"` compares unequal to `6` in every
downstream check and renders quoted in the YAML view. `.sum()`, `.nunique()` and
`(s > 1).any()` are the ordinary spellings, so this is the common case rather than an exotic
one. Duck-typed on `.item()` rather than importing numpy, because this package has no
dependencies and must not acquire one to describe a caller's data.

### Do I need to run this again?

```bash
python -m runprov show --stale     # one stat per input
python -m runprov show --rehash    # re-derive every digest, slower, no resolution limit
```

```
  OK           a4c3ed04a95a3da1  out/final.txt
  STALE        ae31646fa3c0107e  out/mid.tsv    <- an input moved; rebuilding differs
  MODIFIED     b413f47d13ee2fe6  out/aux.bin    <- the ARTIFACT changed, not its inputs
  GONE         2d711642b726b044  out/deleted.txt
  UNVERIFIABLE …                                <- cannot be told, and will not guess
```

**The same five words as `verify`**, because they are the same five ideas. `show` used to
say `current` where `verify` said `OK`, and `?` where `verify` said `UNVERIFIABLE` — one
lowercase word among four uppercase ones, and two commands disagreeing in print about an
artifact they agreed about in fact. There is now one definition and no second spelling to
drift. `show` adds `MODIFIED`, which only the history can support, and `verify` adds
`NO PIN`, which only the bytes can.

**Answered from the history, not from the pin** — which is why it works for `aux.bin`, a
binary that could never hold a pin at all. `verify` reads the block inside an artifact;
this walks the run that produced the artifact and re-checks *that run's* inputs.

**Off unless asked, because the page is free and the check is not.** `--stale` is one
`stat` per input and reads no input bytes — there is a test asserting that. It gets the
sizes and mtimes from the producing run's sidecar, since the history line trims those
fields deliberately — `symlink`, `size_bytes` and `mtime_utc` would cost **19.3%** of an
append-forever file, measured 2026-08-18 by re-serialising a real 2,453-record history
(17,149 input/output entries): 6.39 MB trimmed against 7.62 MB with them kept. If that sidecar is
missing, or a later run has overwritten it, the answer is `UNVERIFIABLE` rather than `OK`:
digests from one run compared against stats from another would be confident nonsense.

`--stale` inherits `moved_since`'s documented limit — a rewrite inside one second that
preserves the byte count is invisible to a `stat`. `--rehash` has no such limit and costs
what reading every input costs. There is a test that constructs exactly that case and shows
one mode missing it and the other catching it.

A target can be a script name, a `run_uid` prefix, a `run_id` or an artifact path, and they
are all tried: someone asking about `build` and someone asking about `out/mid.tsv` are
asking the same question and should not have to say which kind of name they hold.

**It writes nothing.** `show` reads `runs.jsonl` and renders it — no new field, no file, no
change to any record. A view that could alter what it displays would be a view you have to
trust, and the record is the thing being trusted. There is a test asserting the bytes on
disk are identical before and after.

Text by default and YAML with `--format yaml`, deliberately not HTML: this gets read in the
terminal beside the work, many times a day. The YAML quotes **every scalar**, for the reason
the section above gives — the predecessor's log dies on a `Note:` somebody typed.

## What code actually ran

`git_commit` identifies the code **only when the tree is clean**, and during development it
never is. `script_sha256` pins the entry point and nothing it calls. So a run whose numbers
moved because `src/utils/stats.py` moved recorded a commit, a clean-looking entry script,
and no trace of the file that did it.

Every run now hashes the project's **own modules that it actually imported**:

```json
"code": {"imported": {
  "count": 4, "omitted": 0, "digest": "137dc9cf…",
  "files": [{"path": "analyse.py",          "sha256": "b56f3e28…"},
            {"path": "src/utils/stats.py",  "sha256": "99602c77…"}]}}
```

Measured: edit `src/utils/stats.py`, leave the entry script alone, and the digest moves
from `137dc9cf…` to `2e62cd07…` while `script_sha256` is unchanged. That is the gap.

**Read at exit**, so a module imported halfway through the work is still counted. **Under
the project root only** — third-party packages are answered by `packages` and
`env_snapshot_dir`, and hashing site-packages every run would cost far more than it says,
so a virtualenv living inside the root is excluded too.

The **history line carries a summary**, not the list: one `digest` and a `count`. That
answers "did any first-party code change between these two runs" — the question a history
is asked — without multiplying an append-forever file by fifty modules. The per-file hashes
are in the sidecar.

`configure(hash_imported_code=False)` turns it off; `imported_code_max` caps the list, and
`omitted` states the tail. `run.module(m)` remains the sharper tool when you want to know
where one specific import *resolved from*, which is a different question.

## The work that is not Python

For a pipeline whose real work is `samtools`, `bwa`, `Rscript` or a shell wrapper, the
Python environment answers almost nothing: `packages` lists what pip installed, and the
thing that made the BAM is not in it.

```python
with Run("call_variants", vars(args), provenance=PROV) as run:
    run.tool("samtools")  # which one, and what version
    run.tool("bcftools")
    script = run.code(SCRIPTS / "fit.R")  # code that ran, in another language
    subprocess.run(["Rscript", script, run.input(BAM)], check=True)
```

```json
"tools": [{"name": "samtools", "found": true, "version": "samtools 1.20",
           "path": "/home/…/envs/bcftools_env/bin/samtools", "sha256": "e2ff5f14…"}]
```

**The path matters as much as the version.** When a conda env and `/usr/bin` both have a
`samtools`, the environment decides which one ran and the version string cannot tell you.
The binary's own `sha256` settles the case where two builds call themselves `1.20`.

`tool()` **runs the tool** with `--version` — a side effect, which is why it is a call you
make rather than something that happens to every run. Bounded by `timeout` and never
raising: a tool that hangs or is missing is *recorded* as such, because provenance must not
be why a pipeline stops. A version printed to **stderr** with a non-zero exit still counts —
that is `samtools --version` exactly. A tool that is not found is recorded `found: false`
rather than omitted: "we looked and it was not there" is a fact; silence is not.

`code()` registers an R script, a shell wrapper, a Snakefile — anything `sys.modules` will
never see, because the interpreter that ran it was a subprocess. It hashes into the **same
digest** as the Python, so *"did any code change between these two runs"* stays one
comparison whatever language the code is in. It returns the path, so registering is how you
pass it. It is deliberately **not** `input()`: a new column in a data file and a rewritten
model are the same event to a reader who has only one list.

The history line carries `tools` as a compact `name → version` map; the paths and binary
hashes stay in the sidecar.

## A pipeline with no Python in it: `runprov exec`

`tool()` and `code()` are calls someone has to make, and nothing detects an unregistered
`subprocess.run([...])`. A Makefile, a Snakefile or a shell script has no Python to put them
in at all — so the recording is something you put **in front of** the command:

```bash
runprov exec --name sort_rows --input in.tsv --output sorted.tsv \
  -- sort -k2,2nr in.tsv -o sorted.tsv
```

```json
"status": "ok",
"parameters": {"argv": ["sort", "-k2,2nr", "in.tsv", "-o", "sorted.tsv"]},
"tools":   [{"name": "sort", "version": "sort (GNU coreutils) 8.32", "path": "/usr/bin/sort"}],
"inputs":  [{"path": "in.tsv",     "sha256": "edb6e61a…"}],
"outputs": [{"path": "sorted.tsv", "sha256": "e7705a08…"}],
"notes":   {"exit_code": 0}
```

**It returns the command's own exit code** — including **128+N for a signal-killed child**,
which is what a shell returns, so an OOM check looking for 137 still sees 137 — so it drops
into a Makefile rule or a Snakemake
`shell:` without changing what failure means. A non-zero exit is *also* recorded — `status:
"failed"`, the exit code in the notes — and a program that does not exist is recorded with
`found: false` rather than a traceback.

The recorded `command` is the `runprov exec` invocation, which is deliberate: it is what
actually ran, **and re-running it re-runs the tool and records the rerun**. The wrapped argv
sits in `parameters` where a reader sees it directly.

Inputs and outputs are **declared**, because they cannot be inferred without tracing every
syscall the tool makes — the same bargain `run.input()` strikes in Python. `--capture FILE`
tees the command's output, and because that works at file-descriptor level it sees a
subprocess's output, which Python-level capture cannot.

## Verify: is this artifact still made from what it says it is?

```bash
python -m runprov verify                       # the project root
python -m runprov verify results/ --root .     # a subtree
python -m runprov verify --format json         # for a gate
```

Everything needed for this was here from the start — the pin is deterministic, the digests
are in the artifact, the names are root-relative — and until now nothing read it back. That
made invalidation a property of the **format** and not of the **product**: `log` and
`lineage` tell you what happened, and neither tells you whether what happened is still
true. A checker living in another repository is a checker most users do not have.

For every input a pin names, `verify` re-derives that input's digest now and compares. It
does **not** re-run anything and cannot tell you an artifact is correct — only whether the
things it was made from still hash the way they did. That is what staleness is.

It reads the artifact and nothing else. No sidecar, no `runs.jsonl`, no `configure()` —
which is the whole reason the pin is in the bytes, and why `--log` is meaningless here.

```
STALE        results/final.tsv
             STALE        data/in.tsv  (pinned e85456706a564976, now c694cbe65e827d62)  via step1
```

**Transitivity is free, and it is the point.** A step that reads an upstream artifact and
writes its lines through carries the upstream pin as well as its own, so a changed root
surfaces at every level that depends on it — and a grandchild stays stale after the root is
restored, because the child was never rebuilt. `via` names the step whose claim failed,
which for an inherited pin is **not** the artifact's own step. Nothing implements this; it
falls out of the pin being in the bytes.

Four outcomes, and the last two are the ones that make the check worth trusting:

| | |
|---|---|
| `OK` | every pinned input still hashes as pinned |
| `STALE` | one changed — rebuild the artifact |
| `GONE` | a pinned input is no longer there. Counted apart from `STALE` because it is a different repair: a stale artifact is rebuilt, a missing input is **found** |
| `UNVERIFIABLE` | a comparison would be meaningless, so none is claimed: a name outside the root — either `<external>/…`, which is deliberately not a path, or **any spelling that leaves the tree**, an absolute path or one containing `..`; a name carrying an escape (a file named `a\nb` and one named `a<LF>b` render identically); or an input the run recorded no digest for |

**It will not pass having checked nothing.** Zero pins found is a non-zero exit saying so
in those words. A gate that goes green over a directory whose artifacts carry no pins is
worse than no gate, because someone will trust it — the same rule that makes
`git_status_captured: false` mean "we could not look" rather than "clean".

Exit status is 0 when every pinned artifact verifies, 1 on any `STALE`, `GONE`, or nothing
checked — so `python -m runprov verify results/` is a CI step as it stands.

### What `verify` cannot tell you, and the command that can

`verify` reads the pin **inside** an artifact, and a pin lists the run's **inputs**. The
artifact's own digest is not in it and cannot be, because the pin lives inside the file it
would be describing. So over a result someone hand-edited — inputs untouched — `verify`
prints `OK` and exits 0. That is not a bug in `verify`; it is the honest answer to the
question `verify` asks, which is why the command prints the caveat every time it says `OK`.

The digest of the artifact itself is in the **history**, and `show` reads the history. So
the two commands see different things, and neither sees everything:

| | `verify` | `show --stale` |
|---|---|---|
| reads | the pin inside the artifact | the history |
| an artifact you were **emailed**, with no history | ✅ | ❌ |
| a **BAM, parquet or figure** — formats that cannot hold a pin | ❌ | ✅ |
| the **artifact itself** was edited | ❌ | ✅ (`--rehash`) |
| an input registered **after** `header()` | ❌ | ✅ |
| **transitive** staleness through inherited pins | ✅ | one generation |

That fourth row is the one you can cause by accident, so it is worth a paragraph. A pin is
rendered once, when `header()` is written into the artifact — after that, `run.input(p)`
still registers the input in the record, but the bytes already on disk cannot grow a line.
The artifact then **understates what it was made from**, permanently, and its pin will read
`OK` for ever no matter what happens to that input.

The run warns at the moment it happens, and **the record marks it too, as
`inputs_not_in_pin`** — in the sidecar and in the history, omitted when there are none, the
same treatment `unregistered_reads` gets and for the same reason: a warning is ephemeral, a
field travels with the run. `show --stale` works from the record, so it checks every input
including the late one; `verify` works from the artifact's own bytes, so it cannot, and that
is not a defect in `verify` but the price of the property that makes it useful on a copy
someone emailed you. Register every input before the first `open_output()` or `header()` and
the question does not arise.

**The gate is the pair, and it is one line:**

```bash
python -m runprov verify results/ && \
  python -m runprov show --stale --rehash --exit-code
```

`--exit-code` is 0 when every artifact on record is `OK`, 1 on any `STALE`, `GONE` or
`MODIFIED`. `UNVERIFIABLE` does **not** fail it: it means the check could not be made — a missing
sidecar, or a directory input the stat check cannot speak for — and failing on it would make
any project with one reference directory permanently red. The count is on the summary line,
where a reader can see it and reach for `--rehash`.

It refuses two shapes so that exit 1 keeps one meaning: without `--stale` or `--rehash`
there is nothing to gate on, and with a `show <target>` argument, which already exits 1 for
"nothing matched your target". Both are exit 2, a usage mistake rather than a failing
artifact.

Three stated limits. The pin is looked for in the **first 64 KiB** only, because reading
every byte of a 50 GB BAM to learn it has no pin is the cost that gets a checker deleted;
the comment marker is whatever precedes the anchor on its line, so a pin written with
`header("## ")` for a VCF reads back exactly like one written with `# `; and the first pin
block must **begin within the first 4 lines** (`PIN_STARTS_WITHIN`), so a file that merely
MENTIONS the format is not mistaken for an artifact — without it, `verify` over a project
reported this package's own source, its `.pyc` files and the wheel METADATA as artifacts,
and invented a `GONE` for a path that exists only in documentation.

That third one has a consequence worth stating plainly rather than leaving to be
discovered: **a pin placed by hand more than four lines down reads as NO PIN**, not as a
damaged one. `open_output()` writes it first, so this only bites a caller placing
`header()` themselves — a shebang and an encoding declaration above it are fine, a page of
preamble is not. An INHERITED pin further down is still read; it just cannot be the one
that makes the file count as an artifact.

## Every run records the command that produced it

```
command  /home/…/.venv/bin/python scripts/audit/step_review.py --generation v2026
cwd      /home/…/hcv-genotyping-release
argv     ["scripts/audit/step_review.py", "--generation", "v2026"]
```

The interpreter is part of it on purpose. In a mamba-plus-uv layout with several
environments, *which python* is most of the answer to "why did this run differ" — and the
arguments are shell-quoted, so the line can be pasted back.

## Failed runs are recorded

Register the output before you produce it, and a run that dies says what it owed you:

```python
with Run("train_model", vars(args), provenance=MODEL.with_suffix(".prov.json")) as run:
    run.seeds([20250131])
    model_path = run.output(MODEL)  # registered now, written at the end — if we get there
    df = pd.read_csv(run.input(FEATURES), sep="\t")
    ...
```

```json
"status": "failed",
"failure": {"type": "MemoryError",
            "message": "unable to allocate 12.4 GiB for the hexamer matrix"},
"outputs": [{"path": ".../model.joblib", "kind": "MISSING",
             "note": "registered but never written"}]
```

`MISSING` is usually the most informative line in the file. The exception is always
re-raised — the script still exits non-zero with its own traceback; a provenance module
that hides a failure is worse than none. A clean `SystemExit(0)` is not a failure and is
not recorded as one, because `raise SystemExit(main())` is how a CLI ends.

The predecessor appended only on success, so "300 runs" meant 300 *completed* runs with an
unknown denominator, and this package shipped with the same hole for exactly one commit.
`grep '"status": "failed"' runs.jsonl` is now the whole query.

### A kill is recorded too — and the one that cannot be

A crash was recorded and a `kill` was not, which had it backwards for anything long-running:
SLURM's time limit is **SIGTERM**-then-SIGKILL, `scancel` is SIGTERM, `docker stop` is
SIGTERM, and closing a terminal on a detached job is **SIGHUP**. Each of those left no
sidecar and no history line — a run indistinguishable from one that never started.

Inside a `with` block those now raise `Terminated`, which takes the ordinary failure path:

```json
"status": "failed",
"failure": {"type": "Terminated", "message": "terminated by SIGTERM (15)"},
"signals": {"SIGTERM": "armed", "SIGHUP": "armed"}
```

Everything up to the signal is kept — the inputs it had read and hashed, the notes it had
taken, the outputs it never got to write, as `MISSING`. `Terminated` derives from
**BaseException**, like `KeyboardInterrupt`, so an `except Exception:` around a pipeline
step cannot swallow a termination and go on to report success. (SIGINT already worked:
Python raises `KeyboardInterrupt`, which was always recorded.)

Nothing writes a record *from* the handler. A signal handler runs at an arbitrary bytecode
boundary, and doing I/O there is how you get a half-written record; raising hands the run
back to `__exit__`, which already knows how to finish one.

**`SIGKILL` and `SIGSTOP` cannot be caught by any program**, so `kill -9`, the OOM killer,
and SLURM's follow-up after the grace period still leave nothing. That is the operating
system, not a gap to be closed later, and it is said here so a missing record is not read
as a missing run.

Two things it will not do, both recorded rather than assumed. It **will not replace a
handler the caller installed** — a script with its own SIGTERM handler has decided what
termination means for it, and overriding that to improve a log would be provenance changing
the run it claims to observe. And `signal.signal` is main-thread-only, so a `Run` in a
worker thread arms nothing. Both say so in the record instead of implying coverage:

```json
"signals": {"SIGTERM": "not armed — the caller has its own handler", "SIGHUP": "armed"}
"signals": {"SIGTERM": "not armed — not the main thread", "SIGHUP": "not armed — not the main thread"}
```

`Terminated` carries `signum`, so a caller who wants the conventional shell status can have
it — nothing here imposes one:

```python
try:
    with Run("step", provenance=PROV) as run:
        ...
except Terminated as t:
    raise SystemExit(128 + t.signum) from t
```

## Environment snapshots

`environment.packages` records the tracked subset with every run — enough to explain the
usual numerical difference, useless when the cause is a package nobody thought to track.

**It tracks nothing until you ask.** The default used to be this project's own stack, so a
run that touched none of it recorded

```json
"packages": {"numpy": null, "pandas": null, "scipy": null, "sklearn": null}
```

on every line of the history, forever. Every null is truthful — `None` means "asked for,
not present" — but nobody asked, and a field populated by assumption rather than by
observation is the thing this package exists to replace. There is no domain-neutral list: a
genomics pipeline, a web service and a training run share nothing worth pinning, and the
facts that *are* universal — interpreter, platform, environment manager, lock files — are
recorded unconditionally and were never in this list. One line asks:

```python
configure(root=ROOT, tracked_packages=("numpy", "pandas", "scipy", "sklearn"))
```

`run.module(mod)` is the sharper tool for what this field is usually reached for: it
records where an import actually *resolved from*, and hashes it.

Set `env_snapshot_dir` and every run also captures the FULL installed set:

```python
configure(root=ROOT, env_snapshot_dir=ROOT / "provenance" / "environments")
```

Snapshots are **content-addressed** — `env-<sha16>.txt`, named by the digest of their own
body — so identical environments collapse to one file and the run record references it:

```json
"snapshot": {"path": ".../env-4d1ef6e1838eef07.txt",
             "sha256": "4d1ef6e1838eef07fd778849ad47bd44f1888ad756b24d6641ce6ce6c4ac235d",
             "n_packages": 17, "n_unreadable": 0, "python": "3.10.12", "reused": true}
```

`reused: true` means the environment has not moved since some earlier run. That is the
question the predecessor could not answer cheaply: it wrote one timestamped pip freeze per
invocation, and on disk that is **87 files, 32 of 55 steps covered, 8 distinct contents,
1.4 MB** — 79 byte-identical copies, and "did the environment change between these two
runs?" answerable only by diffing across timestamped filenames.

Two smaller differences. The set comes from `importlib.metadata`, so it describes the
interpreter that is actually running — the sibling's shared helper shelled out to bare
`pip freeze`, which is the pip on PATH and in a mamba-plus-uv layout need not be the same
thing. And a distribution whose version cannot be read is recorded as `UNKNOWN` rather
than omitted, because a snapshot silently missing an entry is worse than one that says so.

**conda, mamba, micromamba and pixi environments are mostly not Python distributions.**
`importlib.metadata` cannot see them, and measured on a bare `mamba create -p env
python=3.12` that is not a rounding error: conda installed **27** packages and the snapshot
recorded **8**. The nineteen it missed were `libgcc`, `openssl`, `sqlite`, `icu`, `ncurses`,
`tk` and the rest — and in a bioinformatics environment that same list is where `samtools`,
`blast` and `mmseqs2` live. The snapshot claimed to describe the environment behind a
result while omitting every non-Python tool the result depended on.

So when the interpreter's prefix has a `conda-meta/`, its packages are read from there and
rendered in conda's own `name=version=build` spelling, under a heading, and the record
gains `n_conda_packages`. The rule is *"this prefix has a conda-meta"*, not *"which tool
made it"* — measured on conda and mamba; micromamba and pixi build prefixes in the same
format and so follow by construction, which is stated that way round because neither was
run here. It is a directory read, not a subprocess — the same argument as
for `importlib.metadata`: `conda list` needs a `conda` on PATH, which need not be the one
that owns this interpreter, while the prefix cannot disagree with itself. The prefix
**path** never reaches the body: two identical environments installed at different
locations are the same environment, and a path there would make the digest machine-specific
and defeat the content addressing.

An ordinary venv has no `conda-meta`, gets no section, and its record carries no such
field.

## Rebuilding the environment that produced an output

Set `env_snapshot_dir` and every run writes a **requirements-style file automatically** —
`env-<sha16>.txt`, holding the Python version, the platform, every installed
`name==version`, and, in a conda-family prefix, every `name=version=build` from
`conda-meta`. It is named by the digest of its own body, so identical environments collapse
to one file and a changed one announces itself.

That says *what was installed*. Two more fields say **how to build it again**:

```json
"environment": {
  "manager": {"detected": ["uv", "venv"],
              "evidence": {"pyvenv.cfg:uv": "0.11.8", "CONDA_DEFAULT_ENV": "hcv"}},
  "lockfiles": [{"name": "uv.lock", "sha256": "5ad31d91…", "bytes": 58}],
  "snapshot":  {"path": "…/env-5f76ca1b0bc35e27.txt", "reused": true,
                "lockfiles": [{"path": "…/lock-5ad31d9178461ab8-uv.lock", "reused": false}]}
}
```

**The manager is detected, never guessed and never executed.** `pyvenv.cfg` is written by
`venv`, `virtualenv` and `uv` — and uv stamps its own version into it, which is the most
reliable marker available. `conda-meta/` identifies the conda family. A short list of
environment variables adds `poetry`, `pdm`, `hatch`, `rye`, `mamba` and `pixi`. The answer
is a **list**, because a uv-created venv inside a conda prefix is an ordinary thing here
and a single name would have to be wrong about one of them. An **empty** list is a real
answer too: a system interpreter built by no tool has no manager to name.

Measured on five real environments, and the difference between the two kinds of evidence
matters:

| environment | detected | evidence |
|---|---|---|
| uv-created venv | `["uv", "venv"]` | `pyvenv.cfg:uv = 0.11.8` |
| stdlib `venv` | `["venv"]` | — (a plain venv stamps no tool version) |
| mamba prefix | `["conda-family"]` | `conda-meta` |
| mamba prefix, **activated** | `["conda", "conda-family"]` | + `CONDA_DEFAULT_ENV = "hcv-test"` |
| poetry venv, via `poetry run` | `["venv", "virtualenv"]` | `pyvenv.cfg:virtualenv = 20.25.1` |

**On-disk evidence is always there; environment variables only when the environment is
activated.** That last row is the honest limit: Poetry 1.8's `poetry run` sets `VIRTUAL_ENV`
but not `POETRY_ACTIVE`, so a poetry environment is indistinguishable from any other
virtualenv unless `poetry shell` was used. The `lockfiles` field is what identifies it —
`poetry.lock` at the project root says how the project declares its dependencies, which is
the question that was being asked anyway. The two fields answer different halves: how the
**interpreter** was built, and how the **project** is declared.

Environment **names** are recorded (`CONDA_DEFAULT_ENV`, `HATCH_ENV_ACTIVE`,
`PIXI_ENVIRONMENT_NAME`) because a name is what someone asks for later. The paths beside
them are not: `VIRTUAL_ENV` and `CONDA_PREFIX` carry a username and a machine layout, and
this record gets committed and shared, so only the fact that they were set is kept.

**Lock files are hashed, and archived only when git does not already have them.** Hashing
says *which* lock — naming `uv.lock` without pinning its content names a file that moves.
Archiving as `lock-<sha16>-uv.lock` keeps the run rebuildable after that file moves on.

But a lock git already stores is **not** copied, and the record says so:

```json
{"name": "uv.lock", "sha256": "983dc95d…", "archived": false,
 "git_blob": "45b983be36b73c0788dc9cbcb76cbb80fc7bb057", "note": "git already stores it"}
```

Measured on the project this came from: `uv.lock` is 1.1 MB and the snapshot directory is
tracked, so archiving unconditionally committed a second copy of a file git already
versions — once per lock change, into a repository that is a publication artifact. Git is
the archive; `git cat-file -p <blob>` returns exactly those bytes.

The test is whether git holds these **bytes**, not whether the path is tracked: a tracked
lock with uncommitted edits is not stored yet, and that is precisely when the copy is worth
making. A project whose lock never reaches git — generated at deploy time, or built outside
a repository — gets the copy, which is the case archiving exists for. `uv.lock`, `poetry.lock`, `pdm.lock`, `pixi.lock`,
`conda-lock.yml`, `Pipfile.lock`, `environment.yml`, `requirements.txt` and rye's
`requirements*.lock` are recognised — a fixed list, so what gets captured is reviewable and
cannot quietly widen.

So for any output, at any later date: the record gives the UTC start and finish, the script
and its SHA-256, the commit and whether the tree was dirty, the exact package set, the
manager, and the lock file as it stood at that moment.

**One thing it does not do**: record every script *used* by a run. A `Run` names its own
script. A pipeline of five scripts is five runs, joined by the shared history and by
`python -m runprov lineage`, which reconstructs the DAG from digests rather than from
names.

## What the run printed

The old log's `terminal_log_file`, and the one field of it with no equivalent here until
2026-08-11. It earns its place: §7 of the source project's `RESUME.md` is a list of
incidents whose only evidence was what a run printed.

```python
configure(root=ROOT, terminal_log_dir=ROOT / "logs")  # <script>_<run_id>.log
```

**It is a tee, never a redirect.** Everything written still reaches the terminal, unchanged
and in order; this copies, it does not divert. That rule is absolute because `_report.py`
exists precisely to undo the day this library wrote to the caller's stdout and corrupted a
redirected artifact — a capture is the same defect wearing a useful hat, unless it passes
everything through. If the log cannot be opened, nothing is swapped and the run continues.

**Two mechanisms, and the difference is not a detail.** An ordinary Python write reaches
file descriptor 1 *through* `sys.stdout`. A **subprocess** writes to file descriptor 1
directly and never touches `sys.stdout`.

| mechanism | sees | when |
|---|---|---|
| `fd` | everything, **including subprocesses** | the default; `dup2` a pipe over fds 1 and 2 |
| `python` | this interpreter only | fallback when `dup2` is refused (fds closed, a daemonised job) |

So for a harness that wraps other programs, python-level capture writes **an empty file that
looks like a log**. That is why the mechanism is in the record rather than left to be
inferred — a reader has to be able to tell *"the run printed nothing"* from *"this capture
could never have seen it"*:

```yaml
terminal_log_file: "logs/demo_step_adhoc_20260811T105303Z.log"
terminal_log_capture: "fd"
```

The fallback announces itself when it happens, the same way the `flock` downgrade does.

**Three ways in, one of which takes no stream at all.** `terminal_log=PATH` on a single
`Run` captures there; `terminal_log=False` opts one step out of a project-wide default —
which a step that streams data to stdout will want. And the primitive underneath is
available on its own:

```python
run.terminal_log(LOG)  # register a log the CALLER produced — `make step 2>&1 | tee`
```

That registers an existing file as an ordinary output, hashed and pinned like any other,
and touches no stream whatsoever. It is the shape to reach for wherever taking over fds 1
and 2 would be unwelcome, and it works with subprocesses because the shell did the tee.

**The capture stops before anything is hashed.** The log is still being appended to while
the run is alive, so a digest taken first pins a prefix of the file — the one artifact
describing the run, pinned to something that never existed on disk. `__exit__` ends the
capture before `_finish` runs, which is also why the `provenance -> …` confirmation is on
your terminal but not inside the log.

**Two captures at once form a chain, and it comes apart from the inside out.** File
descriptors 1 and 2 belong to the process, not to a `Run`, so a second capture does not get
its own copy of the terminal — it takes over the first one's pipe, and the "original" it
saves *is* that pipe. Nesting therefore works: the inner capture mirrors into the outer one,
which mirrors to the terminal, and both logs are complete.

Stopping them in the wrong order is the case to know about. An outer capture stopped while
an inner one is still running cannot restore the descriptors — putting back a pipe whose
reader has gone would send every later write in the process into a void. So it does not: it
closes its log, marks the entry

```yaml
terminal_log_file: "logs/outer_adhoc_20260811T105303Z.log"
terminal_log_capture: "fd"
terminal_log_out_of_order: true
```

and the inner capture unwinds it on the way out. The cost is that the outer log ends where
it stopped rather than where its `Run` did, which is what the flag is there to say. If you
control the order, close the inner `Run` first and none of this arises.

## Keeping what the scripts it replaces did

Three things the predecessor's scripts do, and where each one lands here.

**A per-run YAML manifest.** They write `*_manifest_*.yml` with `yaml.safe_dump`.
`runprov.to_yaml()` renders the same shape with **no pyyaml** — it is the renderer behind
`log --format yaml`, so a manifest and the history view cannot disagree about a run:

```python
with Run("validate", vars(args), provenance=PROV) as run:
    ...
MANIFEST.write_text(runprov.to_yaml(run.record), encoding="utf-8")
```

After the block, deliberately: `__exit__` is where outputs are hashed and the status becomes
known. What it will **not** do is append `---` documents to a shared log — that is the
defect that left the predecessor's file unreadable partway through and spawned nine
`fix_transformation_log_*.py` repair scripts. The append-only history is JSONL for that
reason, and this renders a view of it.

### A run that is killed outright

`SIGINT`, `SIGTERM` and `SIGHUP` all reach `__exit__`, so a `scancel`, a walltime kill, a
`docker stop` or a dropped SSH session is recorded as a failed run with everything it had
read and written up to that point. Measured, one history line each.

`SIGKILL` runs no code at all — and neither does the OOM killer, a power loss or a node
failure. Nothing can write a record at that moment, so the record has to exist **before**:

```
runs.jsonl:   {"schema": "runprov.start.v1", "run_uid": "…", "script": "fetch", …}
              ← the ending never arrived

.incomplete/<run_uid>.json    ← removed at __exit__; still there
```

A `started` line is appended when the block is entered, and the matching record arrives at
the end. **A start whose `run_uid` never gets a record is the finding**, and it is permanent:
the history is append-only, so the count survives anything short of rewriting it. Beside it,
`<history>/.incomplete/` holds one marker per running run, deleted at exit — an index of
what is unfinished *now*, which the history cannot answer because it does not know what is
alive.

```
$ python -m runprov show
# 2 run(s) STARTED with no ending recorded:
#   RUNNING      align                2026-08-20T09:02:11Z  pid 41022
#   INTERRUPTED  fetch                2026-08-20T07:12:44Z  pid 24118
#   1 of them ran no ending code at all — a SIGKILL, the OOM killer, a power loss or a
#   node failure.
#   Anything they wrote is on disk and is NOT in the history: it looks exactly like a
#   completed run's output.
```

**A marker is not a death certificate.** It exists for the whole of every run, so `RUNNING`
is the ordinary state of a busy project and is not a finding. A marker from another host
reads `?` rather than being guessed at, because `os.kill(pid, 0)` there would answer about
whichever local process holds that number.

**Deleting `.incomplete/` is safe.** The `started` line is what makes a killed run
permanent, and the pid and host it carries are what decide `RUNNING` from `INTERRUPTED` — so
removing every marker changes nothing a reader sees. Verified rather than asserted: the same
killed run reports `INTERRUPTED … pid 330615` before and after the directory is removed. What
you lose is the fast path, not the answer.

Nothing else removes a marker: `__exit__` deletes its own and there is no TTL, so on a
machine that has had a few hundred SIGKILLs they accumulate. `show` prints the ten newest and
says how many it did not show; `prune` is how the directory actually shrinks.

```bash
$ python -m runprov prune --older-than 30d --dry-run
# would remove 412 in-flight marker(s) from provenance/.incomplete
#     0f3a91c2.json
#     … and 402 more
#   1 still RUNNING on this host — kept, the marker is the live evidence
#   6 from another host, so liveness cannot be checked here — kept (--other-hosts removes
#     them anyway)
```

**`prune` deletes strictly less than `rm -r` does, which is the only reason it is worth
having.** It removes only markers it can positively call `INTERRUPTED`; it never leaves the
`.incomplete` directory, checked per file, so a symlink planted there cannot redirect it; and
it only touches files that parse as runprov markers — anything else in there is counted and
left alone. Drop `--dry-run` to do it, `--older-than` to keep the recent ones, `--other-hosts`
to include the `?` rows, and `python -m runprov show --forget-markers` to clear them straight
after reading the page.

The one thing it will not do is guess. A marker from another host, or one whose `started_utc`
will not parse, is kept and counted with the reason printed — "we could not look" is not a
licence to delete. `rm -r provenance/.incomplete` remains correct and remains documented; it
is simply the blunter of the two.

If `provenance/` is tracked in git — which the README recommends — markers show up as
untracked files carrying a host, a pid and a working directory. `.incomplete/` belongs in
`.gitignore`; the history beside it does not.

Every reader drops the `started` lines, so `show`, `log` and `lineage` count runs and not
line pairs. **A run with no `provenance=` writes neither — and no marker either.** That
shape records nothing by design, and none of this changes it: the marker directory indexes
recorded runs that have no ending yet, so a run with no record has nothing to be missing
from. Entering a `with` block creates no files under `provenance/`.

#### What this does and does not recover

| | recorded after a `SIGKILL`? |
|---|---|
| that the run existed, and when it started | ✅ the `started` line, `fsync`'d on append |
| which script, which host, which pid | ✅ |
| that it never finished | ✅ — a start with no matching record |
| **what it had read, written or noted by then** | ❌ **not unless you checkpoint** |
| the artifacts it had already produced | ❌ they are on disk, linked to nothing |

**Everything a run records lives in memory until `__exit__`.** Measured: a job that
registered an input, wrote an output and noted `records_fetched: 41920`, then took a
`SIGKILL` — the history holds the start line and nothing else. `inputs`, `outputs` and
`notes` are all absent, and `results.tsv` sits beside them belonging to nothing.

**`run.write(PROV)` inside the block is the checkpoint**, and it is the answer for a long
job. It hashes what has been registered so far and puts it on disk, so a kill after it keeps
everything up to that point:

```python
with Run("fetch", provenance=PROV) as run:
    for batch in batches:
        ...
        run.note("records_fetched", n)
        run.write(PROV)  # survives a kill from here on
```

A checkpointed record says **`"status": "running"`** and **`"finished_utc": null`** — never
`ok`. The run has not succeeded, and a checkpoint that claimed it had would be the reassuring
lie the rest of this package refuses. The ending corrects it: `ok` or `failed`, with the real
time.

Two smaller limits, stated rather than discovered:

- **Liveness is same-host only.** `RUNNING` versus `INTERRUPTED` comes from `os.kill(pid, 0)`,
  so a marker written on a compute node reads `?` from anywhere else. And a pid can be
  **reused**: a marker whose number now belongs to an unrelated process reads `RUNNING`. The
  history is the durable half; the marker is a hint about right now.
- **The window before the first line.** A kill in the microseconds between entering the block
  and the `started` line landing records nothing. It cannot be closed — something has to run
  to write the first byte.

### When a line will not parse

Every reader here degrades and says how much it lost:

```
# 3 of 3 run(s) from provenance/runs.jsonl; 1 FAILED; 3 unreadable line(s) skipped
```

That tells you something is wrong and nothing about what. To see the lines themselves:

```bash
python -m runprov log --unreadable
```

```
2: {"run_id": "b", "script": "two.py", "sta
5: not json at all \x1b[31mand a terminal escape\x1b[0m\tand a tab
7: {"huge": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxx…  … 211 more character(s)
```

**Line numbers from the file**, so a count that would send you into a 100,000-line history
sends you to three lines instead. Control characters are **escaped**: the reason a line will
not parse is often that something wrote bytes into it, and printing those raw hands your
terminal whatever corrupted the file. A history line is uncapped caller data, so the text is
bounded at 200 characters and says what it cut.

**There is no repair command, and there will not be one.** JSONL loses the bad line and
counts it — that is the whole reason for the format, so damage costs exactly the damaged
lines and every other record is intact. A command that rewrote `runs.jsonl` would contradict
the claim this package is built on, and would add a new way to lose data: a bad repair
destroys good records, and the tool becomes the risk. The file that *can* be corrupted is
the YAML view, and its recovery already exists — it is printed in that file's own banner:

```bash
python -m runprov log --format yaml > provenance/transformation_log.yml
```

It **exits 0 even when it finds something**: it reports, it does not gate.

**`json_safe()` over pandas and numpy scalars.** Every such script carries a copy, and it is
not optional: `numpy.float64` subclasses `float` and survives, but `numpy.int64` and
`numpy.bool_` subclass neither `int` nor `bool`. Measured before this was fixed,
`run.note("n", df["v"].sum())` recorded the **string** `"6"` — which compares unequal to `6`
in every downstream check and renders quoted in YAML. `df.nunique()`, `.sum()` and
`(s > 1).any()` are the ordinary spellings, so it was the common case. Scalars are now
unwrapped by duck-typing `.item()`, the same rule `json_safe()` used, and with no numpy
import: this package has no dependencies and will not acquire one to describe your data.

**Logging to a file and to the terminal at once.** Keep doing it — `configure_logging()`
composes with capture and needs no change. fd-level capture moves the descriptor underneath
every writer at once, so a `FileHandler` keeps its own log, a `StreamHandler` keeps printing,
and `terminal_log` records both with the formatting intact.

One caveat, and it only bites on the **python-level fallback**: `logging.StreamHandler(sys.stdout)`
stores the stream *object*, so a handler built before the capture writes to the original
stream and its lines never reach the log — while a bare `print()`, which resolves
`sys.stdout` at call time, is captured. A script routing everything through `logging` would
get a log that looks complete and holds almost nothing. The record says so:

```json
"terminal_log": {"capture": "python", "prebound_stream_handlers": 2,
                 "note": "... 2 logging handler(s) were bound to the original streams
                          before capture started and are NOT included"}
```

Reported rather than repaired. Rebinding another library's handlers from inside a provenance
module is the overreach `_report.py` exists to prevent.

## Configuring it

`Project` holds everything location-dependent. Detection is the default; the detected root
is written into every record, so a wrong guess is visible in the artifact instead of
inferred later.

| field | default | note |
|---|---|---|
| `root` | git top level of the CWD | recorded as `code.project_root` |
| `env_snapshot_dir` | `None` (off) | full package set per distinct environment |
| `terminal_log_dir` | `None` (off) | tee stdout+stderr to `<script>_<run_id>.log`; per-`Run` `terminal_log=` overrides, `False` opts out |
| `run_log` | `<root>/provenance/runs.jsonl` | deliberately *not* any path a host repo uses — a misconfigured install must not append to a history it does not belong to. Set it and the CLI needs `--log` |
| `code_paths` | `src scripts conf pyproject.toml Makefile` | what "dirty" means. Include config and rule registries: they are read by the code, so they change behaviour like code does |
| `tracked_packages` | **nothing** | versions recorded per run. There is no domain-neutral list, so it records nothing until asked — see below |
| `run_id` | `$RUNPROV_RUN_ID`, else `adhoc_<utc>` | a chain exports one id so its stages share it; an unset id is *labelled* ad-hoc on purpose |
| `generation` | `$RUNPROV_GENERATION`, else `(default)` | a generation is a corpus; a run is one pass over it |

### What it says on the terminal, and how often

Diagnostics are unsilenceable — `RUNPROV_QUIET` cannot reach them — because a provenance
tool that can be told to stop warning is the one that gets told to stop warning. That makes
*how often* they fire a design question rather than a taste one.

| situation | what you get |
|---|---|
| **not a git repository** | one `PROVENANCE NOTE`, **once per process** |
| **a repository whose `git status` failed** | a full `PROVENANCE WARNING`, **every run** |
| **code dirty relative to the commit** | a warning naming up to 10 files, then `… and N more` |

The split exists because the two used to print the same four-line alarm. Not being under
version control is how a great many people work and will be true of every run they ever
make; repeating an alarm forever for a condition the reader cannot act on is the
permanently-red check this package refuses everywhere else, and it trains people to stop
reading warnings — including the ones that matter. A repository whose `git status` did not
run is a genuine surprise, so that one stays loud.

**The record does not change with any of this.** `git_status_captured: false` is written
the same way, every `git_*` field is null, `runprov log` prints
`DIRTY STATE UNKNOWN (git status did not run)`, and the full dirty-file list is kept. Only
the terminal is quieter, and only where quiet is honest.

### A long history is the point, so it was measured on one

A history that runs for years is the goal, not an edge case, so it was measured on one.

**The fixture, stated so the numbers can be checked rather than believed.** An earlier
version of this table cited a history nobody could rebuild, and its figures could not be
reproduced or refuted. Every number below comes from a JSONL history of exactly:

| | |
|---|---|
| runs | 100,000 |
| bytes | 74,926,704 (75 MB) |
| distinct scripts | 40 |
| distinct artifacts | 5,000 |
| distinct inputs | 500 |
| per record | one input, one output, `parameters` and `notes` on every one |

Machine: 8 cores, 15 GB RAM, CPython 3.12.13, ext4 on an external SSD, warm page cache.
Time is wall clock; memory is peak RSS of the whole process (`/usr/bin/time -f "%e %M"`), so
it includes the interpreter's own ~14 MB.

| | time | peak RSS |
|---|---|---|
| append one run | **711 µs**, and flat — 735 µs at 0 records, 711 µs at 100,000 | — |
| `runprov log` (the timeline) | 3.1 s | 24 MB |
| `runprov log --limit 5` | 2.5 s | 25 MB |
| `runprov log --format yaml` | 9.0 s | 24 MB |
| `runprov show` (the whole project page) | 4.1 s | 29 MB |
| `runprov show --stale` | 6.9 s | 33 MB |
| `show <script>` (2,500 matching runs) | 5.8 s | 46 MB |
| `show <artifact>` (20 matching runs) | 5.5 s | 25 MB |
| `runprov lineage` | 7.3 s | 54 MB |

**MEMORY is the flat one, not time.** Every reader here streams, so peak RSS barely moves
with the history: 24–54 MB against a 75 MB file, and the same at 500,000 runs. Time is
linear in the records read, and says so — an earlier table claimed `log --limit 5` cost
"the same at any history length", which is true of the memory and false of the clock. `--limit`
cannot make the read shorter; it bounds what is HELD, not what is walked.

A previous version of this table also gave `show <script>` as **0.01 s**. It is 5.8 s: the
command reads the whole history to find what matches, and always did.

**`--limit` is a view, never a trim.** `log` reads the history; it has never written to
it. Asking for the last five shows five and leaves the other 99,995 exactly where they
were — verified by sha256 before and after. It keeps a deque of N as records stream past,
so the memory is the same whether the file holds five runs or a hundred thousand.

The page **streams** the history rather than loading it. Materialising every record cost
**392 MB**; consuming them one at a time costs 3. `--stale` makes a second pass over the
file rather than keeping a second copy in memory — re-reading 91 MB costs seconds, holding
it costs hundreds of megabytes, and only one of those grows without bound as the project
does.

That reading was itself a `read_text().splitlines()` until this was measured: the whole file
as one string *and* a list of every line, before a single record was parsed. The same defect
`content_digest` had, in the other function that meets the biggest file.

## Running the network-filesystem tests on your cluster

`flock` on NFS depends on the server, the protocol version and whether `lockd` is running.
None of that can be discovered from a laptop, and asserting it from one would be a claim
about a machine that is not the machine that matters. So the real test is **opt-in and
pointed at your mount**:

```bash
RUNPROV_NETWORK_FS_DIR=/mnt/lustre/scratch/you python -m pytest -k network_fs -s
```

It appends 8 × 20 records of ~4 KB from **separate processes** — processes rather than
threads, because on a cluster the writers are separate jobs, and a thread pool shares one
file description, which is exactly the sharing that hides the bug. 4 KB straddles the bound
below which POSIX guarantees an atomic `O_APPEND` and above which it does not. A second
test *reports* whether `flock` works on that mount at all, as a measurement rather than an
assertion — plenty of exports have no `lockd`, and the point is that the answer is printed.

Without the variable both are **skipped by name**, never quietly passed.

The degraded path — no locking at all — is tested unconditionally by forcing `flock` to
raise `ENOLCK`, which is what such a mount does: every record still lands, the downgrade is
announced on stderr, and a torn line costs **one record, never the file**.

## Concurrency

The history append takes an advisory `flock`, and it is a **portability** property rather
than a Linux one. The bound usually cited for it — POSIX guarantees an `O_APPEND` write is
atomic below `PIPE_BUF`, 4,096 bytes, and the source project's history routinely exceeds it
— measured 2026-08-18 on `hcv-genotyping-release/reports/audit/runs.jsonl`: 2,453 lines,
median 2,149 bytes, max 10,661, with **205 over 4,096**. That file is appended to daily, so
those are a snapshot and re-measuring gives different numbers; the durable claim is that
some lines exceed `PIPE_BUF`, which has been true at every measurement — is not what bites
in practice:
measured with locking disabled entirely on Linux ext4, 8 processes × 20 appends of 9 KB
produced 160/160 intact records, because Linux holds the inode lock across the whole
`write()`. The lock still earns its place, on NFS and CIFS which do not honour the
guarantee at all, and on Windows which has no `O_APPEND` semantics of this kind — where CI
caught 24 concurrent appends producing 23 lines before the `msvcrt` branch existed. With
the lock in place, the same 8 × 20 × 9 KB test gives 160 lines, 160 of which parse as JSON.

Where locking is unavailable the append still happens, unlocked — **and it is announced,
on every platform**. The notice sits outside the platform branches, so an NFS or CIFS mount
whose `flock` raises `ENOLCK`, or a container without the syscall, says so on stderr rather
than degrading quietly. Those filesystems are the lock's entire justification, which makes
that the one case that most deserved announcing.

## The pin is not a comment everywhere, so `open_output` refuses 15 formats and gives 23 a sidecar

`#` is a comment in a TSV, a CSV, a GFF3 and a Makefile. It is not one in a FASTQ, and in a
Newick tree it is worse than not-a-comment: the file still **parses**, and the pin's own
words come back as taxa. Measured on Biopython 1.85, a 3-taxon tree written through the
pin:

```
baseline terminals: 3 ['HCV1a_ref', 'HCV1b_ref', 'HCV2a']
pinned   terminals: 6 ['default', 'yet', '1', 'HCV1a_ref', 'HCV1b_ref', 'HCV2a']
```

No exception, no warning. `default` and `yet` are pieces of the pin's prose —
`generation : (default)` and `commit : NONE — no commit to name (yet)` — and a downstream
clade assignment consumes that tree without complaint. That is the silent wrongness this
package exists to refuse, produced by the method advertised as the safe default.

So `open_output()` stopped leaving "`#` is not a comment everywhere" as a caveat the caller
cannot see the consequence of. **What it does depends on the format, and the split is not
even:**

- **15 suffixes RAISE** — every binary or compressed one. Not because of the pin at all:
  `open_output` returns a TEXT handle, so a caller cannot write a PNG or a parquet through
  it whatever the provenance. The message says so and names `output()` + `pin_sidecar()`.
- **23 suffixes are WRITTEN UNTOUCHED, with the pin beside them** in `<artifact>.prov.txt` —
  FASTA, FASTQ, JSON, JSONL, Newick, SAM, VCF, SVG, XML and the rest. Nothing is refused
  here; the artifact is exactly the bytes you wrote.

**And it says so when it happens**, because that second file is one you did not ask for:

```
PROVENANCE NOTE: calls.json cannot hold an in-band pin, so the provenance was written
BESIDE it as calls.json.prov.txt — a second file you did not ask for, and the one
`verify` will name.
  `run.output_json(path, obj)` embeds the pin as a key INSIDE the JSON instead, which
  keeps it to one file.
```

The surprise otherwise arrives twice: the sidecar is also what `verify` reports as the
pinned artifact, so the file you never asked for is the one the checker names back at you.
It is a note and not a warning — nothing is wrong, the sidecar is the correct answer for a
format that cannot hold a comment, and the run is fully recorded. What has to be visible is
the extra FILE.

`runprov.run.PIN_UNSAFE` is the table (a MODULE constant, not an attribute of a `Run`), with
the reason each format cannot take an in-band `#`:

| suffix | why `#` fails | `open_output` |
|---|---|---|
| `.nwk` `.newick` `.nh` `.tree` | **silently wrong** — the pin parses as taxon names | sidecar |
| `.fastq` `.fq` | no comment syntax at all; a record must begin with `@` | sidecar |
| `.fasta` `.fa` `.fna` `.faa` `.ffn` | breaks `samtools faidx`; `Bio.SeqIO` warns it will become a `ValueError`. FASTA's only spec-legal comment is `;` | sidecar |
| `.vcf` | `##fileformat` must be the first line — `bcftools` says `unknown file type` even when the pin uses `##` | sidecar |
| `.sam` | `@provenance` is not a valid header record type; only `@CO` is | sidecar |
| `.svg` `.xml` `.html` `.htm` `.xhtml` | XML has no comment syntax — measured: the file is written, then `ET.parse` fails at line 1, column 1 | sidecar |
| `.json` `.jsonl` `.geojson` `.ipynb` | JSON has no comment syntax — measured: `json.loads` fails at char 0 | sidecar |
| `.tex` | TeX comments with `%`; `#` is a macro parameter character | sidecar |
| `.bam` `.cram` `.parquet` `.h5` `.npy` `.xlsx` `.gz` `.zst` `.zip` `.png` `.pdf` | binary or compressed; `open_output` is text mode | **raises** |

The text formats are the trap in that table: `open_output` writes them happily and the
result does not look damaged until something parses it. `#` really is a comment in a
TSV, a YAML, a TOML and an INI — it is not one in XML or JSON, which have no comment
syntax at all.

**The rule is an allowlist, and that is deliberate.** `open_output()` writes the pin
in-band only for suffixes known to treat a leading `#` as a comment — `.tsv` `.csv` `.txt`
`.md` `.bed` `.gff3` `.gtf` `.yaml` `.toml` `.ini` and friends. **Everything else gets a
sidecar**, including formats nobody has thought of.

It used to be the other way round: pin in-band unless the suffix was on a list of
known-unsafe ones. That default corrupts whatever is not on the list, and three separate
rounds of review each found something that was not — Newick (a pinned tree *parses*, and
Biopython read a 3-taxon tree back with 6 terminals), then SVG and JSON, then pickle. Each
fix added a row and left the default intact. A guard whose default is to corrupt is not a
guard.

`.py` and `.sh` are deliberately **not** on the allowlist even though `#` is their comment:
their first line can be a shebang, and a pin above it stops the file being executable.
"Is `#` a comment" is not the same question as "is line 1 free".

**A format that cannot hold a pin gets one beside it.** `open_output()` writes the artifact
untouched and puts the pin in `<artifact>.prov.txt`, registered and hashed like any other
output — so a FASTA, a FASTQ, a JSONL or a Newick tree keeps the property that matters:
after the run's own sidecar has been overwritten by the next run, the artifact can still say
what it was made from. Verified with the real tools: `samtools faidx` indexes the FASTA,
Biopython reads it, `pd.read_json(lines=True)` sees **2 rows and not 3**.

It is honestly weaker than in-band — a sidecar can be separated from its artifact by a copy,
a move, or a `tar` that takes one and not the other — which is why in-band stays the default
wherever the format allows it. It is much stronger than nothing, which is what these formats
had.

Two opt-ins, because a measured trade is the caller's to make and must not be made for them:

```python
run.open_output(REF, comment="; ")  # FASTA: Biopython reads it, samtools faidx REJECTS it
run.open_output(Q, comment="-- ")  # SQL, TeX (`% `): no trade, just the right marker
run.output_json(OUT, {"variants": 12})  # JSON: pin as a top-level key — changes your schema
```

`open_output` cannot write JSON's pin, because that means serialising the whole document
rather than prepending a line, so `output_json` is its own method. It refuses a list (there is
nowhere to put a key, and wrapping it would change what the document *is*) and refuses to
overwrite a key you already use. **JSONL has no opt-in**: a leading provenance line parses,
and measured, `pd.read_json(lines=True)` then reports 3 rows for a 2-row file — the Newick
failure again, so it is sidecar-only.

Binary and compressed formats are still refused by `open_output`, and that is about the
*mode* rather than the pin — you cannot write a PNG through a text handle. Use `output()`
and pin beside it:

```python
fig = run.output(OUT / "panel.png")
plt.savefig(fig)
run.pin_sidecar(fig)
```

The old refusal named the way out, and that way out still works:

```python
out = run.output(TREE)  # still registered, still hashed, still in the record
out.write_text(newick, encoding="utf-8")
```

Only the *in-artifact* pin is given up — which a format that cannot hold one never had. The
sidecar and the history are unchanged, so `lineage` still joins on it. A caller who wants a
pin in a format with a real comment syntax can still render one and place it:
`run.header("; ")`.

**What this does not do yet** is place the pin correctly for the formats that could hold one
somewhere other than line 1 — `##provenance` after `##fileformat` in a VCF, `@CO` after
`@HD` in a SAM (which survives SAM→BAM→SAM, so it is the only route to pinning an
alignment file). Those are refusals today rather than placements, and saying so is more
useful than a table that implies they work.

## Extending it: one protocol, no hierarchy

Where records go is the one genuine variable — a git-tracked JSONL beside the code for a
published project, a shared database for a lab running many pipelines. Both are right.

```python
class SqliteSink:  # no base class, no import of runprov
    def __init__(self, conn):
        self.conn = conn

    def append(self, record: dict[str, typing.Any]) -> None:
        self.conn.execute("INSERT INTO runs VALUES (?)", [json.dumps(record, default=str)])
        self.conn.commit()


configure(root=ROOT, sink=SqliteSink(conn))
```

`default=str` matters and is not defensive padding. `parameters` holds whatever the caller
passed, so one `type=pathlib.Path` argparse option is enough: `json.dumps(record)` without
it raises `TypeError: Object of type PosixPath is not JSON serializable` inside your sink,
at the end of the run, with the work already done. `JsonlSink` passes `default=str` for the
same reason.

`RecordSink` is a `typing.Protocol`, so anything with a matching `append` qualifies. It is
`runtime_checkable`, but **that is not what validates a sink and it could not be**:
`isinstance` against a protocol checks attribute *presence*, so `isinstance([], RecordSink)`
is `True`. `configure()` instead binds the real signature against a specimen record, and
rejects a sink with no callable `append` or one whose `append` cannot take a single record:

```
TypeError: sink Wrong.append does not accept one record (missing a required argument: 'b');
RecordSink needs `append(self, record: dict) -> None`.
```

**It does not catch a bare `list`.** `list.append` is callable and takes exactly one
argument, so `configure(sink=[])` is accepted, every record goes into a list nobody reads,
and no file is ever written — verified, and it is the same example the guard's own comment
claims to have fixed. Pass a sink with a named class.

**There is deliberately no abstract base class anywhere in this package.** An ABC would
require implementers to subclass, dragging `runprov` into their type hierarchy while giving
nothing back: there is no shared behaviour to inherit, only a shape to agree on. Everything
else here has exactly one correct implementation — a SHA-256 is a SHA-256 — and inventing
extension points for them would be decoration in a package whose entire claim is that it is
the smallest thing that does the job.

Records carry a `"schema"` marker, and the two record kinds carry **different** ones: a
sidecar written by `provenance=` says `"runprov.run.v2"`, a line appended to `runs.jsonl`
says `"runprov.history.v2"`. A consumer — a script, a dashboard, an agent reading the
history — branches on that instead of guessing from which keys are present, and branching
on the wrong one of the two matches nothing at all. The marker is bumped when a field
changes meaning, never when one is added.

Types ship: the package includes a PEP 561 `py.typed` marker, and it reaches the wheel.
Verified from a consumer package with nothing but the installed wheel — `p: int =
run.input(path)` is reported as `Incompatible types in assignment (expression has type
"Path", variable has type "int")`. Without the marker those annotations are invisible and
"fully typed" means typed only for us.

## What it does not do

The pin **is** sorted and deduplicated: entries are ordered by digest, and registering the
same file twice pins it once, so `for p in DIR.glob("*.tsv"): run.input(p)` gives the same
pin on two machines despite `glob()` returning filesystem order. The sidecar still records
every registration, in order, because how many times a script opened a file is a fact about
the run. What follows from this is a limit, not a bug: **the pin cannot distinguish two runs
that read the same bytes in a different order**, and if that order changes a result, the pin
will not say so.

It cannot tell you a registered read was the read that mattered, and it cannot see a rule
reimplemented as control flow. It records; it does not audit. The checker that fails a
build when a script reads or writes something it never registered is a separate tool
(`scripts/audit/check_declared_writes.py` in the source project), and it is the half that
makes the record trustworthy rather than merely present.

## Versioning, compatibility, and what is promised

**The record format is the contract, not the Python API.** Records outlive the code that
wrote them — that is the entire point of the package — so the promise is stated separately
from the version number:

> **`runprov` reads every schema version it has ever written.** It may stop *writing* an old
> one, and it may *add* fields in any release. A field's meaning never changes without a new
> `schema` value, and a new `schema` value never makes an older record unreadable.

That is not aspirational. `SCHEMA` went `runprov.run.v1` → `v2` when `content_sha256`
changed meaning, and `tests/fixtures/` holds records generated by the v1 code itself, so the
promise is checked by the suite rather than remembered. If it is ever broken, it will be
broken by a test going red.

The **Python API** is `0.x` and may still move — the `with`-block failure recording arrived
after the first working version, and the environment snapshot after that. It goes to `1.0`
when a second project has used it for a while and the signatures have stopped changing, not
on a date.

### Support

Maintained by one person, alongside a doctoral thesis. That is the honest scope, and saying
it is better than leaving silence to be read as abandonment:

* **Bugs**: open an issue with a reproducer. Expect a reply within about two weeks. A defect
  that produces a *wrong record* is the highest priority thing in this project and will be
  treated that way; a missing feature will usually get an honest "not soon".
* **Security**: see
  [SECURITY.md](https://github.com/Taylor-Nicole/runprov/blob/main/SECURITY.md) — email, do
  not open a public issue first.
* **Python versions**: 3.10–3.13, and the classifiers say only those. A new Python is added
  the October it goes green, not on release day. This used to read "whatever CI runs", which
  was the wrong authority: see **What has actually been run** below — every version here has
  now been executed locally, which is not the same as a green matrix.
* **Dependencies**: there are none, and there will not be any. It is the property that makes
  a one-person package safe to depend on — nothing upstream can break it, and upgrading is a
  version-number change and nothing else.
* **Changes**:
  [CHANGELOG.md](https://github.com/Taylor-Nicole/runprov/blob/main/CHANGELOG.md), which
  states what was measured rather than what was improved.

## What has actually been run

A test suite proves nothing about an environment it has never entered, so this states the
environments rather than implying them.

| | state |
|---|---|
| CPython 3.10.12, 3.11.1, 3.12.13, 3.13.15 on Linux | **run in full on today's tree**, 2026-08-19 |
| macOS 3.12 | **run once and green** — 2026-08-12, run `31592997325`, commit `dec57fe6` |
| Windows 3.12 | **run once and green** — same run; it skipped 9 of 288 collected there |
| the full matrix on **today's** tree | **not run.** 93 commits have landed since `dec57fe6` |

**The matrix has run, and the honest gap is that it has not run recently.** Over 140 workflow
runs to 2026-08-19: **34 successful** (2026-08-07 to 2026-08-12), 101 failed, 5 cancelled. The
last fully green run was `31592997325` at `dec57fe6`, where every leg passed — lint, build, and
all six test legs including macOS and Windows, the latter exercising the `msvcrt` locking
fallback that exists for it.

Since **2026-08-13** every run has failed, and almost all of them died before a runner started:
GitHub bills Actions minutes for private repositories and this account's billing is failing. So
the matrix has not seen `show`, `exec`, `verify`, the transformation-log sink, or the CPython
3.13 `resolve()` fix — 93 commits' worth. Making the repository public removes the billing
constraint for standard runners, and is the one action that closes this.

> An earlier version of this section said CI had **never** run and that all 60 runs had failed.
> That was wrong, and how it was wrong is worth keeping: the check used `gh run list --limit 60`,
> which returned the 60 most recent runs — all of them the billing failures from 2026-08-13
> onward — and the window was reported as the whole history. Measuring a window and stating it as
> the total is the exact defect this package exists to catch, committed in the file that claims
> the package catches it. Found by a pre-release review that re-ran the query without a limit.

## Tests

`tests/test_runprov.py`, **about 670 tests**, all of which import `runprov` and exercise the real
objects — a test that reimplements its subject proves only that the test is self-consistent.
There is **one** `unittest.mock` use in the whole suite — in
`test_size_is_stat_ed_after_the_hash_not_before` — to
assert a call ORDER that no returned value can show. Everything else is substituted by a real
thing — **about 2,600** uses of `tmp_path` (`grep -oE '\btmp_path\b' tests/test_runprov.py | wc -l`), actual
files, actual JSONL, actual `Run` objects — or by a
narrow simulation of an environment this machine is not (`sys.platform` for Windows,
`__import__` for an absent package, `subprocess.run` for a machine with no git). Nothing
stubs the subject to make it agree with the test.

Twenty-two of them need something of the filesystem itself — a FIFO, a symlink, a file
`chmod(0o000)` really makes unreadable — and they skip where that is unavailable. The
condition is a PROBE, not `sys.platform`: symlinks work on a Windows machine with Developer
Mode enabled, and `chmod(0o000)` denies nothing to root, so a platform check both skipped
tests that would have run and ran tests that could not fail. Asking the filesystem answers
for the machine in front of you. The Windows leg therefore skips more than any other and does not assert the coverage
floor, which no leg but that one may lower. (The count of 22 is derived from this machine; the one
real Windows run skipped 9 of 288, at a commit 93 behind. Take the number from the job log
once the matrix is green on today's tree.)

Coverage is **100%** of **about 2,560 statements and 910 branches**, and the gate is set there with
`--cov-branch`.

*Every figure in this section is approximate on purpose.* They exist to convey scale, and an
exact count is stale the moment anything is added — this section has drifted four times, and
each repair was another exact number that went stale. A test now re-derives all of them and
fails if any is more than 10% out, which is the point at which the number stops conveying the
scale it was written to convey. The **100%** is not approximate: the gate enforces it, and a
separate test asserts the gate is set there.

 The branch half was added 2026-08-11 and was not decoration: statement
coverage read 100% while five conditions had never been evaluated both ways — including the
`with` block that records nothing, which is a *known* documented gap that no test held. Each
of the five tests closing them was then **mutation-tested**: the guarded behaviour was broken
on purpose in a copy of the package, and each test was confirmed to go red. One did not, and
that is the reason the practice earns its place — "no history line was written" is also true
when the write *crashed*, so the test could not tell correct silence from a swallowed
`KeyError`. It asserts on both now. Run everything CI runs
with `python ci.py` — the workflow calls that same file, so local and CI cannot drift.
