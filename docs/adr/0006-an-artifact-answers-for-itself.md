# 6. An artifact answers for itself

Date: 2026-09-04 · Status: accepted · Ledger: T-17 · Decided by the author, not the applier

## Context

`verify` reads the pin inside an artifact and re-hashes the inputs that pin names. It has
always been able to say *"the things this file was made from still hash the way they did"*.
It has never been able to say *"this file is what it was written as"* — and it said so, in
its own output, every single time it printed a passing result:

    # OK = the inputs each artifact pins still hash the same. It does NOT mean the
    # artifact itself is unedited — for that, `runprov show --stale --rehash`.

That caveat was honest and it was an apology for a gap. A test existed whose premise was the
gap: it appended a fabricated row to an artifact, asserted `verify` still exited **0**, and
called that *"the trap"*. The advice it offered — use `show --stale --rehash` — needs the run
history, which is exactly what is absent in the case the pin exists for: a file somebody
emailed you.

So the tool's distinctive claim, that an artifact travels with its own provenance, stopped
one question short of the one a reader actually asks about a results file.

**Why it was not simply done.** The pin goes into the artifact's FIRST bytes. At the moment
it is written the body does not exist, so its digest cannot be in it. That is a real ordering
problem, not an oversight.

## Decision

**`open_output` publishes the artifact once, complete, with a digest of its own body in the
pin.**

The body streams into a temporary file beside the destination and is hashed as it goes, with
the pin's `body` field standing at a fixed-width placeholder. On close the sixteen characters
are patched **in the temporary file**, and one `os.replace` publishes it. The artifact is
therefore created once, already correct, and never exists in a state where its own pin is
wrong — ADR-0005's rule, applied to the artifact instead of to the record.

`verify` gains a state, **ALTERED**, and it is a state of its own rather than a flavour of
STALE because the two are opposite repairs:

| | what happened | what you do |
|---|---|---|
| **STALE** | an input moved | rebuild the artifact |
| **ALTERED** | somebody edited the artifact | find out who, and why — **do not rebuild** |

Reporting an altered artifact as stale would send a person to re-run a pipeline over a file
somebody had hand-corrected, and destroy the correction.

**Three answers, not two.** An artifact with no `body` field — everything written before this
existed, and everything produced by any other means — reports neither OK-because-checked nor
ALTERED. It is not a finding. Condemning the entire existing corpus on the day this shipped
would have been the loudest possible false positive, so the report also carries **how many
artifacts could be asked at all**: `0 ALTERED` over files that carry no digest says nothing
and looks exactly like `nothing was tampered with`.

**Raw SHA-256 of the body's bytes, deliberately not `content_digest`.** The two answer
different questions. `content_digest` asks *"is this the same DATA?"* and ignores a
line-ending change, which is right for deciding whether an input moved. This asks *"has
anyone touched this FILE?"*, where a line-ending change is a touch.

## Consequences

* **The artifact does not exist at its final path until the handle is closed.** This is the
  cost, and it is the one thing a user can notice. A script that writes an artifact and then
  reads it back *inside the same `with` block* will not find it. After the block — the
  documented shape — nothing changes.
* **A caller who forgets `close()` would have lost the artifact entirely**, since the body
  lives in a temporary file until the rename. `_seal` therefore publishes anything still
  open. Forgetting costs the pin's accuracy at worst, never the file.
* `verify` now reads each pinned artifact in full rather than only its first bytes. Streamed
  from the offset the pin block ended at, so a large artifact costs a read and not a copy.
* `Run.header` takes an optional `body_digest`. Additive; the promised surface is unchanged.
* **The trap test was rewritten to assert the opposite.** Same fixture, same tampering,
  opposite verdict — which is the most useful form the record of a closed gap can take.

## Alternatives considered

**Patch the digest into the artifact after writing it.** Simplest, and it breaks ADR-0005 for
the artifact: the file would exist for the whole write carrying a pin that is wrong, and a
crash in the patch window would leave it that way for ever.

**Append the digest as a trailer.** No seek and no offsets to get wrong, but it puts the
provenance in two places in the file, and a format that tolerates a leading comment does not
always tolerate a trailing one.

**Keep the digest in the sidecar only.** Preserves today's contract exactly and gives up the
entire point: `verify` could then answer only when the sidecar travelled with the file, which
is the case the in-band pin exists because it cannot rely on.

**Write the body to a temporary file and copy it under a finished header.** The obvious
implementation, and measured worse for nothing: 0.76 s against 0.45 s for a 210 MB artifact —
**1.7×** — since the patch is sixteen bytes either way.
