# 27. An environment in a standard vocabulary says only what the record supports

- **Status:** Accepted — built 2026-10-23 in `runprov/export.py`. Sized to one shipped capability
  per FEATURE_WORKFLOW's rule, and separate from ADR-0024 and ADR-0026 because the risk here is a
  different one: **overclaiming in a vocabulary somebody else defined**, which `export.py`'s own
  docstring calls the failure that *"would be harder to catch, because it would be well-formed."*
- **Applies to:** `runprov/export.py`

## Context

**Measured 2026-10-23: `export.py` mentions the environment nowhere.** Both renderers carry files,
runs, scripts and the relations between them, and neither carries a single fact about what the run
ran in. So a record deposited at Zenodo or handed to a reviewer through either standard vocabulary
says what was read and written and **nothing about whether the environment can be checked** —
which is the question ADR-0024 exists to make answerable and the one an external tool is best
placed to ask.

This was scheduled as *"nearly free — put the digest into export"*. It is not: there is no
environment there to add a digest to, and neither RO-Crate 1.1 nor PROV has an environment
concept. **The work is the mapping, and the mapping is where a well-formed lie gets made.**

## Decision

**R-1.** **The environment appears in BOTH vocabularies or in neither.** Two derived views of one
record that disagree about what was recorded are worse than one view: a reader comparing them
cannot tell which is incomplete, and the obvious conclusion — that the record itself differs — is
false.

**R-2.** **In RO-Crate it is a `PropertyValue`, referenced through `additionalProperty`.** There is
no environment type in RO-Crate 1.1, and **inventing one is the well-formed lie**: a validator
would accept it and a consumer would read a type nobody defined. `additionalProperty` is a
property of `Thing`, so every entity already has it, and `PropertyValue` with `propertyID` set to
the algorithm's name is the standard idiom for a measured value the vocabulary does not model.

**R-3.** **In PROV it is an `entity` related by `prov:used`.** PROV defines `used` as the beginning
of utilizing an entity by an activity, and a run utilizes the environment it ran in. The narrower
gloss in `export.py`'s docstring — *"`used` (a run read a file)"* — was true of the only entities
that existed when it was written, and it is **widened in the same commit** rather than worked
around, because a docstring that no longer describes the function is the defect this project has
measured in its own instruments four times.

**R-4.** **DEDUPLICATED BY DIGEST, and that is a feature rather than a saving.** Two runs in one
environment share one node, so the export shows **which runs shared an environment** — the
capture-once-per-distinct-environment property, legible to an outside tool for the first time. In
RO-Crate a repeated `@id` is invalid rather than redundant, so deduplication is also required
there.

**R-5.** **The package LIST is not exported.** It lives in the content-addressed sidecar, not in
the record, and **`export` is pure over records** — `_files` builds from records and opens nothing.
A crate carrying five hundred package names would duplicate a file the export does not ship and
would make the answer depend on what is still on disk.

**R-6.** **A record written before the digest existed exports no environment, and nothing is
recomputed.** This is the same split `diff` holds: *recorded* is what the run asserted and
*recomputable* is what a file still says, and a derived view that recomputed would answer
differently depending on the machine it was rendered on. Nine released versions of records are in
this state and the honest export of them is silence, not a value invented at render time.

**R-7.** **The interpreter is not folded into the package-set node.** It is carried as a separate
property of the run. Two independent facts in one node cannot say which of them moved — ADR-0024
R-2, one vocabulary further out — and a consumer comparing two crates would see one value change
for two reasons.

**R-8.** **Nothing asserts that the environment caused the outputs.** PROV allows the finer claim
and this record cannot support it: it holds *this run ran in this environment*, never *this output
came from that package*. The same limit `wasDerivedFrom` already carries here, and for the same
reason — saying more in a standard vocabulary than the record supports is the failure this whole
document exists to refuse.

## Alternatives considered

**Invent a `SoftwareEnvironment` type.** Rejected under R-2. It would validate, it would look
authoritative, and no consumer could resolve it. **A vocabulary's whole value is that the reader
did not have to ask you what the terms mean.**

**Reuse schema.org `softwareRequirements` on the script's `SoftwareApplication`.** Rejected: that
property means *what this software requires*, and the record holds *what was installed when it
ran*. A reader acting on it would install a declared requirement set that was never asserted.

**Attach the digest with `sha256`, as files carry.** Rejected: `sha256` on a crate entity is a
promise a validator checks against bytes on disk, and there are no bytes here — the package-set
digest is over a canonical rendering, not over a file. The existing comment in `to_ro_crate`
refuses `content_digest` for the same class of reason.

**Export the package list as well.** Rejected under R-5.

**Recompute the digest for older records so every export carries one.** Rejected under R-6. It
would make a derived view depend on the reader's filesystem, and the uniformity would be bought by
making *recorded* and *recomputed* indistinguishable in the one artifact that leaves this machine.
