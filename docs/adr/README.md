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
