# 8. `capture` observes what declaration cannot reach

Date: 2026-09-04 · Status: accepted · Ledger: T-19 · Decided by the author, not the applier

## Context

`run.input(p)` returns the path, so the natural way to open a file is the recorded way. That
design is the reason this package exists and it is not in question here.

It still costs **one edit per call, in every script, paid by every colleague, for ever**. And
the scripts that most need a record are the exploratory ones — the Tuesday-afternoon analysis
whose number reaches a manuscript — which are exactly the scripts nobody is going to refactor.
`WHY.md` makes that argument in the other direction already: *"an engine needs the DAG to
exist; exploration is where the shape is the unknown"*. The same sentence applies one rung
down, to this package's own adoption cost.

Meanwhile the machinery to observe without any edit **was already installed and used only to
scold**. `sys.addaudithook` fires for every `open` in the process; `watch.unregistered()`
already filters the result down to data files under the root, dropping code, caches,
site-packages and this package's own files. Every one of those exclusions was put there by a
false positive somebody met. All of it existed to print a warning.

## Decision

**`runprov capture script.py` runs an unmodified script and records what it actually opened.**

In-process, via `runpy`, and that is the whole mechanism: the audit hook is per-interpreter. A
subprocess has its own, which is why `runprov exec` can record a command, its tool versions
and its exit code but can never see the files inside it. Both subcommands exist because
neither can do the other's job.

**A file the run wrote is an output, even if it also read it.** A script that appends to a
table has not taken that table as an input, and recording it as both would draw a lineage edge
from a file to itself. The audit event has carried the mode all along — PEP 578 passes
`(path, mode, flags)` — and this hook read only the first of the three for the whole of its
life. A missing or non-string mode is read as a **read**, because misfiling a read as an
output would claim the run *produced* a file it only looked at.

**The filtering is `unregistered()`, reused rather than reimplemented.** A second set of rules
about what counts as data is a second set to keep in step, and this one is already correct
because it was corrected by use.

## Consequences

* **An observed record is wider and weaker than a declared one.** It lists what was opened,
  not what mattered, and **nothing in it is pinned into an artifact** — `open_output` remains
  the only way an artifact carries its own provenance and can be checked from its own bytes
  (ADR-0006). This is a rung below `run.input()`, not a replacement, and the README says so
  where somebody about to adopt it will read it.
* **Nothing is wasted at the migration.** The record is the same format, so a project that
  starts with `capture` and later registers properly keeps everything it wrote.
* It runs user code in this interpreter. `sys.argv` is set for the script and restored
  afterwards; `SystemExit` becomes the command's exit code so it composes in a Makefile; an
  exception is recorded as a failed run and re-raised.
* Arguments after the script name belong to the **script** (`argparse.REMAINDER`), so a
  script's own `--provenance` is not eaten by runprov's.

## Alternatives considered

**Make `exec` do it.** It cannot. The child has its own interpreter and its own audit hooks;
seeing inside it would need `strace`, `LD_PRELOAD` or a ptrace supervisor — a dependency, a
platform story, and a privilege question, for a package with none of the three.

**Instrument the script's AST**, as `noWorkflow` does. It yields much more — internal
dataflow, which column mattered — and it changes the program it observes. This package has a
test section headed *"provenance must not change the program it observes"*, and for a record
that may end up in a clinical result that is the right side to be on.

**Monkeypatch the readers**, as `recipy` does — `pandas.read_csv`, `numpy.load` and so on.
Zero code change too, and it only sees the libraries somebody remembered to patch: an
unpatched reader is invisible, silently. The audit hook sees every `open` in the process
because the interpreter emits it.

**Leave it.** The status quo, and the reason to reject it is that the adoption cost falls
hardest exactly where the record is most valuable and least likely to exist.
