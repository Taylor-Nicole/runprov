# 18. A policy is checked against the history, not remembered

**Status:** Accepted — T-34, built 2026-10-02, **not yet released** (`[Unreleased]`). Depends on
ADR-0017 for its machine-readable form. Amended the same day by Taylor's rulings: R-11 the policy
format, R-12 the recommended interpreter, R-7 turned from a prose sentence into a table so the
rule set can be derived from it, and R-9 clarified — *the source* means source code, not the
filesystem, which is what put `inputs_verify` in scope.

**THIS LINE WAS MOVED IN THE LAST BUILD COMMIT AND NOT AFTER IT**, which is the one thing ADR-0017
got wrong: it read *a feature that is not built* through the release that shipped it. The guard
that catches that reads a module's TOP docstring for the ADR number, so `policy.py` deliberately
did not cite ADR-0018 until this commit — citing it earlier would have turned the guard red while
the feature was half-built, and that ordering is now enforced rather than remembered.

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

**R-7.** Opening rule set, each a question the record already answers. **The names are
canonical and this table is the only list of them** — R-5 forbids a second copy in the parser,
the docs or the tests, so a test derives the rule set from here and from the registry and fails
when they disagree. Amended 2026-10-02 from a prose sentence, which could not be derived from.

| rule | asks | reads | CANNOT_CHECK when |
|---|---|---|---|
| `clean_tree` | the working tree was clean when the run started | `git_tree_dirty`, `git_status_captured` | the status was never captured — outside a repository, or no git on PATH |
| `no_unregistered_reads` | the run opened no data file it did not register | `unregistered_reads`, `observation.unregistered_watch_truncated` | the watch hit its cap, so an empty list no longer means none were missed |
| `outputs_pin_inputs` | every run that produced an output declared what it read | `outputs`, `inputs` | the run recorded no outputs, so there is nothing to ask about |
| `commit_recorded` | the run names the commit it ran from | `git_commit`, `git_status_captured` | the status was never captured, so an absent commit is not a missing one |
| `environment_captured` | the run recorded the packages it ran with | `packages`, `observation.packages_recorded` | the record does not say whether packages were recorded at all |
| `finished_ok` | the run reached its end and recorded success | `status`, `failure` | the line carries no status — a `start` with no ending is not a failure |
| `inputs_verify` | every input the run declared still hashes to what it recorded | `inputs` | a declared input is no longer on disk, none was declared, or one was declared as a DIRECTORY — which `runprov verify` re-walks and this rule does not |

**THE `CANNOT_CHECK` COLUMN IS THE SPECIFICATION, not a footnote.** R-3 says an unevaluable rule
is never a pass, and every entry above names the exact state in which this rule cannot answer. A
rule whose column is empty would be a rule claiming it can always decide, which no rule reading a
record can honestly claim.

**AMENDED 2026-10-03 by Taylor's ruling on K-20: EXIT 2 FOR A DECLARED DIRECTORY IS INTENDED.**
The column said nothing about directories either way, so a project that registers one — which the
cross-version corpus scenario does on purpose — could read its permanent exit 2 as a defect rather
than as the specification. It is the specification: `inputs_verify` stops at files, and
`verify.py` owns the tree comparison, its ordering key (A-14, C-03) and the Windows casefold
precedence. A second walk inside a policy rule is how the two come to disagree. The ruling changes
no verdict: `inputs_verify` answered CANNOT_CHECK over a declared directory before and after, and
what K-20 repaired was the SENTENCE — the guard tested a `kind` the writer has never emitted, so
every real record fell through to *could not be read: Is a directory* and sent the reader to an
imagined permissions problem instead of to `runprov verify`. **Reaching the directory from a
policy is T-39 and is new capability, not this.**

**`no_unregistered_reads` CARRIES A BLIND SPOT THE RECORD CANNOT CLOSE, and it is named rather
than hidden.** The watch runs inside a `try` whose `except` warns on stderr and returns, leaving
neither field set — indistinguishable in the record from a run that was watched and read nothing.
That branch is defensive and believed unreachable, and until a record field says *the watch
completed*, the rule's own `blind` text states the limit. R-10's remedy for wanting a property the
record does not carry is a record change with its own ADR, not an inference here.

**R-8.** `--format json` per ADR-0017, because a gate whose result cannot be read by the CI
system running it is a gate that gets deleted.

**R-11. THE FILE IS JSON OR TOML, and every rule carries a `why`.** Added 2026-10-02 by
Taylor's ruling: R-1 said *a policy file* and never said what format, and the two constraints
that decide it are `dependencies = []` and `requires-python = ">=3.10"`.

| format | parser | available |
|---|---|---|
| **JSON** | `json`, stdlib | every supported version |
| **TOML** | `tomllib`, stdlib | **3.11+** only |
| TOML on 3.10 | `tomli` | the `runprov[toml]` extra |

