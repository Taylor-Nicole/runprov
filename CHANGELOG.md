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
- **`verify` no longer mistakes documentation for an artifact.** The anchor was matched
  anywhere in the first 64 KiB, so a bare `runprov verify` in a project with a virtualenv
  reported this package's own `verify.py` and `run.py`, their `.pyc` files, and the wheel
  METADATA — and METADATA embeds the README's *example* pin, so it invented a `GONE` for
  `data/labels.tsv`, a path that exists only in prose. A pinned artifact declares itself at
  the top; a file that merely mentions the format does not. Build, VCS and virtualenv
  directories are no longer walked, and the count of files skipped is **reported** rather
  than silently applied. Measured on one demo project: 898 files and 5 false findings →
  18 files, 1 artifact, 1 OK.
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
  cannot hold a pin.

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
  Measured across fifteen formats: csv, tsv, txt, json, parquet, feather, pickle, npy, npz,
  sqlite and gz were already stable; the three archives were not. Detection is by content,
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

288 tests, 100% statement *and* branch coverage, on Linux 3.10–3.13, macOS and Windows.
Coverage is a floor, not the argument: every fix above was mutation-tested — the defect
reintroduced, the suite required to fail.
