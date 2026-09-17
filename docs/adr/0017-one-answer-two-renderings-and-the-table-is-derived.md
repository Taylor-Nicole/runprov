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

**R-3.** `--format text|json`, defaulting to `text`, on: `diff`, `impact`, `report`,
`resources`, `check`, `verify`, `log`, `show`. `exec`, `capture` and `prune` are excluded
because they act rather than answer; `lineage` is excluded because `export` already emits it in
two standard vocabularies, and a third project-specific one would be a fourth thing to keep
consistent.

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
