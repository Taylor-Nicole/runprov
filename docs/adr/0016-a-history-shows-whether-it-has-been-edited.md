# 16. A history shows whether it has been edited

**Status:** Accepted — T-32, built 2026-09-17, **substantially amended 2026-09-18 after Audit E**.

> **WHAT AUDIT E CHANGED, AND WHY IT IS AN AMENDMENT RATHER THAN A BUGFIX.** The first build
> satisfied all eighteen requirements below, reached 100 % branch coverage, and passed 17 of 17
> mutations derived from its own diff. Five read-only reviewers then found sixteen defects, and
> the important ones were not implementation errors — **R-5 was unimplementable as written**,
> and R-9, R-10 and R-11 specified behaviour that is wrong once you look at what the file can
> actually contain. Mutation testing proves the code does what the specification says; only a
> reader can tell you the specification is wrong.
>
> **The root cause of half of it was one serialisation choice nobody had questioned:** `prev`
> was written LAST in the line — measured at 46 % of the way in, with 75 bytes of digest
> trailing it — so **the one field whose job is to survive truncation sat where a tear destroys
> it first.** That made two defects look like an unavoidable trade between "a torn line must
> never accuse" and "a torn line must not conceal". It is not a trade; it is R-19 below.

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

**R-5.** Once a file contains a chained line, a later line with no `prev` is judged **by the
version that wrote it**, which the record already states:

* `tool.version` names a release from **before** the chain existed → a **coverage gap**, not a
  break. The report names the version and the line count, so the finding is actionable: *"7
  lines written by runprov 0.5.0, which cannot chain — upgrade that machine to close the gap."*
* `tool.version` names a **chain-capable** release, or there is no `tool` block at all after the
  chain began → a **break**. A current version that wrote no `prev` is exactly the splice this
  requirement exists for.

> **Amended 2026-09-18 after Audit E (E-02), and the original was unimplementable.** It said a
> line with no `prev` is a break because that is "what splicing an old-format line in would look
> like". It is also exactly what **a colleague running a supported release** looks like — 0.1.0
> through 0.5.0 are on PyPI, none of them writes `prev`, and from the file alone the two are
> byte-identical. Reproduced with a real 0.5.0 wheel: one ordinary run by one colleague marked
> the history tampered **permanently**, since R-13 gives nothing the power to clear it.
>
> Taylor's decision (2026-09-18) was to keep it strict — *"otherwise people will never update"* —
> and the first proposal for making that survivable was a human acknowledgement line. Taylor
> rejected it on the right grounds: *"most people will just open it and say okay without really
> verifying."* An acknowledgement with no evidence behind it is a button that makes red go away,
> which is a laundering mechanism with extra steps.
>
> The evidence a human would have been asked for is **already in the record**. `tool.version`
> has been written into every history line since 0.3.0 (U-01), so the verifier can attribute an
> unchained line without asking anyone. Nobody clicks anything, the message names the machine to
> fix, and the gap closes by itself when it is fixed.
>
> **The residual, stated rather than hidden:** a forger can copy a `tool` block naming an old
> release. That is acceptable and is what tamper-evidence means — the claim is then ON the
> record, dated and specific, and an assessor who sees a 0.5.0 line in a project that upgraded in
> September has a question to ask. What is not acceptable is a mechanism that cannot tell the
> difference at all, which is what shipped.

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
> command's exit code answer two different questions, which is the objection ADR-0018 R-9
> raises against folding the policy gate into `check`, and this package has given every distinct
> question its own subcommand. Recorded rather than quietly changed, because a specification
> that is edited to match the code is not a specification.


**The verdict is decided in this order: BROKEN dominates (1); otherwise CANNOT_CHECK (2) if
nothing is chained OR any line is unattested; otherwise INTACT (0).**

> **Amended 2026-09-18 after Audit E (E-05, E-10).** The first version consulted only `broken`,
> so a file in which ONE link of six could be checked printed `INTACT` and exited 0 — the
> vacuous pass, delivered as a green gate, in the command built to detect exactly that. Edit
> line 5, truncate line 6, and the accusation vanishes: the attack needs a text editor and no
> hashing at all, which makes it **strictly easier** than the re-chaining this ADR already
> concedes.
>
> `--format json` must carry the same verdict. Three mutations of that payload — forcing
> `"status": "INTACT"`, reporting `lines` as `links_checked`, and deleting the coverage keys —
> all survived the suite, because its only test used a clean two-line history where every field
> equals its correct value numerically.
**R-9.** The report states **what it checked, not only what it found**: how many lines were
examined, from which line the chain begins, and how many predate it. "Intact" over a file whose
chain covers three of nine hundred lines is the vacuous pass this project has fixed in five
places.


