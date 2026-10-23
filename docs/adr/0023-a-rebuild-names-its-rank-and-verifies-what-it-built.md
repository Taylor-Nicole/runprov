# 23. A rebuild names its rank, and verifies what it built

- **Status:** Proposed — T-36b. Specification for a feature that is not built. Split from
  ADR-0020 on 2026-10-23 by Taylor's ruling, because the requirement-citation guard partitions by
  declared status and one record cannot carry a feature shipping across two releases: ADR-0020
  keeps `env` for 0.10.0 and this record carries `rebuild` for 0.11.0. Reverses the *"build the
  environment, not just describe it"* rejection ADR-0020 recorded on 2026-10-21, and answers each
  clause of it below. Depends on ADR-0020 for the rendering it installs, ADR-0007 for its exit
  codes, ADR-0017 for its JSON, and ADR-0021 for the read that answers *"was this ever rebuilt"*.
- **Applies to:** `runprov/environment.py`, read and extended only by R-12's record field. The
  rebuild's own module does not exist yet; the commit that accepts this record names it here.

## Context

ADR-0020 renders a recorded environment. This record is the move after it: **hand that rendering
to a manager, and then measure what came back.** It is the only command in the package with side
effects on the machine, which is why it is a separate verb and a separate decision.

### The rejection this reverses, and why the reversal is admissible

ADR-0020 refused this feature in three clauses. Each is answered by a requirement rather than
set aside:

| the objection | what answers it |
|---|---|
| it needs a package manager | R-6 — and the manager is **already detected and recorded with evidence**. This command never resolves or installs; it *invokes* the manager the record names |
| and a network | R-9 — a **stated precondition**, refused readably, with its own exit code, rather than a hidden assumption |
| and a policy about what to do when resolution fails | **that policy is the design**: R-5 names the unresolvable set and refuses by default, R-14 names what no longer resolves with the manager's own error, and R-15 records which of these happened |

**"`runprov` records; the manager installs" survives the reversal intact**, which is what makes it
admissible. It is the shape of `resources --format slurm`: runprov renders, the scheduler
enforces. Here runprov renders, the manager installs, **and runprov then measures what the
manager did** — the part no renderer can do.

### The measurement that changed this specification

The first draft of this feature said *"re-snapshot the new environment and compare digests"*.
**Measured 2026-10-23 with `_render_snapshot` itself**, two identical package sets on the same
interpreter and two different kernels:

```
digest A 5e722f22f44dcb4c      digest B 8d48d1dcc3265740      equal: False
the only differing line:
  -# platform : Linux-5.15.0-194-generic-x86_64-with-glibc2.35
  +# platform : Linux-6.8.0-51-generic-x86_64-with-glibc2.39
```

`_render_snapshot` writes `# platform : {platform.platform()}` **inside the body that
`write_snapshot` digests.** So the snapshot digest is **not host-independent**, and a comparison
by body digest **could never report `equal` on a different machine** — which is the whole
two-years-later case this feature exists for. It would answer *"it differs"* while listing no
package differences at all, and **a verifier whose only possible answer is "differs" is worse than
none, because it reads as a finding.** R-11 and R-12 are what that measurement produced.

## Decision

### The host check, which runs before anything is created

**R-1.** `rebuild` **compares the recorded host to this host and refuses rather than differing in
silence.** A command that discovers an architecture mismatch as an obscure resolver error has
wasted the record.

**R-2.** Each compared field is classified **BLOCKING**, **WARNING** or **IGNORED**, and the
classification is printed with its reason. **The classified population is derived from the keys
the record actually carries**, never from a list typed into the source: a key present with no
classification **fails the build**, and a key the record does not carry **degrades to WARNING with
that stated as the reason**. A record written by any version must be handled by one code path,
because the environment block will keep growing and a rebuild that enumerates its fields is a
rebuild that silently stops covering them.

**R-3.** The recorded `platform` string contains the machine and the libc version, but **it is not
an addressable field.** The rebuild may parse it **only to report**, and a parse failure is a
WARNING, never a BLOCK. **A brittle parser over a free-form string must not have the power to
refuse a rebuild** — and records written before any dedicated host fields exist will never carry
them, so this parser is permanent for them rather than a bridge.

### Where the packages come from

**R-4.** **Precedence, best to worst, and the command says which rank it used.**

| rank | source | why it is better |
|---|---|---|
| 1 | the project's **lock file**, identified by digest in `environment.lockfiles`, recoverable from git by blob id | it is the declaration the project maintains, and it encodes the resolved graph |
| 2 | the **content-addressed snapshot**, rendered by ADR-0020 | resolves versions but not the graph the lock encodes |
| 3 | **partial** — rank 2 minus the unresolvable set | only under `--allow-partial` |

**A rebuild that does not say which rank it used is a rebuild nobody can trust.**

**R-5.** The **unresolvable set is named**, in its own section, and `rebuild` **refuses by
default** rather than standing up an environment that is quietly not the recorded one.
`--allow-partial` overrides, and **the new environment's own record says it was partial.**
Unresolvable means: no version recorded; an editable or local-path install; a conda package whose
channel was never recorded.

**R-6.** `rebuild` **invokes a manager and never resolves or installs anything itself.** The
manager and its exact argv are printed before it runs and recorded afterwards.

**R-7.** `--tool` **defaults to the recorded manager and may be overridden, and an ambiguity
refuses rather than guessing.** `manager()` records a LIST — the measured record carries
`detected: ["uv", "venv"]` — so the default is resolved through a **precedence table derived over
the recorded entries**, and an entry with no place in that table **exits 2**. A manager that does
not exist yet then reddens on arrival instead of being driven wrongly.

