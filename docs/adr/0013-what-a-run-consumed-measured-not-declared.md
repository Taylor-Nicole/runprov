# 13. What a run consumed, measured rather than declared

Date: 2026-09-16 · Status: **proposed** · Ledger: T-29 · Raised by: Taylor

## Context

The problem, in the words it was raised in: a research developer's next step is running their
pipeline on an HPC or GPU cluster through an orchestrator, and to do that they must **declare**
resources — `--mem`, `--time`, `--cpus-per-task`, or Snakemake's `resources:`. Estimating those
is genuinely hard, and it is guessed far more often than measured.

**That is this package's own argument, one domain across.** `#SBATCH --mem=64G` is a statement
about what the author *believes* the job needs, exactly as a hand-maintained log is a statement
about what the author believes a step read. The remedy is the same: record what actually
happened, as it happens, so the next declaration is derived from a measurement instead of from
memory. On that reading it is **in scope**, and the framing has to be *what this run consumed*
— never *how to make it faster*, which is profiling and is a different tool.

## What already exists — checked, not recalled

| tool | what it gives | why it does not solve this |
|---|---|---|
| **Snakemake `benchmark:`** | a TSV per rule | only once you are already **in** Snakemake — the step this is needed *before* |
| **Nextflow trace/report** | per-task resource trace | same: presupposes adoption |
| **Slurm `sacct` / `seff`** | authoritative, per job | only after the job has run on the cluster. Chicken-and-egg for the first submission |
| `/usr/bin/time -v` | max RSS, wall, CPU | you must remember to wrap the command, and it writes to stderr, unattached to any record |
| `psutil` | everything below, portable | **a dependency**, and this package has `dependencies = []` |
| profilers (`scalene`, `memray`, `py-spy`) | where time and memory go | a different question, and far heavier than "how much did it need" |

**The gap is real and narrow:** you have plain Python scripts, you are not in an orchestrator
yet, and you need numbers to write the resource request that gets you into one. Which is the
same position `runprov` already occupies — it works on scripts that no engine runs.

**Verified from Snakemake's source** (`src/snakemake/benchmark.py`), not from its documentation,
which does not list them — the benchmark TSV columns are:

```
s   h:m:s   max_rss   max_vms   max_uss   max_pss   io_in   io_out   mean_load   cpu_time
```

and it obtains them with **psutil plus a polling daemon thread**.

## Decision

**Record what the run consumed, in a `resources` block, from the standard library only — and
name the columns that cannot be filled rather than filling them with zeros.**

### What zero dependencies can honestly measure

`resource.getrusage`, `os.times`, `time.monotonic`, and `/proc/self/status` on Linux:

| column | can runprov fill it? | from |
|---|---|---|
| `s`, `h:m:s` | **yes** | `time.monotonic` across the run |
| `cpu_time` | **yes** | `ru_utime + ru_stime`, SELF **and** CHILDREN |
| `max_rss` | **yes, with caveats below** | `ru_maxrss`, SELF and CHILDREN; `VmHWM` on Linux |
| `max_vms` | Linux only | `VmPeak` |
| `max_uss`, `max_pss` | **no** | needs `psutil` or parsing `/proc/*/smaps` |
| `io_in`, `io_out` | Linux only | `/proc/self/io` |
| `mean_load` | **no** | requires sampling, which requires a polling thread |

**A column it cannot fill is ABSENT, never zero.** A `max_pss` of `0` in a table beside real
numbers is a measurement that was never taken, presented as one that was — the defect this
package exists to catch, in a new table.

### Three measurements that decide the design

**1. The memory is in the child, and `RUSAGE_SELF` cannot see it.** Measured here: a Python
process that spawns a subprocess allocating 200 MiB reports `ru_maxrss = 15 708 KiB` for SELF
and `217 364 KiB` for CHILDREN. A bioinformatics pipeline's memory is in `samtools`, `bwa`,
`minimap2` — so an implementation reading only SELF would report **15 MiB for a run that needed
200**, and the resulting `--mem` request would be confidently, uselessly wrong.

**2. `RUSAGE_CHILDREN` is a MAXIMUM, not a SUM — and this one under-requests.** Measured: three
children each holding ~150 MiB *concurrently* report `ru_maxrss = 162 MiB`, not ~450. A job
sized from that number is OOM-killed. **This limitation must be printed beside the number**, not
recorded in a document nobody reads next to the table.

**3. Not sampled, which is a real advantage and the honest compensation for the missing
columns.** `ru_maxrss` is a high-water mark maintained by the kernel; Snakemake's `max_rss` is
the maximum of poll samples, and a poll can miss a spike between samples. Fewer columns,
and the ones present cannot miss a transient peak.

### Keeping it out of the way — the export question, which was asked explicitly

1. **Its own block, `resources`, beside `environment`** — not scattered through the record.
2. **Its own command, and its own file.** `runprov resources --format tsv` emits **Snakemake's
   benchmark column names**, in that order, with unfillable columns left empty. A researcher
   pastes it where a benchmark file goes, and existing tooling reads it without learning a new
   format. Adopting somebody else's schema is cheaper than publishing one, and it is the same
   reasoning that made the digests `sha256sum`'s own format and the export RO-Crate and PROV-JSON
   rather than something new.
3. **Never in the artifact pin.** The pin answers *does this result still follow from its
   inputs*. Peak memory does not belong to that question and would make the header
   non-deterministic across identical runs — which ADR-0004 and `content_digest()` exist to
   prevent.

