# Roadmap

<!-- GENERATED FILE — DO NOT EDIT. Rebuild with `python ci.py roadmap`. -->

Every line below is derived: the decision records in `docs/adr/` say what each feature is
and what state it is in, `CHANGELOG.md` says what has been released and when, and
`pyproject.toml` says where the package is now. There is no hand-kept list to go stale,
because a hand-kept roadmap goes stale on the day its release ships.

**This reports what the records claim.** A record marked accepted over code nobody wrote
appears here as accepted; holding records against the tree is a different job, done by the
citation guards and the gate.

## Where the package is

- Current version: **0.10.0**
- Last release: **0.10.0** on 2026-11-05
- Released versions on record: 10
- Sections awaiting the next release: **2**

## In the next release

Dated sections of `CHANGELOG.md` are the per-release record; these are the entries that
have landed and are not yet in one.

- Added — the build is verified reproducible, a property this project was selling unmeasured
- Added — every deriving step declares what it READS, and the declaration is observed

## Features, by the state their decision record declares

2 of 27 records name the release that shipped them, so *accepted* below
mixes shipped-long-ago with built-but-unreleased. The changelog and the tags are where
shipping is actually recorded.

### Accepted — 21

- **ADR-0001** Where records are written, and how a team changes it
- **ADR-0002** Warning when a read bypasses registration
- **ADR-0003** A module's `__all__` ratifies the package's promise; it never makes one
- **ADR-0004** An input cannot be registered after the pin is written, and a pin states what it covers
- **ADR-0005** A record is written whole or not at all
- **ADR-0006** An artifact answers for itself
- **ADR-0007** The gate ships with the tool, and passes the exit code through
- **ADR-0008** `capture` observes what declaration cannot reach
- **ADR-0009** Export is a derived view, in two vocabularies and two scopes
- **ADR-0010** A record states what it was able to observe
- **ADR-0011** A static check answers only the case the runtime one cannot
- **ADR-0013** What a run consumed, measured rather than declared
- **ADR-0014** A difference and an incomparability are not the same answer
- **ADR-0015** `impact` answers what *did* depend on this, never what *will*
- **ADR-0016** (T-32) A history shows whether it has been edited
- **ADR-0020** An environment rendered from a record is a floor
- **ADR-0022** A digest may be declined, but never faked
- **ADR-0024** An environment has two digests, because one digest cannot answer two questions
- **ADR-0025** The canonical package set, and exactly one implementation of it
- **ADR-0026** One environment identity, and one comparison over two of them
- **ADR-0027** An environment in a standard vocabulary says only what the record supports

### Proposed — 4

- **ADR-0012** A notebook run is not linear, and the record must say so
- **ADR-0019** (T-35) `rerun` prints, and never runs
- **ADR-0021** (T-37) Many histories are merged at read time, and never rewritten
- **ADR-0023** A rebuild names its rank, and verifies what it built

### Shipped — 2

- **ADR-0017** (T-33) One answer, two renderings, and the table is derived from the data — shipped in 0.7.0
- **ADR-0018** (T-34) A policy is checked against the history, not remembered — shipped in 0.8.0

Every one of the 27 decision records yielded a row; none was dropped.
