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

**AMENDED 2026-10-03 by Taylor's ruling on K-08, and the amendment is a LIMIT rather than a
widening.** A run that started and has no ending on record — a SIGKILL, the OOM killer, a power
cut — is a run the gate did not examine, and the gate counted it in no field at all: `MET`,
exit 0, `cannot_check: null`, over a history `runprov log` was already reporting `unfinished: 1`
for. It is now a field (`unfinished`) and a `cannot_check` clause, **and `outcome` and the exit
code deliberately do not read it.** The reason is permanence: the start line is append-only,
`runprov prune` clears markers and not history, so an `outcome` arm would fail a project's gate
for ever over one power cut, in a record nobody can amend — measured, including after two further
successful runs — and it would also fail a gate because another job is merely in progress.
`finished_ok`'s own text already refuses to read a missing status as a failure, for the same
reason one level down.

**So the residual limit is documented rather than hidden: a consumer keying only on the exit code
still greens over a lost run, and must read `unfinished` or `cannot_check` to see it.** That is
the contract. The alternative the row implied — feeding unpaired start lines to the rules so
`finished_ok`'s own arm becomes reachable — is not more targeted: a start line carries no
`outputs`, `inputs` or `git_status_captured`, so every rule answers CANNOT_CHECK and exit 2 is
just as universal.

**AND A HISTORY THAT IS THERE AND CANNOT BE READ IS A THIRD STATE, not either of `found`'s two
(K-23).** An `OSError` from the read used to leave the command: a traceback, nothing on stdout,
and exit 1 — which R-2 reserves for *checked and violated*, about a file nothing ever opened a
line of, while ADR-0017 R-15 gives silence on stdout the single meaning *this command never had
a question*. The invocation had one. So it carries a payload, names the failure in its own field
(`read_error`) and exits 2, exactly as a missing history does. **`outcome` DOES read this one**,
and the asymmetry with `unfinished` is the point: an unreadable file is a fact about this
invocation and the next answer is the ordinary one again.

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
| `clean_tree` | the working tree was clean when the run started | `git_tree_dirty`, `git_status_captured` | a run whose git status could not be captured at all |
| `no_unregistered_reads` | the run opened no data file it did not register | `unregistered_reads`, `observation.unregistered_watch_truncated` | a run whose record carries no `observation` block at all |
| `outputs_pin_inputs` | every run that produced an output declared what it read | `outputs`, `inputs` | a run that recorded no outputs, which this rule has nothing to ask about |
| `commit_recorded` | the run names the commit it ran from | `git_commit`, `git_status_captured` | a run outside a repository or with no git on PATH, where the absence of a commit is not a missing one |
| `environment_captured` | the run recorded the packages it ran with | `packages`, `observation.packages_recorded`, `environment_snapshot` | a record with no `observation` block, which cannot say whether packages were recorded at all |
| `finished_ok` | the run reached its end and recorded success | `status`, `failure` | a line carrying no status |
| `inputs_verify` | every input the run declared still hashes to what it recorded | `inputs`, `cwd` | a declared input that is no longer on disk, or a run that declared none at all |

**THE `CANNOT_CHECK` COLUMN IS THE SPECIFICATION, not a footnote.** R-3 says an unevaluable rule
is never a pass, and every entry above names the exact state in which this rule cannot answer. A
rule whose column is empty would be a rule claiming it can always decide, which no rule reading a
record can honestly claim.

**AMENDED 2026-10-04 for L-14: THE COLUMN IS NOW DERIVED, AND HELD BY THE SAME EQUALITY THE
README'S IS.** Each cell must be exactly `blind.split(" — ")[0]` — the FIRST CLAUSE of the
sentence the rule itself prints — which is the form K-39 built for the README's third column
because a prefix comparison is satisfied by an empty cell, by a one-character cell, and by
another rule's opening words.

The row that forced it: `no_unregistered_reads`'s cell named only *the watch hit its cap* and
omitted **a run whose record carries no `observation` block at all** — the rule's FIRST arm, and
the one that actually fires on the corpus oracle, where 0.1.0 has no `observation` block. The
README's guarded table named it correctly, so two documents agreed and this one did not. K-18's
repair amended the prose beside this table and left the row alone, while the same tranche amended
two other rules' columns.

**AND THE REST OF EACH LIMIT TRAVELS IN THE RULE'S OWN SENTENCE, which is where it belongs.** A
cell holds the documented limit; the clauses after the em dash — the defensive routes, the
*named here because the record cannot close it* ones — are carried by `blind` itself, which
`gate` prints beside every verdict and the payload carries in full. Three statements of one fact
with one of them hand-kept is how this column went wrong; now there is one statement and two
derived views of it.

