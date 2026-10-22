# 0002 — Warning when a read bypasses registration

- **Status:** **Accepted**, 2026-08-19, and implemented in 0.1.0 as `runprov/watch.py`. Was Proposed; Taylor's decision to include it is recorded under *Decision* below.
- **Date:** 2026-08-19
- **Raised by:** Taylor, asking whether `runprov` can warn when it is installed in a project but a script is not actually recording.
- **Applies to:** would touch `runprov/run.py`, `runprov/__main__.py`; a new module

## Context

The package's own documentation states the limitation plainly: *it records; it does not audit.*
If a script reads a file without `run.input(p)`, that read is invisible — absent from the
artifact header and from the history — and **nothing says so**. The record then looks complete
while being incomplete, which is worse than an obviously missing record, because it invites
trust it has not earned.

This is the same shape as the defect that produced the package. `append_log(entry)` recorded
what its author *believed* a step read. Registration-at-the-read fixes the accuracy of what is
recorded; it does not fix the *omission* of what is not.

Three distinct situations get conflated when people ask for "a warning", and they have different
answers:

| # | situation | can the package see it? |
|---|---|---|
| A | a script never imports `runprov` at all | **No.** None of its code runs |
| B | a script imports it and creates a `Run`, but opens files without registering them | **Yes** — demonstrated below |
| C | a script imports it and never creates a `Run` | Partly — import happens, so a hook could fire |

Situation A is the one the question names most directly, and it is the one the package
fundamentally cannot answer from inside. Code that is not imported does not execute. Anything
claiming otherwise would have to hook every Python process on the machine.

## Prototype result (B is feasible today)

`sys.addaudithook` (CPython 3.8+) fires an `open` event for every file opened in the process.
Installed at `Run` construction and compared against the record at exit, it identifies reads that
bypassed registration. Measured 2026-08-19 on CPython 3.12:

```python
with runprov.Run("demo", provenance=...) as run:
    with open(run.input(d / "declared.tsv")) as fh:
        ...  # registered
    with open(d / "lookup.csv") as fh:
        ...  # NOT registered
    with run.open_output(d / "out.tsv") as fh:
        ...
```

```
data files opened     : ['declared.tsv', 'lookup.csv', 'out.tsv']
registered            : ['declared.tsv', 'out.tsv']
>> UNREGISTERED READ  : ['lookup.csv']
```

Exactly the unregistered read, no false positives on the registered ones. This is the same
failure a workflow engine also cannot see (an undeclared input), so closing it would be a real
differentiator rather than a nicety.

## Options

**1. Runtime audit hook, warn at run end (situation B).**
Proven above. Report on stderr and, better, record the fact *in the record*: an
`unregistered_reads` field is more valuable than a warning nobody reads, because it makes the
omission visible to `verify` and to a reviewer later. Cost: an audit hook fires on every `open`
in the process, including the interpreter's own imports, so it needs careful filtering and has
a real performance question on read-heavy runs — both must be **measured** before this is
accepted, not assumed.

**2. Static check over the project (situations A and C).**
A separate command — `runprov check` — that parses the project's scripts and reports files that
call `open()`/`read_csv()` without going through `run.input()`, and scripts that never construct
a `Run` at all. This is the "separate checker that fails the build on an unregistered read"
already named in the README as the missing half. It is the only thing that can see situation A,
and it composes with CI. Cost: AST analysis is approximate, and every approximate checker
eventually produces a false positive that erodes trust in it.

**3. A `sitecustomize`/`.pth` hook that fires for every Python process.**
Would catch situation A. **Rejected outright.** A package that installs a global import hook
changes the behaviour of unrelated programs on the machine. That is bad manners and, in a
hospital environment, a support incident waiting to happen.

**4. `runprov exec python script.py`.**
The wrapper already exists for non-Python commands. Extended with option 1's hook, it could
report unregistered reads for a script that does not import `runprov` at all — the only honest
route to situation A at runtime, because the wrapper is the parent process.

## Open questions, deliberately unanswered

- **Warn, or record?** A warning is ephemeral; a field in the record is permanent and checkable.
  Recording it may be strictly better, and cheaper to get right.
- **What is the false-positive rate?** A run legitimately opens config files, fonts, locale data
  and its own outputs. Warning about all of them trains users to ignore the warning — the exact
  failure the deterministic-pin decision exists to prevent (a permanently red check is worse
  than no check).
- **Whose default?** Off by default and opt-in, or on with an opt-out? Opt-in is safer and gets
  used less.
- **Does this belong in this package at all?** The README currently promises the checker is
  *separate*. Absorbing it makes the package larger and the boundary blurrier, and 0.1.0 is a
  bad moment to widen scope. A separate `runprov-check` distribution is a real alternative.

## Decision

