# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/), with the record-format promise in the README
taking precedence over the Python API while this is `0.x`.

Entries state what was **measured**, not what was improved. A fix with no number beside it
is a fix nobody checked.

## [Unreleased] — 0.1.0

Nothing has been published yet. Everything below is what a first release would contain.

### The three properties it exists for

* **Registration is the ergonomic path.** `run.input(p)` returns the path, so the natural
  way to open a file is the recorded way and a skipped read is a visible omission.
* **The pin lives in the artifact**, not only in a sidecar.
* **The pin is deterministic** — no timestamp, no run id, so two identical runs over
  identical inputs do not report every artifact as changed.

### Added

- `Run`, `Project`/`configure`, `describe`, `sha256`, `content_digest`, and an append-only
  JSONL history with `flock` (and a `msvcrt` path on Windows).
- `python -m runprov log | lineage | verify`, and a `runprov` console script — `uvx runprov`
  and `pipx run runprov` resolve the second and cannot reach the first.
- **`show`** — the notebook the history already contained. `show` with no argument is the
  PROJECT page: every script, the inputs it expects (with a digest per distinct version it
  has read), the outputs it writes, its parameter and note keys, and an index of every
  artifact on record with the run that produced it. `show <target>` is one page per run,
  where the target may be a script name, a `run_uid` prefix, a `run_id` or an artifact path.
  Text or `--format yaml`. **It writes nothing** — a reader over `runs.jsonl`, asserted by a
  test that compares every byte on disk before and after.
- **`runprov exec -- <command>`** — a subprocess recorded as a run, for pipelines driven
  from a Makefile, a Snakefile or a shell script where there is no Python to hold a
  `run.tool()` call. Records the resolved tool and its version, the argv, declared inputs
  and outputs (hashed), and the exit code; **returns the command's own exit code** so it
  composes without changing what failure means. A non-zero exit is recorded as a failed run,
  and a missing program is recorded rather than raised. `--capture` tees the command's
  output at file-descriptor level.
- **`run.tool(name)` and `run.code(path)`** — the work that is not Python. `tool()` records
  which binary resolved, its `sha256`, and the version it reports (stdout *or* stderr, since
  `samtools --version` uses the latter and exits non-zero); it is bounded by a timeout,
  never raises, and records `found: false` rather than omitting an absent tool. `code()`
  registers an R script, shell wrapper or Snakefile that `sys.modules` can never see, and
  hashes it into the SAME code digest as the Python — so "did any code change" is one
  comparison across languages. The history line carries `tools` as `name → version`.
- **Every run hashes the project's own modules that it imported.** `git_commit` identifies
  the code only when the tree is clean and `script_sha256` pins the entry point alone, so a
  run whose result changed because a helper module changed had no trace of it. Measured:
  editing `src/utils/stats.py` moves the imported-code digest while `script_sha256` stays
  put. Read at exit so lazy imports count; scoped under the project root, with virtualenvs
  and build trees inside the root excluded. The history line carries one digest and a count
  (the per-file list is in the sidecar, since the history is appended forever);
  `hash_imported_code=False` turns it off and `imported_code_max` caps it.
- **A documented `note()` convention** for the questions a digest cannot answer. A digest
  says an artifact changed; it cannot say a column appeared, and runprov will not open your
  files to find out. `run.note("columns", list(df.columns))` makes "when did that column
  arrive" a dated answer from `runprov show <script>`. Keys worth standardising — `columns`,
  `n_rows`, `dtypes`, `model`/`temperature`/`prompt_sha256`, `tool_version` — are listed in
  the README, and `examples/summarise.py` now records its own schema.
- **`Project(sidecar_per_run=True)`** — a sidecar per run instead of one the next run
  overwrites: `summary.prov.json` becomes `summary.<utc>.<run_uid8>.prov.json`. Sortable by
  name because the time comes first, unique because the run_uid follows, and the stamp goes
  before the WHOLE compound suffix so `*.prov.json` still matches — inserting before `.json`
  alone silently breaks that glob. Default off, so a caller who names a path still gets it.
  It also makes `show --stale` answerable for older runs.
- **`show --stale` / `--rehash`** — a staleness column on the artifact index, answering
  "do I need to run this again" in one page. Computed from the HISTORY rather than the
  in-artifact pin, so it works for binaries that cannot hold one. `current` / `STALE` (an
  input moved) / `MODIFIED` (the artifact itself changed) / `GONE` / `?`. Off by default;
  `--stale` is one `stat` per input and reads no input bytes, taking the sizes and mtimes
  from the producing run's sidecar and reporting `?` when that sidecar is absent or has
  been overwritten by a later run.
