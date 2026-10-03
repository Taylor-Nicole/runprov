# 17. One answer, two renderings, and the table is derived from the data

**Status:** Accepted — T-33, built across 2026-09-25 … 2026-09-30 and **shipped in 0.7.0** (2026-10-01). Amended by Audit I with R-15 and R-3's derived list; by Audit J with R-16, `chain`'s reason, `export`'s exclusion and the absent-path rule. **This line read `Proposed — a feature that is not built` through the release that shipped it.** The guard that catches exactly that reads a module's TOP docstring for the ADR number, and no module's docstring named this one until 2026-10-02 — so it had nothing to check.

## Context

Every command emits a text table built for a person. `runprov diff` prints aligned columns;
`impact` prints an indented tree; `report` prints a page with rules and key-value lines. There
is no machine-readable form of any of it.

ADR-0014's own sketch specifies `runprov diff <A> <B> [--only …] [--format text|json]`. Neither
option was built, and an Audit D reviewer found the `DIMENSIONS` constant that `--only` would
have used sitting in `diff.py` with no reader — a promise made in a ratified decision and then
quietly not kept.

**The cost is adoption, not convenience.** A person runs a command and reads it. A team wires it
into something: a CI check that posts the differing dimensions, a lab dashboard listing which
artifacts are STALE, a submission script that reads `resources` and sizes itself. Every one of
those needs to parse prose today, which means regexes over a layout that has changed four times
in five releases. That is the difference between a tool one person uses and a tool a group
adopts, and it is the single largest thing standing between this package and the second.

## The failure mode this invites, which is why it is an ADR

**Two renderings of one answer are two things that can disagree.** This project has shipped
that defect twice, in the same audit: `packages` and `steps` each existed in two shapes — the
sidecar's and the history's — and each reader knew one of them. `diff` reported
`packages unchanged (0 vs 0)` on every real history for months, and crashed on `steps` for
every project that used `@run.step`, because the fixtures were built in one shape and the
command was fed the other. Both were self-consistent and mutually wrong.

Adding a second output format is the same trap with a new face: a JSON emitter written beside
the table will drift from it, and the drift will be invisible because nobody reads both.

## Decision

**R-1.** Every command computes **one structure**, and both renderings are **derived from it**.
The text table is a function of the same object the JSON serialises. Not two builders reading
the same record — one builder, two renderers.

**R-2.** A test asserts, per command, that **every field in the JSON appears in or is accounted
for by the text rendering**, and that the text rendering introduces no fact absent from the
structure. Derived from the structure's own keys, never a hand-typed list — that list is the
scope pattern, and it would be the ninth instance in this codebase.

**R-3.** `--format text|json`, defaulting to `text`, on every command that ANSWERS a question.
`exec`, `capture` and `prune` are excluded because they ACT rather than answer.

> **AMENDED 2026-09-23, after T-32 shipped in 0.6.0.** This requirement named eight commands as
> a flat list and the list was already wrong when it was written. Measured against the parsers
> rather than remembered:
>
> | | today | what R-3 asks |
> |---|---|---|
> | `verify` | `text,json` | **done** — predates this ADR |
> | `lineage` | `text,json` | **done** — and the old exclusion was FALSE |
> | `chain` | `text,json` | **done** — shipped in 0.6.0 under T-32, after this ADR was written |
> | `diff` | *none* | add. ADR-0014's own sketch specified it and it was never built |
> | `impact` | *none* | add |
> | `report` | *none* | add |
> | `check` | *none* | add |
> | `resources` | `text,tsv,slurm,k8s` | add `json` beside them |
> | `show` | `text,yaml` | add `json` beside it |
> | `log` | `text,yaml,jsonl` | add `json` — see below, it is not the same thing as `jsonl` |
> | `export` | `ro-crate,prov` | excluded: two standard vocabularies already, and this would be a third |
>
> **The `lineage` exclusion was factually wrong.** It said `export` covers it, but `lineage`
> has emitted `--format json` all along. An exclusion justified by a claim about a neighbouring
> command, written without checking the command itself, is the shape this project keeps finding
> — so the rule stands in place of the list: **answer or act**, and the list is derived from the
> parsers by a test rather than written here again.
>
> **`log --format jsonl` does not satisfy this and `--format json` is not redundant with it.**
> `jsonl` is the RECORDS, one per line, as stored. `json` under R-5 is an ANSWER: a single
> object carrying `schema`, the question's result, and what could not be established. A consumer
> asking *"what did this command find"* and a consumer asking *"give me the records"* are asking
> different things, and R-8's null-versus-absent distinction only means something inside the
> first. The same holds for `show --format yaml`, which is a rendering of a page, not an answer.

