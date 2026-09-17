# 21. Many histories are merged at read time, and never rewritten

**Status:** Proposed — T-37. Specification for a feature that is not built.

## Context

On a cluster each job writes its own history file — a shared one would serialise every task in
an array job behind one lock, and on a network filesystem the lock is the least reliable thing
in the stack. `log`, `diff`, `impact` and `report` each read one file.

## Decision

**R-1.** A **directory** is accepted wherever a `--log` file is, and every `*.jsonl` in it is
read. Read-time only.

**R-2.** **No file is ever written, merged, moved or rewritten.** The merged view exists for the
duration of the command. A merge that produced a file would be a second copy of the truth, and
the first question about any record would become which copy it came from.

**R-3.** Ordering is by **`started_utc`, tie-broken by `run_uid`** — and the output states that
clocks on different machines are not a total order. A lineage that depends on ordering between
two nodes must say so rather than present an arbitrary choice as a sequence.

**R-4.** `run_uid` is the identity. The same run appearing in two files — a copied directory, a
re-collected result — is **one run**, deduplicated, and the report says how many duplicates were
collapsed. Counting it twice would inflate every total a reader sees.

**R-5.** The **hash chain (ADR-0016) is per file**, and a merged view **must not claim a chain it
cannot verify.** It reports each file's chain separately: intact, broken, or absent. A single
"intact" over a directory would assert a property that does not exist.

**R-6.** A file that cannot be read is named and counted, never skipped silently. This is the
A-17 rule — an unreadable directory contributed nothing and the command exited 0 — one level up.

## Alternatives considered

**A `runprov merge` that writes one file.** Rejected under R-2.

**A database or an index.** Rejected: it is a store, and the package's claim is that the record
needs no software to read. An index would become the thing that must be present.