- **`verify`** — re-derives every input a pin names and compares. Until it existed,
  invalidation was a property of the format and not of the product: everything needed was
  in the artifact and nothing read it back. Reads the artifact and nothing else — no
  history, no sidecar, no `configure()`. Transitive through inherited pins, measured on a
  two-step chain: changing the root reports **2 stale artifacts**, and `via` names the step
  whose claim failed rather than the artifact's own. `GONE` is counted apart from `STALE`
  (a stale artifact is rebuilt; a missing input is found), and zero pins found is a
  **non-zero exit** rather than a green check over nothing. The pin-digest precedence is
  one function shared with `header()`, so the checker cannot disagree with what wrote it.
- `log --format yaml`, and `runprov.to_yaml()` for a per-run manifest, both from one
  renderer and **without pyyaml**.
- **Terminal capture** (`terminal_log`), tee-never-divert, at file-descriptor level so a
  subprocess's output is seen; falls back to Python level and says so in the record.
- **Environment snapshots**, content-addressed as `env-<sha16>.txt`, plus `conda-meta`
  packages, environment-manager detection with its evidence, and lock files hashed and
  archived.
- **Failure recording.** `with Run(..., provenance=PROV)` records `status: "failed"` with
  the exception type, message and traceback tail, and every registered-but-unproduced output
  as `MISSING`.
- **First-run warning noise.** "Not a git repository" and "this repository's `git status`
  failed" printed the same four-line alarm. The first is how many people work and is true
  of every run they will ever make — repeating an alarm for a condition the reader cannot
  act on is the permanently-red check this package refuses elsewhere. It is now one note,
  once per process; a repository whose git actually failed stays loud on every run. The
  dirty-file list is capped at 10 with the remainder counted. **No record changes.**
- **Nothing is tracked until the project asks.** `tracked_packages` defaulted to this
  project's own stack, so a run touching none of it recorded
  `{"numpy": null, "pandas": null, "scipy": null, "sklearn": null}` on every history line,
  forever. Every null was truthful and nobody had asked — a field populated by assumption
  rather than observation, which is the failure the package exists to replace. There is no
  domain-neutral list; the universal facts (interpreter, platform, environment manager,
  lock files) were never in it and are still unconditional. `configure(tracked_packages=…)`
  is one line, and `run.module(mod)` is the sharper tool for what this is usually reached
  for.
- **`verify` no longer mistakes documentation for an artifact.** The anchor was matched
  anywhere in the first 64 KiB, so a bare `runprov verify` in a project with a virtualenv
  reported this package's own `verify.py` and `run.py`, their `.pyc` files, and the wheel
  METADATA — and METADATA embeds the README's *example* pin, so it invented a `GONE` for
  `data/labels.tsv`, a path that exists only in prose. A pinned artifact declares itself at
  the top; a file that merely mentions the format does not. Build, VCS and virtualenv
  directories are no longer walked, and the count of files skipped is **reported** rather
  than silently applied. Measured on one demo project: 898 files and 5 false findings →
  18 files, 1 artifact, 1 OK.
- **`examples/format_compatibility.py`** — writes an artifact in each of 60 formats through
  runprov and reads it back with that format's real library, reporting pin placement, parse,
  hash and digest stability. Skips (loudly) any format whose library is absent, so it is not
  in the test suite. Measured with every optional library installed: **60 round-tripped,
  0 failed, 0 skipped**. Without them it says so rather than passing quietly — on a bare
  install it reports `18 format(s) round-tripped, 0 failed, 42 skipped for a missing
  library`. Including Pickle, joblib,
  cloudpickle, Parquet, Feather, HDF5, AnnData `.h5ad`, Zarr, NetCDF, `.xlsx`, BAM, CRAM,
  bgzipped VCF, R `.rds`, ONNX, safetensors, PyTorch `.pt`, `.npy`, `.npz`, `.mat`, PNG, TIFF, gzip and
  SQLite. It also
  documents two honest limits: gzip is byte-unstable but content-stable (which is what
  `content_digest` is for), and SciPy `.mat` plus CRAM move on every run — `.mat` writes
  `Created on: <date>` into its header, and CRAM differs across two writes of identical
  records with an identical reference path.
- **A complete, runnable example.** `examples/summarise.py` is standard-library only and
  is executed by the suite, so it cannot quietly stop working — the README had **zero**
  whole scripts in 41 KB, every one a fragment with undefined names.
