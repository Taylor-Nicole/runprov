# 18. A policy is checked against the history, not remembered

**Status:** Proposed — T-34. Specification for a feature that is not built. Depends on ADR-0017
for its machine-readable form.

## Context

`runprov check` is static: it reads source and reports entry points that open files and record
nothing. It answers *could this code fail to record*, which is the case a runtime hook can never
see. Nothing answers the other half — *did the runs that actually happened meet the rules this
project set for itself?*

An accredited laboratory has to show two things: documented controls, and evidence they were
met. This package produces the evidence and has no way to state the control. So the control
lives in a lab manual, in prose, checked by a person remembering to look — and this project's
own ledger is a hundred rows of what happens to a rule that lives only in prose.

## Decision

**R-1.** A policy file in the repository, read by **`runprov gate --policy <file> --log <log>`**.
In the repository, under version control, beside the code it governs: a policy that lives
anywhere else is a policy whose history nobody can show.

**R-2.** The existing **three exit codes, unchanged**: 0 every rule checked and met; 1 a rule
was checked and violated; **2 a rule COULD NOT BE CHECKED.** The third is the whole design.

**R-3.** A rule that cannot be evaluated — the field it reads is absent from those records, the
history predates it, the runs recorded no commit — **is never a pass.** This is ADR-0014's
distinction applied one level up: *violated* and *unverifiable* are different answers, and a
policy engine that collapses them produces the vacuous green that five rows of the ledger
already exist about.

**R-4.** The report states **how many runs each rule was evaluated against**, not only whether
it passed. "No violations" over a log matching zero runs is the same failure as `check`'s A-08,
where a sweep that parsed no files printed a clean bill and exited 0.

**R-5.** The rule set is **derived from a registry the rules register into**, never a hand-typed
list in the parser, the docs and the tests. Three places to update is two places to forget, and
that is the scope pattern — eight instances in this codebase.

**R-6.** Every rule names, in its own text, **what it cannot see.** A rule asserting "no
unregistered reads" must say that it can only speak for runs whose watch did not hit its cap,
and consult `observation.unregistered_watch_truncated` to know. A rule that reads a field
without reading that field's own truncation mark is the C-06 defect rewritten as a policy.

**R-7.** Opening rule set, each a question the record already answers: the tree was clean; no
unregistered reads; every output pins its inputs; a commit was recorded; the environment was
captured; no run finished with a non-`ok` status; every declared input still verifies.

**R-8.** `--format json` per ADR-0017, because a gate whose result cannot be read by the CI
system running it is a gate that gets deleted.

## What this must never become

**R-9.** Not a linter for code. `check` does the static half and this does the recorded half,
and the boundary is that this command **reads only the history and never the source.**

**R-10.** Not a way to make a record say something it does not. A rule may only assert over
fields that exist; if a policy wants a property the record does not carry, the answer is a
record change with its own ADR, not an inference.

## Alternatives considered

**Flags rather than a file** (`--require-clean-tree --no-unregistered-reads`). Simpler, and
rejected: the file is the point. A control that is retyped on each invocation is not a
documented control, and the diff of a policy file is the evidence that it did not quietly
loosen.

**Fold it into `check`.** Rejected: `check` promises never to import or execute the project and
to work on a pipeline that has never heard of runprov. Feeding it a history would make its
exit code mean two different things.
