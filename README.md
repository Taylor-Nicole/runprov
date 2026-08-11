# runprov

Record what a script read, wrote and ran as — in a form a checker can verify.

```python
from runprov import Run, configure

configure(root=REPO, run_log=REPO / "reports" / "runs.jsonl")

PROV = OUT.with_name("build_labels_provenance.json")

# provenance=PROV is what makes a crash record. Without it, __exit__ writes nothing.
with Run("build_labels", vars(args), provenance=PROV) as run:
    df = pd.read_csv(run.input(INPUT), sep="\t")  # registering IS how you open it
    with open(run.output(OUT), "w", encoding="utf-8") as fh:
        fh.write(run.header())  # the pin, inside the artifact
        df.to_csv(fh, sep="\t", index=False)
    run.note("n_rows", len(df))
```

That is the whole ceremony: `configure(...)` once, at import of your paths module, and one
`with Run(..., provenance=PROV)`. **There is no `run.write()` call and you do not need
one** — `__exit__` writes the sidecar and appends the history line, on success and on a
crash, and it is the only place the final status is known.

`encoding="utf-8"` is not decoration either. Without it the artifact is written in the
machine's locale encoding, and `header()` contains an em dash. Measured on one header: 189
bytes under UTF-8, 187 under cp1252 — different bytes, so a different SHA-256 for the same
artifact — and `UnicodeEncodeError` outright under cp932 or ascii. A provenance package
whose artifact hashes depend on the writer's locale has one job and does not do it, which is
why every file `runprov` writes itself pins UTF-8.

**[WHY.md](WHY.md)** explains what this is for at four lengths, with the incident behind
each design choice. **[PUBLISHING.md](PUBLISHING.md)** is the release procedure, and
**[LICENSING.md](LICENSING.md)** the licence and copyright-holder decision.

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

**The CLI does not know what your scripts passed to `configure()`.** With no `--log` it
reads `<detected root>/provenance/runs.jsonl`, so if you set `run_log` — as the quickstart
above does — every one of those commands needs `--log reports/runs.jsonl` or it will
correctly report that nothing has been recorded at the default path.

`--format yaml` deliberately keeps the old field names — `step`, `input`, `output`,
`run_command`, `date` — so anyone who could read the old file can read this one. What changed
is where the values come from: observed and hashed, rather than typed by hand. Measured on
the source project's history: **2,079 runs render to 5.0 MB, and `yaml.safe_load` parses all
2,079 entries in 6.6 s** (2026-08-10). The file it replaces does not parse at all.

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

## Configuring it

`Project` holds everything location-dependent. Detection is the default; the detected root
is written into every record, so a wrong guess is visible in the artifact instead of
inferred later.

| field | default | note |
|---|---|---|
| `root` | git top level of the CWD | recorded as `code.project_root` |
| `env_snapshot_dir` | `None` (off) | full package set per distinct environment |
| `run_log` | `<root>/provenance/runs.jsonl` | deliberately *not* any path a host repo uses — a misconfigured install must not append to a history it does not belong to. Set it and the CLI needs `--log` |
| `code_paths` | `src scripts conf pyproject.toml Makefile` | what "dirty" means. Include config and rule registries: they are read by the code, so they change behaviour like code does |
| `tracked_packages` | numpy, pandas, scipy, sklearn | versions recorded per run |
| `run_id` | `$RUNPROV_RUN_ID`, else `adhoc_<utc>` | a chain exports one id so its stages share it; an unset id is *labelled* ad-hoc on purpose |
| `generation` | `$RUNPROV_GENERATION`, else `(default)` | a generation is a corpus; a run is one pass over it |

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

Records carry `"schema": "runprov.run.v1"`. A consumer — a script, a dashboard, an agent
reading the history — branches on that instead of guessing from which keys are present.
The marker is bumped when a field changes meaning, never when one is added.

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

## Tests

`tests/test_runprov.py`, 100 tests, all of which import `runprov` and exercise the real
objects — a test that reimplements its subject proves only that the test is self-consistent.
Coverage is **100%** of 514 statements and the gate is set there. Run everything CI runs
with `python ci.py` — the workflow calls that same file, so local and CI cannot drift.