- **`runprov --version`.** It previously exited 2 with "the following arguments are
  required: cmd", which is the first thing a bug report asks for.
- **The quickstart no longer configures the CLI into failure.** It set
  `run_log=<root>/reports/runs.jsonl`, which is precisely what makes a bare
  `runprov log` report that nothing has been recorded; the default
  `<root>/provenance/runs.jsonl` is where the CLI looks.
- `CODE_OF_CONDUCT.md`, issue and pull-request templates.
- **Constructing a `Run` no longer imports the packages it tracks.** `_versions` did
  `__import__(mod).__version__`, so building a provenance object imported numpy, pandas,
  scipy and sklearn whether or not the script used them — and numpy/MKL fix their
  thread-pool configuration at import time, so the run was measurably different because it
  was traced. Versions are now read from `sys.modules` (what the script actually holds)
  then from distribution metadata, via `packages_distributions()` so `sklearn` still
  resolves through `scikit-learn`. Measured on one `Run()` with all four installed and none
  imported: **RSS +135 MB → +3 MB**, and the recorded versions are identical. A tracked
  package that is neither imported nor installed now records `None` rather than being
  imported to find out.
- **A symlinked directory inside the root pins as repository data, not as `<external>/`.**
  `_pin_name` resolved every link, so `data/ -> /mnt/bigdisk/data` — the standard layout,
  and every Nextflow/Snakemake work directory, which stages inputs as symlinks — announced
  real repository data as foreign, and `verify` then called it `UNVERIFIABLE` because
  `<external>/` is deliberately not a path. Those inputs were unpinnable *and* uncheckable.
  The spelled form is tried first and is also the more stable one: `/mnt/bigdisk/…` is this
  machine's mount layout, `data/…` is what the repository looks like everywhere. `resolve()`
  is kept as the second attempt, for a root reached through a link (macOS `/tmp`, cluster
  homes); a path containing `..` skips the first attempt, since `link/../x` normalises to
  the parent of the link and means the parent of its target. **This changes pin text for
  symlinked layouts**, which is why it lands before the first release rather than after.
- **A symlink loop no longer kills the run describing it.** `resolve()` raises
  `RuntimeError` on a loop, which is not an `OSError` and was caught by nothing: it escaped
  `_pin_name`, escaped `header()`, and took the run with it. Now pinned as external, which
  is what "we could not place this under the root" means.
- **`content_digest` blocks are bounded by bytes, not only by line count.** "Streamed" was
  true only of files whose *lines* are short, so the shape that defeated it was not a big
  file but a file with few big lines — an unwrapped FASTA, a minified JSON, a one-line
  dump. Peak RSS for one call: **607 MB → 43 MB** on 8,192 contigs of ~30 kb (246 MB file);
  unchanged on ordinary short-line text. Verified across 26 file shapes that **no digest
  moves** — a moved digest would re-pin every artifact at once. A single line longer than
  the block is still read whole, which is stated rather than implied.
- **Termination recording.** SIGTERM and SIGHUP raise `Terminated` (a `BaseException`, so a
  broad `except Exception:` cannot swallow one) and take the same path — so SLURM's time
  limit, `scancel` and `docker stop` leave a record instead of nothing. Verified against a
  real signalled process. Never replaces a handler the caller installed, and does not arm
  outside the main thread; the record states which, per signal. **SIGKILL and SIGSTOP
  cannot be caught by any program** and still leave nothing, which is stated rather than
  worked around.
- **`open_output` refuses formats a `#` pin would corrupt** (`PIN_UNSAFE`). Newick is why:
  a pinned tree *parses*, and Biopython 1.85 read a 3-taxon tree back with **6 terminals**,
  three of them harvested from the pin's own prose. FASTQ, FASTA, VCF, SAM and the binary
  formats are refused for reasons recorded per suffix — VCF and SAM reject the pin **even
  with the format's own marker**. `output()` remains the way to record an artifact that
  cannot hold a pin. **`.svg`, `.xml`, `.html`, `.json`, `.jsonl`, `.geojson`, `.ipynb`
  and `.tex` were added after instrumenting a real matplotlib script**: they are TEXT,
  so `open_output` wrote them happily and the damage only appeared when a parser
  touched it (`ET.parse` at line 1 column 1; `json.loads` at char 0). `.json` had been
  in the suite's list of formats a pin CAN go into, so a passing test was holding the
  corruption in place.
