# 17. One answer, two renderings, and the table is derived from the data

**Status:** Proposed — T-33. Specification for a feature that is not built.

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
