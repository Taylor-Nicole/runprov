# 19. `rerun` prints, and never runs

**Status:** Proposed — T-35. Specification for a feature that is not built. Depends on ADR-0017.

## Context

The record holds the command, the cwd, the commit, the parameters and the inputs. `impact`
already computes which runs derive from a set of bytes and in what order. Everything needed to
say *here is what to execute to rebuild this* is recorded, and nothing says it.

## Decision

**R-1.** **`runprov rerun <artifact>` writes a shell script to stdout and executes nothing.**
Not a flag that defaults to safe — there is no execution path in the command at all.

**R-2.** The steps come from `impact`'s walk, in its order, each with the recorded command, the
recorded cwd, and the commit that run used.

**R-3.** **Every gap in the record is a comment IN the emitted script**, at the step it affects.
A run that read something it did not register, a dirty tree whose commit does not name the code,
a step the history could not see, an input that no longer verifies — each is a `#` line the
person running the script reads at the moment it matters. Not a footer, not a manual.

**R-4.** The script **refuses to be silently complete.** Where the chain is truncated or a step
is missing, it emits a comment saying so and a non-zero `exit` at that point, so a script run
unattended stops where the record stopped rather than appearing to finish.

**R-5.** `--format json` per ADR-0017 emits the same steps as data, for a caller that wants to
drive its own runner.

## What this must never become

**R-6.** **It must never execute anything.** The moment it does, this package is claiming to be
a workflow engine — which `WHY.md` names as the one claim not to make — and it would be claiming
it while providing none of a workflow engine's guarantees: no scheduling, no retry, no
isolation, no dependency resolution beyond what one history happened to record. A derived view
that prints is ADR-0014 clause 6. A runner is a different product with a different contract.

**R-7.** It must never present the script as **sufficient**. It reproduces the runs the history
saw, on a machine whose environment the record describes but does not provide. `runprov env`
(T-36) answers the second half and this command must point at it rather than imply it.

## Alternatives considered

**`--execute`, guarded by a confirmation.** Rejected under R-6. A guard is not a boundary; the
first support request asking to skip it would move it.

**Emit a Snakemake or Nextflow file instead of shell.** Attractive, and rejected for now: each
is a dialect with its own semantics, and generating one implies the record holds enough to
populate it — inputs, outputs, wildcards, resources — which is a bigger claim than the record
supports. Shell is honest about being a transcript rather than a workflow.
