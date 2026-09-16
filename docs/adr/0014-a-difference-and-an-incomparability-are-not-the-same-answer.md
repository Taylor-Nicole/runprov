# 14. A difference and an incomparability are not the same answer

Date: 2026-09-16 · Status: **accepted**, implemented as `runprov/diff.py` · Ledger: T-30

## Context

The question a researcher asks most often is not *what did this run do* — it is **why is today
different from last month**. Same script, same command, different numbers. Today that means
opening two records side by side and reading.

Everything needed is already recorded: inputs with digests, commit and dirty state, parameters,
`environment`, `tool`, `resources`, and the step argument digests ADR-0010 built expressly so
that *"did this function see the same inputs?"* would be answerable. **No command asks it.**

```
$ runprov diff align@2026-08-01 align@2026-09-16
inputs      1 changed    refs/hcv_ref.fa   17ffe705… → a3d2c39b…
code        commit       a1b2c3d → f4e5d6a   (clean → clean)
parameters  threshold    5 → 7
resources   peak memory  312 MiB → 1.4 GiB
```

## The failure mode, which is the whole of this decision

**Two records can be laid side by side and still not be comparable**, and a diff that does not
know the difference reports a change that did not happen — or worse, agreement that was never
established. ADR-0010 already decided this for one dimension:

> Two records whose `observation` blocks differ may be compared on files — that half is
> unchanged and version-independent — but any report about steps says so rather than reporting
> an absence as an agreement.

That principle is right and it is not limited to steps. **Every dimension has its own version
of it**, and the diff is wrong in a different way in each:

| dimension | when the two records are not comparable | what a naive diff reports |
|---|---|---|
| **steps** | `observation.auto_available` differs — 3.11 could not see what 3.12 saw | "5 functions appeared" for a change of interpreter |
| **code** | either record has `git_code_dirty: true` or `git_status_captured: false` | a commit-to-commit change, when one side's commit does not name the code that ran |
| **packages** | `observation.packages_recorded` differs — `none` vs `tracked` vs `snapshot` | "0 → 47 packages", for a configuration change |
| **resources** | `resources.source` differs — `getrusage` vs `cgroup` | "memory 312 MiB → 8 GiB", for moving from a laptop to Slurm and starting to measure the right quantity |
| **inputs** | either record carries `pin_partial`, or `unregistered_reads` | "no input changed", over a run whose real inputs were never recorded |
| **anything truncated** | `steps_truncated`, `observed_truncated`, `auto_stopped_after_calls`, `imported_code.omitted` | an absence in a truncated record read as an absence in the run |
| **schema** | the two records carry different `schema` values | `content_sha256` changed meaning at v1→v2, so the fields are two different quantities |

Every row is a real field that already exists, and every one of them was added because
somebody could otherwise not tell *not measured* from *measured as zero*. A diff is where that
distinction finally gets used — or finally gets thrown away.

## Amendment while implementing — two corrections the table got wrong

**1. `tool.identifies_code` is about runprov, not about the user's code.** The first version of
the table above used it as the precondition for comparing commits, which conflates two
identities that have nothing to do with each other: `tool` says whether *this package's* code
can be recovered, and the project's code identity is `git_commit` with `git_code_dirty` and
`git_status_captured`. A dirty tree is what makes a commit fail to name what ran.

**2. INCOMPLETE is not the same as INCOMPARABLE, and collapsing them loses the useful half.**
A run with an `unregistered_read` did not record all its inputs — but a *changed* input it did
record is still a true finding, and refusing to report it would decline the most useful thing
the command could say. What such a record cannot support is the word **unchanged**.

So the precondition governs which *conclusions* a dimension may draw, not whether it is
examined at all. Two facts, four states:

| | differences found | none found |
|---|---|---|
| **complete** | `changed` | `unchanged`, with what was examined |
| **incomplete** | `changed`, and *not fully comparable: <reason>* | **`not comparable`** — never `unchanged` |

The bottom-right cell is the whole point: a record that could not see all its inputs must never
be the source of "nothing changed".

## Decision

**Comparability is decided PER DIMENSION, and an incomparable dimension is reported as such —
never as "no change".**

1. **The dimensions are independent.** Files are comparable across interpreters, package
   managers and schema versions; steps are not. A diff that refused entirely because one
   dimension was incomparable would be useless, and one that compared everything anyway would
   be wrong. Each dimension carries its own precondition, derived from the fields above.
2. **The output distinguishes three states, not two:** `changed`, `unchanged`, and
   **`not comparable`** with the reason. A reader must be able to tell *"the inputs are the
   same"* from *"nobody recorded what the inputs were"*.
3. **`unchanged` is an assertion and carries its scope.** For any dimension reported unchanged,
   the diff states what was compared — `3 inputs`, `2 of 2 steps` — because "no difference
   found" and "nothing examined" print the same word, which is the defect this package exists
   to catch and which has been caught six times inside it.
4. **Exit 0 means comparable AND identical, in every dimension.** Differences exit 1;
   **an incomparable dimension also exits non-zero**, because a gate that greens while half the
   comparison was impossible is the vacuous pass in a new place. A user who wants "ignore what
   cannot be compared" says so explicitly.
5. **It is a derived view.** It computes nothing that is not already recorded, and no field
   exists in its output that `show` could not also produce. A diff able to say something the
   records do not would be a second source of truth.

## Sketch

```
runprov diff <A> <B> [--log L] [--only inputs,code,…] [--format text|json]
```

`A` and `B` address runs the way `show` already does: a `run_uid` prefix, a `run_id`, or
`script@date` for the latest run of a script on a day. Reusing that resolver matters more than
it looks — a second addressing scheme in the same tool is how two commands come to disagree
about which run you meant.

```
inputs        1 of 3 changed
                refs/hcv_ref.fa    17ffe705… → a3d2c39b…
code          NOT COMPARABLE — A was recorded from a dirty tree (identifies_code: false),
              so its commit a1b2c3d does not name the code that ran
parameters    1 changed: threshold 5 → 7
steps         NOT COMPARABLE — A: auto_available false (3.11), B: census (3.12)
packages      unchanged (snapshot env-8a91… on both)
resources     NOT COMPARABLE — A measured by getrusage, B by cgroup
```

## What it must never claim

* That two runs agreeing on every comparable dimension **produced the same result** — the
  outputs are a dimension like any other, and an unrecorded read can still change them.
* That a dimension reported `unchanged` was **examined at all**, unless it says what it
  examined. See decision 3.
* That the difference it found is the **cause**. It records; it does not audit. Four inputs
  changed and the one that mattered is not marked, because nothing in the record knows.

## Alternatives considered

**Diff the JSON.** `jq` and `diff` already exist and this would be a worse version of them: a
textual diff reports key order, timestamps, paths and every field that legitimately differs per
run, and it has no idea that `observation` decides whether `steps` may be compared at all.

**Refuse entirely when the records differ in capability.** Rejected: the most useful diff in
practice is between a run on a laptop and a run on a cluster, which is exactly the pair whose
`resources.source` and often `observation` differ. Refusing would decline the case it is for.

**Report incomparable dimensions as "unchanged" and note it in a footer.** Rejected for the
reason ADR-0010 exists: the summary is what gets read, and a footer is where a caveat goes to
be ignored.
