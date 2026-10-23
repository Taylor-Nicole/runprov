# 24. An environment has two digests, because one digest cannot answer two questions

- **Status:** Proposed — T-36c. Specification for a feature that is not built. **Six requirements were split out to ADR-0025 and BUILT on 2026-10-23** — the canonical form and its single implementation — because a module's docstring citing a record is a claim the record is implemented, and the gate refuses that against a `Proposed` one. Their numbers here are kept as pointers. What remains is the two-digest record model, the comparison and the preflight, none of it built. Asked for by Taylor
  on 2026-10-23: *"can we make it so that there is an environment digest per host and also per
  package set… one that can be recreated in a containerized environment for a reviewer,
  researcher, auditor etc, and the verification might not always be on the original host."*
  Depends on ADR-0010 for what a record may state, ADR-0007 for its exit codes, and ADR-0005 for
  how the record is written. ADR-0020 and ADR-0023 cite this record rather than restating it.
- **Applies to:** `runprov/environment.py`, whose snapshot body this record forbids changing., `runprov/pkgset.py`

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
| `snapshot.package_sets` *(new; a map, see R-20)* | the canonical package set, below | *are these **the same packages**?* |

**R-2.** **MOVED TO ADR-0025 R-5 on 2026-10-23** — the interpreter is excluded from the package-set digest. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

**R-3.** **`snapshot.sha256` is NOT renamed and NOT redefined.** Its meaning is documented as
host-bound. Renaming it would break every consumer and every corpus record for a gain of clarity
that a sentence buys instead.

### The canonical package set, specified so it can be recomputed without this package

**R-4.** **MOVED TO ADR-0025 R-1 on 2026-10-23** — the canonical text. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

**R-5.** **MOVED TO ADR-0025 R-2 on 2026-10-23** — the total parse, and the refusal on an unclassifiable line. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

**R-6.** **MOVED TO ADR-0025 R-4 on 2026-10-23** — the algorithm's name and its freeze. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

**R-7.** **For a record written before this field existed, the value is RECOMPUTED from the
snapshot body, and the output says it was recomputed rather than read.** Every corpus version from
0.1.0 onward is therefore comparable today. *Recomputed* and *recorded* are not the same evidence
and are never printed as though they were.

**R-8.** **The recorded package-set digest is verifiable against the body**, exactly as `sha256` is,
so a record that disagrees with its own sidecar is detectable rather than believed.

### Verification where the reviewer actually stands

**R-9.** **The comparison reports three independent verdicts, each on its own line:** the package
set, the interpreter, and the host. A reviewer in a container expects the third to differ and must
not be told the environment does not match because of it.

**R-10.** **The exit code is keyed on the PACKAGE SET** and follows ADR-0007: **0** the package
sets are equal · **1** they differ, and the differing packages are named · **2** the comparison
could not be made — no snapshot file, an unclassifiable body, or a record whose
recorded package-set digest disagrees with its body. **An interpreter or host difference is reported and
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

### Where the comparison happens, and what must be present for it

**R-13.** **Comparison is never mandatory.** `runprov env [RUN]` renders and compares nothing;
every comparison is asked for. The renderer remains the default because the commonest use of this
command is *"give me something I can install"*, and a command that verified by default would need
an environment present in order to print a file.

**R-14.** **A comparison of TWO RECORDS requires neither environment to be present.** This is the
case a journal reviewer and an external auditor are actually in: two submissions, or a submission
and a reference, on a laptop with neither environment installed. It is the record-to-record
question — *do these two describe the same package set?* — and it needs only two records and their
sidecars.

**R-15.** **Each operand is named explicitly and an ambiguous operand refuses.** The operand kinds
are: a run in a history; a path to a record or a snapshot body; a **published identity string**
(R-16); and `--here`, meaning the environment this process is running in. **Nothing is inferred
from the shape of an argument** — the same rule ADR-0023 R-7 applies to `--tool`, and for the same
reason: a wrong guess here produces a confident comparison of the wrong two things.

**R-16.** **`runprov.pkgset.v1:<16 hex characters>` is a first-class operand and the quotable
form.** It is what goes in a methods section, a data-availability statement or a review report: one
line, self-describing, naming its own algorithm so it can still be checked when the algorithm has
moved on. A reviewer holding only a paper and the authors' record can answer the question with
those two things.

**R-17.** **THE RECORDER'S OWN VERSION IS THE EXPECTED DIFFERENCE, AND THE OUTPUT MUST SAY SO.**
Measured 2026-10-23 over the nine corpus versions: their package-set digests are **9 distinct
values, and exactly 1** once `runprov` itself is excluded — every other package is identical
across all nine. **So a reviewer who installs runprov in order to check a record will differ by
exactly one package, every time**, and will read a correct rebuild as a failure. Therefore:

* nothing is **ever** excluded by default — `runprov` is in the environment and the digest says so;
* when the differing set is **only the recorder**, the comparison says that in words, names the
  recorded and the local version, and **names the flag** that answers the reviewer's actual
  question;
* `--ignore <name>` excludes named distributions, **the ignored set is always printed and recorded
  in any record the comparison writes**, and an ignore that matched nothing is reported rather
  than silently satisfied.

**A verifier whose commonest correct outcome reads as a failure teaches its users to ignore it**,
which is the same defect as one whose only possible answer is *differs* — one level in, and found
by measuring the corpus rather than by review.

**R-18.** **MOVED TO ADR-0025 R-6 on 2026-10-23** — one stdlib-only implementation, copyable out of the distribution. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

### Three requirements that came from reverifying this record before building it

**R-19.** **MOVED TO ADR-0025 R-3 on 2026-10-23** — a collision of canonical names is refused, not merged. The number is kept rather than reclaimed so that a citation written against this record still resolves to the right requirement, and so the move is visible rather than inferred from a gap.

**R-20.** **The record carries a MAP from algorithm name to digest, not a digest plus a separate
algorithm field:** `snapshot.package_sets: {"runprov.pkgset.v1": "<64 hex>"}`. With two flat
fields a reader that forgets to consult the algorithm name compares a v2 digest against a v1
digest and reports a **false difference**; with a map the lookup is *by* the algorithm the reader
understands, so getting the wrong one is impossible rather than merely discouraged. A record
mid-transition may carry both, which is what R-6 promises and what two flat fields cannot express.
**A reader finding no entry for its algorithm answers *incomparable* — ADR-0014's "an
incomparability is a value, not a missing key" — never *different*.**

**R-21.** **The field is written into BOTH record shapes, and `diff` stops being blocked where it
is present.** Measured: the history projection writes a top-level `environment_snapshot` and the
sidecar writes `environment.snapshot`, and both carry the same seven keys today — so a field added
to one and not the other is invisible to every consumer fed history records, `diff` among them.
And `diff.compare()` currently reports a differing snapshot digest as **`blocked`**, with the
reason *"a snapshot digest covers the interpreter and the platform as well as the packages, so it
cannot say which of them moved"* — **this record's own finding, already in the package, already
reasoned about, and worked around by declining to answer.** With `package_sets` recorded, that
dimension becomes answerable. The old path stays for records written before the field existed,
because `compare()` is pure and never opens a snapshot file, so it cannot recompute what was not
recorded — and *recorded* versus *recomputable* is exactly the split ADR-0014 exists to keep
visible.

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
| nine corpus package-set digests, **excluding `runprov` itself** | **9 distinct become 1** — R-17 |
| a body with two entries normalising to one name, in two orders | **two different digests** — R-19 |

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

**Put the package-set digest in the snapshot body's header.** Rejected under R-3 and *Context*: the
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