**R-4.** JSON goes to **stdout with nothing else on it** — no banner, no warning, no progress.
Diagnostics go to stderr, which they already do. A caller that has to strip a line before
parsing is a caller that will strip the wrong line.

**R-5.** Each payload carries **`"schema": "runprov.<command>.v1"`**, the same discipline the
record already follows. A consumer must be able to tell which shape it has without inferring it
from which keys happen to be present.

**R-6.** The exit code is **unchanged by the format.** `--format json` is a rendering choice,
not a different question, and a gate that behaves differently depending on how it was asked to
print is a gate nobody can reason about.

## What the payload must carry

**R-7.** Everything the text rendering states, including **what could not be checked.** The
qualifications are the package's whole character — `NOT COMPARABLE`, `CANNOT_CHECK`, `at least
N`, `blocked`, `examined` — and a JSON form that carries only findings would let a consumer
build exactly the vacuous green this project keeps fixing. `blocked` and `examined` are fields,
not prose.

**R-8.** **Null and absent mean different things and both must survive.** `"digest": null` is
*looked and found none*; the key being absent is *this version did not look*. That distinction
is load-bearing in `imported_code`, in `resources`, and in `observation`, and JSON is the one
place it is cheap to get right and easy to flatten by accident.

**R-9.** No field is renamed on the way out. A JSON consumer and a reader of the record see the
same names, so a question answered against the record is answerable against the output.

## What this must never become

**R-10.** Not a second source of truth. The payload is a **derived view**, exactly as ADR-0014
clause 6 and ADR-0009 already require: it computes nothing that is not already recorded, and no
field exists in it that could not be got from the record and the command's own logic.

**R-12.** **Each payload is the command's OWN structure, serialised — not a shape invented for
it.** R-1 says one builder and two renderers; this names the builder per command, so that a
reviewer can check the JSON against something that already exists rather than against prose.

| command | the structure it already computes | notes for the payload |
|---|---|---|
| `diff` | `diff.Dimension(name, differences, examined, blocked)`, one per dimension | `blocked` and `examined` are R-7's whole point: `NOT COMPARABLE` is a verdict, not an absence |
| `impact` | `impact.Chain(digest, seeds, steps, unregistered, runs_examined, watch_drops)` with `Step(depth, address, script, outputs)` | `depth` carries the ORDER, which is the answer; `unregistered` and `watch_drops` are the blind spots R-7 requires on every answer |
| `check` | `check.Report(examined, entry_points, flagged, unparseable)` | `examined` is the positive companion — "nothing flagged" and "nothing scanned" must not serialise the same |
| `resources` | `resources.Measurement(...)` | already carries `source` and `unavailable`; `source` says WHICH mechanism answered, which R-7 requires to travel with the number |
| `show` | the `dict` view `render_run`/`render_project` already take | already a structure; the JSON is that view, and the text stays its rendering |
| `log` | the filtered record list, plus what was excluded and why | see R-3's note: this is the ANSWER, not `jsonl`'s records |
| `report` | **none — it has none.** See R-13. | |

**R-13.** **`report` must gain a structure before it can gain a rendering.** `report.Page` is
`lines: list[str]` and a `status`: the facts become prose inside the builder, so there is nothing
to serialise. The other six commands serialise what they already have; this one is a refactor.

> Its own docstring records half of this lesson already — the status is RETURNED rather than
> string-matched back out of the rendered text, because *"the first version of the CLI handler
> decided its exit code by string-matching its own output, which makes the wording load-bearing:
> rephrasing a line would silently change what the command returns to a build."* The same
> argument applies to every other fact on that page, and R-1 is that argument generalised.
>
> **So `report` is the row to build FIRST**, not last: it is the only one whose cost is not
> already paid, and doing it first stops the other six being written against an architecture
> that turns out not to hold. If it proves larger than the rest combined, that is the honest
> signal to ship the six and take `report` separately, rather than to discover it at the end.

**R-14.** **A payload names what it could not establish, in the same object as what it found.**
Not a second call, not a stderr line, not an absence a consumer must infer. `diff` has `blocked`,
`impact` has `unregistered` and `watch_drops`, `check` has `unparseable`, `resources` has
`unavailable` and `source`, `chain` has `merged`, `translated` and `unreadable`. **Every one of
those exists because a reader was once given a clean answer over an incomplete look**, and a
JSON form that dropped them would re-open every one of those findings at once for exactly the
consumers least able to notice.

**R-15. A payload is emitted whenever THIS PACKAGE answers, including when its answer is that
it could not check. Silence on stdout means the INVOCATION was wrong, never that the answer was
empty.** Added 2026-09-29 by Audit I, I-21; Taylor's decision.

Measured across every exit-2 state before the rule was written, and the split was real: `chain`
printed a full `"status": "CANNOT_CHECK"` object for a history it could not read, while `impact`
and `lineage` printed nothing for the same condition, and `diff` printed nothing for a selector
that matched the wrong number of runs. **One condition, one exit code, two classes**, depending
only on which command met it.

**The reason this needs a rule rather than a convention is that exit 2 is already taken.**
`argparse` exits 2 for a usage error, so a consumer seeing 2 cannot tell *this package could not
check* from *you typed the command wrong* — and today the only thing separating them is whether
stdout carries a payload, which is exactly the signal that was inconsistent. Under R-15 that
signal becomes total and a consumer needs one rule:

> **stdout parses ⇒ this package answered, and the verdict says what it found or could not
> establish. stdout empty ⇒ the command was not usable as invoked.**

**WHAT SILENCE MEANS FOR A PATH THAT IS NOT THERE — ruled 2026-10-01 by Taylor, on the
measurement below rather than on a reading of this rule.** R-15 says silence means the
invocation was wrong, and that left one question the rule could not answer on its own: a command
told to read a path that does not exist — has it answered, or was it mis-invoked? Measured
across every command that takes a path, the package was already doing three different things,
and **two of the three were already ruled**; only the third was open.

The rule is the ROLE of the absent path, and the role is readable from the parser:

| the absent path is | behaviour | commands | ruled by |
|---|---|---|---|
| **the history** (`--log`, or `chain`'s positional) | **answer** | all nine that read one | R-15 / I-21, 2026-09-29 |
| **the single subject** (`nargs=1` or `?`) | **silence** | `report`, `impact`, `check`, `export` | the `silent_by_design` bucket |
| **a member of a list of subjects** (`nargs="*"`) | **answer, naming the absent ones** | `verify` | J-24, 2026-10-01 |

**The third clause is why `verify` and `check` differ, and the difference is their ARITY rather
than anyone's taste.** `verify <paths>` takes a list, so `verify good.tsv typo.tsv` is partly
answerable — it genuinely checked one artifact — and J-24 requires a gate to see the absent one;
a command that must answer in the mixed case cannot sensibly fall silent when the list happens
to hold one absent path. `check <root>` takes a single root and has no mixed case: with nothing
to walk it never started, which is the distinction the README already draws for it — *"how a
consumer tells a sweep that could not conclude from a command that could not start."*

**The cost of this ruling, stated rather than hidden: a consumer must know a command's arity to
predict whether stdout will parse.** That was weighed against changing released output — giving
`check` a payload, or reversing J-24 — and the arity is at least discoverable from `--help` and
stable, where the alternatives move behaviour somebody may already depend on. A test derives
each command's arity from the parser and asserts the behaviour, so the three clauses are checked
rather than described.

**`export` IS OUTSIDE THIS RULE AND R-16, and J-26 is the row that says so.** R-3's table
excludes it with a stated reason — *"two standard vocabularies already, and this would be a
third"* — and these two later rules did not mention it at all, though R-15's scope is written as
*whenever THIS PACKAGE answers*. The gap was a scope question, not a violation: read literally,
R-15 would require a `runprov.export.v1` object on the stdout of a command whose entire purpose
is to speak RO-Crate and PROV, which is the third vocabulary R-3 refused.

So the exclusion is the same one, stated once more here: **`export`'s stdout is somebody else's
vocabulary, and those formats carry their own schemas.** Its exit-2 states — a sidecar it cannot
read, a file that is not a runprov sidecar, a scope it was not given — name the path on stderr
and leave stdout empty, which is R-15's *the command was not usable as invoked* and is correct
for them.

**This needs no change to the derived test and that is the point.** `_cli_json_commands()` reads
the parsers for subcommands whose `--format` offers `json`, so `export` is absent by
construction rather than by anyone remembering — and a ratchet asserts that, so the day `export`
is given `--format json` the exclusion above has to be re-read instead of silently lapsing.

This is additive. `chain`, `report` and `verify` already comply; `report` for an absent artifact,
`impact` for an unrecorded file or a missing history, `diff` for a selector matching other than
two runs, and `lineage` for a missing history do not, and gain payloads without any existing key
changing.

> **AMENDED 2026-09-30 by Audit J (J-22).** Two of the states listed above were ruled the other
> way when R-15 was applied in `0327c01`, and that commit changed no documentation, so this
> paragraph and the shipped behaviour contradicted each other for a day. **`report` for an absent
> artifact and `impact` for a target that is neither a file nor a digest STAY SILENT**, because in
> each the command could not FORM its question — the same reading `check` already had for a
> directory that is not there. `diff`'s five states and `impact`'s missing history do serialise.
> The list above is left standing rather than rewritten so the amendment is visible, which is the
> form R-3's own amendment takes. Where a command has no structure to serialise in that state — `diff` and `impact` have
computed nothing — the payload is the schema plus what it could not do, under R-14: the thing it
could not establish belongs in the object, not in a stderr line the consumer never sees.

**It is decided here rather than per command deliberately.** Four commands — `check`, `log`,
`resources` and `show` — are still to gain `--format json` under R-3, and each would otherwise
answer this question for itself. The split above is what that looks like after two rounds.

**R-16. A payload says it could not check IN ITS OWN ANSWER VOCABULARY, and this table names
which field carries it per command.** Added 2026-09-30 after R-15 was applied and two successive
attempts at a single cross-command assertion failed.

R-15 requires the payload to be emitted. It does not say how a consumer finds the inability
inside it, and the answer turned out not to be one field. Measured across all ten:

| command | where it says *could not check* | shape |
|---|---|---|
| `impact`, `diff`, `log`, `lineage`, `resources`, `show` | `cannot_check` | a REASON, or `null` |
| `check` | `examined_nothing` | a REASON, or `null` |
| `report` | `verdict` ∈ `NO PIN`, `UNVERIFIABLE` | a VALUE in a closed enum |
| `chain` | `status` = `CANNOT_CHECK`, **and `cannot_check` for which of four routes** | a VALUE in a closed enum, plus a REASON |
| `verify` | `cannot_check` | a REASON, or `null` |

**These are not an inconsistency to unify, and that is the ruling.** For `report` and `chain`,
*could not check* IS the answer — a legitimate member of the verdict's own vocabulary, not a
failure to produce one. For the other eight there is no verdict enum, so the inability is a
separate fact and needs its own key. A command that has a verdict states it there; a command that
has none carries a reason.

**`check` keeps `examined_nothing` because of R-9.** Renaming it to `cannot_check` for uniformity
would be the one thing R-9 forbids: it is the structure's own property, and the payload uses the
structure's names. The **eight** that carry `cannot_check` have no such conflict — there the key
is added by the payload function and shadows nothing.

> **CORRECTED [K-35]: this sentence said *six* while the table twelve lines above listed
> eight.** `verify` joined the family with J-18's correction, recorded in this very section, and
> `chain` has carried both `status` and `cannot_check` since J-01 gave it a reason — so the
> prose was two behind its own table. Measured from live output: `log`, `lineage`, `impact`,
> `diff`, `resources`, `show`, `verify` and `chain`. **The table is the statement; this sentence
> counts it**, which is why `policy.py` now cites the table instead of repeating the number.

> **CORRECTED 2026-10-01 by J-18. `verify`'s entry was `artifacts_seen` > 0 with
> `artifacts_pinned` = 0, and the paragraph here called it "the weakest of the five" — a
> consumer must compare two counters rather than read a field, which was presented as a
> consequence of `verify` counting unpinned files rather than listing them.**
>
> **It was not weak, it was FALSE in three of the four states `verify` exits 2 in**, and in two
> of them from the other side of the inequality: an empty directory and a path that is not there
> both give `artifacts_seen = 0`, and a report whose every pin is UNVERIFIABLE — the second
> route, which the entry did not describe at all — gives both counters non-zero. A consumer
> applying the rule literally read *verify could check* in three states out of four.
>
> **And the true relationship was not something to document instead**, which is why this became
> a field rather than a corrected sentence. It is two clauses over five counters whose ORDER
> matters: a report with one STALE artifact has `ok == 0` as well, so a consumer testing
> `ok == 0` calls a FINDING an inability — the inversion this rule exists to prevent — and the
> precedence that stops it lived in `__main__._verify`'s branch order, where no consumer can see
> it. `verify.cannot_check()` is now that fold, and both of the command's *NOTHING CHECKED*
> sentences are printed from it rather than composed beside it.
>
> **The objection in the paragraph above does not apply to the fix**: every counter keeps its
> name, its value and its place, and `cannot_check` is appended. Nothing released changed, and
> both pages are byte-identical.
>
> `report` is now the only command whose inability is a verdict and nothing else. **The
> test's copy of this table said `verify`'s vocabulary was `verdict: "NO PIN"`** — a key
> `verify`'s payload has never had — while `_R16_INABILITY` six hundred lines below carried the
> count pair. Both are corrected.

**A test asserts the table's SCOPE against the parser** — that it names exactly the commands
answering in JSON — and asserts each named field is present in that command's payload. R-3's and
R-5's lists both went stale in prose that nothing read; that much goes red.

**CORRECTED 2026-09-30 (J-23): this first claimed the test asserts the table itself, and it does
not.** The test carries its own copy of the mapping, so the two can diverge — and did, when J-01
gave `chain` a reason. It also checks only that a field is PRESENT, in an ORDINARY payload, so it
would pass if every inability field in the package were permanently `null`. Both gaps are filed.

> **CLOSED 2026-09-30 by J-01, and this note was wrong twice.** `chain` now carries
> `cannot_check` naming which route reached the verdict, read off the same fold as `status` so the
> two cannot disagree. Additive on released output, with a CHANGELOG entry.
>
> **The note said THREE routes; there are FOUR** — an edge that is `UNCHECKABLE`, `GAP` or
> `UNCLAIMED`; a line that lost its terminator and merged with the next (G-03); a history with
> nothing chained in it; and no history to read. **And it said the payload "distinguishes none of
> them", which was false**: `lines`, `chained_from`, `merged` and the edge statuses all travelled
> and each route was a different combination. What was missing was the CONCLUSION — naming the
> route needed the fold's PRECEDENCE, which is the part a consumer cannot derive. Corrected here
> rather than quietly dropped, because this note was the authoritative statement while it stood.
>
> **Still open (J-23):** nothing asserts that this table and the test's copy of it agree. The
> precedent for doing it is `test_r31s_shape_list_is_the_one_the_gate_actually_runs`, which reads
> ADR-0016's prose and a decorator's AST and compares them.

**R-11.** Not a stable API on the first release. It is versioned by R-5 and the README says the
JSON shape follows the record-format promise: a field's meaning does not change without a new
schema value.

## Alternatives considered

**Emit JSON only, and pretty-print it in a wrapper.** Clean, and rejected: the text tables are
the package's voice. `resources` printing a Slurm preamble and `impact` printing a rebuild order
are the things people quote in a methods section.

**A `--porcelain` stable text format, as git has.** Cheaper, and rejected: it is a third thing
to keep consistent, and it solves the parsing problem by inventing a grammar rather than by
removing the need for one.

**Per-command flags added as each is needed.** How this would happen by default, and rejected
under R-2: the consistency guarantee is the feature. Eight commands emitting eight shapes
designed a month apart is what the record format already avoided by having one schema.
