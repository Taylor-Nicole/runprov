# 16. A history shows whether it has been edited

**Status:** Accepted — T-32, built 2026-09-17.

## Context

`runprov verify` answers *does this artifact still follow from the inputs it names*. It answers
it from the artifact's own pin, which is why it works with the package uninstalled. What no
command answers is the question one level up: **has the run history itself been edited since it
was written?**

Today, nothing. The history is append-only JSONL and every reader trusts it line by line.
Change line 40 — a parameter, a digest, a status — and `log`, `show`, `diff`, `impact` and
`report` all repeat the new value with no sign anything moved. Delete a line and the run it
described never happened. Reorder two and the lineage changes shape.

That is the difference between *we keep a history* and *the history is evidence*, and it is the
question an assessor asks first in an accredited setting: not "do you record this" but "could
the record have been changed afterwards, and would you know?"

**This is not a hypothetical threat model imported from security work.** The realistic case is
mundane: a results file is queried, a number looks wrong, someone opens the history in an editor
to "fix" a typo in a parameter, and the record now describes a run that never happened. Nothing
in the package notices, and the person who did it may not remember a year later.

## What this is, and what it is not

**Tamper-EVIDENT. Not tamper-proof, and the distinction is the whole honesty of the feature.**

The digest of each line is public, and anyone who can write the file can compute it. So an
attacker who can edit the history can also **append** a well-formed forged line, or rewrite the
file from a chosen point and re-chain everything after it. What the chain detects is
**retroactive editing of an existing history by someone who does not re-chain** — which is the
mundane case above, and is what "would you know?" actually asks.

Claiming more would need a key, an external timestamp, or an append-only store. Each is a real
option and each costs a dependency, an operational burden, or a service; none is justified by
the threat that exists. **What this must never say is "the history cannot be altered."** It says
"an alteration that did not rebuild the chain is visible, and visible without this software."

## Decision

**R-1.** Every history line carries **`prev`**: the **sha256 of the preceding line's exact
bytes**, excluding its trailing newline. Hex, lower case, full 64 characters.

**R-2.** The bytes hashed are **the line as written**, not a re-serialisation of it. No
canonical form, no key reordering, no re-encoding. A verifier hashes what it reads, so the
chain cannot be broken by a formatting change in a later version of this package.

**R-3.** The **first line of a file** carries `prev: "GENESIS"` — a sentinel, not a digest, and
not `null`. Three states must stay distinguishable: **absent** (written before this feature
existed), **`GENESIS`** (the first line of a chain), and a **digest** (chained).

**R-4.** Every line chains to its predecessor **whether or not that predecessor is chained.**
A history that existed before the upgrade gets its next appended line pointing at the last
pre-upgrade line, so the chain anchors the old content immediately rather than starting a fresh
island. This is the whole difference between a feature that protects existing histories and one
that protects only future ones.

**R-5.** Once a file contains a chained line, **every subsequent line must carry `prev`.** A
line with no `prev` after one that has it is a break, not an exemption — it is what splicing an
old-format line in would look like.

**R-6.** `prev` is computed and written **inside the same exclusive lock that performs the
append.** Reading the previous line outside the lock is a race in which two concurrent runs
both chain to the same predecessor and the second silently orphans the first.

**R-7.** The sink's existing torn-line repair runs **before** the predecessor is read. A process
killed mid-append leaves a fragment; the sink already closes it with a newline so it becomes its
own unreadable line. The chain must then treat that fragment as the predecessor it actually is,
so a crash produces **one** broken link at a known place rather than a silent re-anchoring.

## Verification

**R-8.** **`runprov chain [LOG]`** reports the chain and uses the existing exit-code contract
without extending it: **0** intact, **1** broken, **2** could not check — no history, unreadable,
or no chained line in it.

> **Amended 2026-09-17, during the build, before any code was written.** This requirement first
> said `runprov verify --history`. Implementing it surfaced a contract it would have broken:
> `_verify`'s own docstring states that it *deliberately never touches the history* — "the pin
> is in the artifact, which is the whole point of putting it there: a committed result can be
> checked by someone who has the repository and nothing else" — and `--log` is already accepted
> there and ignored, with a printed note explaining why. Adding `--history` would have made one
> command's exit code answer two different questions, which is the objection ADR-0018 R-10
> raises against folding the policy gate into `check`, and this package has given every distinct
> question its own subcommand. Recorded rather than quietly changed, because a specification
> that is edited to match the code is not a specification.

**R-9.** The report states **what it checked, not only what it found**: how many lines were
examined, from which line the chain begins, and how many predate it. "Intact" over a file whose
chain covers three of nine hundred lines is the vacuous pass this project has fixed in five
places.

