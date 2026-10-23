# 20. An environment rendered from a record is a floor

- **Status:** Proposed — T-36a. Specification for a feature that is not built. **Rewritten
  2026-10-23**, from the version of 2026-10-21 that also *rejected* building the environment:
  that rejection is reversed and the build is specified in ADR-0023, so this record keeps the
  renderer alone. Taylor ruled the split on 2026-10-23 — one ADR cannot span two releases,
  because the requirement-citation guard partitions by declared status, so a single record would
  either be accepted with a dozen requirements no code can cite or stay `Proposed` through the
  release that ships it. Depends on ADR-0007 for its exit codes and ADR-0017 for its JSON.
- **Applies to:** `runprov/environment.py`, read and never changed by this. The renderer's own
  module does not exist yet; the commit that accepts this record names it here. **Amended
  2026-10-23 with R-11 and R-12**, the comparison surface, on Taylor's request for a package-set
  digest a reviewer can recompute off the original host; what a digest means is ADR-0024.

## Context

The record carries the interpreter, the platform, the package list, the manager, and — where one
existed — an archived lock file or the git blob id of one. `runprov resources` already renders a
recorded measurement into the syntax of wherever the work is going next. **This is the same move
for the environment**, and nothing more than that: standing an environment up again is ADR-0023.

### The failure this exists to prevent

A result is questioned two years after it was produced. The environment that made it is gone —
the laptop was reimaged, the cluster module was retired, the venv was deleted. The record names
53 packages and an interpreter. **Nobody can tell whether an environment they build today is the
one that ran**, so the question stops being answerable and the result stops being defensible.

The failure has a specific shape, and it is the one that catches people: an environment built
from a package list **satisfies the record without being the environment that ran.** It looks
right. Every version matches. The transitive graph is different, and nothing says so.

### What already exists, so that no part of this is re-derived

Measured by reading `runprov/environment.py` at `251d564`, not carried from design notes:

```
environment:
  python    : "3.12.13"
  platform  : "Linux-5.15.0-194-generic-x86_64-with-glibc2.35"
  hostname  : "<the recording host>"
  cpu_count : 8
  manager   : {detected: [uv, venv], evidence: {VIRTUAL_ENV: set, "pyvenv.cfg:uv": "0.11.8"}}
  lockfiles : []
  packages  : {}                      # the TRACKED subset — ordinarily empty
  snapshot  : {path, sha256, n_packages: 53, n_unreadable: 0, python, reused, lockfiles}
```

Four facts that shape every requirement below:

1. **The package list is not in the record.** It is a sidecar file named by its own digest, at
   `environment.snapshot.path`. The renderer reads a FILE, and that file can be absent.
2. **`packages: {}` is not "no packages".** `run.py` distinguishes `snapshot` / `tracked` /
   `none`, because an empty map *"cannot be told apart from nobody asked"* (ADR-0010).
3. **The snapshot body already separates pip from conda**, with
   `# --- conda-meta (conda / mamba / micromamba / pixi) ---` and conda entries below it as
   `name=version=build`. The classification needs **no access to the original machine**.
4. **The body carries no prefix path**, deliberately, so two identical environments at different
   locations share a digest.

**This record adds no capture.** It adds a reader and a renderer over capture that has shipped
since v0.1.0 — `write_snapshot` arrived in `222bbf4`, 2026-08-07. (Checked against git, not the
CHANGELOG, whose first mention of the snapshot sits under 0.9.0 because the file was ten days
behind the package. **A first mention is not a first release.**)

## Decision

**R-1.** `runprov env [RUN]` writes the recorded environment to stdout, or to a path given by
`--out`. `RUN` defaults to the most recent run, matching every other read command.

**R-2.** **Where a lock file was archived, the lock is the answer** and the package list is not.
A list of installed packages and their versions is not a lock: it does not pin transitive
resolution, and an environment built from it satisfies the record without being the environment
that ran.

**R-3.** **Where only a list exists, the output says it is a floor**, in the emitted file, as a
comment — the same discipline `resources` uses for a measurement that understates. A rendered
environment presented as exact is a wrong record in a new file.

**R-4.** The interpreter version is emitted and is not negotiable. A package set resolved
against a different Python is a different environment, and this is the single most common way a
"reproduced" run differs.

**R-5.** Packages the run could not identify — the `bad` list the environment block already
records — are named in the output. Silence there would present a partial list as a whole one.
Where they cannot be named, the snapshot header's `# UNREADABLE: N` count is stated instead.

**R-6.** The renderer reads the **content-addressed snapshot file**. When it is absent — pruned,
or written on another machine — the command **refuses with exit 2 and names the digest it wanted
and the path it looked at.** *Could not look* is not *nothing to render*, and ADR-0007's
exit-code contract already distinguishes them.

