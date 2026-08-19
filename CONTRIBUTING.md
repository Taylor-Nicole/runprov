# Contributing to `runprov`

## Sign your commits — this is the one hard requirement

Every commit must carry a `Signed-off-by:` line. `git commit -s` adds it:

```
Signed-off-by: Your Name <your.email@example.org>
```

Set it up once and forget it:

```bash
git config user.name  "Your Name"
git config user.email "your.email@example.org"
git config format.signoff true      # -s becomes the default for this repo
```

Forgot on the last commit: `git commit --amend -s --no-edit`. Forgot across a branch:
`git rebase --signoff main`.

### Why, in plain terms

The sign-off is the **Developer Certificate of Origin** ([DCO.txt](DCO.txt), verbatim from
<https://developercertificate.org/>, sha256 `f7ac75b443f4ca16…`). By adding it you state
that you wrote the contribution, or that you have the right to submit it, and that you
understand it will be public and permanent. It is not a copyright assignment and it takes
nothing from you.

The practical reason it is required **from the first commit** rather than added later:

> This project is released under a permissive licence and may one day move to Apache-2.0 —
> the express patent grant is the thing companies' legal teams ask for. Relicensing future
> versions is free **while the copyright is held by one party**. The moment an unsigned
> contribution is merged, its author holds copyright in it and nothing can be relicensed
> without tracking them down. Projects have had to hunt contributors down years later, or
> rewrite code they could not reach.

Note what this does *not* do: released versions stay under the licence they were released
under, permanently. A relicence only ever applies going forward.

A DCO rather than a full CLA is deliberate. A CLA asks you to assign or licence rights to a
third party and usually needs a lawyer to read; a DCO is one line and asserts only what any
honest contributor can already assert. At this project's size, a CLA would cost more than it
protects.

CI enforces the sign-off on every pull request. If it fails, the message tells you the
rebase command.

## What a good change looks like here

**A test must import the thing it tests.** This is not a style note. The project this came
out of recorded a fix as shipped whose class had zero construction sites for its entire
life, because the test covering it *reimplemented* the fix and therefore could never fail. A
test that does not import its subject proves only that the test is self-consistent.

**Zero runtime dependencies, and that is not negotiable.** A provenance module is imported
by every step of a pipeline, so anything it depends on becomes a constraint on all of them —
and a version conflict in a provenance tool is a spectacularly silly reason to be unable to
record a run. Standard library only. `pytest` is a test dependency, not a runtime one.

**The pin must stay deterministic.** Nothing that varies between two runs over identical
inputs may enter `Run.header()` — no timestamp, no run id, no hostname, no path that
contains any of those. This has been broken twice, and both times the symptom was the same:
every pinned artifact reported CHANGED on every run, a permanently red check, which teaches
everyone to ignore the check. `test_header_is_identical_across_two_runs_over_the_same_inputs`
is the guard; do not weaken it.

**Say what a change cost.** If it alters the recorded format, say which existing records
stop comparing and why that is worth it. Provenance whose format drifts silently is worse
than provenance that changes loudly.

**Comments explain why, not what.** The codebase is dense with the incidents behind each
decision, because that is what stops someone "simplifying" a safeguard whose reason is not
visible. Keep that up.

## Run the CI locally, before you push

```bash
python ci.py setup     # dev extras + the pre-commit hooks, once
python ci.py           # lint, test, build — exactly what CI runs
```

**The workflow calls `ci.py`.** It does not restate the commands, because a copy of a
command list is wrong within a month and then "it passes locally" stops meaning anything.
There is one definition, and you can run it.

Individually: `python ci.py lint` (ruff format --check, ruff check, mypy), `python ci.py
test` (pytest with the coverage gate), `python ci.py build` (build, twine check --strict,
then install the wheel into a clean venv and import it from elsewhere — which is what
catches a file that is in git and missing from the package).

They are fast (a few seconds) and hermetic: every test builds its own repository and its own
temporary directories. If a test needs the ambient environment, it is testing the wrong
thing — `test_detect_root_finds_the_git_toplevel` used to assert against whatever repository
happened to contain it, and failed the moment the package moved.

## What the suite skips, and how to un-skip it

**A green run is not a complete run**, and the count is printed so you can tell the
difference. `pytest -rs` lists every skip with its reason. There are five, and four of them
are things this machine cannot honestly do rather than things left undone:

| skip | how to run it |
|---|---|
| the Snakemake comparison | `pipx install snakemake`, or `RUNPROV_SNAKEMAKE=/path/to/snakemake` |
| the predecessor-log figures | `RUNPROV_PREDECESSOR_LOG=/path/to/transformation_log.yml` |
| two network-filesystem tests | `RUNPROV_NETWORK_FS_DIR=/mnt/lustre/scratch/you` — see the README section |
| the YAML recursion branch | **nothing to do.** See below. |

```bash
RUNPROV_PREDECESSOR_LOG=~/data/flaviviridae_20260424_FULL/hcv_genotyping/transformation_log.yml \
RUNPROV_NETWORK_FS_DIR=/mnt/lustre/scratch/you \
  python -m pytest -rs
```

The predecessor-log test **checks the file's sha256 before using it**, and skips rather than
runs if it does not match. Five files of that name exist with different contents, and a
reviewer holding the wrong one produced three confident, wrong corrections to the README.
Pointing this at a different copy gets you a skip, not a false pass.

**The count depends on your Python, and that is not a flake.** The YAML recursion test needs
an interpreter whose JSON encoder consumes Python frames, which CPython stopped doing in
3.12. So it runs on 3.10 and 3.11 and skips on 3.12 and later:

```
python3.11 ci.py test   ->  4 skipped
python3.12 ci.py test   ->  5 skipped
```

`ci.py` uses `sys.executable`, so the gate follows whichever Python you invoked it with. Two
gate logs with different skip counts are two different interpreters, not an unstable test —
this is written down because the difference was once read the other way round.

## Reporting a bug

Provenance bugs are usually **something that should have been recorded and was not**, which
makes them hard to see. The most useful report contains the run record itself: the
`*_provenance.json` and the relevant line from `runs.jsonl`, with anything sensitive removed.
Those two files say more than a description can.

## Scope

Deliberately small. Things that belong here: recording reads, writes, code version,
environment and outcome; making that record checkable. Things that do not: scheduling,
caching, data versioning, remote storage, a UI. Those exist elsewhere and compose fine —
`runprov` does not need to become them.
