# runprov

Record what a script read, wrote and ran as — in a form a checker can verify.

```python
from runprov import Run, configure

configure(root=REPO, run_log=REPO / "reports" / "runs.jsonl")

run = Run("build_labels", vars(args))
df = pd.read_csv(run.input(INPUT))  # registering IS how you open it
with open(run.output(OUT), "w") as fh:
    fh.write(run.header())  # the pin, inside the artifact
    df.to_csv(fh, sep="\t", index=False)
run.note("n_rows", len(df))
run.write(OUT.with_name("build_labels_provenance.json"))
```

**[WHY.md](WHY.md)** explains what this is for at four lengths, with the incident behind
each design choice. **[PUBLISHING.md](PUBLISHING.md)** is the release procedure, and
**[LICENSING.md](LICENSING.md)** the licence and copyright-holder decision.

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

## One continuous history, and reading it back

`runs.jsonl` is created once by the first `run.write(...)` and **appended to forever** —
every run this project has ever recorded, in order, never rewritten. That is the same job
the sibling project's `transformation_log.yml` did, and the continuity is the property worth
keeping.

It is JSONL rather than YAML because the predecessor's file **stopped being readable**: its
writer appended `---` documents into a file that began as a list, so `yaml.safe_load_all`
raises at line 14,575, and eleven `fix_transformation_log_*.py` repair scripts exist — one of
which is itself a step in the pipeline the log documents. In JSONL every line stands alone. A
corrupt line costs one record, never the file, and the reader counts what it could not read
instead of pretending it was not there.

Read it back with the CLI — a log nobody reads is a log nobody checks:

```bash
python -m runprov log                          # timeline, oldest first
python -m runprov log --format yaml            # the transformation-log shape
python -m runprov log --failed                 # only the runs that died
python -m runprov log --script build_labels --limit 5
```

`--format yaml` deliberately keeps the old field names — `step`, `input`, `output`,
`run_command`, `date` — so anyone who could read the old file can read this one. What changed
is where the values come from: observed and hashed, rather than typed by hand. Measured on
the source project's history: **1,938 runs render to 4.7 MB, and `yaml.safe_load` parses all
1,938 entries in 3.0 s.** The file it replaces does not parse at all.

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

```python
with Run("build_labels", provenance=OUT / "build_labels_provenance.json") as run:
    ...
```

On an exception the record is written with `status: "failed"`, the exception type, message
and traceback tail, and every registered output that was never produced listed as
`MISSING` — usually the most informative line in the file. The exception is always
re-raised; a provenance module that hides one is worse than none.

Without the `with` block, `write()` is the last line of a script and a step that dies
halfway records nothing. That was the predecessor's flaw — it appended only on success, so
"300 runs" meant 300 *completed* runs with an unknown denominator — and this package
shipped with the same hole for exactly one commit. `grep '"status": "failed"' runs.jsonl`
is now the whole query.

## Environment snapshots

`environment.packages` records the tracked subset with every run — enough to explain the
usual numerical difference, useless when the cause is a package nobody thought to track.
Set `env_snapshot_dir` and every run also captures the FULL installed set:

```python
configure(root=ROOT, env_snapshot_dir=ROOT / "provenance" / "environments")
```

Snapshots are **content-addressed** — `env-<sha16>.txt`, named by the digest of their own
body — so identical environments collapse to one file and the run record references it:

```json
"snapshot": {"path": ".../env-144af87a3f4ce0fa.txt", "n_packages": 253,
             "python": "3.12.13", "reused": true}
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

## Configuring it

`Project` holds everything location-dependent. Detection is the default; the detected root
is written into every record, so a wrong guess is visible in the artifact instead of
inferred later.

| field | default | note |
|---|---|---|
| `root` | git top level of the CWD | recorded as `code.project_root` |
| `env_snapshot_dir` | `None` (off) | full package set per distinct environment |
| `run_log` | `<root>/provenance/runs.jsonl` | deliberately *not* any path a host repo uses — a misconfigured install must not append to a history it does not belong to |
| `code_paths` | `src scripts conf pyproject.toml Makefile` | what "dirty" means. Include config and rule registries: they are read by the code, so they change behaviour like code does |
| `tracked_packages` | numpy, pandas, scipy, sklearn | versions recorded per run |
| `run_id` | `$RUNPROV_RUN_ID`, else `adhoc_<utc>` | a chain exports one id so its stages share it; an unset id is *labelled* ad-hoc on purpose |
| `generation` | `$RUNPROV_GENERATION` | a generation is a corpus; a run is one pass over it |

## Concurrency

The history append takes an advisory `flock`. That is not belt-and-braces: measured on
this repository's own `runs.jsonl` — median line 2,032 bytes, **max 7,274, and 65 of 1,908
lines over 4,096** — the size below which POSIX guarantees an `O_APPEND` write is atomic.
Above it, two concurrent runs can interleave into a line that is not JSON and silently
corrupt the one append-only record. Sequential chains hide this; a parallel pipeline will
not. Where `flock` is unavailable the append still happens and the downgrade is printed.

## Extending it: one protocol, no hierarchy

Where records go is the one genuine variable — a git-tracked JSONL beside the code for a
published project, a shared database for a lab running many pipelines. Both are right.

```python
class SqliteSink:  # no base class, no import of runprov
    def append(self, record: dict) -> None:
        self.conn.execute("INSERT INTO runs VALUES (?)", [json.dumps(record)])


configure(root=ROOT, sink=SqliteSink(conn))
```

`RecordSink` is a `typing.Protocol`, so anything with a matching `append` qualifies. It is
`runtime_checkable`, and `configure()` refuses a sink that does not match — at
configuration, not at the end of a two-hour run when the record is about to be written.

**There is deliberately no abstract base class anywhere in this package.** An ABC would
require implementers to subclass, dragging `runprov` into their type hierarchy while giving
nothing back: there is no shared behaviour to inherit, only a shape to agree on. Everything
else here has exactly one correct implementation — a SHA-256 is a SHA-256 — and inventing
extension points for them would be decoration in a package whose entire claim is that it is
the smallest thing that does the job.

Records carry `"schema": "runprov.run.v1"`. A consumer — a script, a dashboard, an agent
reading the history — branches on that instead of guessing from which keys are present.
The marker is bumped when a field changes meaning, never when one is added.

Types ship: the package includes a PEP 561 `py.typed` marker, so a downstream `mypy` sees
every annotation. Without it they are invisible and "fully typed" means typed only for us.

## What it does not do

The pin lists inputs in REGISTRATION order, not sorted, so a script whose read order
varies between runs produces a different pin from the same data. Deterministic scripts are
unaffected; a script with a conditional read order should register in a fixed order.

It cannot tell you a registered read was the read that mattered, and it cannot see a rule
reimplemented as control flow. It records; it does not audit. The checker that fails a
build when a script reads or writes something it never registered is a separate tool
(`scripts/audit/check_declared_writes.py` in this repository), and it is the half that
makes the record trustworthy rather than merely present.

Coverage is **100%** and the gate is set there. Run everything CI runs with `python ci.py` — the workflow calls that same file, so local and CI cannot drift.

Tests: `tests/unit/test_runprov.py`. They import this package and assert that
`scripts/audit/_provenance.py` re-exports these objects rather than reimplementing them —
so the shim cannot quietly fork.
