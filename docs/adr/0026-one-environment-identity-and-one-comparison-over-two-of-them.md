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

**R-12.** **The named view the comparison reads must carry everything the canonical form carries,
and a residual disagreement between the digest and the named view is a REFUSAL.** The package-set
digest is taken over `pip <name> <version>` and `conda <name> <version> <build>` (ADR-0025 R-1),
so a comparison whose named view is a name-to-version map is **lossy exactly where conda is** —
and then two sources disagree about one environment for a reason neither of them is wrong about.
*The digests agree* and *the named sets agree* must be the same question. Where they are not, the
comparison reports exit 2 and names the contradiction: this is R-8's rule, which held for one
identity against its own sidecar and did not hold across two.

**MEASURED, 2026-10-24, and this requirement exists because the first repair was wrong.**
`samtools=1.21=h50ea8bc_0` against `samtools=1.21=hAAAAAAA_9` is two digests over one map. The
shipped behaviour decided *same* from the digests and built the difference list from the maps, so
the list won silently: **exit 0, nothing named, nothing blocked** — a real difference reported as
agreement. Refusing the disagreement outright, the obvious reading of R-8, then accused that
ordinary pair of being corrupt. **Restoring the build is the repair; refusing is what is left.**

**IT WAS WRITTEN HERE THAT NO SHIPPED SOURCE COULD REACH THE REFUSAL, AND THAT WAS MEASURED FALSE
ON 2026-10-24** (`skeptic-3`, open). The argument — R-8 covers `from_record`, `from_here` takes
both halves from one rendered body, and an identity string names no packages — is true of each
operand on its own, and the contradiction this refuses is **between two of them**. Two ordinary
records reach it: a conda build string differing by a trailing space gives **agreeing** maps,
because `_comparable` rstrips, and **differing** digests, because `canonical_text` does not. Each
operand is self-consistent; the pair is not.

No replacement unreachability claim is made. The same sentence stood in three places — here, in
`env.py` and in the test — and a reader reaches for it exactly when deciding whether it still
applies. What the refusal holds is the invariant **between two fields of a constructible
identity**, including the further operand source R-4's framing leaves room for; the failure mode
it covers is a silent exit 0, which is the one this feature cannot afford.

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
