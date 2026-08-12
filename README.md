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

## Installing it

Zero dependencies and pure Python, so there is not much to say — but it was verified rather
than assumed, wheel and sdist, each installed and imported and `python -m runprov log` run:

| | |
|---|---|
| `pip install runprov` | wheel and sdist both ✓ |
| `uv pip install runprov` | ✓ |
| conda / mamba prefix, via `pip` | ✓ — and see [Environment snapshots](#environment-snapshots), which reads `conda-meta` |
| **Poetry ≤ 1.8** | **cannot consume it**, and neither can it consume many current packages |

That last row is worth the detail because the error names nothing useful. Poetry 1.8
resolves the dependency and then reports `Unable to create package with no name`, leaving an
environment that installed cleanly and cannot import. The cause is its bundled `pkginfo`
&lt; 1.11, which returns `name = None` for any wheel whose `Metadata-Version` is newer than it
knows; `hatchling` emits **2.5**. It is not the licence metadata — a build with the
pre-PEP-639 `license = {text = ...}` emits 2.5 just the same, which was measured before this
paragraph was written.

**The fix is Poetry 2.x** (released January 2025), whose `pkginfo` parses it. Nothing here
needs changing, and nothing here can change it short of a different build backend.

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
* **Security**: see [SECURITY.md](SECURITY.md) — email, do not open a public issue first.
* **Python versions**: whatever CI runs, currently 3.10–3.13, and the classifiers say only
  those. A new Python is added the October it goes green, not on release day.
* **Dependencies**: there are none, and there will not be any. It is the property that makes
  a one-person package safe to depend on — nothing upstream can break it, and upgrading is a
  version-number change and nothing else.
* **Changes**: [CHANGELOG.md](CHANGELOG.md), which states what was measured rather than what
  was improved.

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