- **In-band pinning is an ALLOWLIST.** It was a denylist, so a format nobody had thought of
  got a `#` written into it — and three rounds of review each found another: Newick, then
  SVG and JSON, then pickle. Each fix added a row and left the default intact. Now only
  suffixes known to take a `#` comment are pinned in-band and everything else gets a
  sidecar, so an unrecognised format gets the safe outcome. `.py`/`.sh` are excluded on
  purpose: a pin above a shebang stops the file being executable. `.sql` and `.tex` pin
  in-band when the caller names their real marker (`-- `, `% `).
- **A sidecar pin for every format that cannot hold one in-band.** `open_output` no longer
  refuses a text format: it writes the artifact byte-exact and puts the pin in
  `<artifact>.prov.txt`, registered and hashed. Verified with the real tools — `samtools
  faidx` indexes the FASTA, Biopython reads it, `pd.read_json(lines=True)` sees 2 rows not
  3. `run.pin_sidecar(p)` does the same for a file another library wrote (a figure, a BAM).
  Two measured opt-ins: `comment="; "` for FASTA (Biopython reads it, `samtools faidx`
  rejects it — stated on stderr as the trade is made) and `run.output_json()`, which embeds
  the pin as a top-level key. **JSONL gets no opt-in**: a leading provenance line makes
  pandas read 3 rows for a 2-row file, which is the Newick failure again.

### Fixed for long histories

- **Reading the history streamed instead of slurped.** `_load` did
  `read_text().splitlines()` — the whole file as one string *and* a list of every line,
  before one record was parsed. Measured on a 100,000-run, 91 MB history: **488 MB → 392 MB**
  just from streaming the read.
- **`log` streams too, in every format.** Each record is written as it passes and dropped;
  `--limit N` keeps a deque of N and nothing else. Measured on the 91 MB history:
  **1.2 s, 24 MB**, the same at any length. The YAML banner moved to the command so it is
  emitted once rather than per record. `lineage` still materialises, because a graph
  joining outputs to inputs across the whole history has nothing to stream past.
- **`show` no longer materialises the history at all.** It consumes a stream and counts as
  it goes, so the project page costs **1.6 s and 33 MB** on that same 91 MB history instead
  of holding 392 MB. `--stale` takes a second pass over the file rather than a second copy
  in memory. Guarded by ratio tests: 4x the runs must not cost 4x the memory.

### Tested

- **NFS / Lustre.** The degraded path is tested unconditionally by forcing `flock` to raise
  `ENOLCK`: every record lands, the downgrade is announced, and a torn line costs one record
  rather than the file. The REAL test is opt-in and pointed at your mount with
  `RUNPROV_NETWORK_FS_DIR`, appending from 8 separate processes; a second one reports
  whether `flock` works there at all. Skipped by name when the variable is unset.
- **Performance regressions, as ratios rather than thresholds.** History append does not
  slow as the file grows; `project_view` is linear in run count; `read_pins` does not read
  past `SCAN_BYTES`; `content_digest` peak memory is flat against both line count and line
  length; `Run()` construction does not scale with the history. An absolute number is a test
  of the machine that set it — this suite learned that when `peak < size / 4` passed locally
  and failed in CI by 2%.

### Fixed, with what each was measured to be

- **A filename could forge a pin entry.** A crafted name containing a newline wrote an extra
  line into the `# provenance` block embedded in an artifact. Control characters, lone
  surrogates and undecodable bytes are escaped; `café` is left alone.
- **An undecodable filename killed the caller's write** — `UnicodeEncodeError` from inside
  provenance capture, so the artifact was never created.
- **Two terminal captures stopped out of order destroyed the process's stdout.** File
  descriptors 1 and 2 are process-global; restoring them in the wrong order reinstalled a
  pipe whose reader had gone, and every later write in the process vanished.
- **A `chdir` mid-run recorded a digest its own path did not name** — the record named a
  file that did not exist and carried the hash of a different one.
- **An input replaced after registration was undetectable**, so a run could pin `sha256:
  abc…` and finish beside a file that no longer had those bytes.
- **`.xlsx`, `.zip` and `.tar` rebuilt from unchanged data hashed differently every time.**
  Measured across fourteen formats: csv, tsv, txt, json, parquet, feather, pickle, npy,
  npz, sqlite and gz — eleven — were already stable; the three archives were not. Detection is by content,
  so `.docx`, `.odt`, `.whl` and `.npz` are covered too.
- **`lineage` lost every edge crossing the v1/v2 schema boundary**, reporting a real
  producer as "produced by no recorded run". Tested against records generated by the v1 code
  itself.