### The capability difference, which this package already has a pattern for

**Windows has no `resource` module.** `observation` gains a field saying what could be measured,
for the same reason it carries `auto_available`: a record with no resource block on Windows must
not read like a run that used no memory.

## Amendment before implementation — the number a scheduler enforces is not the number `getrusage` reports

Raised by Taylor: the output should serve **Slurm and Kubernetes**, not just be a number. Asking
that question properly exposes a weakness in the decision above.

### They measure different things, and the difference is in the dangerous direction

| | what it counts |
|---|---|
| `getrusage(RUSAGE_CHILDREN)` | the high-water mark of the **largest single child**, no page cache |
| a scheduler's **cgroup** | every process in the group **concurrently**, plus page cache charged to it |

Slurm's `--mem` and Kubernetes' `resources.limits.memory` are both enforced against the
**cgroup**, and both kill the job when it is exceeded. So the `getrusage` number is
systematically **smaller** than the number that does the killing — measured above: three
concurrent children at ~150 MiB report 162, not 450. **A user who pastes a runprov figure into
`--mem=` gets OOM-killed**, and would be right to blame the tool.

**Therefore the measurement is reported as a FLOOR, never as an answer.** It is what the run
provably used; the request has to be above it, and the record says so in the same breath as the
number. A tool that hands somebody a request that kills their job has done worse than nothing.

### Where the right number lives, when it exists at all

cgroup v2 exposes `memory.peak` — the cgroup's own high-water mark, which **is** the quantity
Slurm and Kubernetes enforce. Read from one file, no dependency. Two things were measured here
and both limit it:

* **It is not always present.** This machine runs 5.15 and has `memory.current` and `cpu.stat`
  but **no `memory.peak`**; it arrived in a later kernel. Absent must be reported as absent.
* **Outside a scheduler the cgroup is not yours.** The ambient cgroup here is the user's desktop
  session and reads **8 138 MiB** of `memory.current` — the browser, the editor, everything.
  Charging that to a script would be a wildly wrong number presented with more authority than
  the honest small one.

So the cgroup is read **only when the run demonstrably owns one**, detected from markers rather
than guessed: `SLURM_JOB_ID` / `SLURM_STEP_ID`, `KUBERNETES_SERVICE_HOST`, or a cgroup path
naming `kubepods`, `docker`, `containerd` or `slurm`. On a laptop none of those is set — checked
here — and the block falls back to `getrusage` **and records which mechanism produced it**.

**That field is not decoration.** A cgroup peak and a `getrusage` peak are different quantities,
and two records that do not say which they hold are two records someone will compare anyway —
the argument `observation` already exists for.

### Units are canonical; scheduler syntax is a thin renderer

The block stores **bytes and seconds**, never `8G` or `500m`. Rendering is separate and per
target, because the targets disagree in ways that silently corrupt a number:

* **Slurm** takes `--mem` in MiB by default, `--time` as walltime, `--cpus-per-task` as a count.
* **Kubernetes** takes `memory` in binary units (`Mi`, `Gi`) — and `M`/`G` mean *decimal*, a
  4.8 % error that looks like a typo — while **CPU is a rate in millicores**, so `cpu: "500m"`
  is half a core over time, not a core count. Its `requests` schedule and its `limits` kill;
  exceeding a CPU limit **throttles** rather than killing, which memory does not.

A renderer that emitted one number into both syntaxes would be wrong in at least one of them.
Canonical storage plus per-target rendering is the only shape that survives adding a third
target, and there will be a third.

### What this adds to the decision

1. Store bytes and seconds; render per target.
2. Record **which mechanism** measured it — `cgroup` or `getrusage` — never the number alone.
3. Read the cgroup only when a marker proves the run owns one.
4. Present every figure as a **floor with a stated margin**, never as a request to paste.
5. `resources.available` says what could not be measured here, the `observation` pattern again:
   no `resource` module on Windows, no `memory.peak` on an older kernel, no cgroup of our own.

## What this must never claim

* **It is not a profiler.** It says *how much*, never *where it went*.
* **It is a measurement of one run on one machine**, not a prediction. A larger input needs more;
  this records the point you measured, not the curve.
* **`max_rss` under-reports concurrent children**, per measurement 2 above, and the number is
  useless — worse than useless — without that caveat attached to it.
* **It is not the number your scheduler enforces**, unless it came from a cgroup the run owned
  — and the record says which. Treated as a request rather than a floor, it gets the job killed.
* **It does not measure GPU memory.** `nvidia-smi` is a subprocess and a dependency in all but
  name, and a GPU field that is silently absent on a GPU job would be the worst row in the table.

## Alternatives considered

**Take `psutil` and match Snakemake column for column.** Rejected: `dependencies = []` is a
stated property of this package, load-bearing for locked-down and air-gapped machines. A
provenance tool that cannot be installed where the analysis runs records nothing.

**Shell out to `/usr/bin/time -v`.** Rejected: it measures a child, not this process, so it
answers the question only for `runprov exec`, and it is absent or different on macOS.

**A polling thread, to get `mean_load` and sampled peaks.** Rejected for now on the evidence of
the heartbeat: a thread in this package cost signal handling, fork safety and an `atexit`
backstop to get right. The high-water marks need no thread and cannot miss a spike.

**Do nothing.** The honest baseline. The record already carries `started_utc` and `finished_utc`,
so wall time is derivable today; what is missing is memory, which is the number people actually
get wrong.
