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
  rejects it — stated on stderr as the trade is made) and `run.write_json()`, which embeds
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

### Known and deliberate

- **Line endings are not content.** `content_digest` ignores a trailing newline and CRLF vs
  LF. Measured: making them significant moves 91.11% of artifacts in the project this came
  from. `sha256` is recorded beside it and preserves exact bytes.
- **A `Run` names one script.** A pipeline of five scripts is five runs, joined by the
  history and by `lineage`.
- **Records contain absolute paths and a hostname.** See `SECURITY.md`.

### Verified

568 tests, 100% statement *and* branch coverage.

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
