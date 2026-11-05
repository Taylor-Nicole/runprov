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

- Current version: **0.9.0**
- Last release: **0.9.0** on 2026-10-22
- Released versions on record: 9
- Sections awaiting the next release: **50**

## In the next release

Dated sections of `CHANGELOG.md` are the per-release record; these are the entries that
have landed and are not yet in one.

- Fixed — the audited-section rule refused the one state a release passes through
- Added — `ROADMAP.md`, the first document in this repository that is generated rather than written
- Changed — the claims auditor now says what its population cannot see (a declared RULE move)
- Fixed — two promises of totality that were true of less than they said
- Added — a gate's number describes the tree it ran over, and `ci.py` now refuses when that moved
- Added — the connectivity gate: one walk, and every declared input accounted for
- Fixed — the one place every other never-raises promise discharges into could itself raise
- Fixed — marking a run in flight could be what ended it
- Fixed — a promise not to raise was being spent as a premise one function down
- Fixed — a verdict named a cause it had inferred from a predicate that conflates three
- Fixed — two repairs were held by nothing, and one of them was the headline of its own commit
- Fixed — a caller could still choose which branch decided a digest
- Fixed — two recorded digests were properties of the environment, not of the value
- Fixed — provenance could abort the program it observes, and a run could finish while lying
- Fixed — two defects the hosted matrix found in the six commits the local gate had cleared
- Added — the record file now carries its own digest in the history, checkable with `sha256sum`
- Tested — four guards, and the expected digest is computed from the bytes rather than restated
- Fixed — the harnesses' own controls: ownership by substring, a floor satisfied by a constant, a digest of prose
- Tested — eight guards, two of them recorded as void controls
- Fixed — the corpus's leak control could not fail, and the artifacts it calls untouchable were not
- Tested — three guards, each red on the pre-repair module for its own reason
- Fixed — the claims auditor's own haystacks, including a binding a withdrawn quote could satisfy
- Tested — five new guards, one of them a void control, and one existing test was pinning the defect
- Fixed — three more release-path checks, one of them refusing for a reason that was not true
- Tested — and one of the five controls is recorded as VOID rather than red
- Fixed — three holes in the gate that stands in front of an irreversible PyPI upload
- Tested — every one of those refusals now has a control, and three of them went red on the old script
- Tested — the resolution seam had no guard, and the primitive it needs is wider than it looks
- Changed — ADR-0020 and ADR-0024 are Accepted, over code built on 2026-10-23
- Tested — every requirement of ADR-0020 and ADR-0024 is now cited, and the last one got a guard
- Added — a guard for "no command emits a container recipe", which had none
- Fixed — six requirements across three decision records that no longer described the build
- Tested — two guards that could not fail, and what each one's control had to prove
- Fixed — `lineage` could report "produced by no recorded run" when a clock disagreed
- Changed — one definition for "which history does this command read"
- Tested — the CI section's job total was asserted by a check that could not fail
- Added — `env --can-check` answers in JSON, which it had never done
- Fixed — a refusal answered in the wrong shape in two of `env`'s four modes
- Tested — the refusal-shape rule now has a guard, over all four modes instead of one
- Changed — the hosted matrix no longer runs on a label that can move without a commit
- Tested — an `include` whose label drifts from the axis ADDS a leg instead of modifying one
- Fixed
- Added
- Fixed
- Added
- Changed
- Fixed
- Added
- Fixed
- Tested — W-1's narrowing of the conda matcher, which nothing in the suite could see undone

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
