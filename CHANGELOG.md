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