**Option 1 is implemented and on by default in 0.1.0.** The Proposed version of this ADR
recommended deferring to 0.2.0 on scope grounds. Taylor overrode that, and the reasoning is
better than the recommendation was: *the point of the package is to build a culture of
traceability around the user*, and a package that silently lets you not record is not building
one. A limitation the documentation merely concedes is a limitation the user never learns about.

What shipped, and how each open question above was settled:

| question | settled as |
|---|---|
| warn, or record? | **both** — stderr once, plus `unregistered_reads` in the sidecar AND the history, because a warning scrolls past and a field is checkable years later |
| false-positive rate | **measured, zero**: a run importing four stdlib modules, setting a locale, reading a config and a registered input produced 13 opens and 1 report — the config, genuinely unregistered |
| default | **on**, per the culture argument; `warn_unregistered_reads=False` per project |
| does it belong here? | yes for case B, which is runtime and cheap. Case A still needs a separate static check, and the README says so rather than implying the warning covers it |

Cost, measured: paid once at exit, proportional to *distinct* unregistered files — +0.4 ms for
a realistic run, +21 ms for 400. `WATCH_MAX_PATHS` bounds memory at 2,000 distinct paths.

Two implementation notes worth keeping, because both were bugs first:

- **Exclude the package's own files as FILES, never as directories.** `run_log=` and
  `provenance=` both routinely point at the project root, so excluding a parent directory
  silently disables the whole check. Both forms of that were written and caught here — a filter
  that is too broad reports nothing and looks exactly like one that works.
- **Registered outputs are not in `record["outputs"]` yet** when the check runs, because they
  are described in `write()`. `_pending` has to be consulted too, or every correct output is
  reported as an unregistered read.

## Amended 2026-10-22 — the observer must not attribute its own reads [G8]

The second implementation note above is correct about the subject it was written for and was
applied to a subject it was not. *"Exclude the package's own files as FILES, never as
directories"* is about `run_log=` and `provenance=`, which routinely point at the project root;
excluding a parent there would disable the whole check, and that is why the rule exists.

A **registered directory input** is a different population. `run.input(DIR)` hashes the tree, so
`hashing` opens every file under it — and the audit hook saw those opens and reported them as the
caller's unregistered reads. Measured on 0.9.0:

| | |
|---|---|
| the record said | `unregistered_reads: ["ckpt/config.json", …]` |
| the pin said | `bb1a9a79e5328600  ckpt`, the `sha256_tree` over exactly those bytes |
| the warning said | *"NOT in the record and NOT in the pin"* — **false** |
| `no_unregistered_reads` said | **VIOLATED**, `GATE: VIOLATED (exit 1)`, for a correct run |
| scope | a directory input **under the project root**, with digest on. Outside the root, clean; with `digest=False`, clean — the negative control that named the cause |

Worse than a noisy warning: the remedy it offered was `warn_unregistered_reads=False`, which this
ADR's own `blind` note records as the route to a vacuous green, and which is PROJECT-level. Taking
the advice would trade one false VIOLATED for a permanent silent MET.

**Decision: suppress attribution while the package itself is reading, and nowhere else.**
`watch.our_reads()` is a re-entrant, per-thread depth counter; the audit hook returns early while
it is raised; `hashing.sha256` and `hashing.content_digest` carry `@_our_read`, and those two are
the only public functions in that module that open a file — asserted from the AST rather than
listed, so a new public reader goes red on arrival.

**The rejected repair, and why, because it is the one that looks obvious.** Excluding a registered
directory's whole subtree in `unregistered`'s filter would have been unsound: a tree digest covers
the files that existed **when it was taken**, so a file created after registration and then read
is not in the pin, and a subtree exclusion would have hidden it. The test for this carries that
exact case as its control and requires it to still be reported.

**Per-thread, not per-process**, because a read is attributable to whoever made it: a module-level
counter would blind the watch to another thread's genuine omissions for as long as a hash took,
and this package already runs two pump threads during capture.

**What this does not reach.** `sys.addaudithook` callbacks are invisible to `coverage.py`, so
`tools/bench.py` returns **VOID** for a mutation inside the hook — neither control 6 (executed)
nor control 6s (read) can fire. The red was demonstrated by hand instead, with the restore proved
by digest, and the AST guard was benched normally and reported CAUGHT. That is a third entry
beside the harness's known blind spots, a subprocess read and a docstring subject.

## Superseded recommendation, kept for the record

Do **not** implement before 0.1.0. The package should ship doing one thing that it does
completely. But option 1 is cheap, proven, and addresses a limitation the documentation
currently just concedes — so it is the strongest candidate for 0.2.0, with the false-positive
question settled by measurement on a real pipeline before anything is switched on.

Whatever is chosen, the honesty rule holds: if the package cannot see situation A, it must say so
rather than implying its warning means "everything was recorded".
