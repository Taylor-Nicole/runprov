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

**When you have named the next action, take it.** Do not end a turn with a recommendation and a
request for permission; that converts your own conclusion into another prompt. Ask only when
proceeding under any assumption would be unsafe, or would waste the work if the assumption is
wrong. Pushing this repository is the exception: that is always the maintainer's explicit call.

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
