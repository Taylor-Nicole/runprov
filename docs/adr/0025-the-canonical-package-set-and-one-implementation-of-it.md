# 25. The canonical package set, and exactly one implementation of it

- **Status:** Accepted — T-36c, built 2026-10-23 as `runprov/pkgset.py`. **Split out of ADR-0024
  on the day it was built, and the gate is what forced it:** a module's top docstring naming a
  decision is a claim that the decision is implemented, so `test_every_adr_is_listed_in_the_adr_index`
  refuses a module citing a `Proposed` record — *"ADR(s) implemented in code but still marked
  proposed"*. ADR-0024 specifies twenty-one requirements across two releases; **six of them are
  built now and the rest are not**, which is the same lesson D-1′ settled for ADR-0020 and
  ADR-0023, found again one layer down and by measurement rather than by reasoning. ADR-0024 keeps
  the two-digest record model and the comparison, and cites this record for the form.
- **Applies to:** `runprov/pkgset.py`

## Context

ADR-0024 establishes *why* an environment needs a second, host-independent digest. This record is
the **form that digest is computed over, and the single module that computes it.** It is separate
because its audience is partly outside this package: a journal referee, an external auditor or a
researcher reproducing a result verifies an environment somewhere other than where it was
recorded, and what they run must not require installing anything.

## Decision

**R-1.** **The canonical text is, in order, LF-separated with a trailing newline:**

```
runprov.pkgset.v1
pip <name> <version>            # one line per pip distribution, sorted by canonical name
conda <name> <version> <build>  # one line per conda package, sorted by canonical name
```

`<name>` is **PEP 503 normalised** — runs of `-`, `_` and `.` collapsed to a single `-`, then
lowercased. The first line is a **domain separator**, so this digest can never collide with a
digest of anything else in this package and a reader holding a bare hex string can be told which
algorithm produced it. The package-set digest is the SHA-256 of that text.

**R-2.** **The parse is total, and a line it cannot classify is a REFUSAL rather than a dropped
line.** Every non-blank line of a snapshot body is exactly one of: the conda separator
(`# --- conda-meta (conda / mamba / micromamba / pixi) ---`); a conda entry, after it; a pip
entry, before it; or an **annotation**, which is any other line beginning with `#`. A line
matching none of those raises, and callers map it to exit 2 — *could not look*, never a finding.
**A canonicaliser that recovers from input it does not understand reports a digest for a smaller
environment than the one it was given, and nothing downstream can tell.**

**R-3.** **Two entries that normalise to one canonical name are a refusal, not a merge, and the
refusal names both versions.** R-1's normalisation maps `zope.interface` and `zope_interface` onto
one name, and `installed_packages()` keys its map by the raw metadata `Name`, so a body carrying
both is something capture can produce. **Measured 2026-10-23, before this module existed: keeping
the last of two colliding entries made the digest depend on line order** — one environment, two
digests — which contradicts R-1's own order-invariance. Choosing one invents an answer, and the
two versions differing is precisely why it must be refused rather than resolved.

**R-4.** **The algorithm is named `runprov.pkgset.v1`, the name is the key the digest is filed
under, and it is frozen on release.** Like `runprov.gate.v1` and `runprov.policy.v1`: a change to
a single byte of what R-1 produces is `runprov.pkgset.v2` under its own key, never a redefinition
of this one. **A digest quoted in a paper cannot be recalled**, and it can only still be checked
years later if the paper and the record both say which algorithm they mean. The freeze is held by
nine committed digests — one per released corpus version — so a change moves nine rows at once
and names itself.

**R-5.** **The interpreter is not in this digest.** It is recorded in its own field and compared
on its own. A digest covering the package set *and* the interpreter could not say which of them
moved, which is the defect ADR-0024 exists to repair; committing it again at a smaller scale would
be the same mistake with a quieter failure.

**R-6.** **There is exactly ONE implementation, it ships in the distribution, and it imports
nothing but the standard library.** `runprov/pkgset.py` can be copied out of an installed runprov
or an unpacked sdist and run in a container with no runprov in it, and the rest of the package
imports that module rather than carrying a second copy. **Two copies of one rule differ by whole
cases rather than by details**, and a canonical form is the kind of rule that cannot afford it.
The property is **guarded, not intended**: one check derives the module's intra-package import
edges with the same resolver the layering guard uses and requires none, a second asks the
interpreter whether every remaining import is stdlib, and a third copies the file to a temporary
directory and runs it under `-I` with the repository nowhere on the path. The static halves cannot
see a package-level side effect; the subprocess can.

## Alternatives considered

**Keep the reimplementation written from ADR-0024's text as a second implementation.** Rejected
under R-6. It was written to measure one thing — *is the specification sufficient to rebuild
from?* — and it answered yes, reproducing all nine corpus digests with no runprov and no network.
**That is evidence about the specification, not a licence to maintain two canonicalisers**, and
the project's own measured history is that two halves of one rule drift by whole cases.

**Ship it under `docs/` instead of in the package.** Rejected on Taylor's objection, 2026-10-23:
*"if it's not shipped inside of runprov, no one will see it or use it as a feature."* Correct — a
file under `docs/` is not a feature and nobody runs it. The drift worry that motivated that
placement is answered by there being one copy, not by hiding it.

**Hash the snapshot body's text directly.** Rejected: the body carries the host's platform string
and the interpreter version, which is exactly what makes the existing digest unable to answer this
question (ADR-0024's *Context*), and it would make line order and name spelling significant.

**Normalise versions as well as names** — PEP 440 canonical forms, so `1.0` and `1.0.0` agree.
Rejected for v1: a version string is what the environment reports about itself, two spellings are
a real difference in what is installed, and normalising them would make this digest assert an
equivalence the package cannot verify. Revisit as `v2` with evidence, never silently.

## What this does not claim

**Equal package sets are not equal environments.** This covers names, versions and conda builds.
It does not cover the resolved transitive graph, a wheel rebuilt under the same version, system
libraries, or anything outside Python — which is why ADR-0023 keeps rank 1, the lock file, above
it. **It answers *are these the same packages* and nothing more.**