**The count is of ATTESTED LINES — verified hash-edges — never of comparisons performed**, and
it can never equal the number of lines.

> **Amended 2026-09-18 after Audit E (E-04, E-09).** The first version counted comparisons, which
> is wrong by one in every history born after this feature: line 1's comparison is against the
> `GENESIS` sentinel, which attests no bytes at all — any forger writes `"prev": "GENESIS"` for
> free. Verified by editing each line of a six-line history in turn: lines 1 and 6 edit
> undetected, lines 2-5 break. Six comparisons, five attested lines, and the report said six of
> six. Printing `N of N` asserts coverage the mechanism cannot have — R-9's own sentence
> producing the failure R-9 exists to prevent. The arithmetic could also go NEGATIVE
> (`INTACT: -7 link(s) checked of 10 line(s)`) because it subtracted unreadable lines from
> before the chain began.
**R-10.** A break names **the line number and both digests** — claimed and computed — and says
that **the line before it is the one that changed**, because that is the counter-intuitive part
and a reader will otherwise inspect the wrong line.


**Every clause of that sentence must be true of the break being reported**, and where one is
not, the message says what actually happened instead.

> **Amended 2026-09-18 after Audit E (E-07).** The first version formatted one sentence for every
> break and so produced three false statements: **"LINE 0 IS WHAT CHANGED"** when the first line
> was deleted, naming a line that does not exist; **"line 0 hashes to GENESIS"**, presenting the
> sentinel as a digest; and **"LINE N-1 IS WHAT CHANGED"** for a line that made no claim at all,
> where R-10's own reasoning — "N's claim is a statement about its predecessor" — does not hold
> because there is no claim. A diagnostic that sends a reader to an untouched line costs them
> the time and then their confidence in the answer, which is what R-10 exists to buy.
**R-11.** An **unreadable or torn line** is reported as `CANNOT_CHECK` for that link, never as
tampering. Corruption and editing are different findings and a package that confuses them will
be disbelieved the first time a disk goes bad.


**And it must not ABSOLVE either.** An unreadable line at position *i* destroys the only
statement anything makes about line *i-1*, so line *i-1* becomes **unattested** — reported as
such, and decisive under R-8.

> **Amended 2026-09-18 after Audit E (E-05, E-06). The code violated this in BOTH directions at
> once.** A mid-file torn line was reported `COULD NOT CHECK` *and* accused — "LINE 3 IS WHAT
> CHANGED… the history was edited after it was written" — because the walk compared against the
> previous line's bytes without asking whether that predecessor was readable. A bad disk sector
> accused the user. Meanwhile an end-of-file torn line concealed an edit to its predecessor
> entirely, reporting `INTACT` and exit 0. **The two errors cancel in the only case the test
> suite exercised**, which is why 17 of 17 mutations passed over them.
>
> An earlier proposal distinguished corruption at the END of a file from corruption in the
> MIDDLE. **That distinction does not hold**, killed by measurement rather than argument: an
> honest crash file permits a silent edit of the fragment's predecessor. An unreadable line
> costs exactly one thing wherever it sits. Position is not the axis — whether the unreadable
> line's `prev` survived is, which is what R-19 and R-20 make possible.
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


## Added after Audit E

**R-19.** **`prev` is the FIRST field of the serialised line.** Not the last, which is where it
was: measured at 46 % of the way into the line with 75 bytes of digest trailing it, so a
truncation destroyed the field whose entire purpose is to survive one. R-2 is untouched — the
bytes are still hashed as written — and R-12's `sed` recipe is untouched.

This single change is what makes R-11's two halves compatible instead of a trade. With `prev`
first, a torn line usually still carries an intact, verifiable claim about its predecessor: the
crash costs nothing, and an edit hidden behind a deliberate truncation is *upgraded to a
detection*, because the surviving `prev` disagrees with the edited line.