**R-7.** The pip/conda split is taken from **the snapshot's own separator line**, never from this
host's `conda-meta`. A classification that needs the original machine cannot run where it is
needed.

**R-8.** `--format` selects `requirements`, `conda`, `uv` or `json`, and **defaults to the format
implied by the recorded manager** rather than to a fixed one. `json` follows ADR-0017: one
builder, two renderings, so the JSON is the same answer rather than a second one.

**R-9.** The renderer **never writes into the project** and never overwrites: `--out` refuses an
existing path unless `--force`.

**R-10.** **This command may not state or imply that the run will reproduce.** It renders a
description of an environment. It installs nothing, executes nothing, and makes no claim about
the numbers. **Re-run-and-verify remains a capability runprov does not have**, and ADR-0019's
`rerun` still prints and never runs.

### Verification, added 2026-10-23 and keyed on ADR-0024

**R-11.** `runprov env --compare` **answers whether the environment standing here is the one the
record names, and it is side-effect-free like the rest of this command.** It prints ADR-0024 R-9's
three independent verdicts — package set, interpreter, host — and exits on ADR-0024 R-10's codes,
keyed on the **package set**. A reviewer, a researcher or an auditor is **never on the original
host**, so a host difference is reported and does not by itself decide the answer.

**R-12.** The comparison **names the packages** per ADR-0024 R-11 — *only in the record*, *only
here*, *different version* — never a pair of digests. **This is why verification ships with the
renderer rather than waiting for `rebuild`:** the reader, the canonicaliser and the output format
are already here, and a reviewer who builds an environment by hand in a container needs the
comparison and not the build.

**What a digest means, and how the canonical package set is computed, is ADR-0024** and is not
restated here. One rule, one definition.

## The cross-version test bed, and the three branches it does not reach

`tests/corpus/` holds records written by **nine real released wheels**, installed from PyPI, each
running one scenario. It is the only place in this project where a record is read by code that did
not write it, which is the question this feature lives or dies by: *a snapshot written by 0.1.0
must still render under 0.10.0.* **Measured 2026-10-23 over all 27 records in all 9 versions:**
every record carries the full eight-key environment block; the content-addressed snapshot file is
present at the path the record names in 27 of 27; its body hashes to the recorded `sha256` in 27
of 27; and its filename equals that digest's first sixteen characters in 27 of 27. The nine bodies
are **nine distinct files**, differing by the line naming runprov's own version, so this is nine
writers rather than one file counted nine times.

**And it reaches exactly one of this record's input shapes.** The scenario installs two packages
into a bare venv, so — measured, not assumed:

| branch | corpus coverage | how it is reached instead |
|---|---|---|
| **R-2 rank 1**, a lock file archived | **none** — `lockfiles: []` in 9 of 9 | a synthetic fixture |
| **R-7**, the `# --- conda-meta …` separator | **none** — absent from 9 of 9 bodies | a synthetic fixture |
| **R-5**, a distribution with no readable name | **none** — `n_unreadable: 0` in 9 of 9 | a synthetic fixture |

Stating this is the point rather than a caveat: **nine green versions would otherwise read as
coverage of a feature with three untested branches**, which is the shape of conflating *not
observed* with *not there*. The three zeros are asserted as counts in
`test_every_corpus_version_carries_the_environment_that_env_WILL_RENDER`, **so each one announces
its own retirement** — the day the scenario gains a lock file or a conda prefix, that test goes
red and names the row of this table that has stopped being true.

## Alternatives considered

**Build the environment, not just describe it.** The 2026-10-21 version of this record
**rejected** this, in these words: *"it needs a package manager, a network, and a policy about
what to do when resolution fails. `runprov` records; the manager installs."* **That rejection is
reversed, and the feature is specified in ADR-0023**, which answers each of the three clauses by
requirement number rather than setting them aside. The sentence that justified the rejection —
*"runprov records; the manager installs"* — is the sentence ADR-0023's R-6 preserves, which is
why the reversal is admissible rather than merely convenient. **The reversal is recorded here, in
the record that made the refusal**, because three times in this project a later pass has
re-proposed something an ADR had already refused, and the refusal is what stops it.

**Emit a container recipe.** Rejected, and the reason is unchanged: the record describes a Python
environment, not a system one, and a Dockerfile implies the latter. Revisit if the lock and the
system packages are ever both recorded.

**Classify pip versus conda by reading this host's `conda-meta`.** Rejected on measurement: the
snapshot body already carries the separator, and a classifier that needs the original machine
cannot run in the place a rendering is needed. R-7.

## What this does not describe, stated as a limit rather than discovered by a user

**System packages.** `conda-meta` covers what conda installed — libgcc, openssl, samtools, blast
— and **nothing in the record covers a system package manager.** A rendering from this record
describes a Python environment, whatever system it is read on.
