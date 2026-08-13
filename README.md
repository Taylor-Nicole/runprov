# runprov

Record what a script read, wrote and ran as — in a form a checker can verify.

A **whole script**, standard library only, that you can paste into a file and run. It is
also shipped as
[`examples/summarise.py`](https://github.com/Taylor-Nicole/runprov/blob/main/examples/summarise.py),
and a test runs it, so it cannot quietly stop working:

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
bytes under UTF-8, 187 under cp1252 — different bytes, so a different SHA-256 for the same
artifact — and `UnicodeEncodeError` outright under cp932 or ascii. A provenance package
whose artifact hashes depend on the writer's locale has one job and does not do it, which is
why every file `runprov` writes itself pins UTF-8.

**[WHY.md](https://github.com/Taylor-Nicole/runprov/blob/main/WHY.md)** explains what this
is for at four lengths, with the incident behind each design choice.
**[PUBLISHING.md](https://github.com/Taylor-Nicole/runprov/blob/main/PUBLISHING.md)** is the
release procedure, and
**[LICENSING.md](https://github.com/Taylor-Nicole/runprov/blob/main/LICENSING.md)** the
licence and copyright-holder decision.

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
| 1.11.0 | `'runprov'` — warns `NewMetadataVersion`, then parses |
| 1.12.1.2 | `'runprov'` |

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

## Two shapes that record nothing, and the one that does

Both of these look like they are recording. Neither prints a warning. Measured, running the
same failure through each:

| what the script does | crash halfway |
|---|---|
| `run = Run(...)` … `run.write(PROV)` at the end | **nothing.** No sidecar, no history line, no output |
| `with Run(...) as run:` … `run.write(PROV)` at the end | **nothing.** The `with` block is not enough |
| `with Run(..., provenance=PROV) as run:` | `status: "failed"`, the exception type, message, traceback tail, and every registered-but-unproduced output as `MISSING` |

The first shape was this README's front page for the package's whole life, and someone
integrating it copied it, their script died halfway, and the record was lost — the exact
defect the package exists to eliminate, taught by its own quickstart. The second is worse
because it looks like the fix: `__exit__` only writes when `provenance=` was passed to the
**constructor**, so adding `with` while leaving `write()` at the end buys nothing.

Calling `run.write(P)` *inside* a `with Run(..., provenance=PROV)` block is fine and
sometimes useful (a caller may want the record at a second path); the history is still
appended exactly once, at exit, with the true status. It is `write()` **instead of**
`provenance=` that loses the run.

## Why this exists rather than the obvious thing

The obvious thing already existed. `hcv_genotyping/src/utils/log_transformation.py`
exposes `append_log(entry: dict)` and **248 files import it.** It still produced a
provenance log that needs a tolerant line-wise recovery parser and eleven
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
Measured on the real 2.1 MB, 24,300-line file, which holds only **295 entries** because a
single hand-written `description:` runs to 6,544 lines:

| | |
|---|---|
| `yaml.safe_load` | `ComposerError` at line **14,547** — the writer appended `---` documents into a file that began as a list, and there are **252** of them |
| `yaml.safe_load_all` | `ScannerError` at line **14,554** — an unquoted `Note:` inside a description. A colon in prose someone typed |

Two different defects, and the second is the one that matters here: it is not the multi-
document problem at all. It is a hand-written value that happened to contain `: `, in a
writer whose quoting was correct only for the values its author had thought of. That is why
`_yaml` in this package quotes **every scalar unconditionally** rather than sniffing for
characters that need it — and why the same string round-trips through it cleanly. Eleven
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

Measured on this project's real history — **2,196 records**:

| | resolvable | ambiguous | orphan |
|---|---:|---:|---:|
| join on the path *(the heuristic)* | 282 | **3,444** | 4,188 |
| join on the digest *(this)* | **3,657** | **0** | 4,257 |

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
| in-band | TSV, CSV, BED, YAML, Markdown, SQL (`-- `) |
| in the document | JSON (`write_json`, a top-level key) |
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

### What code actually ran

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

### The work that is not Python

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
`run.note("n", df.shape[0])` records `118` and not `"<scalar 118>"`.

### Do I need to run this again?

```bash
python -m runprov show --stale     # one stat per input
python -m runprov show --rehash    # re-derive every digest, slower, no resolution limit
```

```
  current  a4c3ed04a95a3da1  out/final.txt
  STALE    ae31646fa3c0107e  out/mid.tsv       <- an input moved; rebuilding differs
  MODIFIED b413f47d13ee2fe6  out/aux.bin       <- the ARTIFACT changed, not its inputs
  GONE     2d711642b726b044  out/deleted.txt
  ?        …                                   <- cannot be told, and will not guess
```

**Answered from the history, not from the pin** — which is why it works for `aux.bin`, a
binary that could never hold a pin at all. `verify` reads the block inside an artifact;
this walks the run that produced the artifact and re-checks *that run's* inputs.

**Off unless asked, because the page is free and the check is not.** `--stale` is one
`stat` per input and reads no input bytes — there is a test asserting that. It gets the
sizes and mtimes from the producing run's sidecar, since the history line trims those
fields deliberately (they would cost 15.6% of an append-forever file). If that sidecar is
missing, or a later run has overwritten it, the answer is `?` rather than `current`:
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
| `UNVERIFIABLE` | a comparison would be meaningless, so none is claimed: a name outside the root (`<external>/…` is deliberately not a path), a name carrying an escape (a file named `a\nb` and one named `a<LF>b` render identically), or an input the run recorded no digest for |

**It will not pass having checked nothing.** Zero pins found is a non-zero exit saying so
in those words. A gate that goes green over a directory whose artifacts carry no pins is
worse than no gate, because someone will trust it — the same rule that makes
`git_status_captured: false` mean "we could not look" rather than "clean".

Exit status is 0 when every pinned artifact verifies, 1 on any `STALE`, `GONE`, or nothing
checked — so `python -m runprov verify results/` is a CI step as it stands.

Two stated limits. The pin is looked for in the **first 64 KiB** only, because reading
every byte of a 50 GB BAM to learn it has no pin is the cost that gets a checker deleted;
and the comment marker is whatever precedes the anchor on its line, so a pin written with
`header("## ")` for a VCF reads back exactly like one written with `# `.

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
defect that left the predecessor's file unreadable partway through and spawned eleven
`fix_transformation_log_*.py` repair scripts. The append-only history is JSONL for that
reason, and this renders a view of it.

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

## Concurrency

The history append takes an advisory `flock`, and it is a **portability** property rather
than a Linux one. The bound usually cited for it — POSIX guarantees an `O_APPEND` write is
atomic below `PIPE_BUF`, 4,096 bytes, and the source project's history has median line
2,017 bytes, max 7,274, with 107 of 2,086 lines over that (measured 2026-08-10; the file is
still being appended to, so these are a snapshot) — is not what bites in practice:
measured with locking disabled entirely on Linux ext4, 8 processes × 20 appends of 9 KB
produced 160/160 intact records, because Linux holds the inode lock across the whole
`write()`. The lock still earns its place, on NFS and CIFS which do not honour the
guarantee at all, and on Windows which has no `O_APPEND` semantics of this kind — where CI
caught 24 concurrent appends producing 23 lines before the `msvcrt` branch existed. With
the lock in place, the same 8 × 20 × 9 KB test gives 160 lines, 160 of which parse as JSON.

Where locking is unavailable the append still happens, unlocked. **It is only announced on
Windows** — see the finding below.

## The pin is not a comment everywhere, so `open_output` refuses some formats

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

So `open_output()` now **refuses** rather than leaving "`#` is not a comment everywhere" as
a caveat the caller cannot see the consequence of. `run.PIN_UNSAFE` is the table, with the
reason each format fails:

| | |
|---|---|
| `.nwk` `.newick` `.nh` `.tree` | **silently wrong** — the pin parses as taxon names |
| `.fastq` `.fq` | no comment syntax at all; a record must begin with `@` |
| `.fasta` `.fa` `.fna` `.faa` `.ffn` | breaks `samtools faidx`; `Bio.SeqIO` warns it will become a `ValueError`. FASTA's only spec-legal comment is `;` |
| `.vcf` | `##fileformat` must be the first line — `bcftools` says `unknown file type` even when the pin uses `##` |
| `.sam` | `@provenance` is not a valid header record type; only `@CO` is |
| `.svg` `.xml` `.html` `.htm` `.xhtml` | XML has no comment syntax — measured: the file is written, then `ET.parse` fails at line 1, column 1 |
| `.json` `.jsonl` `.geojson` `.ipynb` | JSON has no comment syntax — measured: `json.loads` fails at char 0 |
| `.tex` | TeX comments with `%`; `#` is a macro parameter character |
| `.bam` `.cram` `.parquet` `.h5` `.npy` `.xlsx` `.gz` `.zst` `.zip` `.png` `.pdf` | binary or compressed; `open_output` is text mode |

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
run.write_json(OUT, {"variants": 12})  # JSON: pin as a top-level key — changes your schema
```

`open_output` cannot write JSON's pin, because that means serialising the whole document
rather than prepending a line, so `write_json` is its own method. It refuses a list (there is
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

**A locking downgrade is silent on POSIX.** If `flock` raises — an NFS or CIFS mount, a
container without the syscall — `_exclusive()` falls through to an unlocked append and
prints nothing, because the notice sits inside the branch that only runs on `win32`.
Verified by stubbing `fcntl.flock` to raise `OSError`: the line is written, no notice
appears. The lock's whole justification is the filesystems where this happens, so this is
the case that most deserves to be announced.

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
* **Python versions**: whatever CI runs, currently 3.10–3.13, and the classifiers say only
  those. A new Python is added the October it goes green, not on release day.
* **Dependencies**: there are none, and there will not be any. It is the property that makes
  a one-person package safe to depend on — nothing upstream can break it, and upgrading is a
  version-number change and nothing else.
* **Changes**:
  [CHANGELOG.md](https://github.com/Taylor-Nicole/runprov/blob/main/CHANGELOG.md), which
  states what was measured rather than what was improved.

## Tests

`tests/test_runprov.py`, 288 tests, all of which import `runprov` and exercise the real
objects — a test that reimplements its subject proves only that the test is self-consistent.
There are **no mocks**: not one `unittest.mock` import in the suite. Substitution is either a
real thing (898 uses of `tmp_path` — actual files, actual JSONL, actual `Run` objects) or a
narrow simulation of an environment this machine is not (`sys.platform` for Windows,
`__import__` for an absent package, `subprocess.run` for a machine with no git). Nothing
stubs the subject to make it agree with the test.

Coverage is **100%** of 1,282 statements **and 424 branches**, and the gate is set there with
`--cov-branch`. The branch half was added 2026-08-11 and was not decoration: statement
coverage read 100% while five conditions had never been evaluated both ways — including the
`with` block that records nothing, which is a *known* documented gap that no test held. Each
of the five tests closing them was then **mutation-tested**: the guarded behaviour was broken
on purpose in a copy of the package, and each test was confirmed to go red. One did not, and
that is the reason the practice earns its place — "no history line was written" is also true
when the write *crashed*, so the test could not tell correct silence from a swallowed
`KeyError`. It asserts on both now. Run everything CI runs
with `python ci.py` — the workflow calls that same file, so local and CI cannot drift.
