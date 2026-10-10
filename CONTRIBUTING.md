# Contributing to `runprov`

## Sign your commits — this is the one hard requirement for a contribution

**Every commit in a pull request must carry a `Signed-off-by:` line**, and CI enforces it on
the whole range. `git commit -s` adds it:

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

CI enforces the sign-off on every pull request (`.github/workflows/dco.yml`, over the whole
`base..head` range, not just the tip). If it fails, the message tells you the rebase command.

### What this requirement does NOT cover, stated because the history shows it

**The maintainers' direct pushes to `main` are not signed, and are not covered.** Measured on
2026-09-21: **16 of 352 commits carry a sign-off**, all from 2026-08-07 to 2026-08-11, and every
commit in the repository is by a copyright holder. The DCO check runs `on: [pull_request]`, so a
direct push never reaches it.

That is a scope, not an oversight, and the reason is the rationale above: the hazard is an
**unsigned contribution from someone who is not already a copyright holder** — their copyright
in it is what cannot later be relicensed without finding them. A commit by a holder creates
nobody to track down. This section used to open *"every commit must carry"* without that
distinction, which made a hard requirement out of something 95 % of the history does not do —
and a rule nobody follows teaches readers to skip the rules that matter.

**The history will not be retro-signed.** Rewriting it to add trailers would change every commit
SHA, and this project's own records cite SHAs by the hundred — the 2026-09-15 attribution
rewrite is the worked example: 272 commits rewritten, 253 citations regenerated across four
locations, and three closed pull requests still hold frozen copies that no write can reach. The
cost is real and the benefit here is zero, because the commits in question are the holders' own.

### No tool may sign on your behalf

A sign-off is a statement **by a person** that they have the right to submit the work. Automated
tooling — scripts, bots, coding agents — must never add a `Signed-off-by:` line naming a human
who did not personally make that certification. If a tool prepares a commit, the person adopting
it signs it. This is written down because it has already happened here: an agent added a
sign-off in the maintainer's name to two commits on 2026-09-21, and they were stripped before
anything was pushed.

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

## How this project knows things, and what enforces each rule

The rules above are about the product. These are about how a change is *believed* — and every one
of them exists because it was broken here first. **Each names the guard that enforces it, because a
rule with nothing behind it is a hope**, and the ones with nothing behind it yet say so.

**Delete the list, do not extend it — wherever the subject is enumerable by the language.** A check
written against the instance someone just found passes, and the next review finds the member nobody
thought of. Walk every instance of the primitive, give every form a decision, and assert nothing was
left over. The import graph, `argparse`'s own tables, the suffix maps and the filesystem are all
enumerable. **A sentence's meaning is not**, which is why the claim registries in `tools/claims.py`
exist — and why they hold the *pair*, a sentence and how to compute it, and never the value.

**A floor says how much of the subject was read; an exhaustiveness assertion says the subject was
accounted for.** Prefer the second. `test_every_collected_test_carries_exactly_one_tier` is the
shape: every item pytest collected must carry exactly one tier, so a test the derivation cannot see
is red rather than quietly absorbed.

**And an exhaustiveness assertion is only as total as the primitive it quantifies over.** Write the
claim in one sentence and the quantification in one sentence, and check they are the same sentence.
A guard asserting *"every pattern matched something"* over a mixed population passed while half its
detection was broken — one total over two primitives is a floor wearing an equality's clothes.
`test_every_command_the_documentation_tells_you_to_run_actually_EXISTS` now counts each primitive
separately for that reason.

**Stop stating a number: derive it, or state the property.** Do not correct a stale figure to a
newer one — state the class, the direction or the property, which is what the reader needed anyway.
Where a number genuinely must appear, bind it: `tools/claims.py` compares the README's figures to
the constants they describe on every run, and `docs/claims-baseline.txt` holds what is still
unbound so it cannot grow quietly.

**A docstring is not a contract until something checks it.** A check whose prose says the subject is
asserted, while no assertion names the subject, is the same defect one layer up — and that has
happened here inside a test written to retire exactly that shape. If the docstring claims a case,
there must be a mutant that proves it.

**A demonstration is the specification.** No guard is believed until it has been shown red, in a
copied tree, for the reason it names — `tools/bench.py` exists to make that a command rather than a
good intention, and `ci.py bench` checks the harness can still tell CAUGHT from SURVIVED from VOID.

**When a demonstration passes, check the demonstration happened.** A mutant that was never applied
reads exactly like a guard that works. Assert the anchor matched before replacing it, anchor by
position rather than by quoted text where the text holds anything exotic, and diff the mutant before
running the test.

**Classify by return code, and only the codes that mean something.** For `pytest`: `0` passed,
`1` a test failed, `5` nothing collected, and **anything else is an error, not a result**. An
instrument failure must never wear a verdict's exit code.

**A guard that is uninformed looks exactly like one that is satisfied.** ADR-0017 read *a feature
that is not built* through the release that shipped it, because the guard meant to catch that reads a
module's docstring for the ADR number and no module named it yet. Prefer a check whose subject
cannot be absent: `test_every_accepted_decision_record_declares_what_it_GOVERNS_and_the_targets_resolve`
holds the code and the record to each other, with both sides derived.

