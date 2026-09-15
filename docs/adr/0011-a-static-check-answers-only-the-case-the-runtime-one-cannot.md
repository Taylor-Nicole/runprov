# 11. A static check answers only the case the runtime one cannot

Date: 2026-09-15 · Status: **proposed** · Ledger: L-107 (case A), T-27

## Context

ADR-0002 closed **case B** — a script that creates a `Run` and then opens files without
registering them — with `watch.py`, shipped in 0.1.0. It explicitly left **case A**: a script
that never imports `runprov` at all. `watch.py` says so in its own docstring:

> A script that never imports `runprov` at all — **not seen, ever.** Code that is not imported
> does not run. Only a static check over the source, or wrapping the command with
> `runprov exec`, can reach that case, and this module does not pretend otherwise.

The README makes the same concession, and points at a checker that **is not in this package**:
"a separate tool (`scripts/audit/check_declared_writes.py` in the source project)". So the
package's central argument — registration is syntactically visible, therefore a build can fail
on a read that bypassed it — rests on something it does not ship.

ADR-0002 left one question open and named the failure to avoid: *"a permanently red check
teaches its audience to ignore it."*

## Measured first, because the false-positive rate decides whether this is worth having

A prototype over `runprov/`, `examples/` and `tools/`, flagging any file with an `open`-like
call not wrapped in `run.input(...)`:

```
 20 files
  7  CASE A: does file I/O, never imports runprov
  1  CASE B: imports runprov, has unwrapped opens
```

**Eight of nine flagged files were false positives, and seven of them were this package's own
source** — a library that opens files, obviously. Excluding the package's own directory, the
way `Observer._own` already derives it rather than listing it:

```
 17 skipped: runprov's own source
  3 files
  1 CASE B: examples/format_compatibility.py, 22 unwrapped opens
  0 CASE A
```

**One flagged file, and it is also a false positive** — and *why* is the finding that shapes
this ADR. Its 22 unwrapped opens are `PIL.Image.open(p)`, `gzip.open(p, "rt")` and friends:
the file writes an artifact through `run.open_output`, then **reads it back with the format's
own library** to prove it parses. Those are read-backs, not unregistered data reads.

**A parser cannot tell those apart. The runtime check can.** `watch.py` does not report them
because the path is already in `record["outputs"]` — information that exists only while the
program runs. Proving it statically means proving the `p` on line 80 is the `p` written on
line 74: alias analysis, on arbitrary Python.

## Decision

**`runprov check` answers case A only, and says so.**

It reports a source file that **does data I/O and never imports `runprov`**. It does **not**
look for unregistered opens inside files that do use the package, because `watch.py` already
does that at runtime with information a parser cannot have, and the prototype shows that
trying produces noise — the one outcome ADR-0002 said to avoid.

Consequences of scoping it that way, on this repository: **zero false positives**, against
eight for the version that tried to do both.

1. **Scope is DERIVED, never listed.** Files under the project root, excluding vendored
   directories and excluding the installed `runprov` package itself — `Observer._own`'s rule,
   which is what took the noise from eight files to one.
2. **It reports a file, not a line.** The claim is "this file does data I/O and records
   nothing", which is checkable. "This particular read should have been registered" is not,
   for the reason above.
3. **It cannot pass vacuously.** It prints how many files it examined. A run that parsed
   nothing — a wrong root, a syntax error everywhere — must not print the same thing as a
   clean project. The same rule `attribution_check` follows.
4. **Unparseable files are REPORTED, not skipped.** A file that fails to parse was not
   checked, and "not checked" must never be silently folded into "clean".
5. **Exit non-zero on findings**, so it can gate a build — which is the whole point, and the
   half the README says makes the record trustworthy rather than merely present.

## What it still cannot see, stated before anyone relies on it

* A file that opens nothing directly and calls a library that does.
* `getattr(builtins, "open")`, `importlib`, `exec` of a string.
* Anything outside the project root.
* Whether a registered read was the read that **mattered** — unchanged, and not this tool's
  question.

So its output is *"these files record nothing"*, never *"everything else is recorded"*. The
second is not a claim any static check can make, and a checker that implied it would be the
defect this package exists to catch.

## Validated on real pipelines, 2026-09-15 — and two more rules came out of it

Measured read-only over three of the platform's repositories. **No install, no upgrade, no
run:** the check parses source, so validating it needed nothing from the pipelines but read
access — which is worth stating, because "we would have to upgrade runprov everywhere first"
is the reasonable objection it does not have.

| repo | files | flagged, naive | + entry points only | + transitive imports |
|---|---|---|---|---|
| `hcv-acquisition` | 175 | 59 | 30 | **2** |
| `taxon-corpus` | 152 | 25 | 25 | **2** |
| `hcv_pipeline_v2` | 647 | — | 14 | **14** |

**Rule 2: the subject is an ENTRY POINT, not any file that opens something.** The naive pass
flagged 59 of 175 — `lib/fsio.py`, `lib/fasta.py`, `gates/chain.py` — library modules inside
an installed package. A library function that opens a file is not analysis code; it is called
*by* analysis code. Derived from the source (`if __name__ == "__main__":`), not from a path
convention, since `src/` layouts and `scripts/` directories vary per project.

**Rule 3: "uses runprov" is resolved TRANSITIVELY through the project's own modules.** This is
the one that mattered, and it would have made the check useless without ever looking wrong.
`hcv-acquisition` has **3** files importing runprov directly and **96** reaching it through
its own `provenance.py` wrapper. Its stages `import provenance`, not `runprov` — and wrapping
the library is the *normal* way a platform adopts one. A direct-import test calls all 28
recording entry points unrecorded: false, and noisy in precisely the way ADR-0002 warned about.

**What the numbers say.** On the two repositories that do use runprov, 2 flagged files out of
175 and 152, both the same pair (`tooling/golden.py`, `gates/chain.py`) — small enough to
examine by hand, and plausibly true findings. On `hcv_pipeline_v2`, which reaches runprov
**nowhere**, 14 of 647: `build_feature_matrix_combined.py`, `classify_sequences.py`,
`run_train_golden_n9.py` and friends. That is the question this check exists to answer, asked
of a real pipeline and answered usefully.

## Alternatives considered

**Do both cases statically.** Rejected on the measurement above: it duplicates `watch.py`
worse, and the duplicate is the noisy one.

**A `sitecustomize` hook to catch case A at runtime.** Rejected in ADR-0002 and still
rejected: changing the behaviour of unrelated programs on a hospital machine is a support
incident waiting to happen.

**Leave it to `runprov exec`.** `exec` already records a command that has no runprov calls in
it, which covers case A for anything run through it. It does not answer *"is there analysis
code in this project that has never recorded anything?"*, which is the question a platform
lead actually asks, and which is answerable from the source.
