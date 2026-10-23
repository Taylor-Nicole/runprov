# 26. One environment identity, and one comparison over two of them

- **Status:** Accepted — built 2026-10-23 as `runprov/pkgset.py`'s consumer, `runprov/env.py`,
  and the `env` subcommand. **The third split forced by the same cause, and the cause is now
  named:** ADR-0020, ADR-0023 and ADR-0024 were each written as a specification spanning two
  releases, and a module's docstring citing a decision is a claim the decision is implemented —
  so every build against one of them reddens the index guard until the built part is carved out.
  **An ADR should be sized to one shipped capability.** This record is that size: eleven
  requirements, all built, all cited by a test. ADR-0020, ADR-0023 and ADR-0024 keep their
  unbuilt remainder.
- **Applies to:** `runprov/env.py`, `runprov/__main__.py`

## Context

ADR-0024 establishes why an environment needs a host-independent digest; ADR-0025 is the form of
it. This record is **what a reader does with them**, and its shape is chosen for what has to be
added later rather than for what is needed now.

Five features consume this answer: `env --compare` and `--digest` and `--can-check` today, a
merged group of histories after ADR-0021, and **ADR-0023's rebuild, which verifies what a manager
built by asking exactly this question.** If each of those carried its own comparison, this project
would hold five implementations of *are these the same packages* — the shape it has measured to
drift by whole cases rather than by details, on its own hardest rule.

## Decision

**R-1.** **ONE identity, resolved from a source; ONE comparison, over two identities.** An
identity carries the package-set digest by algorithm, the interpreter version, the host-bound
snapshot digest, the package count, and the parsed package map **when a body was readable** —
each with **how it was obtained**: recorded, recomputed, observed, or absent with the reason.
**Every later feature is a new source**, which is a function returning an identity; nothing
downstream of it changes. A new comparison is forbidden, and ADR-0023 R-11 is a consumer of this
one.

**R-2.** **Three independent verdicts — package set, interpreter, host — and the exit code is
keyed on the package set alone.** A reviewer in a container expects the host to differ and must
not be told the environment does not match because of it: that is a fact about where they are
standing, not about whether the environment was rebuilt correctly. 0 the package sets agree · 1
they differ · 2 the question could not be put [ADR-0007].

**R-3.** **Comparison is never mandatory.** The command renders, compares, prints an identity or
describes what can be checked, and the caller says which. A mode is **required in this release**
because ADR-0020 R-1's renderer is not built and there is therefore no honest default; when it
lands it becomes the default and every invocation written today keeps its meaning — an addition,
not a change.

**R-4.** **A comparison of two records requires neither environment to be present.** This is the
case a journal referee and an external auditor are in: two submissions on a laptop with neither
environment installed.

**R-5.** **Operands are named and never inferred from their shape.** `--here` is a flag, a
published identity is recognised by the algorithm prefix it is required to carry, and anything
else is a run in the history. The same rule ADR-0023 R-7 applies to `--tool`: a wrong guess
compares the wrong two things and reports it with the confidence of a right one.

**R-6.** **Nothing is excluded by default, and the expected difference is named with its flag.**
Measured over nine released corpus versions: their package-set digests are nine distinct values
and **exactly one** once `runprov` is excluded. A reviewer who installs runprov to check a record
differs by one package every time. So when the differing set is **only the recorder**, the output
says that in words and names `--ignore`; an ignore is printed, carried in the payload, and
**reported when it matched nothing**; and under an ignore **no recorded digest is quoted**,
because a digest covers the whole set including the excluded name.

**R-7.** **The detail level is declared, never left to be inferred from silence.** The packages
are named where **both** bodies are readable; where one is not, the comparison reports the digest
pair, says which side it could not open, and says that **whether the only difference is the
recorder cannot be determined.** Silence about an unavailable diagnosis is indistinguishable from
that diagnosis coming back negative.

**R-8.** **A record that disagrees with its own sidecar is neither believed.** Where both the
recorded digest and the body are in hand they must agree; when they do not, one has been edited,
nothing here can say which, and naming a winner would be an invention. The reason carries both
values.

**R-9.** **A digest absent from a record is recomputed from the body, and labelled as
recomputed.** That is what makes every corpus version comparable today — nine releases wrote the
snapshot and none wrote the digest. *Recorded* is what the run asserted and *recomputed* is what
the file still says; a reader that cannot tell them apart cannot tell what the answer is evidence
of.

**R-10.** **The preflight is the identity describing itself** [ADR-0020 R-13, ADR-0024 R-23], not
a second walk of the record. One that measured the subject separately could report a question
answerable while the comparison refuses it — one sentence bound to two instruments, which this
project has already paid for once, green on both while they disagreed by a hundred.

**R-11.** **One builder, two renderings** [ADR-0017], and **the provenance of every field travels
with it** into both. A consumer that cannot tell a recorded digest from one recomputed on the
reading machine cannot tell what the answer is evidence of, and that is a property of the data
rather than of the prose beside it.

## Alternatives considered

**A comparison function taking two records.** Rejected: `--here` has no record, a published
identity string has no record, a merged group has many, and ADR-0023's rebuild compares an
environment it has just created. Four of the five sources are not records, so a record-shaped
signature would have been replaced on the first addition.

**Three digests — packages, packages plus interpreter, and everything.** Rejected under ADR-0024
R-1: the middle one is a product of two independent facts and so cannot say which moved.

**Excluding `runprov` from the comparison by default.** Rejected under R-6: it is in that
environment and the digest says so. The repair for a false alarm is to name it, not to hide it.

**Inferring an operand's kind from its shape.** Rejected under R-5. A path that looks like a run
id, or a run id that looks like a digest, would be resolved silently and compared confidently.

**A separate preflight command.** Rejected under R-10. Two instruments over one subject is the
failure this project measured on its own claim counts.