**`tomli` is an OPTIONAL extra and NOT a dependency**, so `dependencies = []` stays empty — the
property the README states. A `.toml` policy on 3.10 without the extra is a usage error that
names its own fix, which is this package's rule everywhere: say what could not be done rather
than do less of it quietly.

**THE `why` IS REQUIRED ON EVERY RULE, in both formats, and that is the substance rather than
the syntax.** JSON has no comments, and the first reading of that is a loss: a control's
rationale has nowhere to live. It is the opposite. **A comment cannot be checked and does not
travel.** A required field can be demanded of every rule, and it reaches the page and the
payload — so an assessor reads *ISO 15189 5.5.1: the analysis must be traceable to a known code
state* beside the verdict, instead of finding it in a file nobody rendered. R-6 already asks each
rule to say what it cannot see; this asks it to say why it exists. TOML admits comments too, and
they are welcome, but they are not where the rationale belongs.

**R-12. THE RECOMMENDED INTERPRETER IS 3.12 OR LATER, and the reason is not this file's format.**
`sys.monitoring` (PEP 669) is 3.12+, so `observation.auto_available` is false on 3.10 and 3.11 and
automatic step observation does not happen there at all — ADR-0010's stage two is unreachable,
and ADR-0014 reports `steps` as NOT COMPARABLE across the boundary. 3.11 buys `tomllib` in the
stdlib and nothing else; **3.12 buys a materially fuller record.** The floor stays at 3.10
because dropping a version is its own decision with its own cost, and it must not be made to win
a config-format argument. What changes is that the recommendation is now written down where a
user reads it rather than implied by a matrix.

**R-13. THE PAYLOAD CARRIES THE NORMALISED POLICY AS ITS OWN OBJECT, and it is a PROJECTION of
the rule rows rather than a second parse.** Added 2026-10-03 by Taylor's ruling on an open
question, and the question's own answer is the reason for the shape.

**What was established first:** the payload already contained every field of the normalised
policy. Each rule row carries `rule` and `why`, which is exactly what `load()` returns, so
*emit the policy as well* was already true in substance — interleaved with the verdicts rather
than absent. The argument against a top-level key was therefore that it duplicates data already
present, and ADR-0017 R-10 forbids a second source of truth.

**So the key is added and the duplication is not.** `policy` is built from the same rule rows
the verdicts are built from, in one place, so the two cannot disagree: one builder, two views,
which is ADR-0017 R-1's shape applied inside a single payload. **A second pass over the parsed
file would be the defect** — H1-6 is the row where one fact computed twice disagreed with
itself, and `matched` is the row where two builders invented one name. What a consumer gains is
the control as a separable object: the policy can be archived, diffed and filed without
reassembling it from the verdicts that happen to carry it.

**R-14. `--emit-policy` READS AND VALIDATES A POLICY AND ANSWERS ABOUT NOTHING ELSE.** Taylor,
2026-10-03, the third half of the same ruling: someone who writes TOML needs the machine-readable
form to file, and that is a question about the FILE rather than about any run.

| | |
|---|---|
| **reads** | the policy, through the same `load()` the gate uses — one validation path, so a policy that emits is a policy the gate accepts |
| **never reads** | the history. `--log` is not consulted and the command says so rather than silently defaulting to the project's |
| **schema** | `runprov.policy.v1`, its own, because a normalised policy is a different shape from a gate result and must not wear the gate's version |
| **exit codes** | **0 usable, 2 not usable, and there is no 1.** A policy cannot carry a *finding*; it is either a document this version can act on or it is not, which is L-81's second and third codes with the middle one deliberately absent |

**A FLAG ON `gate` AND NOT A SUBCOMMAND, with the reason recorded so it is not re-proposed.**
ADR-0017 R-3 partitions subcommands by the question each answers, and a validator for the policy
file is a question about the thing `gate` already reads — the only command that has one. A
`runprov policy` subcommand would be heavier than the question deserves, and it would be a second
place where a policy is read.


## What this must never become

**R-9.** Not a linter for code. `check` does the static half and this does the recorded half,
and the boundary is that this command **reads only the history and never the source.**

**CLARIFIED 2026-10-02 by Taylor, because the sentence admits two readings and one of R-7's own
rules depends on which.** *The source* means SOURCE CODE — the thing `check` parses. It does not
mean the filesystem. So `inputs_verify` re-hashing a declared input is in scope: data files are
not source, and *"does the evidence still match the files"* is the question an accredited
laboratory asks first. The cost is stated rather than hidden: with that rule in a policy, `gate`
scales with the DATA and not with the history, and a declared input that is gone is a
`CANNOT_CHECK` naming the path rather than a violation.

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