- **A `numpy.int64` was recorded as the string `"6"`.** It subclasses neither `int` nor
  `bool`; `numpy.float64` survived because it subclasses `float`.
- **A `Run` built and never entered kept capturing for the life of the process**, with every
  line the program printed afterwards accumulating in its log.
- **The published artifact was the one nothing verified.** The release workflow built with a
  bare `python -m build`; the full check ran only on pushes to `main`, never on a tag.
- The pin used the platform path separator (found by Windows CI); a diagnostic could kill
  the run it described; a corrupt gzip escaped `content_digest`; and a `.gz` rewritten from
  identical bytes never hashed the same twice.

### The defaults follow one rule, and it is written down

- **`Project`'s docstring now states the rule the defaults follow: record everything
  observed, guess nothing.** A review read `hash_imported_code=True` against
  `DEFAULT_TRACKED=()` as a contradiction — one doing work nobody asked for while the other
  argued "record nothing until asked". The axis is not more against less, it is OBSERVED
  against GUESSED: the modules that were imported are a fact about the run, and a package
  list is a guess about somebody's domain. The old `tracked_packages` default recorded
  `{"numpy": null, "pandas": null, …}` on every line of every history — four truthful
  answers to a question nobody posed.
- **The cost the review measured was not where it said.** It reported `hash_imported_code`
  at a 4.9x exit cost and read that as the price of hashing. Measured on 86 modules with 40
  under the root: the walk alone **17.97 ms**, hashing all 41 files **0.43 ms**. 96% of it
  was asking the filesystem at every exit whether each stdlib and site-packages module lives
  under the project root — a question whose answer cannot change for a module already
  imported.