### Invocation

**R-8.** `--dry-run` prints the exact command and the rendered input **and creates nothing.**

**R-9.** A **network is a precondition**, stated in the help and in the refusal. Absent, the
command exits 2 — *could not look* — never 1.

**R-10.** **The interpreter must match.** A mismatch between `environment.python` and the
interpreter the manager would use is **BLOCKING**, overridable only by `--python-match minor`, and
the override is recorded. *(ADR-0020 R-4 at rebuild time: the renderer emits, the rebuild
enforces.)*

### Verification, which is the point of the command

**R-11.** **RE-SNAPSHOT THE NEW ENVIRONMENT AND COMPARE THE PACKAGE SET — never the body
digest.** The comparison is over the snapshot body **below its header**, and the header's host
differences are reported **separately, as host drift.** The measurement above is why: the
digested body contains this host's platform string, so a body-digest comparison answers a
question about the host when it was asked a question about the packages.

**R-12.** **The snapshot BODY is never changed; `packages_sha256` is added to the snapshot
RECORD** — the digest of the body below the header — and for a record written before that field
existed the rebuild **recomputes it from the snapshot file**, which R-11 makes it read anyway.
The body is content-addressed and nine releases of digests depend on its bytes, so **a field goes
into the record and is derived for older records by re-reading the file.** This is the rule for
every future addition, not a one-off.

**R-13.** The exit code follows ADR-0007 and is never collapsed: **0** the rebuilt package set
equals the recorded one · **1** it differs, and the differences are listed · **2** the rebuild
could not look — no snapshot file, no network, no manager, or a blocking host difference.

**R-14.** A package that **no longer resolves** — yanked, deleted, or moved behind an index that
is gone — is reported **by name with the manager's own error**, never swallowed into a generic
failure. This is the ordinary two-years-later failure, not an edge case.

### The record of the rebuild

**R-15.** The rebuild **writes a record of itself into its OWN history**, naming the source run by
`run_uid`, the rank used, the manager, the partial flag and the comparison result. **It does not
write into the source project's history**, for two reasons: the source may be read-only — an
archived supplement is the two-years-later case, not an edge case — and ADR-0021 has already
decided that *"no file is ever written, merged, moved or rewritten… a merge that produced a file
would be a second copy of the truth."* *"Was this ever rebuilt, and did it match?"* is therefore a
**merged read** across both histories, on ADR-0021's `run_uid` identity. `--log` pointing at the
source project remains available to a user who wants it there and can write there. **The cost is
stated: until ADR-0021's merged read exists, answering that question means naming two log files.**

### A record is untrusted input

**R-16.** **The rebuild hands names from a record to a package manager, so the record is an attack
surface.** Every name taken from a snapshot is validated against the distribution-name grammar
before it reaches an argv, and anything that is not a bare name and version is **refused, named,
and never passed through** — in particular a leading `-`, a URL, a path, an index directive, or an
environment marker. A record is a file that travels: it arrives by email, in a supplement, from a
collaborator's repository. **Without this requirement `runprov rebuild` is a remote-code-execution
primitive wearing a provenance label.**

**R-17.** `rebuild` **never writes into an existing environment.** `--name` must not already exist
unless `--force`, because the failure it prevents — silently mutating the environment the user is
standing in — is unrecoverable.

### What this must never claim

**R-18.** **This command may not state or imply that the run will reproduce.** It stands up an
environment; it does not re-execute anything and makes no claim about the numbers. The output
carries that sentence and the limitations keep saying so. **Re-run-and-verify remains a capability
runprov does not have**, and ADR-0019's `rerun` still prints and never runs.

**R-19.** **The first version drives pip and uv only.** conda entries are rendered by ADR-0020 and
rebuilt only at rank 1 from a lock, because **a conda package whose channel was never recorded is
unresolvable by construction** (R-5) and no snapshot written to date carries a channel. Shipping a
conda path before the channel is captured would mean shipping mostly-refusals. The order is
**R-12, then channel capture, then conda** — and the channel is a change to the snapshot body,
which is why it waits for R-11 to stop comparing bodies.

## Alternatives considered

**Resolve dependencies ourselves.** Rejected: a resolver is a large, wrong-to-own problem, and the
managers already detected are better at it. R-6 makes the boundary explicit.

**Emit a container recipe.** Rejected, carried from ADR-0020: the record describes a Python
environment, not a system one, and a Dockerfile implies the latter.

**Default to `--allow-partial`.** Rejected: an environment that is quietly not the recorded one is
precisely the failure ADR-0020's *Context* names, and a default that produces it would make the
command worse than nothing.

**Compare the snapshot body's digest.** Rejected on the measurement above — it can never report
equal on a different host, which is the only host that matters here. R-11.

**Write the rebuild's record into the source project's history.** Rejected under R-15: it requires
write access to the thing being questioned, and ADR-0021 already decided that histories are
merged at read time rather than written into each other.

**Add dedicated host fields first and compare them instead of parsing `platform`.** Not rejected,
and deliberately not a dependency: records written before such fields exist will never carry them,
so R-3's parser is permanent either way and R-2's derived classification handles both vintages in
one code path. Adding the fields improves records written afterwards and changes nothing here.

## What this does not record, stated as a limit rather than discovered by a user

**System packages.** `conda-meta` covers what conda installed — libgcc, openssl, samtools, blast
— and **nothing in the record covers a system package manager.** An environment rebuilt from a
record is a Python environment on whatever system it is standing on.

**The resolved graph, at rank 2.** Two rebuilds from one record can differ at rank 2. That is what
R-11 measures and what rank 1 exists to avoid. **This record claims no determinism it cannot
deliver.**