**An instruction that does not run is worse than none**, because the reader tries it before
disbelieving it. Every `ci.py` step, `tools/` script and test name cited in a shipped document must
resolve — guarded, derived from `ci.py`'s own step table and the filesystem.

**A warning nobody reads is a check nobody has.** `ruff check` passed on a file Python itself warned
about at import, because the rule was not selected. If a tool can be made to fail on it, make it.

**The subject is what ships, not what is on disk.** A bare glob of the repository root audited a file
absent from the sdist. Ask `git ls-files`, and fall back to the glob only where there is no `git` —
an unpacked sdist, which contains nothing else.

**Measured, not assumed — and isolate the host before blaming the claim.** A documented timing was
five times worse here and the difference was the drive this checkout lives on. Re-measure on the
thing the claim describes.

### Rules with nothing behind them yet

Stated here so the gap is visible rather than implied:

- **A decision record should say what it governs** — enforced for `accepted` records, but nothing
  checks that an `accepted` record's *reasoning* still holds. That needs a human.
- **A claim in prose should be bound** — the count of unbound ones is ratcheted, not driven to zero;
  `docs/claims-baseline.txt` carries the residue and what would close each part of it.

## Run the CI locally, before you push

```bash
python ci.py setup     # dev extras + both pre-commit hook types, once
python ci.py           # lint, test, build — the Linux subset of what CI runs
```

**The workflow calls `ci.py`.** It does not restate the commands, because a copy of a
command list is wrong within a month and then "it passes locally" stops meaning anything.
There is one definition, and you can run it.

**"The Linux subset", not "exactly what CI runs", and the difference has cost two days.** This
line said *exactly* until 2026-10-10. It is not: `ci.py` runs one platform and one interpreter,
while `test.yml` runs eight jobs including Windows, where this package hashes bytes AS WRITTEN
and a fixture using `Path.write_text` with no `newline=""` produces CRLF. The Windows leg was
red for thirteen commits with the local gate green each time. `ci.py`'s own `matrix_check`
docstring states this correctly — *"the gate is green and CI is green are different claims"* —
so the two were contradicting each other in the same repository. **Before a tag, run `python
ci.py matrix-check`,** which reads the hosted matrix's jobs by name.

### The one thing `ci.py setup` installs that you cannot see working

`python ci.py setup` installs **two** hook types, and the second is a `commit-msg` hook that
refuses assistant-attribution trailers — `Co-Authored-By:` naming an assistant, and
`Claude-Session:`.

**Why at commit time rather than at review time.** GitHub parses `Co-Authored-By:` and counts
the named account as a contributor, so removing them means rewriting history — and the cost of
doing that is already measured above, under *"The history will not be retro-signed"*. The figures
are not repeated here: they are one measurement, and a measurement restated in two places is two
claims that can disagree.

What that section does not say, because it is about signing rather than about this hook, is the
part that decides WHERE the guard belongs. Some of those frozen `refs/pull/*/head` snapshots can
never be removed — GitHub refuses every write to them and no API deletes them — so **a trailer
that reaches a branch with a pull request is permanent, whatever `main` says afterwards.** A
guard at merge time would be too late. This one is not.

**There is one definition of the rule**, `_REFUSED_TRAILERS` in `ci.py`. The hook calls
`python ci.py check-commit-msg <file>`; it does not restate the pattern. It used to, in `sh`, in
an untracked `.git/hooks/commit-msg` — and the two copies drifted into guarding *different
families*, so each covered what the other missed and the union existed only on one machine.

**And `ci.py lint` is the authority, not the hook.** `.git/hooks` is not versioned, so a fresh
clone's first commit is unguarded until you run `setup`; the history check runs on every push and
covers every family. If you never run `setup`, nothing is lost except the early warning.

Individually: `python ci.py lint` (ruff format --check, ruff check, mypy), `python ci.py
test` (pytest with the coverage gate), `python ci.py build` (build, twine check --strict,
then install the wheel into a clean venv and import it from elsewhere — which is what
catches a file that is in git and missing from the package).

They are fast (a few seconds) and hermetic: every test builds its own repository and its own
temporary directories. If a test needs the ambient environment, it is testing the wrong
thing — `test_detect_root_finds_the_git_toplevel` used to assert against whatever repository
happened to contain it, and failed the moment the package moved.

## `python ci.py torture` — damage a record and read it back

Not part of the gate. It exits 0 today; it exited 1 until ADR-0005 landed.

The suite builds records the way the package expects them and reads them back the way the
package expects to. Everything it feeds a reader was written by someone who knew what the
reader wanted. `tools/torture.py` feeds the readers what a crash or a bad sector produces:

* **Torn writes.** It enumerates every write site in a real run rather than listing them by
  hand, and crashes inside each one in turn at 0%, 50% and 90%. This is what found that all
  six of the package's writes left a prefix on disk; they go through `runprov/_atomic.py`
  since **ADR-0005** and the destination now survives every tear. Part A checks that each
  call site GOES THROUGH the helper — the helper's own correctness is held by the unit tests,
  and its docstring says so, because a harness silently not covering something is the failure
  this file exists to make loud.