**R-10.** A break names **the line number and both digests** — claimed and computed — and says
that **the line before it is the one that changed**, because that is the counter-intuitive part
and a reader will otherwise inspect the wrong line.

**R-11.** An **unreadable or torn line** is reported as `CANNOT_CHECK` for that link, never as
tampering. Corruption and editing are different findings and a package that confuses them will
be disbelieved the first time a disk goes bad.

**R-12.** **The chain is verifiable with `sha256sum` alone**, with runprov uninstalled, and the
ADR carries the recipe. Measured before this was written: a nine-line `sh` loop using
`sha256sum` and `sed` detects an edit to line 3 by reporting line 4 broken.

```sh
n=0; prev="GENESIS"
while IFS= read -r line; do
  n=$((n+1))
  claimed=$(printf '%s' "$line" | sed -n 's/.*"prev": *"\([^"]*\)".*/\1/p')
  [ "$claimed" = "$prev" ] || echo "line $n breaks the chain (line $((n-1)) changed)"
  prev=$(printf '%s' "$line" | sha256sum | cut -d' ' -f1)
done < history.jsonl
```

This requirement is why R-2 forbids canonicalisation and why `prev` is a plain top-level
string. A chain that needed this package to check it would contradict the claim the README
leads with, and would be worth less than no chain at all — because it would be believed.

## Legitimate discontinuities

**R-13.** **Nothing in this package removes a history line, so no rebuild mechanism exists.**

> **Corrected 2026-09-17, during the build, and the original requirement was written on a false
> premise.** It said `prune` removes lines and must therefore append a rebuild marker and
> re-chain. It does not: `prune` unlinks in-flight MARKER FILES inside a `.incomplete`
> directory, and `prune.py`'s own docstring states in capitals that the history is **what it is
> not allowed to touch** — "`runs.jsonl` is the append-only record and is what makes an
> interruption permanent". Verified in the code before this was written.
>
> So the requirement is inverted rather than dropped: **the chain has no legitimate
> discontinuity, and any break is a finding.** That is a stronger guarantee than the one first
> specified, and it is free. Should a future command ever need to remove a line, the rebuild
> marker described here is the design — but it is not built for a caller that does not exist,
> and a mechanism for laundering a history is not one to build speculatively.
>
> Recorded rather than quietly deleted: a specification edited to match the code is not a
> specification, and the fact that a requirement was wrong is more useful to the next reader
> than the tidy version would be.

**R-14.** Rotation needs no support. A new file starts at `GENESIS`; the old file stays
verifiable on its own. A chain spanning files would need an index, which is a store, which is
the thing this package does not build.

## What must not change

**R-15.** `prev` is **excluded from every comparison.** It differs between any two lines by
construction, so `diff` would report a change on every pair ever compared — A-07's
gate-that-cannot-pass, arriving through a field added for a different purpose. Stated as a
requirement because it is exactly the kind of interaction a later audit finds.

**R-16.** `prev` does **not** appear in `export`'s RO-Crate or PROV-JSON output. Those are
provenance vocabularies; this is a property of one file's storage, and emitting it would assert
something in a standard vocabulary that the standard does not mean.

**R-17.** `prev` does **not** appear in an artifact pin, for the same reason ADR-0013 keeps
`resources` out of it: the pin answers whether a result follows from its inputs, and the
integrity of a log file is not part of that question.

**R-18.** A record read from a **sidecar** is unaffected. Sidecars stand alone and have no
predecessor; the chain is a property of the history file only.

## Alternatives considered

**Sign each line.** Strongest, and rejected: it needs a key, key management is an operational
burden this package deliberately has none of, and the only implementation without a dependency
would be a hand-rolled one — which is worse than no signature.

**An external timestamp authority or a public ledger.** Would defeat re-chaining, which is the
one attack the chain does not stop. Rejected: it requires a network service, and the package's
claim is that the record is readable and checkable with nothing installed.

**A Merkle tree over the file.** More efficient for proving one line's membership, and this
does not need that: verification is a linear read of a file that is already read linearly.
Complexity with no purchaser.

**Hash a canonical serialisation rather than the bytes.** Would let the chain survive a
reformat. Rejected under R-2 and R-12: it makes shell verification impossible, and "survives a
reformat" is not a property wanted from a file whose whole claim is that its bytes are evidence.

**Do nothing, and say the history is append-only.** The honest reading of today's state. It is
what this ADR replaces, because "append-only" describes how the package writes the file and not
how anyone else can.
