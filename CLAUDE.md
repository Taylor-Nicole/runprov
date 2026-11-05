# Working in this repository

Rules an assistant must follow here. Each one is written because breaking it cost this project
real work, and the cost is named so that a later cleanup cannot delete the rule as ceremony.

Attribution for commits is governed elsewhere and is not restated here, because a rule stated
twice drifts and this project has measured that happening.

## How to work

Keep the goal in mind. Use the skills. Reverify every solution for scalability, modularity,
future-proofing and end-to-end integration **before** applying it, and if it does not pass,
propose a different solution rather than a narrower version of the failed one. Fan out agents
when the work is separable. Prefer one comprehensive pass to several partial ones.

**A step with exactly one correct answer is CODE, never an estimate.** A count, a diff, a merge,
a dedupe, a comparison — write the command and read its output. Judgement is what you are for;
arithmetic is not, and an estimate is indistinguishable from a measurement once it has been written
down. Measured here: a subagent brief said *"roughly twelve narrow handlers across the package"*
when twelve was the count inside one promising subset and the package-wide figure was more than ten
times it. A census scheduled off that number would have reported itself complete with the great
majority never examined. The brief is where this costs most, because the agent cannot check a
number you supplied.

**When you have named the next action, take it.** Do not end a turn with a recommendation and a
request for permission; that converts your own conclusion into another prompt. Ask only when
proceeding under any assumption would be unsafe, or would waste the work if the assumption is
wrong.

**Pushing is NOT an exception to that, and stating it as one was a mistake measured in a single
sitting.** The maintainer's gate on a push is a PERMISSION RULE in the project settings, which
surfaces every `git push` for a decision and does not depend on an assistant remembering anything.
Carrying a stop-and-report instruction *as well* bought no safety and cost the maintainer the same
instruction on five consecutive turns, each time for work already committed and already
gate-green. Where a mechanism and an instruction cover one risk, the instruction is the one to
delete.

So push gate-green work on `main`, and let the permission prompt be the gate. **Push it before
starting the next thing, not at the end of the session** — a gate-green commit once sat unpushed
while the next task was begun, and the drive holding it dropped out from under the session an hour
later. Nothing was lost, and nothing about that was by design. Ask, separately and
every time, before a force-push — never do one — before pushing a tree that is dirty or that no
gate has run on, before a tag or a release, and before pushing to any other branch or remote.
Those are the irreversible ones: a trailer that reaches a branch with a pull request cannot be
removed afterwards, whatever the default branch says later.

**Report STATE first, in two or three lines, then reasoning.** Lead with what is done, what is
blocked and on whom, each with its commit or its exit code. Narrative buries the outcome: a report
that opened by saying a batch was committed and closed, four hundred words later, with a section
headed *what is still yours* produced an instruction to commit the batch again. The long form
belongs in the audit ledger, which is read when it is wanted. The terminal gets the state.

**And `unresolved` is a verdict.** A requirement you could not check is reported as unresolved,
naming the evidence that is missing — never omitted, and never folded in with the passes because
nothing contradicted it. Silence reads as a pass to every later reader, including you.

## Continuity — the task comes from the graph, not from the prompt

**Before asking what to do next, read the frontier.** The audit repository holds a prompt graph: a
node is any file there carrying a `## Next action`, and its generator prints the FRONTIER — the
nodes whose next action nothing has started from. That frontier *is* the scope. Run the generator
and read the standing prompt it belongs to, in the audit repository beside this one:

    python3 tools/prompt-graph.py
    sed -n '/^## The standing prompt/,$p' HANDOFF-PROMPT.md

Read them, never recall them. The hand-off and the emitted prompt drifted by six lessons once
already, which is why the generator exists and why the prose is read from the file.

**A frontier node also says how it will be checked.** `tools/node-contract.py` refuses a live
node carrying `## Next action` and no `## Done when`, and refuses a contract item naming nothing
that resolves. Write the contract BEFORE the work, not after seeing the result: a standard chosen
once the answer is known is not a standard. Every node in the graph said what to do next and none
said what done would look like, which is the mechanism behind tasks that stop rather than finish.

**And feed it before calling a session done.** Findings go in the audit ledger; the next action
goes in a dated node. **The ledger is NOT a node**, so findings written only there leave the
frontier unchanged — measured: the graph lapsed for eleven days exactly that way, while the
maintainer asked for it to be used fourteen times in one session. `tools/graph-is-fed.py` refuses
that state now; run it before reporting the session finished.

