# 12. A notebook run is not linear, and the record must say so

Date: 2026-09-16 · Status: **proposed** · Ledger: T-28

## Context

The README already argues the position: a notebook records the **narrative**, not the
**dependency**. It holds cell source, outputs, an `execution_count` and a kernel name; it holds
no hash, no statement of *which* file `pd.read_csv("data.csv")` actually read or what was in it,
no package versions, no commit, and nothing about a previous session — re-run a cell and
yesterday's output is overwritten.

And the export is where it gets worse rather than better. `df.to_csv("results.csv")` leaves the
notebook entirely: the notebook keeps its inline copy, and the file sent to a collaborator
carries nothing. **This is where most manuscript figures come from**, which is why it is worth
solving rather than conceding.

Partial support already exists and is easy to mistake for the whole thing: `.ipynb` is hashed as
first-party code, and as an output it is refused an inline pin (JSON has no comment syntax) and
given a sidecar. What does **not** exist is running a `Run` inside a live kernel.

## The problem that makes this an ADR rather than a patch

**Cells run out of order.** Cell 5 is edited and re-executed after cell 10. A cell is run twice
with different variables in scope. Half the notebook is never run at all in this session. The
kernel's state depends on the *order of execution*, which the `.ipynb` records only as an
`execution_count` per cell — and only for the last execution of each.

So a "notebook run" is **not** a script-shaped run, and the tempting implementation — open a
`Run` when the kernel starts, close it when it stops, record inputs and outputs — produces a
record that looks exactly like a linear script and is not one. Every field would be present and
well-formed. The provenance would be **confidently wrong**, which is worse than absent, and it
is this package's own defect class arriving through the feature meant to serve it.

The same shape has now been caught six times here: a census reporting zero because it enumerated
nothing; `twine check` passing because it could not render; a guard whose scope stopped covering
what it named; a mutation that missed its anchor; `observation.steps` unable to report the value
it existed for; and a line covered by a dirty working tree rather than a test.

## Decision

**A notebook session is one `Run`, and the record states that its execution was non-linear.**

1. **The order of execution is RECORDED, not smoothed.** Every cell execution appends an entry:
   the cell's identity, its `execution_count`, and when it ran. A reader can then see that cell 5
   ran after cell 10, which is the fact that decides whether the notebook's story is true.
2. **`observation` gains the capability field**, the way it did for `sys.monitoring`:
   `"host": "notebook"` and a flag saying execution was non-linear, present whether or not any
   cell ran out of order. *Not observed* and *could not be observed* must stay distinguishable,
   and a linear notebook and a script must not be reported identically when they are not.
3. **The audit hook needs no change.** `sys.addaudithook` sees every `open` regardless of who
   called it, so reads and writes are captured in a kernel exactly as in a script. That half is
   already done and is the reason this is worth building at all.
4. **A cell boundary is a CHECKPOINT, not an end.** The record is rewritten after each cell, and
   the existing rule applies unchanged: *a checkpoint must not claim an outcome*. A notebook
   session has no exit — the kernel is killed, the browser tab is closed, the laptop sleeps — so
   a design that writes only at the end writes nothing, which is exactly the failure U-02
   recorded for long runs.

## Open, and each needs measuring before this is implemented

* **How a session ends, honestly.** `atexit` in a kernel is unreliable and a killed kernel runs
  nothing at all. The checkpoint rule above makes this survivable rather than solved, and the
  record must be readable as *"this session was still open"* rather than as a completed run.
* **What `ipykernel` actually exposes** at cell-execution time, and under which versions.
  IPython is not installed in this project's environment, so **nothing here has been verified
  against a running kernel** — and an ADR written from documentation about a mechanism this
  project has not run is exactly the kind of claim it deletes elsewhere.
* **Which object identifies a cell across re-execution.** `execution_count` increments; cell
  ids exist in nbformat 4.5+. Recording a count alone cannot say *which* cell ran.
* **Whether the magic (`%load_ext runprov`) or an explicit `with Run(...)` is the adoption
  shape.** The package's whole argument is that registration is visible in the code a reviewer
  reads, and a magic is the opposite of that. This is the design question, not a detail.

## Alternatives considered

**Reconstruct provenance from the `.ipynb` after the fact.** Rejected: the file records the last
output of each cell and no execution order beyond a counter, so the reconstruction would be a
guess dressed as a record.

**Refuse notebooks and say so.** The honest baseline, and what the README does today. It stays
correct until someone needs this badly enough to accept a record whose central field is *"this
did not run in the order it is written in"*.
