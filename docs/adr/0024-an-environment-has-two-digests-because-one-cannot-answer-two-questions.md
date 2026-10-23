# 24. An environment has two digests, because one digest cannot answer two questions

- **Status:** Proposed — T-36c. Specification for a feature that is not built. Asked for by Taylor
  on 2026-10-23: *"can we make it so that there is an environment digest per host and also per
  package set… one that can be recreated in a containerized environment for a reviewer,
  researcher, auditor etc, and the verification might not always be on the original host."*
  Depends on ADR-0010 for what a record may state, ADR-0007 for its exit codes, and ADR-0005 for
  how the record is written. ADR-0020 and ADR-0023 cite this record rather than restating it.
- **Applies to:** `runprov/environment.py`, whose snapshot body this record forbids changing.

## Context

`write_snapshot` digests the whole rendered body, and `_render_snapshot` writes
`# platform : {platform.platform()}` into that body. **Measured 2026-10-23**, two identical
package sets on the same interpreter, one on `x86_64`/`glibc2.35` and one on
`aarch64`/`glibc2.39`:

| | digest |
|---|---|
| host A | 5e722f22f44dcb4c |
| host B | ec158ad0de033419 |

The only differing line was the platform header. **So the one digest the package has answers
*"has this environment changed on this machine?"*, and it has been read as answering *"are these
the same packages?"*** The two questions have different answers and only one of them is the one a
reviewer in a container can ask.

### Why this is not a rounding error in the claim

The failure is not that a comparison is unavailable. It is that **the available comparison
answers *differs* when the packages agree**, and reports it in the same shape as a real finding.
A reviewer who rebuilds the package set in a container, correctly, is told the environment does
not match. **A verifier whose only possible answer is "differs" is worse than none.**

And a reviewer, researcher or auditor is **never** on the original host. That is the whole case:
the laptop was reimaged, the cluster module was retired, the container is the only ground left.

### What this record does NOT do

**It does not change the snapshot body.** The body is content-addressed, its filename *is* its
digest, and nine released versions of recorded digests depend on its bytes — measured, 27 of 27
corpus records hash to the value they carry and to the name they are stored under. A new line in
the header would move every future digest, turn `reused` false once for every project on earth,
and make the nine-release corpus incomparable with itself. **The second digest therefore goes in
the RECORD and is recomputed from the body, never written into it.**

## Decision

### The two digests, and the rule that produces them

**R-1.** **A digest answers exactly one question.** The record carries two, and neither is derived
from the other:

| field | over | answers |
|---|---|---|
| `snapshot.sha256` *(exists today, unchanged)* | the whole rendered body, header included | *has this environment changed **on this host**?* |
| `snapshot.packages_sha256` *(new)* | the canonical package set, below | *are these **the same packages**?* |

**R-2.** **The interpreter is not in either package-set digest; it is compared as its own recorded
field.** `environment.python` and `snapshot.python` already carry it. A digest bundling the
package set with the interpreter cannot say which of them moved, which is the defect this record
exists to repair — repeating it one level in would be the same mistake with a smaller blast
radius. Measured: a `# python` header change moves `sha256` and leaves `packages_sha256` alone.

**R-3.** **`snapshot.sha256` is NOT renamed and NOT redefined.** Its meaning is documented as
host-bound. Renaming it would break every consumer and every corpus record for a gain of clarity
that a sentence buys instead.

### The canonical package set, specified so it can be recomputed without this package

**R-4.** The canonical text is, in order, LF-separated with a trailing newline:

```
runprov.pkgset.v1
pip <name> <version>          # one line per pip distribution, sorted by name
conda <name> <version> <build>  # one line per conda package, sorted by name
```

`<name>` is **PEP 503 normalised** — runs of `-`, `_` and `.` collapsed to a single `-`, then
lowercased. The first line is a **domain separator**, so this digest can never collide with a
digest of anything else. `packages_sha256` is the SHA-256 of that text.

**R-5.** **The parse is total, and an unclassifiable line is a refusal rather than a dropped
line.** Every non-blank line of a snapshot body is exactly one of: the conda separator
(`# --- conda-meta (conda / mamba / micromamba / pixi) ---`); a conda entry, after it; a pip
entry, before it; or an **annotation**, which is any other line beginning with `#`. A line that
matches none of these **refuses with exit 2**. A canonicaliser that silently drops a line it does
not understand produces a digest that is confidently wrong, and nothing downstream can tell.