**AMENDED 2026-10-03 for K-19: `environment_captured` READS THE RECORD'S OWN
`environment_snapshot`, and opens no file.** `_observed_packages` returns `"snapshot"` on the mere
PRESENCE of the key, and a capture that raised stores `{"error": …}` under it — so a run that
printed *WARNING: could not write environment snapshot* and recorded no package answered MET over
`packages: {}`, which this rule's own `blind` text calls indistinguishable from nobody having
asked. An unwritable snapshot directory in CI turned the environment control into a no-op for
every run after it. The flattened `environment_snapshot` carries the snapshot's digest, its path
AND its package count, so the count is in hand and **R-9 is not bent: no snapshot file is
opened.**

**The wider claim this row first carried is WITHDRAWN** — refuted twice, independently. *Every
released wheel answers MET over `packages: {}`* is true and is not a defect: those records carry
`n_packages: 2` beside the digest, so they did record what they ran with. **CORRECTED [L-11]: the
withdrawal paragraph then said *MET is correct for all seven*, and it is correct for SIX.** 0.1.0
answers CANNOT_CHECK — correctly — through the no-`observation` arm, because it predates ADR-0010,
carries no `observation` block and never reaches the snapshot arms. Measured:
`0.1.0: CANNOT_CHECK`, `0.2.0 … 0.7.0: MET`. **The claim is corrected and the REASON is not:**
`n_packages: 2` is exactly why the other six are stable, which is the half the row's own gloss got
loose. It is the K-38 error one row over — a figure taken from the wrong side of the thing it
describes. **And `environment["snapshot"]` is the wrong field to read**: in a HISTORY record
`environment` is `None`, because the block is flattened to the top level and `environment.snapshot`
exists only in the sidecar, which `gate` never opens. A rule reading it would have evaluated
`None` on every record — a guard that is uninformed looking exactly like one that is satisfied.

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

**CORRECTED 2026-10-03 for K-18: the paragraph above was DEFECTIVE BY OMISSION, and the omission
is the ordinary route rather than the defensive one.** `warn_unregistered_reads=False` skips
`attach()` altogether, and such a run records a full `observation` block and neither field — so
the state reached is identical and the rule answers **MET** for a run that performed real
unregistered reads. Reproduced. The setting is documented, package-recommended for a step that
deliberately reads files it does not want recorded, and **PROJECT-level**, so one step's exemption
silences the watch for every run that shares the project and the rule says MET over all of them.
Presenting the `except` arm as the only route made a supported configuration look impossible.

**THE VERDICT IS DELIBERATELY LEFT ALONE UNTIL THE RECORD CAN CARRY THE FACT, and that decision is
measured rather than preferred.** `observation.unregistered_watch` cannot be back-filled, so a rule
requiring it would turn **every clean pre-0.8.0 history from MET to CANNOT_CHECK for ever** —
worse than the defect — and the same check placed before the violation arm turns a real VIOLATED
finding into CANNOT_CHECK on all seven corpus histories, measured. The record field gets its own
ADR, which is R-10's answer and not a loosening of it. What this amendment does is make the limit
true where R-6 requires it to be written: in the rule's own `blind` text, and here.

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

**WHICH OF THE TWO IS *THE CONTROL*, because there are now two shapes and a consumer has to know
[K-13].** `--emit-policy`'s `runprov.policy.v1` IS the control: a document that names itself,
carries the file it was read from, and can be filed and diffed on its own. `gate.policy` is **the
control AS APPLIED** — the same rules, inside the answer, so a filed result carries what it was
judged against.

**AND IT IS DELIBERATELY NOT A SECOND COPY OF THAT DOCUMENT.** Making the two one shape would put
`schema` and `policy_path` **twice in one payload**, which is exactly the duplication the
paragraph above says this requirement avoided. `{"rules": [...]}` is not an unversioned invention
either: it is precisely what a policy FILE may contain, and `_checked` refuses a `schema` key in
one. ADR-0017 R-5 governs PAYLOADS and not the objects nested inside them — the container already
carries the version and the path — and R-9 forbids renaming a field, not adding one, so nothing
here freezes at 0.8.0 and this can be revisited with evidence rather than before it.

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

> **CORRECTED [K-33]. This paragraph cited `ADR-0017 R-3` as *partitioning subcommands by the
> question each answers*, and R-3 is the `--format text|json` requirement.** The only partition
> R-3 draws is answering-versus-acting, to keep `exec`, `capture` and `prune` out of the format
> flag, and it does not reach this question. The paragraph exists so the decision *"is not
> re-proposed"* — and the next person to re-propose `runprov policy` would have followed the
> citation, found a format rule, and concluded the reason was never written down, which is the
> state it was added to prevent. **The rule it should have cited is written down twice:**
> **ADR-0016's 2026-09-17 amendment** states it in those words — *"this package has given every
> distinct question its own subcommand"* — and it rests on **this ADR's own R-9**, whose objection
> to folding the policy gate into `check` is that one command's exit code would then answer two
> different questions.

The argument stated directly rather than by citation: `--emit-policy` is not a distinct QUESTION.
It is a question about the file `gate` already reads, and `gate` is the only command that reads
one. A `runprov policy` subcommand would be a second place where a policy is read — the
duplication R-9's boundary exists to prevent — and heavier than the question deserves.


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