**R-20.** When a line cannot be parsed as JSON, the reader makes **one anchored attempt** on its
raw bytes — `^\{"prev": "(GENESIS|[0-9a-f]{64})"` — and uses the claim if it matches. Anchored
and exact-shape, and only for lines the parser has already refused, so garbage cannot match.
This is not a second parser: it reads one fixed prefix that R-19 guarantees the position of.

**R-21.** A history whose lines end **CRLF** is reported as `CANNOT_CHECK` **naming the cause**,
never as tampering.

> Audit E (E-01). Reproduced with a real `git clone --config core.autocrlf=true` — git's Windows
> default — on a history committed with LF: **every line from the second onward reported BROKEN**,
> each naming a specific innocent line. `README.md` recommends tracking `provenance/` in git, and
> this project's own `.gitattributes` protects `tests/fixtures/**` and `tests/corpus/**` and
> nothing a user would have. The bytes genuinely did change, so `INTACT` would be false; what is
> false is calling it tampering. The message names the cause and the fix (`-text`), which turns
> the maximal false accusation into one actionable sentence.

**R-22.** When the append could not take an exclusive lock, the line is written **with no chain
claim at all** rather than with one that may be wrong.

> Audit E (E-03). `_exclusive` documents a no-lock fallback for NFS, CIFS and containers, and
> its own measurement records that 8 processes × 20 appends produced 160/160 intact records
> there. Chaining under that fallback lets two writers claim the same predecessor, so the file
> is complete and correct and the chain calls it tampered — reproduced with `ENOLCK`: 80/80
> records present, 4 false accusations naming untouched lines. **A degraded mode that used to
> lose nothing must not be promoted into one that manufactures a verdict.**
>
> **AMENDED after Audit G (G-01).** This clause used to end *"an honest absence of claim is
> reported under R-5 as a coverage gap"*, and that was false — and **unreachable by
> construction**. R-5 fires only on `writer = PRE_CHAIN`, which means a version below
> `CHAINS_FROM`; the release that writes the unlocked line is at or above it, so it resolves
> `CAPABLE` and rule 7 called it a splice. Measured: three ordinary locked appends followed by
> three unlocked ones gave **BROKEN, three untouched lines accused**, every record present and
> every byte as written. The honest absence is now reported under rule 7 as `UNCLAIMED` — see
> R-25, and R-32 for why that is not an accusation.

**R-23.** The report states that **the newest line is not yet attested**, and that **truncation
of the tail cannot be detected from this file alone.**

> Audit E (E-04). Line N's bytes are attested only by line N+1, so the last line is attested by
> nothing — and after the next append the chain actively **certifies** a tail forgery rather than
> merely failing to notice it. Both are genuinely undetectable from one file: a line cannot
> contain its own digest, and nothing in a file says how long it used to be. A head-anchor
> sibling file and a sidecar anchor were both examined and rejected with measurements — the
> sidecar is written *before* its own history line, so it cannot attest the newest one and closes
> nothing. What is fixable is the report, and this project's stated character is to say what it
> cannot tell you rather than to guess.
>
> The CHANGELOG and `README-pypi.md` sentence "an edit, a **deletion** or a reordering breaks
> every link after it" is corrected with it: for a tail deletion there are no links after it, so
> the claim is vacuously true and reads as a detection.

**R-24.** A history line that is valid JSON but **not an object** is `CANNOT_CHECK`, never an
exception.

> Audit E (E-11). `json.loads(b"[1, 2, 3]")` succeeds and has no `.get`. Every fixture for R-11
> fed *syntactically invalid* JSON, so the half of the guard that handles "or not an object" was
> asserted by nothing and a mutation removing it survived. `verify`'s own docstring promises it
> never raises, and a verifier that raises is one nobody runs twice.


---

# The decision table

**Added 2026-09-18, after Audit F, and before any code is written against it. This section is
the specification of the reader; everything above it specifies the writer.**

## Why this section exists

Two full repair rounds produced 26 defects. They are not scattered: **15 of the 26 are in one
half of the feature** — turning a file's partial evidence into one of three words. The other
half (compute a digest, put it first, make no claim under no lock) has been stable since the
second day.

That half has a state space nobody holds in their head, and I tried to three times. Each time
the mutation pass came back complete — 17 of 17, then 16 of 16 — because **mutation testing
proves the code does what the design says, and the design was an incomplete enumeration.**