**R-6.** `snapshot.packages_algo` **records the canonical form by name** — `runprov.pkgset.v1`.
The name is **frozen on release**, like `runprov.gate.v1` and `runprov.policy.v1`. A change to the
canonical text is `runprov.pkgset.v2`, both remain computable, and a record states which it used.
**A digest whose algorithm is not named is a number nobody can reproduce in five years.**

**R-7.** **For a record written before this field existed, the value is RECOMPUTED from the
snapshot body, and the output says it was recomputed rather than read.** Every corpus version from
0.1.0 onward is therefore comparable today. *Recomputed* and *recorded* are not the same evidence
and are never printed as though they were.

**R-8.** **The recorded `packages_sha256` is verifiable against the body**, exactly as `sha256` is,
so a record that disagrees with its own sidecar is detectable rather than believed.

### Verification where the reviewer actually stands

**R-9.** **The comparison reports three independent verdicts, each on its own line:** the package
set, the interpreter, and the host. A reviewer in a container expects the third to differ and must
not be told the environment does not match because of it.

**R-10.** **The exit code is keyed on the PACKAGE SET** and follows ADR-0007: **0** the package
sets are equal · **1** they differ, and the differing packages are named · **2** the comparison
could not be made — no snapshot file, an unclassifiable body, or a record whose
`packages_sha256` disagrees with its body. **An interpreter or host difference is reported and
does not by itself move the exit code**, because it is a fact about where the reviewer is
standing, not about whether the environment was rebuilt correctly.

**R-11.** A difference **names the packages**, in three sets — *only in the record*, *only here*,
*different version* — never a digest pair alone. Two digests tell a reviewer that they failed; the
three sets tell them what to install.

**R-12.** **No command emits a container recipe.** ADR-0020's rejection stands: the record
describes a Python environment, not a system one. What this record guarantees instead is that
everything a reviewer needs to *build* one is in the output — the rendering, the interpreter
version, and the digest to compare against — and that **the comparison does not require this
package**, by R-4.

## The measurement this record rests on

Twelve properties, measured 2026-10-23 against `_render_snapshot` and all 27 corpus records:

| property | result |
|---|---|
| host digest moves across architecture / libc | **yes** |
| package-set digest holds across architecture / libc | **yes** |
| `# python` change moves the host digest, not the package-set digest | **yes, both** |
| `# UNREADABLE` change moves the host digest, not the package-set digest | **yes, both** |
| package-set digest moves on a version bump | **yes** |
| package-set digest moves on an added package | **yes** |
| line order does not matter | **yes** |
| `Foo_Bar` and `foo-bar` digest identically (R-4) | **yes** |
| conda entries parse, and a pip-only set differs from pip+conda | **yes** |
| conda package-set digest is host-invariant too | **yes** |
| every line of all 27 corpus bodies classified, 0 unclassifiable | **27 / 27** |
| **R-4 reimplemented from this text alone** — stdlib only, no runprov, no network — reproduced all nine corpus package-set digests | **9 / 9** |

The last row is the one that matters for R-12, and it is the reason R-4 is written as bytes rather
than as prose: a specification a reviewer cannot reimplement is a specification that makes them
trust this package, which is the opposite of the point.

## Alternatives considered

**Strip the platform line from the body instead.** Rejected: it changes the body, so every future
digest moves, `reused` goes false once everywhere, and the nine-release corpus stops being
comparable with itself — for a property that a second field delivers at no cost to anything
already written.

**One digest, with the platform removed.** Rejected under R-1: the question *"did this environment
change on this machine?"* is a real question with real users, and deleting its answer to gain the
other one trades a defect for a different defect.

**Three tiers — packages, packages + interpreter, packages + interpreter + host.** Rejected: the
middle tier is the product of two independent facts and so cannot say which moved. Two digests
plus a separately-compared field answers all three questions and attributes every difference.

**Put `packages_sha256` in the snapshot body's header.** Rejected under R-3 and *Context*: the
body is the thing being digested, so a digest of the body cannot be inside it, and any new header
line moves every existing digest.

**Let the reviewer diff the rendered `requirements.txt` instead.** Rejected: a diff of rendered
text is sensitive to name spelling, ordering and the renderer's own version, which is why R-4
normalises. It also gives a reviewer no single value to quote in a paper or a review.

## What this does not record, stated as a limit rather than discovered by a user

**Equal package sets are not equal environments.** The package-set digest covers names, versions
and conda builds. It does not cover the resolved transitive graph, a wheel rebuilt under the same
version, system libraries, or anything outside Python. **It answers *are these the same packages*
and nothing more**, which is why ADR-0023 keeps rank 1 — the lock file — above it.
