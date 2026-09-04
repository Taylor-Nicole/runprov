# 7. The gate ships with the tool, and passes the exit code through

Date: 2026-09-04 · Status: accepted · Ledger: T-18 · Decided by the author, not the applier

## Context

`WHY.md` has named the missing half since before this repository was extracted:

> **It records; it does not audit.** … The half that makes the record trustworthy is a
> separate checker that fails the build on an unregistered read.

The checker exists. `runprov verify` and `runprov show --stale --rehash --exit-code` have
returned the right codes since the exit-code contract landed. Both were reachable only by a
person choosing to type them — and the pain a provenance record addresses is felt six months
later, by somebody else. Tools for deferred pain are not adopted on merit; they are adopted
when something fails without them.

## Decision

**The gate ships as `.pre-commit-hooks.yaml` and `action.yml` in this repository**, so a
project adopts it by pointing at a ref rather than by assembling three lines of `run:`.

Two hooks, because they answer the same question from opposite ends and one of them works
where the other cannot:

| hook | what it reads | works without |
|---|---|---|
| `runprov-stale` | the run history, re-hashing every input | — |
| `runprov-verify` | the pins inside the artifacts | a history, a database, a network |

The second is the one that works on a file a collaborator sent you, which is what the in-band
pin is for.

Both are `always_run` with `pass_filenames: false`. The question is about the **project** —
*is anything on record stale?* — not about the files in this commit. A staged file is rarely
the artifact that went stale; the input it was made from usually is.

**The exit code is passed through, never collapsed.** This is the decision worth writing
down. A gate that reported every non-zero as "failure" would make these indistinguishable:

* **1** — a result no longer follows from the inputs it was made from. *Rebuild, or
  investigate.*
* **2** — the check could not look. No history, no pins, nothing recorded here. *There is
  nothing to rebuild; something is not wired up.*

Collapsing them turns "your provenance is not running at all" into "your results are stale",
and sends somebody to re-run a pipeline over a problem that re-running cannot touch.

`version:` on the Action is pinnable for the reason the ruff pin exists in this repository's
own config: a gate that changes under you turns a push that touched nothing red.

## Consequences

* Neither file ships in the sdist, and the reason is recorded beside the other deliberate
  exclusions: both are consumed by **cloning** this repository — `pre-commit` clones it, and
  `uses:` reads it from a git ref — so neither is reachable from a tarball.
* The hooks are tested by **running** them in an empty directory, which is the state of a
  fresh clone. That pins a second property nobody asked for and everybody wants: the gate
  says something useful when there is no history yet, rather than crashing. Exit 2 is the
  right answer there, and it is in the contract.
* A project that adopts this and has no provenance at all gets exit 2 on its first run. That
  is correct and it will be surprising. It is documented in the README rather than smoothed
  over, because smoothing it over is the collapse this ADR refuses.

## Alternatives considered

**Document the commands in the README and stop there.** What existed. A check that has to be
assembled by hand is a check most projects do not add.

**One hook instead of two.** `verify` alone cannot see an artifact that was never pinned;
`show --stale` alone cannot answer without the history. Neither is a superset, and offering
one would quietly narrow what a project ends up checking.

**Collapse 1 and 2 into "failed".** Simpler to explain and wrong in the one case that matters
most on the first day somebody adopts it.