* **A census of zero VOIDS the run.** When the writes moved to `_atomic`, this harness still
  patched `Path.write_text`: it reported no findings over a line reading *0 of them
  runprov's*. A harness that has lost sight of its subject prints exactly what one that found
  nothing prints.
* **Corrupted records.** Eleven byte-level and structural mutations over the history, both
  sidecars, the YAML twin, a pinned artifact and `transformation_log.yml`, with every reader
  held to the exit-code contract (0 / 1 / 2, see `__main__.py`) and to printing no
  traceback. Roughly six hundred reader invocations per run; **no findings** as of
  2026-09-01, which is the sentence the tool exists to be able to say.

Two controls run before any case, because *no findings* and *nothing was damaged* are the
same green: a clean tree must verify 0, and a byte appended to a pinned input must verify 1.
If either fails the run exits **2** and reports nothing, having measured nothing. Every
mutation also asserts its own bytes changed.

A finding prints the flags that reproduce it: `--case a:3 --seed 0 --keep`.

## `python ci.py corpus` — records written by the versions that came before

`tests/corpus/` holds real records produced by every released wheel, captured by installing
each one from PyPI and running one fixed scenario under it. The suite materialises those trees
and holds the current package to them: every artifact every release ever wrote must still
verify OK, every command must read an old history without a traceback, and byte-identical
inputs must have produced byte-identical digests in every version.

This closes the one hole the rest of the suite structurally cannot: every other test builds a
record with the code that reads it. `python ci.py corpus` self-checks the fixture rather than
the package — that the trees carry no path from the machine that built them, that the manifest
matches the scenario on disk, and, with a planted path, that the leak scanner works at all.

Regenerating needs the network and is documented in `tests/corpus/README.md`. **Editing
`tools/corpus_scenario.py` requires regenerating the trees**, and a test enforces it.

## Cutting a release, in order

The order matters, and every step below exists because skipping it cost something real.

1. **Bump the four version sources** — `pyproject.toml`, `runprov/__init__.py`, `CITATION.cff`,
   `CHANGELOG.md` — and re-date `CITATION.cff`'s `date-released` in the *same* commit.
   `release-check` fails on a mismatch and names all four, because a tag on a tree that still
   says the old version publishes the old version, and PyPI never lets that file name be
   reused.
2. **Turn `## [Unreleased]` into `## [<version>] — <tag day>`, and do NOT open a fresh
   `[Unreleased]` yet.** `build` reads the *first* `## [...]` heading, so a new empty section
   above the dated one makes the tagged build report the shipped version as unreleased. It
   refuses, correctly — the alternative is a CHANGELOG on PyPI that describes the release as
   unreleased, permanently.

   **In the SAME commit, clear every ADR status that still says *not yet released*.**
   `docs/adr/` ships in the sdist, so a status saying the feature is unreleased is permanent in
   the release that shipped it. ADR-0017 went out reading *a feature that is not built* through
   0.7.0; this is the same miss one rung over, on the release-state half of the line rather than
   the Proposed/Accepted half, and it is enforced rather than remembered:
   `test_every_adr_is_listed_in_the_adr_index` reads the same first heading `build` does and
   refuses any such status once it is not `[Unreleased]`. Dating the heading and clearing the
   statuses are therefore one commit, not two.
3. **`python ci.py`** — the local gate.
4. **Push the commit and let the hosted matrix finish, then `python ci.py matrix-check`.**
   This step is the one that was missing, and the cost of its absence is measured: the Windows
   leg was red for two days and thirteen commits while the local gate came back green every
   time. **`ci.py` runs one platform**, so *the gate is green* and *CI is green* are different
   claims. `publish.yml` does not close the gap either — it gates the upload on
   `needs: [build, test]`, but its own test job is `ubuntu-latest` only, so the sole Windows
   signal is `test.yml` on the commit you are about to tag. `matrix-check` reads every job **by
   name**, because a run can conclude `success` with a leg skipped, and skipped is not passed.
5. **Tag and push it.** `git tag -a v<version>` then `git push origin v<version>`. The tag
   alone publishes to PyPI, through `publish.yml`.
6. **Create the GitHub release** — `gh release create v<version> --verify-tag --notes-file …`.
   Nothing automates this, and Zenodo's webhook fires on `release`, **not** on the tag: no
   release, no DOI.
7. **Then, and only then, the post-release three:** capture the corpus from the wheel PyPI
   actually serves (`python tools/corpus.py generate --version <version>`), record the new
   version DOI in `CITATION.cff` and the README's version-DOI example, and open the fresh
   `## [Unreleased]`.

Zenodo latency is normal and its webhook log is noisy in a way that looks like failure: GitHub
sends three actions and Zenodo accepts exactly one — `release:released` returns 202 while
`release:published` and `release:created` return 500. Those 500s are refused duplicates.
**Re-firing the webhook while a job is queued risks two records for one version.**

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