## The reframing that makes it tractable

The defects have one root: **I was computing a status per LINE, and a line carries two
different facts that I kept conflating** —

* is *this line's claim about its predecessor* verifiable and correct?
* are *this line's own bytes* attested by its successor?

Those are different questions with different answers, and E-05, E-06, F-01 and F-04 are all
what happens when they share a variable.

**So the unit of judgement is the EDGE, not the line.** Every adjacent pair `(n-1, n)` is one
edge, carrying line *n*'s claim about line *n-1*. An edge has exactly one status; a line's bytes
are attested if and only if the edge above it HOLDS; and the file's verdict is a fold over
edges. That is the whole model, and it is small enough to enumerate.

## The enumerated inputs

| dimension | values |
|---|---|
| `claim` | `NONE` · `GENESIS` · `DIGEST` — what line *n* says, after R-20's raw-bytes recovery |
| `predecessor` | `NONE_FIRST` · `READABLE` · `UNREADABLE` — the state of line *n-1* |
| `agreement` | `NA` · `MATCHES` · `DIFFERS` — the claim against `digest(line n-1 AS IT SITS)` |
| `writer` | `PRE_CHAIN` · `CAPABLE` · `UNSTATED` · `UNREADABLE` — who wrote line *n* |
| `started` | `NO` · `YES` — had any earlier line carried a claim? |

**216 combinations, of which only some are reachable** — and the reachable set is COMPUTED by
the test from `verify`'s own construction, never written down here.

> **CORRECTED after Audit G (G-14).** This paragraph used to read *"133 are structurally
> impossible and 83 require a written verdict"* and claimed the impossibility of each was
> asserted. **Nothing asserted it, and the number was wrong by 31.** Derived three independent
> ways that agree exactly — an instrumented `classify` driven by `verify`, an analytic
> derivation from the walk's constraints, and a third re-derivation during adjudication — the
> true split is **52 reachable / 164 impossible**. A repairer implementing R-30 against the old
> number would have edited the code until 83 cells were reachable. The lesson is not a better
> number: **52 is a property of the WALK, not of the table**, and it moves whenever
> `chained_from` does, so it must be computed where it is used. A cell assumed impossible is
how F-07 shipped. An
impossible cell must be *demonstrated* impossible, never assumed — assuming is what produced
F-07, where R-5's no-`tool` arm had no fixture because every fixture happened to carry one.

## The rules, in precedence order

**R-25.** The edge status is decided by the FIRST rule that applies:

| # | condition | edge status | why |
|---|---|---|---|
| 0 | `claim = GENESIS`, `predecessor ≠ NONE_FIRST` | `BROKEN` | `GENESIS` is a SENTINEL, not a digest. A line with a predecessor claiming it makes an impossible statement whatever that predecessor's state — no tear can make bytes hash to the literal string — so rule 9 must not downgrade it |
| 1 | `predecessor = NONE_FIRST`, `claim = GENESIS` | `HOLDS_TRIVIAL` | the first line has no predecessor; it attests nothing and that is not a fault |
| 2 | `predecessor = NONE_FIRST`, `claim = DIGEST` | `BROKEN` | it claims a predecessor and has none — lines removed from the front |
| 3 | `predecessor = NONE_FIRST`, `claim = NONE`, `writer = UNREADABLE` | `UNCHECKABLE` | the first line is TORN. It did not "predate the chain" — we cannot read what it said |
| 3b | `predecessor = NONE_FIRST`, `claim = NONE` | `UNCHAINED` | a file that begins before the chain existed |
| 4 | `claim = NONE`, `started = NO` | `UNCHAINED` | R-4's pre-chain prefix; unverifiable and NOT fixable, so never decisive |
| 5 | `claim = NONE`, `writer = PRE_CHAIN` | `GAP` | R-5. Unverifiable and FIXABLE — decisive, and the report names the version |
| 6 | `claim = NONE`, `writer = UNREADABLE` | `UNCHECKABLE` | line *n* is torn and R-20 recovered nothing; it made no statement we can read |
| 7 | `claim = NONE`, `writer ∈ {CAPABLE, UNSTATED}` | `UNCLAIMED` | a release that can chain wrote no claim. Detected and disclosed, **never an accusation** — R-32 |
| 8 | `predecessor = UNREADABLE`, `agreement = MATCHES` | `HOLDS` | the honest crash. R-7 repairs the fragment BEFORE the successor reads it, so the claim is over the fragment as it sits — measured, it matches exactly |
| 9 | `predecessor = UNREADABLE`, `agreement = DIFFERS` | `UNCHECKABLE` | a tear that happened AFTER the fact and an edit are indistinguishable from the file. Never `BROKEN` (R-11) and never silent (F-01) |
| 10 | `agreement = MATCHES` | `HOLDS` | |
| 11 | `agreement = DIFFERS` | `BROKEN` | |

