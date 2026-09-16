# Architecture decision records

Each file records **one decision**: what was decided, what it rests on, and what it costs. The
format is deliberately plain — context, decision, consequences, alternatives — and the rule for
this project is that a decision with a measurement in it must name what was measured.

## Why these exist

The code cites ADR-015, ADR-024, ADR-026, ADR-028 and ADR-029 in comments, and **none of those
files are in this repository**: they belong to the monorepo this package was extracted from.
A reader who follows one of those references finds nothing. So the numbering here starts fresh
at 0001, and where a decision was genuinely inherited, its ADR says so and restates the
reasoning rather than pointing at a file nobody can open.

## Status vocabulary

| status | meaning |
|---|---|
| **Accepted** | in force; the code does this today |
| **Proposed** | written to be argued with; not implemented |
| **Superseded by NNNN** | replaced, kept for the reasoning |
| **Rejected** | considered and declined; kept so it is not re-proposed |

A *Proposed* ADR is the right place to think out loud. It is not a promise.

## Index

| # | title | status |
|---|---|---|
| [0001](0001-provenance-layout-and-overrides.md) | Where records are written, and how a team changes it | Accepted |
| [0002](0002-detecting-unregistered-reads.md) | Warning when a read bypasses registration | Accepted |
| [0003](0003-a-module-all-ratifies-it-does-not-decide.md) | A module's `__all__` ratifies the package promise, never makes one | Accepted |
| [0004](0004-a-pin-states-what-it-covers.md) | An input cannot be registered after the pin, and a pin states what it covers | Accepted |
| [0005](0005-a-record-is-written-whole-or-not-at-all.md) | A record is written whole or not at all | Accepted |
| [0006](0006-an-artifact-answers-for-itself.md) | An artifact answers for itself | Accepted |
| [0007](0007-the-gate-passes-the-exit-code-through.md) | The gate ships with the tool, and passes the exit code through | Accepted |
| [0008](0008-capture-observes-what-declaration-cannot-reach.md) | `capture` observes what declaration cannot reach | Accepted |
| [0009](0009-export-is-a-derived-view-in-two-vocabularies.md) | Export is a derived view, in two vocabularies and two scopes | Accepted |
| [0010](0010-a-record-states-what-it-was-able-to-observe.md) | A record states what it was able to observe | Accepted |
| [0011](0011-a-static-check-answers-only-the-case-the-runtime-one-cannot.md) | A static check answers only the case the runtime one cannot | Accepted |
| [0012](0012-a-notebook-run-is-not-linear-and-the-record-must-say-so.md) | A notebook run is not linear, and the record must say so | **Proposed** |
| [0013](0013-what-a-run-consumed-measured-not-declared.md) | What a run consumed, measured rather than declared | Accepted |
| [0014](0014-a-difference-and-an-incomparability-are-not-the-same-answer.md) | A difference and an incomparability are not the same answer | **Proposed** |
| [0015](0015-impact-answers-what-did-depend-on-this-never-what-will.md) | `impact` answers what did depend on this, never what will | **Proposed** |