- Two repairs: the verdict is **memoised** per `(root, __file__)`, and `os.path.realpath` +
  `startswith` replaces `Path.resolve()` + `relative_to`, because `relative_to` signals
  "not under the root" by RAISING and that is the common case (~150 exceptions built and
  thrown to compute ~150 no's). End to end: **55.3 ms → 12.2 ms** for a one-run process, and
  **1.6 ms** for every run after the first.
- **`runprov exec` follows `sidecar_per_run` instead of contradicting it.** It hardcoded
  `{name}_{run_id}.json` — per-run naming reached by a second route — while the library
  default overwrites. Two entry points, the same decision, opposite answers, and a project
  that had chosen one got the other depending on which door it came through.

### An unrequested file is announced, and the fourteen methods have families

- **`open_output` created a second file and said nothing.** Where the format cannot hold a
  comment — JSON, FASTA, JSONL, Newick, SVG and 18 others — the pin goes BESIDE the artifact
  as `<name>.prov.txt`, which is correct and was silent: a caller who wrote one `.json`
  found two files in their results directory. The surprise arrives twice, because the
  sidecar is also what `verify` reports as the pinned artifact. It now says which file was
  created, that `verify` will name it, and — for JSON — that `output_json()` keeps it to one
  file. A NOTE, not a warning: nothing is wrong, and it is the extra FILE that has to be
  visible. The in-band TRADE a few lines away already announced itself; the larger of the
  two surprises did not.
- **`Run`'s docstring now groups its fourteen methods into REGISTER / WRITE / ANNOTATE /
  FINISH.** They were listed alphabetically and nowhere else, so the rule for choosing among
  four ways of registering one artifact was invisible at the call site: `open_output` and
  `output_json` both write a JSON result correctly, produce provenance of different shapes,
  and only one of them can be checked from the artifact alone. The boundary that matters is
  the last one — `write()` is NOT in the WRITE family, because those write YOUR DATA and it
  writes THE RECORD. A test asserts every method has a line and that this boundary holds,
  since prose rots and a method added without one is a method with no stated family.

### A run that is killed outright is on record

Reported by the first real consumer: *anything runprov writes only at the end is lost by
every interrupted long job, and the artifacts on disk look identical to a successful run's.*

- **Measured before anything was built.** `SIGINT`, `SIGTERM` and `SIGHUP` were already
  covered — one history line each — because the signal handler raises and `__exit__` runs.
  `SIGKILL` was not: artifact on disk, **zero** history lines, no sidecar. So the gap was
  SIGKILL-class only: the OOM killer, `kill -9`, a power loss, a node failure. For an
  8-hour job the OOM killer is the likeliest ending there is.
- **A `runprov.start.v1` line is appended when the block is entered**, so the record of a
  run exists before the run can be killed. A start whose `run_uid` never gets a matching
  record is the finding, and it is permanent — the history is append-only. This is the
  "unknown denominator" the package criticises its predecessor for, closed for the one
  ending that runs no code.
- **`<history>/.incomplete/<run_uid>.json`** is written beside it and deleted at exit: an
  index of what is unfinished *now*, which the history cannot answer because it does not
  know what is alive. `show` is driven by the history and enriched by the markers, so the
  finding survives deleting the directory.
- **A marker is not a death certificate.** It exists for the whole of every run, so
  `RUNNING` is the ordinary state and not a finding; `INTERRUPTED` is; and a marker from
  another host reads `?` rather than being guessed at, since `os.kill(pid, 0)` there would
  answer about whichever local process holds that number.
- Every reader drops the `started` lines — `_load` and `_counted` for `show`/`lineage`, and
  `log` separately because it streams raw — so nothing counts a completed run twice. The
  YAML view declines them too: it is a narrative of completed runs.
- **A run with no `provenance=` writes neither.** That shape records nothing by design, and
  a start line there would mean constructing a `Run` created a history file.
- **A checkpoint no longer claims the run succeeded.** `run.write(PROV)` inside the block is
  the only way to get inputs, outputs and notes onto disk before a SIGKILL — `__exit__` is
  where they are persisted and SIGKILL never reaches it — so the README recommends the call.
  It used to leave `"status": "ok"` and a `finished_utc`: measured, a job checkpointed at
  hour 7 and then killed had a sidecar claiming success sitting beside the artifact, while
  the history said it never ended. A checkpoint now records `"status": "running"` and
  `"finished_utc": null`, and the ending corrects it to `ok` or `failed`.
- Sealing the record moved from `write()` to `_finish`, because one exit branch writes no
  file at all — when the caller has already written the constructor's path themselves — and
  a status stamped inside `write()` was never stamped there. Sealing is an act of ENDING a
  run, not of writing a file.
- **The README states what a SIGKILL does NOT recover**: everything registered during the
  run, unless it was checkpointed. Plus two smaller limits — liveness is same-host only and
  pids get reused, and a kill in the microseconds before the first line records nothing.

### You can see the lines a reader skipped

- **`log --unreadable`** prints the lines that will not parse, with their line numbers, and
  stops. The count was already there — `3 unreadable line(s) skipped` — and was actionable
  only as a number: it says something is wrong in a file that may hold 100,000 lines, and
  nothing about where. Control characters are escaped, because the reason a line will not
  parse is often that something wrote bytes into it and printing those raw hands the
  terminal whatever corrupted the file. Bounded at 200 characters, with what was cut stated.
- **It reads, and there is no repair command.** JSONL loses the bad line and counts it,
  which is the whole reason for the format, so damage costs exactly the damaged lines. A
  command that rewrote `runs.jsonl` would contradict the claim the package is built on and
  add a new way to lose data. The corruptible file is the YAML view, and regenerating it
  from the record of truth is already the heal story.
- **Exit 0 even when it finds something.** `log` already returns 1 for "no history at that
  path", and a second meaning on that code is a decision of its own, not a side effect of
  this one.
- Refuses `--limit`, `--script`, `--run-id`, `--failed` and `--format yaml|jsonl` with exit
  2: those select and render RECORDS, and a line that will not parse has none.

### `__all__` is 17 names, down from 27

- **Five more withdrawn on 2026-08-20**, for a different reason from the first five. Those
  were mechanism, or documentation rendered into a message, or defaults `Project` already
  supplies. These are names **nobody was ever told to call**: `installed_packages`,
  `write_snapshot`, `Capture`, `MemorySink` and `git` each had ZERO references in README,
  GETTING-STARTED, WHY and every ADR — checked, not assumed — while the features they belong
  to are documented entirely as configuration and record fields (`Project(env_snapshot_dir=…)`,
  `terminal_log`, `sink=`).
- `git` in particular: 6 internal call sites, no documented caller, and a bare `git` in an
  importing namespace collides with GitPython's top-level module. Its contract is *swallow
  every exception, return None, 20s timeout* — provenance capture, not a general-purpose
  runner. ADR-0003 left this open because a module cannot decide a package promise;
  withdrawing it closes the question without touching a single call site.
- All ten withdrawn names still exist on their own modules: `runprov.project.git`,
  `runprov.terminal.Capture`, `runprov.sinks.MemorySink`, and so on. **Withdrawn, not
  deleted**, and a test pins that.

### Every module now declares its public surface

- **Eleven of twelve modules declared no `__all__`**, so 101 top-level names were importable
  but unpromised and would have been frozen BY USE rather than by decision at the first
  upload — the same defect `__all__` itself had one level up. The rule adopted, recorded as
  **ADR-0003**: a module's `__all__` **ratifies** the package's promise and never makes one.
  A name belongs in a module list if and only if `__init__.py` promises it, with exactly one
  other source of publicness — `[project.scripts]`, which binds `runprov.__main__:main`.
  `_report.py` declares nothing: the underscore in the module name is already the statement.
  A test asserts both directions, since a missing name and an invented one are different
  mistakes.
- **Five renames landed first**, because a name recorded in two surfaces is twice the work
  to change: `environment.render` → `_render_snapshot` and `verify.render` →
  `render_report` (two different functions sharing a word, and neither raised on the other's
  input — a `verify` report rendered as a plausible environment snapshot);
  `verify.ANCHOR` → `hashing.PIN_ANCHOR` (the one sentence identifying a pin, written as a
  literal in the writer and held as a separate constant in the reader); and the deletion of
  `show.SHORT` (a second spelling of `PIN_DIGEST_CHARS`, with a test that only asserted the
  two agreed) and `run.SERIALISATION_ERRORS` (dead — a promise about an `except` clause that
  no longer exists).

### One vocabulary for both checkers

- **`show`'s states are now `verify`'s states**, imported rather than restated: `current`
  became `OK` and `?` became `UNVERIFIABLE`. They were the same five ideas spelled two ways
  — plus a case split nobody chose, one lowercase word among four uppercase ones — so the
  two commands disagreed in print about artifacts they agreed about in fact. `verify` owns
  the four shared definitions, `show` imports them and adds `MODIFIED` (only the history
  holds the artifact's own digest), `verify` keeps `NO PIN` (only the bytes can be missing
  one). There is no second spelling left to drift.
- The artifact-index column is now DERIVED from the vocabulary rather than written as a
  number. It was a hand-written 9, which fitted `MODIFIED` and not `UNVERIFIABLE` — and
  since a `:<` field pads but never truncates, the long state did not merely misalign the
  column, it ran INTO the digest: `UNVERIFIABLEa4c3ed04a95a3da1`. Widening it to 12 by hand
  would have left the same defect, one character narrower; `STATE_COLUMN` is the longest
  state plus one, so the next state added widens the column with it.
- **`--format yaml`'s `state:` values change with it**, which is the one thing here a
  machine consumer would notice. Nothing has been published, so nothing is broken.
- Two docstring errors fixed in the same pass. `staleness` described `MODIFIED` as
  "(rehash only)" — it is reachable from the STAT path too, which the code four screens
  below has always done and which is now measured in the test suite.

### The two staleness checkers now compose into a gate

- **`show --stale --exit-code`.** The package shipped two commands that answer "is this
  result still good?", and **the one that finds more problems was the one that exits 0**.
  Measured on a throwaway project: with the input changed, `verify` said STALE and exited 1
  while `show --stale` said STALE and exited 0; with the input restored and the RESULT
  hand-edited, `verify` said `OK` and exited 0 while `show --rehash` said MODIFIED — and
  exited 0. And over a BAM, which cannot carry a pin, `verify` could say nothing at all.

  The considered fix was to merge them into one engine. It was designed, reviewed by five
  independent adversarial lenses, and **abandoned on the evidence**: a pin lists the run's
  INPUTS, so the artifact's own digest is not in it and cannot be — the pin lives inside the
  file it would describe. An engine merging the two could not close the tampering case,
  because the evidence `verify` reads does not contain the answer. The two commands share
  about 12 executable lines of ~400; the rest is genuinely different work.

  So the fix is composition, and the README now documents it as the gate:

      python -m runprov verify results/ && \
        python -m runprov show --stale --rehash --exit-code

  `?` does not fail the gate — it means the check could not be made, and failing on it would
  make any project with one directory input permanently red. Two shapes are refused with
  exit 2 so exit 1 keeps one meaning: no `--stale`/`--rehash` (nothing to gate on), and a
  `show <target>` argument (which already exits 1 for "nothing matched"). `verify`'s exit
  codes are unchanged, and the exit-code VOCABULARY question is a separate open decision.

### Fixed before it could gate

- **`show --rehash` called an artifact MODIFIED that it had never digested.** `_short`
  returns `-` for an entry with no digest — right to print, and it was being compared
  against. Today's digest is not `-`, so anything the run recorded as `kind: UNHASHABLE`
  (a FIFO, a socket, a device) came back MODIFIED once the path became readable. The line
  read `MODIFIED -  pipe.out  [UNHASHABLE]`: a definite finding, the absent digest and the
  reason it is absent, contradicting each other on one line. The same comparison ran on
  INPUTS, where it reported STALE. Both now report `?` with the reason, which is what
  `verify` has always said for the same condition. Harmless only for as long as `show`
  exits 0 — which is about to change.

### Named before anyone depended on it

Nothing here is a rename to a user, because there are no users yet. It is written down
because these were the last names to settle and the reasoning belongs with the release:

- `run.write_json()` is **`run.output_json()`**. It shared a verb with `write()`, which
  writes the provenance record rather than your data; `output`, `open_output` and
  `output_json` now all mean "your data" and `write` alone means "the record".
- `show.to_yaml()` is **`show.render_yaml()`**, so there is one obvious name and not two
  reachable functions with the same one.
- `--format timeline` is **`--format text`** on every subcommand, spelled the same way
  everywhere. `timeline` still works and renders the identical bytes; it is simply not
  advertised.
- `__all__` is **22 names, down from 27**. `VOLATILE`, `VOLATILE_JSON`, `PIN_UNSAFE`,
  `default_run_id` and `default_generation` left the public surface — the first three are
  mechanism, the last two are defaults `Project` already supplies. All five still exist on
  their own modules (`runprov.run.PIN_UNSAFE`, and so on); they are no longer promises.

### Known and deliberate

- **Line endings are not content.** `content_digest` ignores a trailing newline and CRLF vs
  LF. Measured: making them significant moves 91.11% of artifacts in the project this came
  from. `sha256` is recorded beside it and preserves exact bytes.
- **A `Run` names one script.** A pipeline of five scripts is five runs, joined by the
  history and by `lineage`.
- **Records contain absolute paths and a hostname.** See `SECURITY.md`.

### Verified

601 tests, 100% statement *and* branch coverage.

**Run in full on CPython 3.10.12, 3.11.1, 3.12.13 and 3.13.15**, on Linux, 2026-08-19.
**macOS 3.12 and Windows 3.12 ran green once**, on 2026-08-12 (run `31592997325`, commit
`dec57fe6`) — but 93 commits have landed since, so the matrix has not seen `show`, `exec`,
`verify`, the transformation-log sink or the 3.13 fix. Every run since 2026-08-13 has failed
before a runner started, on Actions minutes billed for a private repository. A previous version
of this paragraph said CI had never run at all; that was measured with `gh run list --limit 60`,
which showed only the recent billing failures — a window reported as the whole. See README,
*What has actually been run*.

Widening from one interpreter to four found two defects a 3.12-only gate could not, and both
are the same shape: a test asserting something true of the interpreter rather than of the
code.

- `show --format yaml`'s depth guard hands the remainder to `json.dumps`, which is itself
  recursive, so a 1,000-deep record rendered on 3.12 and raised `RecursionError` on 3.10 —
  the version `requires-python` names as the floor. A reader narrower than its writer.
- CPython 3.13 changed `Path.resolve()`: a symlink loop **returns the path unchanged** rather
  than raising `RuntimeError`. Four tests asserted that exception as their premise and failed
  on 3.13 while the package behaved correctly — the run survives and the path pins as
  external. The premise is now expressed once, in a form true on every version, and
  `run.py`'s `RuntimeError` guards stay: a relaxation on the newer version, not a removal on
  the older ones.

Windows 3.12 runs the same suite **minus 22 tests and without the coverage floor**, and the
distinction is the point: those 22 build a fixture Windows cannot build — a FIFO
(`os.mkfifo` does not exist), a symlink (blocked without Developer Mode or admin), or a file
`chmod(0o000)` genuinely makes unreadable — so they skip, their lines go unmeasured, and
100% stops being reachable there by construction. They are skipped by PROBE rather than by
platform name, which also fixes the reverse error: `chmod(0o000)` denies nothing to root
either, so those tests could not fail inside a root container and a `win32` check called
that a pass.

This section previously read "on Linux 3.10–3.13, macOS and Windows", which the Windows job
could not have supported: all 22 failed in setup. What is verified from Linux is that the
suite has no failures when those four constructs are unavailable; whether Windows agrees
about path separators, line endings and open-file deletion is answered by the job, not from
here.

Coverage is a floor, not the argument: every fix above was mutation-tested — the defect
reintroduced, the suite required to fail.