**R-26.** The file's verdict is the worst edge, and nothing else:

    any BROKEN            -> BROKEN        exit 1
    else any UNCHECKABLE
         or any GAP
         or any UNCLAIMED -> CANNOT_CHECK  exit 2
    else                  -> INTACT        exit 0

`UNCHAINED` is disclosed and never decisive — no upgrade can retroactively chain a line already
written, and making it decisive means no project predating the feature can ever exit 0.

**R-27.** `attested` is the count of edges whose status is `HOLDS`. Not `HOLDS_TRIVIAL`, which
attests nothing. Not a subtraction from a total, which is how it came to overcount (F-04) and
to go negative (E-09) — a count of a status cannot do either.

**R-28.** **CRLF is a per-line fact, never a file-level bail-out.** A line whose terminator was
translated has had its bytes changed, so its edge is `UNCHECKABLE` and the report names the
cause. It does not suppress the judgement of other lines. The file-level early return is exactly
what let one `\r` byte discard every finding and print "not tampering" (F-03).

**R-29.** **The writer of a line is resolved from the RUN, not the line.** No released version
writes a `tool` block into a `runprov.start.v1` line — half of every history — so reading the
line alone makes every start line `UNSTATED`, which rule 7 calls a splice (F-02). Resolution
order: the line's own `tool.version`; else the `tool.version` of the completed record sharing
its `run_uid`; else `UNSTATED`. Any JSON shape must be tolerated — a `tool` that is a string,
a list or `null` resolves to `UNSTATED` and never raises (F-05).

> **Rules 0 and 3 were added on review, before any code.** Running the eleven rules as written
> over all 216 combinations showed two cells with the wrong verdict: a mid-file line claiming
> `GENESIS` behind a torn predecessor fell to rule 9 and was downgraded to `UNCHECKABLE`, which
> an attacker can trigger by tearing one line; and a torn FIRST line was reported as
> "predates the chain" — **F-06's wrong message, reproduced inside the table written to prevent
> it.** Both were found by executing the table, not by reading it, which is the whole argument
> for having one.

**R-30.** **The table is TOTAL, and a test proves it.** The test enumerates all 216 input
combinations, asserts each falls under exactly one rule of R-25, and asserts that every cell
marked impossible cannot be constructed. A cell that is reachable must have a fixture.

**R-31.** **THE ACCEPTANCE GATE: no single-byte edit may go unnoticed.** A test takes a history,
flips **every byte** in the chained region one at a time, and asserts the verdict is never
`INTACT`.

> This is not a supplement to the table; it is the check that does not depend on my having
> enumerated it correctly. Measured on the CURRENT code before this section was written: **623
> byte positions in a five-line history, 0.1 seconds, and 163 of them leave the verdict
> INTACT** — all one defect class, F-01. One assertion, no reasoning, and it finds in a tenth of
> a second what four reviewers and sixteen mutations took a day to surface.
>
> It runs over several shapes: intact, containing a torn line, containing an unlocked run,
> mixed-version, and CRLF-translated.
>
> **CORRECTED after Audit G (G-03, G-07, G-10). THIS GATE HAD THREE HAND-WRITTEN SCOPES INSIDE
> IT AND TWO WERE WRONG**, which is the scope pattern this codebase keeps finding, inside the
> one mechanism built to be immune to it. They are one repair, not three: fixing any one of
> them exposes the next, and applied separately they fight.
>
> * **Which bytes.** The loop skipped every `\n`. The only escape that existed was at a `\n`:
>   overwrite the terminator of the last attested line and two records merge into one
>   unreadable line while the verdict stays `INTACT`, exit 0. A terminator is a record
>   boundary and is part of what the chain must protect. Nothing is skipped now.
> * **Which shapes.** Two of the four were already non-`INTACT` before any flip, so
>   `assert not escaped` was vacuously true and the test passed with the mutation replaced by a
>   no-op. The precondition is now asserted, and those two shapes are built the way an upgrade
>   and a pre-chain history really arrive — oldest lines first — rather than by editing a
>   chained file into a shape no writer produces.
> * **Which region.** `range(last)` excluded the last line and nothing else, so over a
>   correctly built pre-chain shape it swept lines that nothing attests **by design** (R-4) and
>   called 82 disclosed limits defects. The region is derived from the report instead: a line's
>   bytes are attested if and only if the edge above it `HOLDS`, which is this document's own
>   model, so the region moves when the model does. R-23's exclusion of the newest line falls
>   out of that rather than being subtracted.
>
> And the test asserts that something WAS examined, because `not escaped` and *nothing was
> examined* print the same word: on a one-line fixture the old loop ran zero times and passed,
> and the gate adds no unique coverage, so the 100 % floor could not notice either. The floor
> is not a number — a number would be a fourth hand-written scope — it is the byte-count
> identity of the derived region.
>
> **The rule this restates: a property test with a hand-written scope is a hand-written test
> with extra confidence.**