The rule this instance teaches, and it is the one worth keeping: **when something put in place
fails, or the same problem recurs, the lesson is not to try harder — it is to find the mechanism
that would have made the lapse red.** An instruction that can be forgotten silently is not yet a
mechanism, however carefully it is written down.

## Reviewing and verifying

**A reviewer must be a fresh agent, never a fork of you.** A fork inherits your framing, and your
framing is what a review exists to test. Measured here: the worst defect found this week came from
a fresh skeptic given one claim to refute, and twice in the same week the sentence that justified a
repair was itself the defect. A reviewer that starts from your context will return your conclusion
with more words attached.

**Build at ordinary effort; verify at high.** Writing a repair and attacking one are not the same
task, and the attack is where being wrong is expensive. Give any agent whose job is to refute,
audit or verify a high effort level, and say so in its brief. Mechanical work — a sweep, a census,
a census of a census — does not need it.

**Brief a reviewer with the code and the claim, never with your conclusion.** State what is to be
refuted, what has already been ruled out, and every operating rule below that its work could
touch; a rule left out of a brief is a rule the agent will break, and that has happened here.

**A refuted repair goes back with the reason, the evidence and a scope of one — and not more
than three times.** After that the node is the problem rather than the attempt: stop and say so.
Never repeat an approach that failed without naming what is different this time. Measured here: a
remedy refuted for restoring a *count* rather than a *check* was re-attempted in a narrower form,
which is the same failure wearing less surface.

**After a change large enough to need a reviewer, ask for the three things that outlive it:** what
the defect's shape was and where else it could occur, which sweep came back empty and over what
denominator, and which guard would now go red. The first two go in the ledger; the third is the
only one that survives without a reader.

## Running things

- Use the virtualenv interpreter, `.venv/bin/python`. Never a bare `python`.
- Set `TMPDIR` to a directory under `/dev/shm` for any gate or test run, and remove it afterwards.
  Every record write is fsynced, so the cost is write-count times fsync latency and none of it
  parallelises: the same gate takes minutes on tmpfs and hours on the project drive. It looks
  exactly like a failing disk and is not one. Tmpfs pages count as RAM, so leaving the directory
  behind is a memory leak on a host that is usually short of memory.
- The gate is `ci.py lint test build`, plus `surface torture claims corpus` before a commit.
- `ci.py matrix-check` refuses to run on a dirty tree, so commit before polling it.
- `--cov` cannot measure `ci.py`: it is spec-loaded, so its module name is never imported. Use
  `coverage run --include=`, which measures by path.
- Device names are not stable. Read the mount source live; never reuse one written in a note.

## Never

- Never `pkill -f`, and never pipe `pgrep -f` into `kill`. Both match the shell holding the
  pattern. This has happened repeatedly here, including to a subagent whose brief omitted the
  rule, so the rule belongs in every brief you write as well as here.
- Never `nohup` a worker, a gate or a self-hosted runner.
- Never force-push.
- Never read `$?` after a pipe: it measures the last command in it. Read the first element of
  `PIPESTATUS` instead, or write the exit code into the log yourself before reading it back.
- Never pass a commit message with `-m` when it contains backticks. Write it to a file and use
  `-F`, and pre-test it with `ci.py check-commit-msg` before committing.

## Before you commit

- **Demonstrate every new guard red against the tree it was written to condemn.** A guard that
  has only ever been seen passing is a guard whose subject is unproven; several here were green
  over the exact defect they were added for.
- Assert that every bulk replacement matched what you expected, by count. A silent no-op reads
  as success, and a regex cannot reliably revert a regex: restore from `git show HEAD:<path>`
  and hand-fix only your own lines.
- Resolve every identifier you cite against its definition, never against the prose around it.
- Re-run the step whose input is the git history **after** committing. That step is `test`, not
  `lint`, because the changelog guard reads committed history and cannot fire before the commit
  exists. `pytest -k changelog` is the cheap substitute.
- Write fixture files with `write_bytes`, never `write_text`, whenever you then assert on their
  bytes, size or digest. Text mode translates newlines per platform, and this has turned a
  committed digest red on the Windows leg more than once, most recently inside the fixture of the
  test written to hold that very rule.
- A local gate is not the matrix. Several repairs here passed every local check and failed on a
  platform leg; a platform name is not the property, and a version is not a platform either.

## Where the durable record goes

Findings and their reasoning go in the audit repository's ledger, in the entry for the task that
found them, including the sweeps that came back empty. Re-usable lessons go in the project skills
or this file, because a lesson recorded only in a task's working notes is not recorded.