**R-32.** **A LINE THAT MADE NO CLAIM IS NOT AN ACCUSATION.** `UNCLAIMED` is a fifth edge status:
decisive (it forces `CANNOT_CHECK`, exit 2, so it can never be mistaken for a clean bill),
disclosed by name in both renderings, and **never `BROKEN`**. Approved by Taylor, 2026-09-20.

> **What this costs, stated plainly, because it is a real loss.** Rule 7 was the only rule that
> caught a forger appending an unchained line **at the very end** of a history. Measured during
> adjudication:
>
>     mid-file splice   -> BROKEN with rule 7, BROKEN without it   (rule 11, at the next edge)
>     mid-file overwrite-> BROKEN with rule 7, BROKEN without it   (rule 11)
>     TAIL splice       -> BROKEN with rule 7, CANNOT_CHECK without
>
> So the exchange is: **detection of a lazy tail-splice, for never accusing an intact file.**
>
> **Why that trade is right.** The forger this rule caught is one who appended a line and did
> not compute `prev` — while the nine-line `sha256sum` recipe for computing it is printed in
> this package's own module docstring, so any forger who reads the documentation is unaffected.
> Against that, rule 7 as an accusation produced **two classes of permanent false accusation
> over complete, correct files**, neither needing an adversary:
>
> * **G-01** — an append that could not take the lock (NFS, CIFS, a container without `flock`),
>   which R-22 requires to carry no claim. Every record lands; three untouched lines are named
>   as tampered; and because R-13 forbids clearing a finding, the only way to remove it is to
>   edit the history — the act this feature exists to detect.
> * **G-05** — a `runprov.start.v1` line from a run that is still going or was killed. The same
>   bytes report `GAP` once the run's completion record arrives, so the verdict depended on
>   whether a process had finished, and for a killed run it never does.
>
> **A tamper-detector that cries wolf over correct files is worse than none**, because the next
> real break is discounted. Exit 2 still means *look at this*; nothing becomes silently green,
> and the acceptance gate of R-31 is untouched — no new `INTACT` is produced by this change.
>
> **The table stays at 216 cells.** The rejected remedy for G-01 was a fourth `CLAIMS` value, a
> sentinel the sink would write on the unlocked path. It was measured and refused: it helps no
> history already on disk, it contradicts R-22's "no claim at all", it costs 72 extra cells and
> six documentation citations — and **it hands a tail-forger a free downgrade**, since typing
> the sentinel into a spliced line buys exactly the `BROKEN` → `CANNOT_CHECK` move this rule
> otherwise charges for. The status is a property of the JUDGEMENT, not a new thing to write
> into the file, and that is why it costs nothing.

> **The report must name the cause it cannot distinguish.** An `UNCLAIMED` edge has two
> innocent explanations and one guilty one, and the sentence says so rather than picking:
> *"line N carries no chain claim. A run that could not take the file lock writes none (it
> prints a NOTE when that happens), and a run still in flight has not written its completion
> record yet — but so would a line inserted by hand. This is not evidence of an edit."* It must
> never say *"upgrade that machine"*, which belongs to rule 5 alone and is false here.
